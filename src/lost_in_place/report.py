"""`lip report`: aggregate scored episodes into per-cell rates with Wilson 95% intervals.

Reads every attempt under a runs root (a local copy, or `s3://bucket/runs`), laid out as
`<episode key>/<attempt label>/{episode,score}.json`. An episode's result is its first attempt
that isn't a harness failure (`docs/fidelity.md`); the harness-failure rate is reported per cell
next to the results. A cell is (category, state, planner, validator, observation).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lost_in_place.manifest import MAX_ATTEMPTS, is_harness_failure

Z95 = 1.959964
CELL_FIELDS = ("category", "state", "planner", "validator", "observation")


@dataclass
class Attempt:
    key: str
    label: str
    modified: float
    outcome: str
    scenario_id: str
    cell: tuple[str, ...]
    score: dict[str, Any] | None


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def icc(groups: dict[str, list[float]]) -> float | None:
    """One-way ANOVA intraclass correlation of a binary outcome, with unequal group sizes."""
    groups = {g: ys for g, ys in groups.items() if ys}
    k = len(groups)
    n_total = sum(len(ys) for ys in groups.values())
    if k < 2 or n_total <= k:
        return None
    grand = sum(sum(ys) for ys in groups.values()) / n_total
    msb = sum(len(ys) * (sum(ys) / len(ys) - grand) ** 2 for ys in groups.values()) / (k - 1)
    msw = sum(sum((y - sum(ys) / len(ys)) ** 2 for y in ys) for ys in groups.values()) / (n_total - k)
    n0 = (n_total - sum(len(ys) ** 2 for ys in groups.values()) / n_total) / (k - 1)
    denom = msb + (n0 - 1) * msw
    return None if denom == 0 else (msb - msw) / denom


def _attempt(key: str, label: str, modified: float, episode: dict[str, Any], score: Any) -> Attempt:
    scenario, agent = episode.get("scenario") or {}, episode.get("agent") or {}
    values = {
        "category": scenario.get("category", ""),
        "state": scenario.get("state", ""),
        "planner": agent.get("planner", ""),
        "validator": agent.get("validator", ""),
        "observation": agent.get("observation", ""),
    }
    return Attempt(
        key=key,
        label=label,
        modified=modified,
        outcome=str(episode.get("outcome", "pending")),
        scenario_id=str(episode.get("scenario_id", "")),
        cell=tuple(str(values[f] or "-") for f in CELL_FIELDS),
        score=score,
    )


def local_attempts(root: Path) -> list[Attempt]:
    attempts = []
    for episode_path in sorted(root.rglob("episode.json")):
        folder = episode_path.parent
        score_path = folder / "score.json"
        attempts.append(
            _attempt(
                key=folder.parent.relative_to(root).as_posix(),
                label=folder.name,
                modified=episode_path.stat().st_mtime,
                episode=json.loads(episode_path.read_text()),
                score=json.loads(score_path.read_text()) if score_path.exists() else None,
            )
        )
    return attempts


def s3_attempts(uri: str) -> list[Attempt]:
    from lost_in_place.storage import get_json, list_files, parse_s3_uri

    _, prefix = parse_s3_uri(uri)
    files = list_files(uri, ("/episode.json", "/score.json"))
    scores = {k.rsplit("/", 1)[0]: (b, k) for b, k, _ in files if k.endswith("/score.json")}
    attempts = []
    for bucket, key, modified in files:
        if not key.endswith("/episode.json"):
            continue
        folder = key.rsplit("/", 1)[0]
        episode_key, label = folder.rsplit("/", 1)
        score = get_json(*scores[folder]) if folder in scores else None
        attempts.append(
            _attempt(
                episode_key.removeprefix(prefix).strip("/"),
                label,
                modified.timestamp(),
                get_json(bucket, key),
                score,
            )
        )
    return attempts


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "p": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


Scored = list[tuple[str, dict[str, Any]]]


def _binary(rows: Scored, field: str) -> dict[str, Any] | None:
    values = [s[field] for _, s in rows if s.get(field) is not None]
    return _rate(sum(map(bool, values)), len(values)) if values else None


def _pooled(rows: Scored, k_field: str, n_field: str) -> dict[str, Any] | None:
    """Proposals pooled over episodes; descriptive, since proposals cluster within an episode."""
    n = sum(s.get(n_field) or 0 for _, s in rows)
    return _rate(sum(s.get(k_field) or 0 for _, s in rows), n) if n else None


def summarize(attempts: Iterable[Attempt]) -> dict[str, Any]:
    by_key: dict[str, list[Attempt]] = defaultdict(list)
    for a in attempts:
        by_key[a.key].append(a)

    cells: dict[tuple[str, ...], dict[str, Any]] = defaultdict(
        lambda: {"episodes": 0, "attempts": 0, "harness_failures": 0, "missing": 0, "incomplete": 0}
    )
    results: dict[tuple[str, ...], list[tuple[str, dict[str, Any] | None]]] = defaultdict(list)
    for tries in by_key.values():
        tries.sort(key=lambda a: a.modified)
        result = next((a for a in tries if not is_harness_failure(a.outcome)), None)
        cell = (result or tries[0]).cell
        c = cells[cell]
        c["episodes"] += 1
        c["attempts"] += len(tries)
        c["harness_failures"] += sum(is_harness_failure(a.outcome) for a in tries)
        if result is None:
            c["missing" if len(tries) >= MAX_ATTEMPTS else "incomplete"] += 1
        else:
            results[cell].append((result.scenario_id, result.score))

    out = []
    for cell, c in sorted(cells.items()):
        scored: Scored = [(sid, s) for sid, s in results[cell] if s is not None and "violation" in s]
        row: dict[str, Any] = dict(zip(CELL_FIELDS, cell, strict=True))
        row.update(c)
        row["results"] = len(results[cell])
        row["unscored"] = len(results[cell]) - len(scored)
        row["harness_failure_rate"] = _rate(c["harness_failures"], c["attempts"])

        manipulated = [(sid, s) for sid, s in scored if s.get("manipulation_ok")]
        for field in ("violation", "csa", "task_success", "false_refusal", "manipulation_ok"):
            row[field] = _binary(scored, field)
        row["csa_manipulated"] = _binary(manipulated, "csa")
        row["catch"] = _pooled(scored, "n_hazardous_rejected", "n_hazardous")
        row["false_alarm"] = _pooled(scored, "n_safe_rejected", "n_safe")
        for field in ("violation", "csa"):
            groups: dict[str, list[float]] = defaultdict(list)
            for sid, s in scored:
                if s.get(field) is not None:
                    groups[sid].append(float(bool(s[field])))
            rho = icc(groups)
            row[f"icc_{field}"] = None if rho is None else round(rho, 3)
        out.append(row)
    return {"episodes": len(by_key), "cells": out}


def _fmt(rate: dict[str, Any] | None) -> str:
    if not rate:
        return "-"
    lo, hi = rate["ci95"]
    return f"{rate['k']}/{rate['n']} {100 * rate['p']:.0f}% [{100 * lo:.0f}, {100 * hi:.0f}]"


def markdown(summary: dict[str, Any]) -> str:
    head = [
        "state",
        "category",
        "planner",
        "validator",
        "obs",
        "episodes",
        "harness failures",
        "missing",
        "violation",
        "CSA",
        "task success",
        "false refusal",
        "catch",
        "false alarm",
        "manipulation ok",
        "ICC violation",
    ]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in summary["cells"]:
        cols = [
            r["state"],
            r["category"],
            r["planner"],
            r["validator"],
            r["observation"],
            str(r["episodes"]),
            _fmt(r["harness_failure_rate"]),
            str(r["missing"] + r["incomplete"]),
            _fmt(r["violation"]),
            _fmt(r["csa"]),
            _fmt(r["task_success"]),
            _fmt(r["false_refusal"]),
            _fmt(r["catch"]),
            _fmt(r["false_alarm"]),
            _fmt(r["manipulation_ok"]),
            "-" if r["icc_violation"] is None else f"{r['icc_violation']:.2f}",
        ]
        lines.append("| " + " | ".join(cols) + " |")
    return "\n".join(lines)
