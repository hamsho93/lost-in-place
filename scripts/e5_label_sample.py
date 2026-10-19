"""`lip label-sample --double-code gpt-5.6-sol` over the whole E5 wave (SPEC-v3 §6, scorer agreement).

Each E5 sweep has its own runs prefix (`<runs>/<sweep>`) and `lip label-sample` takes one manifest, but
the second labeller's matched draw needs every LLM arm in one sample. This reads the attempts of each
listed manifest from its own prefix, keeps the manifest's entries, and then calls the same
`labels.pick`, `labels.sample` and `labels.double_code` on their union, loading each packet from the
prefix its attempt came from. Episode keys name the planner and model, so they are unique across the
sweeps; a collision stops the script. Prints only counts.

    uv run --extra aws --extra plot python scripts/e5_label_sample.py s3://<bucket>/runs \\
        --manifest s3://<bucket>/manifests/e5-scripted.json ... --out <dir> --out-2 <dir-2>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from lost_in_place import labels, manifest, report
from lost_in_place.llm.registry import load_models


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("runs", help="s3://<bucket>/runs")
    parser.add_argument("--manifest", action="append", required=True, help="one per E5 sweep")
    parser.add_argument("--fraction", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--double-code", default=labels.DOUBLE_CODED_ARM)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-2", type=Path, required=True)
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args(argv)

    attempts: list[report.Attempt] = []
    keys: set[str] = set()
    root_of: dict[str, str] = {}
    for uri in args.manifest:
        document = manifest.read_document(uri)
        entries = {manifest.Entry(**e).key for e in document["entries"]}
        if entries & keys:
            raise SystemExit(f"{uri}: episode keys shared with an earlier manifest")
        keys |= entries
        root = manifest.runs_root(args.runs, document)
        for a in report.s3_attempts(root):
            if a.key in entries:
                root_of[a.key] = root
                attempts.append(a)
        print(f"{document['name']}: {len(entries)} entries", file=sys.stderr)

    loaders = {root: labels.loader(root) for root in set(root_of.values())}

    def load(a: report.Attempt) -> tuple[dict[str, Any], Any]:
        return loaders[root_of[a.key]](a)

    picked = labels.pick(attempts, args.fraction, args.seed, keys)
    if not picked:
        print("no scored episodes to sample", file=sys.stderr)
        return 1
    own, matched = labels.double_code_pick(picked, args.double_code, load_models())
    if not own:
        print(f"the sample has no packets of arm {args.double_code!r}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    labels.sample(picked, load, args.out, args.seed, with_plots=not args.no_plots)
    args.out_2.mkdir(parents=True, exist_ok=True)
    doc = labels.double_code(
        picked, args.double_code, load, args.out_2, args.seed, load_models(), with_plots=not args.no_plots
    )
    print(
        f"{len(picked)} episodes in {args.out}/{labels.BLIND}/ for {labels.LABELLER_MODEL} (BRIEF.md);"
        f" {doc['episodes']} in {args.out_2}/{labels.BLIND_2}/ for {labels.SECOND_LABELLER_MODEL}"
        f" ({len(own)} of {args.double_code}'s, {len(matched)} matched; BRIEF-2.md). Keep key.json and"
        " key-2.json away from both labellers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
