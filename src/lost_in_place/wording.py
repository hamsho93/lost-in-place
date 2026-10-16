"""The wording check's interpretation probe and rule A-1 (freeze-v3 §2.3, §5.2).

The probe sends each family's healthy post-leg-1 prompt (`dryrun.post_leg1_prompt`, DEXI's path at
O0), under each template and at both DEXI prompt versions, `PROBE_REPS` times with no tools, followed
by `PROBE_QUESTION`. A reply restates the remaining waypoint; it is a misread if that point is more
than `MISREAD_M` from the true last waypoint, and invalid if no point can be read from it.

A-1 adopts T1 if the student's T1 dry-run success is at least its T0 success minus 10 points, and,
for every probed model, the T1 misread rate (misreads over valid replies) is no higher than T0's.
Otherwise it keeps T0.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from lost_in_place import families as fam
from lost_in_place.llm import dexi
from lost_in_place.llm.clients import ChatClient
from lost_in_place.scenario import Scenario

PROBE_REPS = 3
PROBE_VERSIONS = (dexi.PROMPT_V2, dexi.PROMPT_VERSION)
MISREAD_M = 0.5
STUDENT_MARGIN_POINTS = 10.0
PROBE_QUESTION = (
    "Do not choose an action yet. First, where is the point the mission says to hold at, in the same"
    " north/east coordinates as the current state? Reply with one line only, in this form:\n"
    "north <metres> m, east <metres> m"
)

_NUMBER = r"([-+]?\d+(?:\.\d+)?)"
_NORTH_EAST = re.compile(rf"north\s*[:=]?\s*{_NUMBER}\s*m?\b[\s,;]*(?:and\s+)?east\s*[:=]?\s*{_NUMBER}", re.I)
_PLACE = re.compile(rf"{_NUMBER}\s*m\s+(north|south)\b[\s,;]*(?:and\s+)?{_NUMBER}\s*m\s+(east|west)\b", re.I)
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)


def probe_message(prompt: str) -> str:
    return f"{prompt}\n\n{PROBE_QUESTION}"


def parse_point(text: str) -> tuple[float, float] | None:
    """The (north, east) point a reply restates: the last "north X m, east Y m", or "X m north and
    Y m west", outside any reasoning block. None if there is none."""
    visible = _THINKING.sub("", text).split("<think>", 1)[0].replace("\u2212", "-")
    found: list[tuple[int, tuple[float, float]]] = []
    for m in _NORTH_EAST.finditer(visible):
        found.append((m.start(), (float(m[1]), float(m[2]))))
    for m in _PLACE.finditer(visible):
        north = float(m[1]) * (1 if m[2].lower() == "north" else -1)
        east = float(m[3]) * (1 if m[4].lower() == "east" else -1)
        found.append((m.start(), (north, east)))
    return max(found)[1] if found else None


@dataclass(frozen=True)
class ProbePrompt:
    family: str
    template: str
    prompt_version: str
    message: str
    goal: tuple[float, float]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.message.encode()).hexdigest()


def probe_prompts(by_template: Mapping[str, Sequence[Scenario]]) -> list[ProbePrompt]:
    """Every family x template x prompt version, in that order within each template."""
    from lost_in_place.dryrun import post_leg1_prompt

    out = []
    for template, scenarios in by_template.items():
        for scenario in scenarios:
            assert scenario.mission is not None and scenario.pair_id is not None
            goal = scenario.mission.waypoints[-1]
            for version in PROBE_VERSIONS:
                message = probe_message(post_leg1_prompt(scenario, version))
                out.append(
                    ProbePrompt(scenario.pair_id, template, version, message, (goal.north_m, goal.east_m))
                )
    return out


@dataclass(frozen=True)
class ProbeReply:
    family: str
    template: str
    prompt_version: str
    rep: int
    prompt_sha256: str
    text: str
    point: tuple[float, float] | None
    valid: bool
    misread: bool | None
    distance_m: float | None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


def judge(
    prompt: ProbePrompt, rep: int, text: str, input_tokens: int = 0, output_tokens: int = 0
) -> ProbeReply:
    point = parse_point(text)
    distance = None if point is None else round(math.dist(point, prompt.goal), 3)
    return ProbeReply(
        prompt.family,
        prompt.template,
        prompt.prompt_version,
        rep,
        prompt.sha256,
        text,
        point,
        point is not None,
        None if distance is None else distance > MISREAD_M,
        distance,
        input_tokens,
        output_tokens,
    )


def run_probe(
    prompts: Sequence[ProbePrompt],
    client: ChatClient,
    reps: int = PROBE_REPS,
    jobs: int = 4,
    progress: Callable[[ProbeReply], None] | None = None,
) -> list[ProbeReply]:
    """Send every prompt `reps` times with no tools. A client error is recorded, not raised."""

    def one(item: tuple[ProbePrompt, int]) -> ProbeReply:
        prompt, rep = item
        try:
            c = client.complete("", prompt.message, [])
            reply = judge(prompt, rep, c.text, c.input_tokens, c.output_tokens)
        except Exception as exc:
            reply = ProbeReply(
                prompt.family,
                prompt.template,
                prompt.prompt_version,
                rep,
                prompt.sha256,
                text="",
                point=None,
                valid=False,
                misread=None,
                distance_m=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        if progress is not None:
            progress(reply)
        return reply

    items = [(p, r) for p in prompts for r in range(reps)]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(one, items))


def summarise_probe(replies: Iterable[ProbeReply]) -> dict[str, dict[str, Any]]:
    """Per template: replies, client errors, invalid (unparseable), valid, misreads, and the misread
    rate over valid replies (None with none valid)."""
    out: dict[str, dict[str, Any]] = {}
    for r in replies:
        row = out.setdefault(r.template, {"n": 0, "error": 0, "invalid": 0, "valid": 0, "misread": 0})
        row["n"] += 1
        row["error"] += bool(r.error)
        row["invalid"] += not r.error and not r.valid
        row["valid"] += r.valid
        row["misread"] += bool(r.misread)
    for row in out.values():
        row["misread_rate"] = row["misread"] / row["valid"] if row["valid"] else None
    return out


def replies_to_json(replies: Iterable[ProbeReply]) -> list[dict[str, Any]]:
    return [asdict(r) for r in replies]


def replies_from_json(rows: Iterable[Mapping[str, Any]]) -> list[ProbeReply]:
    return [
        ProbeReply(**{**row, "point": None if row["point"] is None else tuple(row["point"])}) for row in rows
    ]


class IncompleteInput(ValueError):
    """A wording-check input that doesn't cover every family, template and repeat, or has errors."""


def a1(
    student: Mapping[str, Mapping[str, int]],
    probes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    families_per_template: int = 66,
) -> dict[str, Any]:
    """Rule A-1 from the dry-run summary (`dryrun.summarise`) and each probed model's
    `summarise_probe`. Raises `IncompleteInput` unless every input is complete and error-free."""
    expected_replies = families_per_template * len(PROBE_VERSIONS) * PROBE_REPS
    for template in fam.TEMPLATES:
        row = student.get(template)
        if row is None or row["n"] != families_per_template or row["harness_error"]:
            raise IncompleteInput(f"dry run, {template}: {row}")
        for model, summary in probes.items():
            prow = summary.get(template)
            if prow is None or prow["n"] != expected_replies or prow["error"]:
                raise IncompleteInput(f"probe {model}, {template}: {prow}")
    rate = {t: 100.0 * student[t]["success"] / student[t]["n"] for t in fam.TEMPLATES}
    student_ok = rate["t1"] >= rate["t0"] - STUDENT_MARGIN_POINTS
    models_ok = {}
    for model, summary in probes.items():
        r0, r1 = summary["t0"]["misread_rate"], summary["t1"]["misread_rate"]
        models_ok[model] = r0 is not None and r1 is not None and r1 <= r0
    adopt = "t1" if student_ok and all(models_ok.values()) else "t0"
    return {
        "rule": "A-1: adopt t1 iff student t1 success >= t0 success - 10 points and, for every probed model,"
        " t1 misread rate <= t0 misread rate; otherwise t0",
        "student": {t: {**student[t], "success_pct": round(rate[t], 2)} for t in fam.TEMPLATES},
        "student_condition": student_ok,
        "probes": {m: {t: dict(s[t]) for t in fam.TEMPLATES} for m, s in probes.items()},
        "probe_conditions": models_ok,
        "template": adopt,
    }


# Rough cost, for the --dry-run plans. Bedrock on-demand $ per million input and output tokens.
# A fallback entry (`<model>-fbN`) is priced as its model.
TOKEN_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "gpt-6.1-sol": (2.20, 11.0),
    "claude-sonnet-5-5": (2.20, 11.0),
}
# Reasoning tokens a call adds, unmeasured (§10.1 assumes a median of 200 at effort low).
REASONING_TOKENS = {"gpt-6.1-sol": 200, "claude-sonnet-5-5": 200}
# Claude 4.7 and later tokenize the same text into about 30% more tokens.
TOKENIZER_FACTOR = {"claude-sonnet-5-5": 1.3}
CMU_USD_PER_MIN = 0.05718
STUDENT_CMUS = 2


def probe_cost(
    model: str, prompts: Sequence[ProbePrompt], reps: int = PROBE_REPS, output_tokens: int = 25
) -> dict[str, Any]:
    base = re.sub(r"-fb\d+$", "", model)
    factor = TOKENIZER_FACTOR.get(base, 1.0)
    calls = len(prompts) * reps
    tokens_in = sum(len(p.message) for p in prompts) * reps / 3.5 * factor
    tokens_out = int(calls * (output_tokens * factor + REASONING_TOKENS.get(base, 0)))
    price = TOKEN_PRICES.get(base)
    usd = None if price is None else round((tokens_in * price[0] + tokens_out * price[1]) / 1e6, 2)
    return {
        "model": model,
        "calls": calls,
        "input_tokens": int(tokens_in),
        "output_tokens": tokens_out,
        "usd": usd,
    }


def dryrun_cost(
    episodes: int, calls_per_episode: float = 6.0, copy_minutes: Sequence[float] = (15, 45)
) -> dict[str, Any]:
    """An imported model is billed per copy-minute in 5-minute windows while a copy is up, whatever
    the call count: the range is a warm run with its idle tail, and one with a slow restore."""
    usd = [round(STUDENT_CMUS * CMU_USD_PER_MIN * m, 2) for m in copy_minutes]
    return {
        "episodes": episodes,
        "calls": int(episodes * calls_per_episode),
        "copy_minutes": list(copy_minutes),
        "usd": usd,
    }
