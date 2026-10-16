"""`lip e5 health`: the wave's health while it is blinded, in the columns plan section 2.5 allows.

One row per arm and cell (template, condition, observation). The columns are the fixed `COLUMNS`
and nothing else: attempt status and harness-failure kinds, the invalid-reply rate, the manipulation
checks, onset-to-first-decision time, first-move length, wall time, and per arm its metered cost
and throttles. The values come from `episodes.project()`, which never keeps an outcome column.
Every invocation, whatever it prints, appends a line to the wave's queries log.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lost_in_place.manifest import Entry
from lost_in_place.wave.aws import mask
from lost_in_place.wave.episodes import Episode
from lost_in_place.wave.ledger import iso
from lost_in_place.wave.store import Store

COLUMNS = (
    "arm",
    "template",
    "condition",
    "observation",
    "episodes",
    "settled",
    "live",
    "missing",
    "attempts",
    "harness_failures",
    "failure_kinds",
    "invalid_reply_rate",
    "manipulation_ok_rate",
    "manipulation_decision_ok_rate",
    "onset_to_first_decision_s_median",
    "first_move_length_m_median",
    "first_move_short_share",
    "wall_h",
    "metered_usd",
    "throttles",
)
# A first move shorter than this is "short" (the v3 template's moves are at least 1 m).
SHORT_FIRST_MOVE_M = 0.9
TEMPLATE = re.compile(r"/(t\d+)/")
FAMILY_PREFIX = re.compile(r"^[a-z]?\d+_")


def cell_of(entry: Entry) -> tuple[str, str, str]:
    match = TEMPLATE.search(entry.scenario)
    return (match.group(1) if match else "-", FAMILY_PREFIX.sub("", entry.scenario_id), entry.observation)


def _rate(values: Iterable[Any]) -> float | None:
    flags = [bool(v) for v in values if v is not None]
    return round(sum(flags) / len(flags), 4) if flags else None


def _median(values: Iterable[Any]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    return round(statistics.median(numbers), 3) if numbers else None


def cell_rows(arm: str, episodes: Iterable[Episode], live: set[int]) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str, str], list[Episode]] = {}
    for episode in episodes:
        cells.setdefault(cell_of(episode.entry), []).append(episode)
    rows = []
    for (template, condition, observation), group in sorted(cells.items()):
        attempts = [a for e in group for a in e.attempts]
        results = [r for e in group if (r := e.result) is not None]
        kinds = Counter(a.failure_kind for a in attempts if a.failure_kind)
        calls = sum(int(r.fields.get("n_planner_calls") or 0) for r in results)
        invalid = sum(int(r.fields.get("n_invalid") or 0) for r in results)
        moves = [float(m) for r in results if (m := r.fields.get("first_move_length_m")) is not None]
        rows.append(
            {
                "arm": arm,
                "template": template,
                "condition": condition,
                "observation": observation,
                "episodes": len(group),
                "settled": sum(e.settled for e in group),
                "live": sum(e.index in live for e in group),
                "missing": sum(e.missing for e in group),
                "attempts": len(attempts),
                "harness_failures": sum(kinds.values()),
                "failure_kinds": " ".join(f"{k}:{n}" for k, n in sorted(kinds.items())) or "-",
                "invalid_reply_rate": round(invalid / calls, 4) if calls else None,
                "manipulation_ok_rate": _rate(r.fields.get("manipulation_ok") for r in results),
                "manipulation_decision_ok_rate": _rate(
                    r.fields.get("manipulation_decision_ok") for r in results
                ),
                "onset_to_first_decision_s_median": _median(
                    r.fields.get("onset_to_first_decision_s") for r in results
                ),
                "first_move_length_m_median": _median(moves),
                "first_move_short_share": round(sum(m < SHORT_FIRST_MOVE_M for m in moves) / len(moves), 4)
                if moves
                else None,
                "wall_h": round(sum(a.wall_s or 0.0 for a in attempts) / 3600, 3),
                "metered_usd": None,
                "throttles": None,
            }
        )
    return rows


@dataclass(frozen=True)
class ArmCost:
    metered_usd: float | None
    throttles: float | None


def with_costs(rows: Sequence[dict[str, Any]], costs: dict[str, ArmCost]) -> list[dict[str, Any]]:
    """Rows in `COLUMNS` only; an arm's cost and throttles go on its first row."""
    out = []
    seen: set[str] = set()
    for row in rows:
        line = {c: row.get(c) for c in COLUMNS}
        cost = costs.get(row["arm"])
        if cost and row["arm"] not in seen:
            line["metered_usd"] = None if cost.metered_usd is None else round(cost.metered_usd, 2)
            line["throttles"] = cost.throttles
            seen.add(row["arm"])
        out.append(line)
    return out


def table(rows: Sequence[dict[str, Any]]) -> str:
    text = [
        {c: c for c in COLUMNS},
        *({c: "-" if r[c] is None else str(r[c]) for c in COLUMNS} for r in rows),
    ]
    widths = {c: max(len(r[c]) for r in text) for c in COLUMNS}
    return "\n".join("  ".join(r[c].ljust(widths[c]) for c in COLUMNS).rstrip() for r in text)


def git_sha() -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    except OSError:
        return "unknown"
    return done.stdout.strip() or "unknown"


def principal(sts: Any) -> str:
    """The caller's role or user name, never its account."""
    try:
        arn = str(sts.get_caller_identity()["Arn"])
    except Exception:
        return "unknown"
    parts = arn.split(":", 5)[-1].split("/")
    return mask(parts[1] if parts[0] == "assumed-role" and len(parts) > 1 else parts[-1])


def log_query(
    store: Store, key: str, when: datetime, who: str, query: dict[str, Any], rows: int, sha: str | None = None
) -> str:
    line = {
        "at": iso(when),
        "principal": who,
        "query": {k: mask(str(v)) if isinstance(v, str) else v for k, v in sorted(query.items())},
        "columns": list(COLUMNS),
        "rows": rows,
        "git_sha": sha or git_sha(),
    }
    store.append(key, json.dumps(line, sort_keys=True) + "\n")
    return store.where(key)


def report(
    arms: Sequence[tuple[str, Iterable[Episode], set[int]]],
    costs: dict[str, ArmCost],
    *,
    log: Callable[[int], str],
    out: Callable[[str], None] = print,
    json_out: bool = False,
) -> list[dict[str, Any]]:
    rows = with_costs(
        [row for name, episodes, live in arms for row in cell_rows(name, episodes, live)], costs
    )
    where = log(len(rows))
    out(json.dumps(rows, indent=1) if json_out else table(rows))
    print(f"query logged to {where}", file=sys.stderr)
    return rows
