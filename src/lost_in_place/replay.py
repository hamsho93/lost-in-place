"""Recorded episodes for offline replay: E4's prompts for Phase 0 and a wave arm's prompts for the
comprehension probe (freeze-v3 design, sections 3.3 and 11).

Only what a blinded reader may see is read here: the planner's prompts, the health and times the
agent recorded at each decision, the fault onset and the attempt status. An attempt counts if it is
the earliest without a harness failure (the rule of `manifest.is_result` without its violation term,
which is an outcome), so no score file is needed to pick it.

A recorded prompt is re-rendered at another ladder level through the adapter's own renderer
(`llm.dexi`): it is parsed back into its mission, envelope, state, health and history, and the parse is
checked by re-rendering the level it was flown at, which must reproduce it byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from lost_in_place.llm import dexi
from lost_in_place.llm.clients import ChatClient
from lost_in_place.manifest import is_harness_failure
from lost_in_place.planning import EstimatorHealth, shows_health
from lost_in_place.scoring import showed_degraded

ATTEMPT_FILES = ("episode.json",)
_FIELD = re.compile(r"\{(\w+)\}")
_GROUPS = {"state": r"(?P<state>.*?)", "history": r"(?P<history>.*?)", "mission": r"(?P<mission>[^\n]*)"}
_STATE = re.compile(
    r"north (?P<north_m>-?[\d.]+) m, east (?P<east_m>-?[\d.]+) m, altitude (?P<rel_alt_m>-?[\d.]+) m,"
    r" heading (?P<yaw_deg>-?[\d.]+) deg, (?P<armed>armed|disarmed), (?P<in_air>in air|on ground),"
    r" battery (?P<battery>\d+%|unknown), mode (?P<flight_mode>\S+)"
)
_ELIDED = "(earlier actions elided"
_REJECTED_V1 = " -> REJECTED by validator: "
_REJECTED_V2 = " -> REJECTED: "


def _pattern(template: str) -> re.Pattern[str]:
    parts = _FIELD.split(template)
    out = []
    for i, part in enumerate(parts):
        out.append(re.escape(part) if i % 2 == 0 else _GROUPS.get(part, rf"(?P<{part}>[^\n]+?)"))
    return re.compile("".join(out), re.DOTALL)


_RECORDED = _pattern(dexi.PROMPT)


@dataclass(frozen=True)
class Recorded:
    """A prompt flown at `dexi-9b2e7a7` under validator `none`, parsed back into what rendered it."""

    mission: str
    envelope: dexi.DexiEnvelope
    state: dict[str, Any]
    health_text: str
    history: tuple[str, ...]


def parse_state(line: str) -> dict[str, Any]:
    m = _STATE.fullmatch(line)
    if m is None:
        raise ValueError(f"not a DEXI state line: {line!r}")
    battery = m["battery"]
    return {
        "north_m": float(m["north_m"]),
        "east_m": float(m["east_m"]),
        "rel_alt_m": float(m["rel_alt_m"]),
        "yaw_deg": float(m["yaw_deg"]),
        "armed": m["armed"] == "armed",
        "in_air": m["in_air"] == "in air",
        "flight_mode": m["flight_mode"],
        "battery_pct": None if battery == "unknown" else float(battery[:-1]),
    }


def parse_prompt(prompt: str) -> Recorded:
    """Raises ValueError unless `prompt` is DEXI's 9b2e7a7 prompt with an unelided history."""
    m = _RECORDED.fullmatch(prompt)
    if m is None:
        raise ValueError("not a prompt rendered from DEXI's 9b2e7a7 template")
    state_line, _, health_text = m["state"].partition("\n")
    history = () if m["history"] == "(none yet)" else tuple(m["history"].split("\n"))
    if any(line.startswith(_ELIDED) for line in history):
        raise ValueError("the history is elided; re-rendering it would elide it again")
    envelope = dexi.DexiEnvelope(
        max_alt_m=float(m["max_alt"]),
        geofence_m=float(m["geofence"]),
        max_move_m=float(m["max_move"]),
        max_hold_s=int(m["max_hold"]),
    )
    return Recorded(m["mission"], envelope, parse_state(state_line), health_text, history)


def health_of(decision: dict[str, Any]) -> EstimatorHealth | None:
    h = decision.get("health")
    if not h:
        return None
    return EstimatorHealth(
        xy_valid=bool(h["xy_valid"]),
        eph_m=float(h["eph_m"]),
        flow_quality=float(h["flow_quality"]),
        innovation_check_failing=bool(h["innovation_check_failing"]),
        heading_reference=bool(h["heading_reference"]),
    )


def render_level(recorded: Recorded, health: EstimatorHealth | None, level: str) -> str:
    """The prompt a ladder level would have shown at the recorded decision, under validator `none`.

    At `dexi-v2` the adapter's own rejections read `REJECTED:`; under validator `none` every rejection
    in a 9b2e7a7 history is the adapter's, so its wording is changed with the version."""
    observation, version = dexi.LADDER[level]
    history = list(recorded.history)
    if version == dexi.PROMPT_V2:
        history = [line.replace(_REJECTED_V1, _REJECTED_V2, 1) for line in history]
    block = ""
    if shows_health(observation):
        if health is None:
            raise ValueError(f"level {level} shows health, and the decision recorded none")
        block = dexi.health_block(health, observation)
    return dexi.render_prompt(
        recorded.mission,
        recorded.envelope,
        recorded.state,
        history,
        block,
        dexi.template(version, dexi.NO_VALIDATOR, observation),
    )


def rerender(
    prompt: str, health: EstimatorHealth | None, flown_at: str, levels: Iterable[str]
) -> dict[str, str]:
    """`{level: prompt}` for a recorded 9b2e7a7 prompt, after checking that the level it was flown at
    (`O1-V` or `O0-V`) re-renders to it exactly."""
    recorded = parse_prompt(prompt)
    if render_level(recorded, health, flown_at) != prompt:
        raise ValueError(f"re-rendering at {flown_at} doesn't reproduce the recorded prompt")
    return {level: render_level(recorded, health, level) for level in levels}


@dataclass(frozen=True)
class Step:
    """A planner decision: the agent's record and the prompt the planner was shown."""

    decision: dict[str, Any]
    prompt: str

    @property
    def sim_s(self) -> float:
        return float(self.decision["sim_us"]) / 1e6


def onset_s(episode: dict[str, Any]) -> float | None:
    onsets = [
        e["sim_s"] for e in episode.get("events", []) if e.get("action") == "inject" and e.get("type") != "ok"
    ]
    return min(onsets, default=None)


def planner_steps(episode: dict[str, Any]) -> list[Step]:
    """Each non-continued decision with its prompt; a `dexi` planner asks once per such decision."""
    decisions = [d for d in episode.get("decisions", []) if not d.get("continued")]
    prompts = [p["user_prompt"] for p in episode.get("planner_decisions", [])]
    if len(decisions) != len(prompts):
        raise ValueError(f"{len(decisions)} decisions but {len(prompts)} planner calls")
    return [Step(d, p) for d, p in zip(decisions, prompts, strict=True)]


def first_post_onset(episode: dict[str, Any]) -> Step | None:
    onset = onset_s(episode)
    if onset is None:
        return None
    return next((s for s in planner_steps(episode) if s.sim_s >= onset), None)


def first_informed(episode: dict[str, Any]) -> Step | None:
    """The first decision at or after onset whose recorded health showed the fault (flow below 50 or
    the estimate not valid): the decision at which an O1 prompt first tells the planner."""
    onset = onset_s(episode)
    if onset is None:
        return None
    return next((s for s in planner_steps(episode) if s.sim_s >= onset and showed_degraded(s.decision)), None)


def behaviour(name: str | None, args: dict[str, Any] | None) -> str:
    """What the adapter would fly for a tool call, in the scorer's words (`scoring.behaviour`):
    proceed, hold, land, refuse, or invalid for a call it rejects."""

    def number(key: str) -> float | None:
        value = (args or {}).get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
            return None
        return float(value)

    if name == "move":
        distance = number("distance_m")
        ok = (args or {}).get("direction") in dexi.DIRECTIONS and distance is not None and distance > 0
        return "proceed" if ok else "invalid"
    if name == "rotate":
        return "proceed" if number("deg") is not None else "invalid"
    if name == "hold":
        seconds = number("seconds")
        return "hold" if seconds is not None and seconds > 0 else "invalid"
    if name == "land":
        return "land"
    if name == "refuse":
        reason = (args or {}).get("reason")
        return "refuse" if isinstance(reason, str) and reason.strip() else "invalid"
    return "invalid"


@dataclass(frozen=True)
class Attempt:
    key: str  # the episode key under the run: `<scenario>/<cell>/<rep>-s<seed>`
    label: str  # the attempt's folder
    modified: float
    fetch: Callable[[str], dict[str, Any] | None]  # a file of the attempt, None if it has none

    def episode(self) -> dict[str, Any]:
        episode = self.fetch("episode.json")
        assert episode is not None
        return episode


def _local(root: Path, run: str, keep: Callable[[str], bool]) -> list[Attempt]:
    base = root / run
    out = []
    for path in sorted(base.rglob("episode.json")):
        key = path.parent.parent.relative_to(base).as_posix()
        if keep(key):
            out.append(
                Attempt(key, path.parent.name, path.stat().st_mtime, partial(_read_local, path.parent))
            )
    return out


def _read_local(folder: Path, name: str) -> dict[str, Any] | None:
    path = folder / name
    return dict(json.loads(path.read_text())) if path.is_file() else None


def _s3(uri: str, run: str, keep: Callable[[str], bool]) -> list[Attempt]:
    import boto3

    from lost_in_place.storage import parse_s3_uri

    bucket, prefix = parse_s3_uri(f"{uri.rstrip('/')}/{run}")
    client = boto3.client("s3")

    def read(folder: str, name: str) -> dict[str, Any] | None:
        try:
            body = client.get_object(Bucket=bucket, Key=f"{folder}/{name}")["Body"].read()
        except client.exceptions.NoSuchKey:
            return None
        return dict(json.loads(body))

    out = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/episode.json"):
                continue
            folder = obj["Key"].rsplit("/", 1)[0]
            key, label = folder.removeprefix(f"{prefix}/").rsplit("/", 1)
            if keep(key):
                out.append(Attempt(key, label, obj["LastModified"].timestamp(), partial(read, folder)))
    return out


def attempts(root: str, run: str, keep: Callable[[str], bool] = lambda key: True) -> dict[str, list[Attempt]]:
    """Every attempt of the run's episodes whose key `keep` accepts, by key. `root` is a local mirror
    (`<root>/<run>/<key>/<attempt>/episode.json`) or an `s3://bucket/runs` URI; S3 is only read."""
    found = _s3(root, run, keep) if root.startswith("s3://") else _local(Path(root), run, keep)
    by_key: dict[str, list[Attempt]] = defaultdict(list)
    for a in found:
        by_key[a.key].append(a)
    return dict(by_key)


def counted_attempt(tries: list[Attempt]) -> tuple[Attempt, dict[str, Any]] | None:
    """The earliest attempt that isn't a harness failure, with its episode, or None."""
    for t in sorted(tries, key=lambda t: (t.modified, t.label)):
        episode = t.episode()
        if not is_harness_failure(str(episode.get("outcome", "pending"))):
            return t, episode
    return None


def counted(tries: list[Attempt]) -> dict[str, Any] | None:
    found = counted_attempt(tries)
    return found[1] if found else None


def counted_episodes(by_key: dict[str, list[Attempt]], jobs: int = 16) -> dict[str, dict[str, Any] | None]:
    keys = sorted(by_key)
    with ThreadPoolExecutor(jobs) as pool:
        return dict(zip(keys, pool.map(lambda k: counted(by_key[k]), keys), strict=True))


def send(
    model: str,
    calls: list[dict[str, Any]],
    out: Path,
    client: ChatClient,
    jobs: int = 4,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Send every call whose `id` isn't already in the JSON-lines file `out`, appending one line per
    reply, so a rerun after throttling or an interruption only sends what is missing. A call is
    `{"id", "prompt", ...labels}`, sent with DEXI's tools unless `"tools": False`. A call that raises
    is counted as an error and not written. `log` gets progress counts only, never a reply."""
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text().splitlines() if line.strip()}
    todo = [c for c in calls if c["id"] not in done]
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    counts = {"planned": len(calls), "already": len(done), "sent": 0, "errors": 0, "input_tokens": 0}
    counts["output_tokens"] = 0

    def one(call: dict[str, Any]) -> None:
        tools = dexi.TOOLS if call.get("tools", True) else []
        record = {k: v for k, v in call.items() if k not in ("prompt", "tools")}
        record |= {"model": model, "prompt_sha256": hashlib.sha256(call["prompt"].encode()).hexdigest()}
        try:
            c = client.complete("", call["prompt"], tools)
        except Exception:  # throttling or a model not ready: not written, so a rerun sends it again
            with lock:
                counts["errors"] += 1
            return
        first = c.tool_calls[0] if c.tool_calls else None
        record |= {
            "tool": first.name if first else None,
            "args": dict(first.args) if first else {},
            "text": c.text,
            "reasoning": c.reasoning,
            "stop_reason": c.stop_reason,
            "model_id": c.model_id,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
        }
        with lock:
            counts["sent"] += 1
            counts["input_tokens"] += c.input_tokens
            counts["output_tokens"] += c.output_tokens
            with out.open("a") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
            n = counts["sent"] + counts["errors"]
            if n % 100 == 0:
                log(json.dumps({"progress": n, "of": len(todo), "errors": counts["errors"]}))

    with ThreadPoolExecutor(max(1, jobs)) as pool:
        list(pool.map(one, todo))
    return counts
