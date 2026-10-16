"""The in-wave comprehension probe (freeze-v3 design, section 3.3), run right after an arm settles.

For each loud-anchored family and each of the 7 loud ladder levels of the arm's wave sweep, the prompt
of the first planner decision at or after onset in its counted attempt is re-sent once to the arm's
model, with no tools and the frozen question (`lost_in_place.comprehension`): 840 calls per arm at
N = 120. A reply that isn't two yes/no lines is invalid.

The answers are blinded until the pinned analysis run (section 2.5): `run` writes them to its JSON-lines
file and prints only call counts, missing families and the invalid-reply rate. Only prompts, decision
times, the onset and attempt status are read from the episodes; no score file is opened.

    uv run --extra aws python scripts/e5_probe.py estimate s3://<bucket>/runs --sweep e5-claude-sonnet-4-6
    uv run --extra aws python scripts/e5_probe.py run s3://<bucket>/runs --sweep e5-claude-sonnet-4-6 \\
        --out runs/e5-probe/claude-sonnet-4-6.jsonl [--live] [--jobs 8]

Export `LIP_BEDROCK_PROFILES` (the job definition's map) first, so the spend carries the project tag.
Without `--live` every call is answered by a stub and no model is called. A rerun sends only the
calls missing from `--out`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from lost_in_place import replay
from lost_in_place.comprehension import parse_answers, probe_prompt
from lost_in_place.llm.clients import ChatClient, Completion, make_client
from lost_in_place.llm.dexi import LADDER, ladder_level
from lost_in_place.llm.registry import get_model

LOUD = "_loud"
LEVELS = tuple(LADDER)


def _loud(key: str) -> bool:
    return key.split("/", 1)[0].endswith(LOUD)


def collect(root: str, sweep: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The probe's calls for one arm's sweep, and what is missing: an episode with no counted attempt
    or no decision after onset."""
    episodes = replay.counted_episodes(replay.attempts(root, sweep, _loud))
    calls, models = [], set()
    missing: Counter[str] = Counter()
    for key, episode in sorted(episodes.items()):
        if episode is None:
            missing["no_counted_attempt"] += 1
            continue
        agent = episode.get("agent") or {}
        options = agent.get("planner_options") or {}
        level = ladder_level(str(agent.get("observation")), str(options.get("prompt", "")))
        if level is None:
            raise SystemExit(
                f"{key}: not a ladder level ({agent.get('observation')}, {options.get('prompt')})"
            )
        step = replay.first_post_onset(episode)
        if step is None:
            missing["no_decision_after_onset"] += 1
            continue
        models.add(str(options.get("model")))
        family = str(episode.get("scenario_id", key.split("/", 1)[0])).removesuffix(LOUD)
        calls.append(
            {
                "id": f"{family}/{level}",
                "key": key,
                "family": family,
                "level": level,
                "prompt": probe_prompt(step.prompt),
                "tools": False,
            }
        )
    if len(models) > 1:
        raise SystemExit(f"sweep {sweep} mixes models {sorted(models)}")
    ids = [c["id"] for c in calls]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"sweep {sweep}: two counted episodes for one family and level")
    per_level = Counter(c["level"] for c in calls)
    return calls, {
        "model": next(iter(models), None),
        "families": len({c["family"] for c in calls}),
        "calls": len(calls),
        "per_level": {lv: per_level.get(lv, 0) for lv in LEVELS},
        "missing": dict(missing),
    }


class StubClient:
    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        return Completion((), "1. yes\n2. no", "end_turn", 0, 0, 0, "stub")


def invalid_rate(out: Path) -> dict[str, Any]:
    """The probe's invalid replies, the only column of its file a blinded reader may see."""
    records = (
        [json.loads(line) for line in out.read_text().splitlines() if line.strip()] if out.exists() else []
    )
    invalid = sum(parse_answers(r.get("text") or "") is None for r in records)
    return {"replies": len(records), "invalid": invalid, "rate": invalid / len(records) if records else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("estimate", "run"):
        p = sub.add_parser(name)
        p.add_argument("runs_root", help="local mirror of runs/ or s3://bucket/runs (read only)")
        p.add_argument("--sweep", required=True, help="the arm's wave sweep name, e.g. e5-claude-sonnet-4-6")
        if name == "run":
            p.add_argument("--out", type=Path, required=True)
            p.add_argument("--jobs", type=int, default=8)
            p.add_argument("--live", action="store_true", help="call the arm's model (costs money)")
    args = parser.parse_args(argv)
    calls, summary = collect(args.runs_root, args.sweep)
    if args.cmd == "estimate" or not calls:
        print(json.dumps(summary, sort_keys=True))
        return 0
    model = str(summary["model"])
    client: ChatClient = make_client(get_model(model)) if args.live else StubClient()
    counts = replay.send(model, calls, args.out, client, args.jobs)
    print(
        json.dumps(
            {**summary, "live": args.live, **counts, "invalid": invalid_rate(args.out)}, sort_keys=True
        )
    )
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
