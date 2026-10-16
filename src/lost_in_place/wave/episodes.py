"""What the wave may know about its episodes while it is blinded, and the S4 attempt rule.

Every attempt is read once, by `project()`, which keeps only the columns the freeze-v3 plan allows
during the wave (section 2.5): attempt status and harness-failure kind, the invalid-reply counts, the
manipulation checks, onset-to-first-decision time, first-move length, token counts (metered cost) and
wall time. The S4 rule (`manifest.is_result`: a harness failure whose ULog shows a violation still
counts) needs the violation flag of harness-failed attempts; it is evaluated inside `project()` and
only its verdict, result or retry, leaves it. Nothing else of the score or the decisions is kept.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lost_in_place.manifest import MAX_ATTEMPTS, Entry, is_harness_failure, is_result

ALLOWED_SCORE_FIELDS = (
    "n_planner_calls",
    "n_invalid",
    "manipulation_ok",
    "manipulation_decision_ok",
    "onset_to_first_decision_s",
    "first_move_length_m",
)
LINK_FAILURE_KINDS = ("no_local_position", "error: LinkLost")
# A harness-failed attempt is classified once its score.json is there, or this long after its
# episode.json was uploaded (the runner uploads both together; an attempt without a ULog has none).
SETTLE_S = 300.0


def failure_kind(outcome: str) -> str | None:
    """`timeout`, `no_local_position`, `error: LinkLost`, ...; None for an attempt that isn't a harness
    failure."""
    if not is_harness_failure(outcome):
        return None
    head, _, rest = outcome.partition(":")
    if head.strip() == "error" and rest.strip():
        return f"error: {rest.strip().split(':', 1)[0].strip()}"
    return head.strip()


@dataclass(frozen=True)
class AttemptView:
    key: str
    label: str
    modified: float
    failure_kind: str | None
    counts: bool  # S4: this attempt is the episode's result
    fields: dict[str, Any]
    input_tokens: int
    output_tokens: int
    wall_s: float | None

    @property
    def harness_failure(self) -> bool:
        return self.failure_kind is not None


def project(
    key: str, label: str, modified: float, episode: dict[str, Any], score: dict[str, Any] | None
) -> AttemptView:
    outcome = str(episode.get("outcome", "pending"))
    calls = episode.get("planner_decisions") or []
    stopped = (episode.get("wall_s") or {}).get("stopped")
    return AttemptView(
        key=key,
        label=label,
        modified=modified,
        failure_kind=failure_kind(outcome),
        counts=is_result(outcome, score),
        fields={f: score.get(f) for f in ALLOWED_SCORE_FIELDS if f in score} if score else {},
        input_tokens=sum(int(c.get("input_tokens") or 0) for c in calls),
        output_tokens=sum(int(c.get("output_tokens") or 0) for c in calls),
        wall_s=float(stopped) if stopped is not None else None,
    )


class Runs(Protocol):
    def folders(self) -> dict[str, tuple[float, bool]]:
        """{attempt folder relative to the sweep's runs root: (episode.json upload time, has score.json)}"""
        ...

    def read(self, folder: str, name: str) -> dict[str, Any]: ...


class S3Runs:
    def __init__(self, s3: Any, bucket: str, prefix: str) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/"

    def folders(self) -> dict[str, tuple[float, bool]]:
        episodes: dict[str, float] = {}
        scores: set[str] = set()
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                key = str(obj["Key"])
                folder, _, name = key.removeprefix(self.prefix).rpartition("/")
                if name == "episode.json":
                    episodes[folder] = obj["LastModified"].timestamp()
                elif name == "score.json":
                    scores.add(folder)
        return {f: (m, f in scores) for f, m in episodes.items()}

    def read(self, folder: str, name: str) -> dict[str, Any]:
        body = self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}{folder}/{name}")["Body"].read()
        return dict(json.loads(body))


class LocalRuns:
    """A local mirror laid out like the bucket (`<root>/<episode key>/<attempt label>/...`)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def folders(self) -> dict[str, tuple[float, bool]]:
        return {
            p.parent.relative_to(self.root).as_posix(): (
                p.stat().st_mtime,
                (p.parent / "score.json").exists(),
            )
            for p in self.root.rglob("episode.json")
        }

    def read(self, folder: str, name: str) -> dict[str, Any]:
        return dict(json.loads((self.root / folder / name).read_text()))


@dataclass
class Episode:
    index: int
    entry: Entry
    attempts: list[AttemptView] = field(default_factory=list)
    pending: int = 0  # harness-failed attempts not yet classified

    @property
    def result(self) -> AttemptView | None:
        return next((a for a in self.attempts if a.counts), None)

    @property
    def settled(self) -> bool:
        return self.result is not None or len(self.attempts) >= MAX_ATTEMPTS

    @property
    def missing(self) -> bool:
        return self.result is None and len(self.attempts) >= MAX_ATTEMPTS


class Scanner:
    """Reads a sweep's attempts incrementally: each attempt folder is projected once, when it can be
    classified, and never re-read."""

    def __init__(
        self, runs: Runs, entries: dict[int, Entry], settle_s: float = SETTLE_S, clock: Any = time.time
    ):
        self.runs = runs
        self.entries = entries
        self.by_key = {e.key: i for i, e in entries.items()}
        self.settle_s = settle_s
        self.clock = clock
        self.views: dict[str, AttemptView] = {}

    def scan(self) -> tuple[dict[int, Episode], list[AttemptView]]:
        """Every episode of `entries`, and the attempts classified in this call."""
        episodes = {i: Episode(i, e) for i, e in self.entries.items()}
        new: list[AttemptView] = []
        now = self.clock()
        for folder, (modified, has_score) in self.runs.folders().items():
            key, _, label = folder.rpartition("/")
            index = self.by_key.get(key)
            if index is None:
                continue
            view = self.views.get(folder)
            if view is None:
                trace = self.runs.read(folder, "episode.json")
                outcome = str(trace.get("outcome", "pending"))
                if is_harness_failure(outcome) and not has_score and now - modified < self.settle_s:
                    episodes[index].pending += 1
                    continue
                score = self.runs.read(folder, "score.json") if has_score else None
                view = project(key, label, modified, trace, score)
                self.views[folder] = view
                new.append(view)
            episodes[index].attempts.append(view)
        for episode in episodes.values():
            episode.attempts.sort(key=lambda a: a.modified)
        new.sort(key=lambda a: a.modified)
        return episodes, new


def attempts_in_order(episodes: Iterable[Episode]) -> list[AttemptView]:
    return sorted((a for e in episodes for a in e.attempts), key=lambda a: a.modified)


def results_in_order(episodes: Iterable[Episode]) -> list[AttemptView]:
    return sorted((r for e in episodes if (r := e.result) is not None), key=lambda a: a.modified)
