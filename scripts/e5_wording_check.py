"""E5's wording check (freeze-v3 §5.2, rule A-1): the interpretation probe and the decision.

Nothing here calls a model unless `probe ... --live` is given. The student's dry run is
`lip dryrun qwen3-4b-student --live`; this script reads its output.

    uv run python scripts/e5_wording_check.py estimate                      # call counts and rough $
    uv run python scripts/e5_wording_check.py prompts --out runs/wording-check/prompts.json
    uv run python scripts/e5_wording_check.py probe claude-sonnet-4-6 \\
        --out runs/wording-check/probe-claude.json --live
    uv run python scripts/e5_wording_check.py decide --dryrun runs/wording-check/dryrun.json \\
        --probe runs/wording-check/probe-claude.json [--probe runs/wording-check/probe-gpt.json] \\
        --models claude-sonnet-4-6 [gpt-5.6-sol] [claude-sonnet-5-5] --out runs/wording-check/a1.json

`--models` names every model whose probe counts for A-1: Claude Sonnet 4.6, and GPT-5.6 Sol and
Claude Sonnet 5.5 each only if its smoke call passed (name the accepted entry, e.g. `-fb1`). Each
needs exactly one `--probe` file, and no other probe file is accepted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lost_in_place import dryrun, wording
from lost_in_place import families as fam

STUDENT = "qwen3-4b-student"


def _prompts(scenarios: Path) -> list[wording.ProbePrompt]:
    return wording.probe_prompts({t: dryrun.wording_families(scenarios, t) for t in fam.TEMPLATES})


def cmd_estimate(args: argparse.Namespace) -> int:
    prompts = _prompts(args.scenarios)
    student = wording.dryrun_cost(len(fam.TEMPLATES) * (len(dryrun.RETIRED) + len(fam.E5_CANARIES)))
    probes = [wording.probe_cost(m, prompts) for m in args.models]
    total = [student["usd"][0], student["usd"][1]]
    for p in probes:
        total = [x + (p["usd"] or 0.0) for x in total]
    print(
        json.dumps({"dryrun": student, "probes": probes, "total_usd": [round(x, 2) for x in total]}, indent=1)
    )
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    prompts = _prompts(args.scenarios)
    rows = [{**p.__dict__, "sha256": p.sha256} for p in prompts]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(rows)} probe prompts -> {args.out}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    prompts = _prompts(args.scenarios)
    cost = wording.probe_cost(args.model, prompts)
    print(f"probe: {args.model}, {cost['calls']} calls (no tools), about ${cost['usd']}")
    if not args.live:
        print("plan only: add --live to call the model (it costs money)")
        return 0
    from lost_in_place.llm.clients import make_client
    from lost_in_place.llm.registry import get_model

    spec = get_model(args.model)
    replies = wording.run_probe(prompts, make_client(spec), jobs=args.jobs)
    summary = wording.summarise_probe(replies)
    doc = {
        "model": args.model,
        "model_id": spec.model_id,
        "temperature": spec.temperature,
        "reps": wording.PROBE_REPS,
        "question": wording.PROBE_QUESTION,
        "summary": summary,
        "replies": wording.replies_to_json(replies),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(summary))
    return 1 if any(row["error"] for row in summary.values()) else 0


def decide(
    dryrun_doc: dict[str, Any],
    probe_docs: list[dict[str, Any]],
    models: list[str],
    prompts: list[wording.ProbePrompt],
) -> dict[str, Any]:
    """A-1 from the dry run and probe outputs, after checking they cover every family, template,
    prompt version and repeat, and that every probe reply answers one of `prompts` as it is now."""
    if dryrun_doc["model"] != STUDENT:
        raise wording.IncompleteInput(f"the dry run is {dryrun_doc['model']}, not {STUDENT}")
    settings = {k: dryrun_doc[k] for k in ("prompt", "observation", "driver", "validator", "latency_s")}
    if settings != {
        "prompt": "dexi-v2",
        "observation": "O0",
        "driver": "dexi",
        "validator": "none",
        "latency_s": 0,
    }:
        raise wording.IncompleteInput(f"the dry run's settings aren't §5.2's: {settings}")
    by_model = {d["model"]: d for d in probe_docs}
    if len(by_model) != len(probe_docs) or sorted(by_model) != sorted(models):
        raise wording.IncompleteInput(
            f"probe files are for {sorted(by_model)}, --models says {sorted(models)}"
        )
    names = sorted({(p.template, p.family) for p in prompts})
    flown = sorted((e["template"], e["family"]) for e in dryrun_doc["episodes"])
    if flown != names:
        raise wording.IncompleteInput("the dry run doesn't fly each wording-check family once per template")
    want = sorted(
        (p.template, p.family, p.prompt_version, r, p.sha256)
        for p in prompts
        for r in range(wording.PROBE_REPS)
    )
    replies = {m: wording.replies_from_json(d["replies"]) for m, d in by_model.items()}
    for model, rows in replies.items():
        got = sorted((r.template, r.family, r.prompt_version, r.rep, r.prompt_sha256) for r in rows)
        if got != want:
            raise wording.IncompleteInput(
                f"probe {model} doesn't answer each current probe prompt {wording.PROBE_REPS} times"
            )
    probes = {m: wording.summarise_probe(rows) for m, rows in replies.items()}
    student = dryrun.summarise([_run(e) for e in dryrun_doc["episodes"]])
    return wording.a1(student, probes, families_per_template=len(names) // len(fam.TEMPLATES))


def _run(row: dict[str, Any]) -> dryrun.DryRun:
    run = dryrun.DryRun(row["family"], row["template"], row["end"], row["success"])
    run.harness_error = row["harness_error"]
    run.decisions = [dryrun.Flown(**d) for d in row["decisions"]]
    return run


def cmd_decide(args: argparse.Namespace) -> int:
    doc = json.loads(args.dryrun.read_text())
    probes = [json.loads(p.read_text()) for p in args.probe]
    try:
        result = decide(doc, probes, args.models, _prompts(args.scenarios))
    except wording.IncompleteInput as exc:
        print(f"A-1 can't be decided: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="e5_wording_check", description=__doc__.split("\n\n")[0])
    p.add_argument("--scenarios", type=Path, default=Path("scenarios"))
    sub = p.add_subparsers(dest="command", required=True)
    est = sub.add_parser("estimate", help="call counts and rough cost of the whole wording check")
    est.add_argument("--models", nargs="+", default=["claude-sonnet-4-6", "gpt-5.6-sol", "claude-sonnet-5-5"])
    est.set_defaults(func=cmd_estimate)
    pr = sub.add_parser("prompts", help="write every probe prompt with its hash; no calls")
    pr.add_argument("--out", type=Path, required=True)
    pr.set_defaults(func=cmd_prompts)
    probe = sub.add_parser("probe", help="send the probe to one model (plan only without --live)")
    probe.add_argument("model", help="a key in llm/models.yaml, e.g. claude-sonnet-4-6 or gpt-5.6-sol")
    probe.add_argument("--out", type=Path, required=True)
    probe.add_argument("--jobs", type=int, default=4)
    probe.add_argument("--live", action="store_true", help="call the model; it costs money")
    probe.set_defaults(func=cmd_probe)
    dec = sub.add_parser("decide", help="A-1's inputs and result, from the dry run and the probes")
    dec.add_argument("--dryrun", type=Path, required=True)
    dec.add_argument("--probe", type=Path, action="append", default=[], required=True)
    dec.add_argument("--models", nargs="+", required=True, help="the models whose probe counts for A-1")
    dec.add_argument("--out", type=Path)
    dec.set_defaults(func=cmd_decide)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
