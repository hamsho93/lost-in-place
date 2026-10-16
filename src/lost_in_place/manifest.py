"""Sweeps as manifests, episode keys, and which attempt of an episode counts.

A sweep spec crosses scenarios x reps x planner x validator x observation. The manifest lists
every episode once, in a shuffled order, and a Batch array job runs entry `offset + index`.
Each episode has a key, `<scenario>/<planner>-<validator>-<obs>/r<rep>-s<seed>`, and every attempt
uploads to its own folder under `<runs>/<sweep name>/<key>/`, so a resubmitted array skips episodes
that already have a result (see `docs/fidelity.md`, "Which attempt counts").
"""

from __future__ import annotations

import glob
import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lost_in_place.planning import (
    DEXI_PLANNER,
    LLM_PLANNER,
    arm_name,
    available_planners,
    check_observation_level,
)
from lost_in_place.scenario import load_scenario

BATCH_ARRAY_MAX = 10_000
OPTION = re.compile(r"[a-z0-9][a-z0-9._-]*")
MAX_ATTEMPTS = 3  # Batch's retry_attempts; an episode with no result after these is missing
HARNESS_FAILURES = (
    "boot_stall",
    "stuck_on_ground",
    "sim_stall",
    "inject_failed",
    "no_local_position",
    "arming_denied",
    "timeout",
    "error",
    "pending",  # the runner died before an outcome was set
)


def is_harness_failure(outcome: str) -> bool:
    return outcome.split(":", 1)[0].strip() in HARNESS_FAILURES


def is_result(outcome: str, score: dict[str, Any] | None = None) -> bool:
    """Whether an attempt counts as the episode's result: any outcome but a harness failure, or a
    harness failure whose ULog shows a violation (design section 8). The violation came before the
    stall, timeout or error that ended the run, so a retry would re-roll it."""
    return not is_harness_failure(outcome) or bool(score and score.get("violation"))


def episode_seed(scenario_group: str, rep: int) -> int:
    """`hash(scenario, rep)`: stable across machines and Python versions, unlike `hash()`."""
    digest = hashlib.sha256(f"{scenario_group}/{rep}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2**31


@dataclass(frozen=True)
class Entry:
    scenario: str  # path inside the image, relative to the repo root
    scenario_id: str
    rep: int
    seed: int
    planner: str = "scripted-naive"
    validator: str = "none"
    observation: str = "O0"
    latency_s: float = 2.0
    planner_options: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_observation_level(self.observation)

    @property
    def cell(self) -> str:
        return f"{arm_name(self.planner, self.planner_options)}-{self.validator}-{self.observation}"

    @property
    def key(self) -> str:
        return f"{self.scenario_id}/{self.cell}/r{self.rep:03d}-s{self.seed}"


class SweepSpec:
    """The YAML a manifest is built from; see `sweeps/`."""

    def __init__(self, data: dict[str, Any]) -> None:
        unknown = set(data) - {
            "name",
            "scenarios",
            "reps",
            "planners",
            "validators",
            "observations",
            "latency_s",
            "planner_options",
            "shuffle_seed",
        }
        if unknown:
            raise ValueError(f"unknown sweep keys: {sorted(unknown)}")
        self.name: str = data["name"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.name):
            raise ValueError(f"sweep name {self.name!r}: use lower case, digits, '.', '_' and '-'")
        self.scenarios: list[str] = list(data["scenarios"])
        self.reps: int = int(data["reps"])
        self.planners: list[tuple[str, dict[str, str]]] = [
            _planner_entry(p) for p in data.get("planners", ["scripted-naive"])
        ]
        self.validators: list[str] = list(data.get("validators", ["none"]))
        self.observations: list[str] = [
            check_observation_level(str(o)) for o in data.get("observations", ["O0"])
        ]
        self.latency_s = float(data.get("latency_s", 2.0))
        self.planner_options = {str(k): str(v) for k, v in (data.get("planner_options") or {}).items()}
        self.shuffle_seed = int(data.get("shuffle_seed", 0))
        self.data = data

    @classmethod
    def load(cls, path: Path) -> SweepSpec:
        return cls(yaml.safe_load(path.read_text()))


def _planner_entry(entry: str | dict[str, Any]) -> tuple[str, dict[str, str]]:
    """A `planners` item: an arm name, or `{arm: <name>, options: {...}}` for options of that arm
    alone, on top of the sweep's `planner_options`."""
    if isinstance(entry, str):
        return entry, {}
    unknown = set(entry) - {"arm", "options"}
    if unknown or "arm" not in entry:
        raise ValueError(f"planner entry {entry!r}: use a name or {{arm: <name>, options: {{...}}}}")
    return str(entry["arm"]), {str(k): str(v) for k, v in (entry.get("options") or {}).items()}


def resolve_arm(name: str, options: dict[str, str]) -> tuple[str, dict[str, str]]:
    """A sweep's planner entry is an installed planner, a model name from `llm/models.yaml`, or
    `<planner>/<model>` for a planner that runs a model (`dexi/qwen3-4b-student`). Options other
    than `model` name the arm too (`planning.arm_name`), so they must be safe in an S3 key."""
    for key, value in options.items():
        if key != "model" and not (OPTION.fullmatch(key) and OPTION.fullmatch(str(value))):
            raise ValueError(f"planner option {key}={value!r}: use lower case, digits, '.', '_' and '-'")
    if name in available_planners():
        planner, resolved = name, dict(options)
    else:
        from lost_in_place.llm.registry import get_model

        prefix, _, model = name.rpartition("/")
        if prefix and prefix not in available_planners():
            raise LookupError(f"no planner '{prefix}'; installed: {', '.join(available_planners())}")
        from lost_in_place.llm.scripted_dexi import SCRIPTED_PROVIDER

        if get_model(model).provider == SCRIPTED_PROVIDER and prefix != DEXI_PLANNER:
            raise ValueError(f"'{model}' is scripted on DEXI's prompt: use '{DEXI_PLANNER}/{model}'")
        planner, resolved = prefix or LLM_PLANNER, {**options, "model": model}
    if planner == DEXI_PLANNER:
        from lost_in_place.llm.dexi import check_options

        check_options(resolved)
    return planner, resolved


def build(spec: SweepSpec, root: Path = Path()) -> list[Entry]:
    """Every episode of the sweep, shuffled across cells.

    Seeds come from the scenario's matched pair (its id when it has none) and the rep, so a fault
    scenario and its healthy control fly the same spawn, and every arm sees the same seeds.
    """
    paths = sorted({p for pattern in spec.scenarios for p in glob.glob(str(root / pattern))})
    if not paths:
        raise ValueError(f"no scenarios match {spec.scenarios}")
    entries = []
    for path in paths:
        scenario = load_scenario(path)
        group = scenario.pair_id or scenario.id
        for rep in range(spec.reps):
            for planner, options in (
                resolve_arm(arm, {**spec.planner_options, **own}) for arm, own in spec.planners
            ):
                for validator in spec.validators:
                    for observation in spec.observations:
                        entries.append(
                            Entry(
                                scenario=Path(path).relative_to(root).as_posix(),
                                scenario_id=scenario.id,
                                rep=rep,
                                seed=episode_seed(group, rep),
                                planner=planner,
                                validator=validator,
                                observation=observation,
                                latency_s=spec.latency_s,
                                planner_options=options,
                            )
                        )
    keys = [e.key for e in entries]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate episode keys: two scenario files share an id")
    random.Random(spec.shuffle_seed).shuffle(entries)
    return entries


def dumps(spec: SweepSpec, entries: list[Entry]) -> str:
    spec_text = json.dumps(spec.data, sort_keys=True)
    return json.dumps(
        {
            "name": spec.name,
            "runs_prefix": spec.name,
            "spec": spec.data,
            "spec_sha256": hashlib.sha256(spec_text.encode()).hexdigest()[:16],
            "entries": [asdict(e) for e in entries],
        },
        indent=1,
    )


def loads(text: str) -> list[Entry]:
    return [Entry(**e) for e in json.loads(text)["entries"]]


def read_document(uri: str) -> dict[str, Any]:
    if uri.startswith("s3://"):
        import boto3

        from lost_in_place.storage import parse_s3_uri

        bucket, key = parse_s3_uri(uri)
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return dict(json.loads(body.decode()))
    return dict(json.loads(Path(uri).read_text()))


def read(uri: str) -> list[Entry]:
    return [Entry(**e) for e in read_document(uri)["entries"]]


def runs_root(runs: str, document: dict[str, Any]) -> str:
    """Where a manifest's episodes live in the results bucket: `<runs>/<sweep name>`.

    Episode keys don't name the sweep, so without the prefix a sweep over the same scenarios and
    arms as an earlier one would skip those episodes and count the earlier results as its own.
    Manifests built before the prefix existed (the pilots) have none and stay at the root.
    """
    prefix = document.get("runs_prefix")
    return f"{runs.rstrip('/')}/{prefix}" if prefix else runs


def array_chunks(n: int) -> list[tuple[int, int]]:
    """(offset, size) for each array job a manifest of `n` entries needs."""
    return [(offset, min(BATCH_ARRAY_MAX, n - offset)) for offset in range(0, n, BATCH_ARRAY_MAX)]


def settled(attempts: list[tuple[str, dict[str, Any] | None]]) -> bool:
    """Given an episode's attempts so far, as (outcome, score or None): does it have a result, or is
    it out of attempts?"""
    return any(is_result(o, s) for o, s in attempts) or len(attempts) >= MAX_ATTEMPTS
