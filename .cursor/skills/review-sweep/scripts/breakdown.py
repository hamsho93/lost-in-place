"""Mirror a sweep's attempts locally, optionally re-score them, and break results down below the cell level.

    uv run python .cursor/skills/review-sweep/scripts/breakdown.py mirror s3://BUCKET/runs \
        --manifest manifests/e1-pilot.json --out /tmp/sweep-mirror [--rescore]
    uv run python .cursor/skills/review-sweep/scripts/breakdown.py breakdown /tmp/sweep-mirror \
        --manifest manifests/e1-pilot.json

`mirror` copies every attempt's episode.json (mtime set to the upload time, which `report.counted`
orders by) and the counted attempt's score.json. `--rescore` re-scores counted attempts from their
ULog with the scorer in this checkout instead. `lip report <out> --manifest ...` then gives the cell table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lost_in_place import manifest, report


def _keys(manifest_path: str) -> set[str]:
    return {e.key for e in manifest.read(manifest_path)}


def mirror(runs: str, manifest_path: str, out: Path, rescore: bool) -> None:
    import boto3
    from botocore.exceptions import ClientError

    from lost_in_place.scoring import load_tracks, score
    from lost_in_place.storage import list_files, parse_s3_uri

    keys = _keys(manifest_path)
    _, prefix = parse_s3_uri(runs)
    s3 = boto3.client("s3")
    by_key: dict[str, list[report.Attempt]] = defaultdict(list)
    sources: dict[tuple[str, str], tuple[str, str]] = {}

    def fetch(item: tuple[str, str, Any]) -> report.Attempt | None:
        bucket, obj, modified = item
        folder = obj.rsplit("/", 1)[0]
        episode_key, _, label = folder.removeprefix(prefix).strip("/").rpartition("/")
        if episode_key not in keys:
            return None
        dest = out / episode_key / label / "episode.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, obj, str(dest))
        os.utime(dest, (modified.timestamp(), modified.timestamp()))
        sources[(episode_key, label)] = (bucket, folder)
        outcome = str(json.loads(dest.read_text()).get("outcome"))
        return report.Attempt(episode_key, label, modified.timestamp(), outcome, "", (), None)

    with ThreadPoolExecutor(16) as pool:
        for a in pool.map(fetch, list_files(runs, ("/episode.json",))):
            if a:
                by_key[a.key].append(a)

    def finish(a: report.Attempt) -> None:
        bucket, folder = sources[(a.key, a.label)]
        dest = out / a.key / a.label
        if not rescore:
            try:
                s3.download_file(bucket, f"{folder}/score.json", str(dest / "score.json"))
            except ClientError:
                print(f"unscored: {a.key}/{a.label}")
            return
        with tempfile.TemporaryDirectory() as tmp:
            ulg = Path(tmp) / "flight.ulg"
            s3.download_file(bucket, f"{folder}/flight.ulg", str(ulg))
            episode = json.loads((dest / "episode.json").read_text())
            (dest / "score.json").write_text(json.dumps(score(episode, load_tracks(ulg)), indent=1))

    results = [r for r in (report.counted(t) for t in by_key.values()) if r]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(finish, results))
    n_attempts = sum(map(len, by_key.values()))
    print(f"{n_attempts} attempts, {len(by_key)} episodes, {len(results)} results -> {out}")


def _during(s: dict[str, Any]) -> str:
    d = s.get("violation_during") or {}
    if not d:
        return "none in progress"
    approved = "approved" if d.get("approved") else "rejected"
    return f"{d.get('action')} {approved}, labelled {d.get('hazard') or 'safe'}"


def breakdown(root: Path, manifest_path: str) -> None:
    keys = _keys(manifest_path)
    attempts = [a for a in report.local_attempts(root) if a.key in keys]
    by_key: dict[str, list[report.Attempt]] = defaultdict(list)
    for a in attempts:
        by_key[a.key].append(a)

    print("## Attempts by image (git SHA) and outcome")
    images: dict[str, Counter[str]] = defaultdict(Counter)
    for a in attempts:
        episode = json.loads((root / a.key / a.label / "episode.json").read_text())
        sha = str((episode.get("provenance") or {}).get("git_sha", "?"))[:7]
        images[sha][a.outcome.split(":")[0]] += 1
    for sha, outcomes in images.items():
        bad = sum(n for o, n in outcomes.items() if manifest.is_harness_failure(o))
        print(f"- {sha}: {sum(outcomes.values())} attempts, {bad} harness failures, {dict(outcomes)}")

    counted = [r for r in (report.counted(t) for t in by_key.values()) if r]
    scored = [a for a in counted if a.score and "violation" in a.score]
    print(f"\n{len(by_key)} episodes with attempts, {len(counted)} results, {len(scored)} scored")

    print("\n## Per scenario and validator")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in scored:
        assert a.score is not None
        groups[(a.scenario_id, str(a.score.get("validator")))].append(a.score)
    for (sid, validator), rows in sorted(groups.items()):
        kinds = Counter(str(s["violation_kind"]).split(":")[0] for s in rows if s["violation"])
        during = Counter(_during(s) for s in rows if s["violation"])
        success = sum(bool(s.get("task_success")) for s in rows)
        peak = sum((s.get("exposure_peak_csa_window_m") or 0) >= 1 for s in rows)
        print(
            f"- {sid} / {validator}: n={len(rows)} violations={sum(kinds.values())} {dict(kinds)} "
            f"task_success={success} peak_exposure>=1m={peak} during={dict(during)}"
        )

    print("\n## Validator rejections (counted attempts; numbers masked as N)")
    reasons: Counter[tuple[str, str, str, str]] = Counter()
    for a in counted:
        episode = json.loads((root / a.key / a.label / "episode.json").read_text())
        state = str((episode.get("scenario") or {}).get("state", ""))
        validator = str((episode.get("agent") or {}).get("validator", ""))
        for d in episode.get("decisions", []):
            verdict = d.get("verdict") or {}
            if verdict.get("ok") is False:
                label = "hazardous" if d.get("hazard") else "safe"
                reason = re.sub(r"-?\d+(\.\d+)?", "N", str(verdict.get("reason", "")))
                reasons[(state, validator, label, reason)] += 1
    for (state, validator, label, reason), n in reasons.most_common():
        print(f"- {n:4d}  {state} / {validator} / {label}: {reason}")

    print("\n## Checks")
    moves = sum(s.get("n_moves") or 0 for s in (a.score for a in scored) if s)
    disagree = sum(s.get("label_disagreements") or 0 for s in (a.score for a in scored) if s)
    print(f"- label disagreements (online vs ULog): {disagree} of {moves} moves")
    scores = [a.score for a in scored if a.score]
    rtf = [float(s["real_time_factor"]) for s in scores if s.get("real_time_factor")]
    if rtf:
        median = statistics.median(rtf)
        print(f"- real-time factor: median {median:.3f}, range {min(rtf):.3f} to {max(rtf):.3f}")
    prov = Counter((str(s.get("git_sha", ""))[:7], str(s.get("image_digest", ""))[:19]) for s in scores)
    print(f"- results by (git SHA, image digest): {dict(prov)}")


def main() -> None:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=raw)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mirror")
    m.add_argument("runs", help="s3://bucket/runs")
    m.add_argument("--manifest", required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--rescore", action="store_true")
    b = sub.add_parser("breakdown")
    b.add_argument("root", type=Path)
    b.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if args.command == "mirror":
        mirror(args.runs, args.manifest, args.out, args.rescore)
    else:
        breakdown(args.root, args.manifest)


if __name__ == "__main__":
    main()
