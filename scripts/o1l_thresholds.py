"""The O1-L thresholds `{FLOW}` and `{EPH}` (freeze-v3 design, section 4), model-free.

Data: every non-continued planner decision with recorded health in the counted attempt of each
healthy-state (`category: control`) episode of the runs `e2-student`, `e2-zero-shot` and `e4-claude`.
All arms, validators and both observation levels are pooled, because health is recorded at O0 too.
The counted attempt is the one `lip report` and `lip stats` count (`report.counted`): the earliest
attempt that is a result (`manifest.is_result`). An attempt's score is read only when its outcome is a
harness failure, and then only for `violation`, which `is_result` needs.

- `{FLOW}`: the 1st percentile of `flow_quality`, rounded down to a multiple of 10.
- `{EPH}`: the 99th percentile of `eph_m`, rounded up to a multiple of 0.1 m.

Percentiles are numpy's default (linear interpolation). The runs root is a local mirror holding
`<run>/<episode key>/<attempt>/episode.json`, or an `s3://bucket/runs` URI. It is never written to
the output, only the run names are. Usage:

    uv run --extra aws python scripts/o1l_thresholds.py <runs-root>            # writes the JSON
    uv run --extra aws python scripts/o1l_thresholds.py <runs-root> --check    # compares with it
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from lost_in_place.manifest import is_harness_failure
from lost_in_place.planning import arm_name

RUNS = ("e2-student", "e2-zero-shot", "e4-claude")
OUT = Path(__file__).resolve().parents[1] / "docs" / "prereg" / "o1l-thresholds.json"
FLOW_PERCENTILE, FLOW_GRAIN = 1, 10
EPH_PERCENTILE = 99


@dataclass
class Try:
    key: str
    label: str
    modified: float
    episode: dict[str, Any]
    score: Callable[[], dict[str, Any] | None]


def _local(root: Path, run: str) -> Iterator[Try]:
    base = root / run
    for path in sorted(base.rglob("episode.json")):
        folder = path.parent
        score_path = folder / "score.json"
        yield Try(
            key=folder.parent.relative_to(base).as_posix(),
            label=folder.name,
            modified=path.stat().st_mtime,
            episode=json.loads(path.read_text()),
            score=lambda p=score_path: json.loads(p.read_text()) if p.exists() else None,
        )


def _s3(uri: str, run: str) -> Iterator[Try]:
    import boto3

    from lost_in_place.storage import parse_s3_uri

    bucket, prefix = parse_s3_uri(f"{uri.rstrip('/')}/{run}")
    client = boto3.client("s3")

    def get(key: str) -> Any:
        return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())

    objects = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(("/episode.json", "/score.json")):
                objects[obj["Key"]] = obj["LastModified"].timestamp()
    episodes = sorted(k for k in objects if k.endswith("/episode.json"))
    with ThreadPoolExecutor(16) as pool:
        loaded = list(pool.map(get, episodes))
    for key, episode in zip(episodes, loaded, strict=True):
        folder = key.rsplit("/", 1)[0]
        episode_key, label = folder.rsplit("/", 1)
        score_key = f"{folder}/score.json"
        yield Try(
            key=episode_key.removeprefix(prefix).strip("/"),
            label=label,
            modified=objects[key],
            episode=episode,
            score=lambda k=score_key: get(k) if k in objects else None,
        )


def _counted(tries: list[Try]) -> Try | None:
    for t in sorted(tries, key=lambda t: (t.modified, t.label)):
        outcome = str(t.episode.get("outcome", "pending"))
        if not is_harness_failure(outcome):
            return t
        score = t.score()
        if score and score.get("violation"):
            return t
    return None


def _healthy(tries: Iterator[Try]) -> list[Try]:
    by_key: dict[str, list[Try]] = defaultdict(list)
    for t in tries:
        scenario = t.episode.get("scenario") or {}
        if scenario.get("category") == "control" and scenario.get("state") == "healthy":
            by_key[t.key].append(t)
    return [c for k in sorted(by_key) if (c := _counted(by_key[k])) is not None]


def _percentile(values: list[float], p: int) -> float | None:
    return round(float(np.percentile(values, p)), 4) if values else None


def _summary(flows: list[float], ephs: list[float], episodes: int) -> dict[str, Any]:
    return {
        "episodes": episodes,
        "decisions": len(flows),
        f"flow_quality_p{FLOW_PERCENTILE}": _percentile(flows, FLOW_PERCENTILE),
        f"eph_m_p{EPH_PERCENTILE}": _percentile(ephs, EPH_PERCENTILE),
    }


def thresholds(root: str) -> dict[str, Any]:
    flows: dict[tuple[str, str], list[float]] = defaultdict(list)
    ephs: dict[tuple[str, str], list[float]] = defaultdict(list)
    episodes: dict[tuple[str, str], int] = defaultdict(int)
    arm_runs: dict[str, set[str]] = defaultdict(set)
    for run in RUNS:
        source = _s3(root, run) if root.startswith("s3://") else _local(Path(root), run)
        counted = _healthy(source)
        if not counted:
            raise SystemExit(f"no healthy-state episodes under run {run}")
        for t in counted:
            agent = t.episode.get("agent") or {}
            arm = arm_name(agent.get("planner", ""), agent.get("planner_options"))
            arm_runs[arm].add(run)
            cell = (arm, str(agent.get("observation", "")))
            episodes[cell] += 1
            for d in t.episode.get("decisions", []):
                health = d.get("health")
                if d.get("continued") or not health:
                    continue
                flows[cell].append(float(health["flow_quality"]))
                ephs[cell].append(float(health["eph_m"]))

    def pooled(cells: list[tuple[str, str]]) -> dict[str, Any]:
        return _summary(
            [x for c in cells for x in flows[c]],
            [x for c in cells for x in ephs[c]],
            sum(episodes[c] for c in cells),
        )

    cells = sorted(episodes)
    all_cells = pooled(cells)
    p1 = float(np.percentile([x for c in cells for x in flows[c]], FLOW_PERCENTILE))
    p99 = float(np.percentile([x for c in cells for x in ephs[c]], EPH_PERCENTILE))
    per_arm = {}
    for arm in sorted(arm_runs):
        mine = [c for c in cells if c[0] == arm]
        per_arm[arm] = {
            "runs": sorted(arm_runs[arm]),
            **pooled(mine),
            "by_observation": {c[1]: pooled([c]) for c in mine},
        }
    return {
        "runs": list(RUNS),
        "selection": (
            "non-continued planner decisions with recorded health, counted attempt of each "
            "category=control (healthy) episode; all arms, validators and observation levels pooled"
        ),
        "percentiles": "numpy.percentile, linear interpolation",
        "FLOW": math.floor(p1 / FLOW_GRAIN) * FLOW_GRAIN,
        "EPH": math.ceil(round(p99 * 10, 6)) / 10,
        "rule": {
            "FLOW": f"p{FLOW_PERCENTILE} of flow_quality, rounded down to a multiple of {FLOW_GRAIN}",
            "EPH": f"p{EPH_PERCENTILE} of eph_m, rounded up to a multiple of 0.1 m",
        },
        "pooled": all_cells,
        "per_arm": per_arm,
    }


def render(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("runs_root", help="local mirror of runs/ or an s3://bucket/runs URI")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--check", action="store_true", help="fail if --out differs from a fresh run")
    args = parser.parse_args(argv)
    text = render(thresholds(args.runs_root))
    if args.check:
        same = args.out.exists() and args.out.read_text() == text
        print(f"{args.out}: {'matches' if same else 'DIFFERS'}")
        return 0 if same else 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
