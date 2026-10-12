"""Sweeps as manifests, episode keys, and which attempt of an episode counts.

A sweep spec crosses scenarios x reps x planner x validator x observation. The manifest lists
every episode once, in a shuffled order, and a Batch array job runs entry `offset + index`.
Each episode has a key, `<scenario>/<planner>-<validator>-<obs>/r<rep>-s<seed>`, and every attempt
uploads to its own folder under it, so a resubmitted array skips episodes that already have a
result (see `docs/fidelity.md`, "Which attempt counts").
"""

from __future__ import annotations

import glob
import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lost_in_place.scenario import load_scenario

BATCH_ARRAY_MAX = 10_000
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

    @property
    def cell(self) -> str:
        return f"{self.planner}-{self.validator}-{self.observation}"

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
        self.scenarios: list[str] = list(data["scenarios"])
        self.reps: int = int(data["reps"])
        self.planners: list[str] = list(data.get("planners", ["scripted-naive"]))
        self.validators: list[str] = list(data.get("validators", ["none"]))
        self.observations: list[str] = list(data.get("observations", ["O0"]))
        self.latency_s = float(data.get("latency_s", 2.0))
        self.planner_options: dict[str, str] = dict(data.get("planner_options", {}))
        self.shuffle_seed = int(data.get("shuffle_seed", 0))
        self.data = data

    @classmethod
    def load(cls, path: Path) -> SweepSpec:
        return cls(yaml.safe_load(path.read_text()))


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
            for planner in spec.planners:
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
                                planner_options=spec.planner_options,
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
            "spec": spec.data,
            "spec_sha256": hashlib.sha256(spec_text.encode()).hexdigest()[:16],
            "entries": [asdict(e) for e in entries],
        },
        indent=1,
    )


def loads(text: str) -> list[Entry]:
    return [Entry(**e) for e in json.loads(text)["entries"]]


def read(uri: str) -> list[Entry]:
    if uri.startswith("s3://"):
        import boto3

        from lost_in_place.storage import parse_s3_uri

        bucket, key = parse_s3_uri(uri)
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return loads(body.decode())
    return loads(Path(uri).read_text())


def array_chunks(n: int) -> list[tuple[int, int]]:
    """(offset, size) for each array job a manifest of `n` entries needs."""
    return [(offset, min(BATCH_ARRAY_MAX, n - offset)) for offset in range(0, n, BATCH_ARRAY_MAX)]


def settled(outcomes: list[str]) -> bool:
    """Given an episode's attempt outcomes so far: does it have a result, or is it out of attempts?"""
    return any(not is_harness_failure(o) for o in outcomes) or len(outcomes) >= MAX_ATTEMPTS
