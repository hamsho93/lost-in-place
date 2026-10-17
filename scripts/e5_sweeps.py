"""The E5 sweeps (freeze-v3 design, sections 5 and 6) and the E5 canary sweeps (section 11, item 4).

Every arm is its own sweep: one YAML, one manifest and one runs prefix per arm, so the submitter tops
each arm up on its own. A ladder level is the (observation level, prompt version) pair of
`dexi.LADDER`; a sweep is the union of one cell per (state, prompt version), since the design isn't a
full cross product. Every arm runs `driver=dexi` under validator `none` with a 2.0 s latency and one
episode per family per cell.

Wave sweeps, for the template, N and arm set fixed by the adaptation rules (A-1, A-2, A-3, A-7):

| State | Families | Levels per LLM arm |
|---|---|---|
| loud | c000 to c(N-1) | all 7 |
| brief | c000 to c(N-1) | O0, O1, O1-L, O1-N, O1-R, O1-V |
| healthy | c000-c059 | as brief |
| flowscale, misaligned | c000-c029 each | O1, O1-N |

The scripted control (`dexi/rule-2m`, `dexi/rule-leg`) flies section 6's cells on all N families:
loud O1 and O0, timed loud O1 (`timed/`), brief O1, healthy O1 and flowscale O1.

Canary sweeps, committed for both templates before the pre tag: each LLM entry on k000-k005 at the 7
loud and 6 brief levels (78 episodes), one file per GPT-5.6 Sol and Claude Sonnet 5.5 entry so
whichever the smoke call accepts is already there (never the smoke-only `claude-haiku-5-5`); and the
scripted canary, the G-S cell (loud O1) at both cadences plus `scripted-cautious` at loud O1 (18
episodes).

    uv run python scripts/e5_sweeps.py wave --template t1 --n 120 [--arms ...] [--gpt gpt-5.6-sol-fb1] \\
        [--sonnet55 claude-sonnet-5-5]
    uv run python scripts/e5_sweeps.py canary [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from lost_in_place import manifest
from lost_in_place.llm.dexi import DEXI_DRIVER, LADDER, NO_VALIDATOR, PROMPT_V2

ROOT = Path(__file__).resolve().parents[1]
WAVE_OUT = Path("sweeps/e5")
CANARY_OUT = Path("sweeps/e5-canary")
TEMPLATES = ("t0", "t1")
NS = (120, 160)
SHUFFLE_SEED = 0
LATENCY_S = 2.0
HEALTHY_FAMILIES = 60
SILENT_FAMILIES = 30
CANARY_FAMILIES = 6
LLM_MODELS = ("qwen3-4b-student", "qwen3-4b-zero-shot", "claude-sonnet-4-6")
GPT_ARM = "gpt-5.6-sol"
GPT_ENTRIES = (GPT_ARM, f"{GPT_ARM}-fb1")
SONNET55_ARM = "claude-sonnet-5-5"
SONNET55_ENTRIES = (SONNET55_ARM,)
# The arms whose registry entry is the one their smoke call accepted (sections 9.2 and 9.3).
SMOKED = {GPT_ARM: GPT_ENTRIES, SONNET55_ARM: SONNET55_ENTRIES}
SCRIPTED_ARM = "scripted"
SCRIPTED_MODELS = ("rule-2m", "rule-leg")
CAUTIOUS = "scripted-cautious"
ARMS = (*LLM_MODELS, GPT_ARM, SONNET55_ARM, SCRIPTED_ARM)

SIX = ("O0", "O1", "O1-L", "O1-N", "O1-R", "O1-V")
LEVELS: dict[str, tuple[str, ...]] = {
    "loud": tuple(LADDER),
    "brief": SIX,
    "healthy": SIX,
    "flowscale": ("O1", "O1-N"),
    "misaligned": ("O1", "O1-N"),
}
CANARY_STATES = ("loud", "brief")
# Section 6: (directory, state, observation level) per scripted cell.
SCRIPTED_CELLS = (
    ("families", "loud", "O1"),
    ("families", "loud", "O0"),
    ("timed", "loud_timed", "O1"),
    ("families", "brief", "O1"),
    ("families", "healthy", "O1"),
    ("families", "flowscale", "O1"),
)
# Section 10.1: episodes per LLM arm and for the scripted control, by N.
EXPECTED = {120: {"llm": 2040, "scripted": 1440}, 160: {"llm": 2560, "scripted": 1920}}
CANARY_EXPECTED = {"llm": 78, "scripted": 18}


def family_globs(prefix: str, n: int) -> list[str]:
    """Glob patterns matching exactly the 3-digit names `<prefix>000` to `<prefix><n-1>`."""
    if not 0 < n <= 1000:
        raise ValueError(f"{n} families: names have 3 digits")
    out = [f"{prefix}{h}??" for h in range(n // 100)]
    h, rest = divmod(n, 100)
    tens, units = divmod(rest, 10)
    if tens:
        out.append(f"{prefix}{h}[0-{tens - 1}]?")
    if units:
        out.append(f"{prefix}{h}{tens}[0-{units - 1}]")
    return out


def family_counts(n: int) -> dict[str, int]:
    return {
        "loud": n,
        "brief": n,
        "healthy": HEALTHY_FAMILIES,
        "flowscale": SILENT_FAMILIES,
        "misaligned": SILENT_FAMILIES,
    }


def _scenarios(template: str, directory: str, prefix: str, state: str, count: int) -> list[str]:
    return [f"scenarios/e5/{template}/{directory}/{g}_{state}.yaml" for g in family_globs(prefix, count)]


def _planner(model: str, prompt: str = PROMPT_V2) -> dict[str, Any]:
    return {"arm": f"dexi/{model}", "options": {"driver": DEXI_DRIVER, "prompt": prompt}}


def ladder_cells(model: str, scenarios: dict[str, list[str]]) -> list[dict[str, Any]]:
    """One cell per (state, prompt version), with that state's levels at that version."""
    cells = []
    for state, paths in scenarios.items():
        by_prompt: dict[str, list[str]] = {}
        for level in LEVELS[state]:
            observation, prompt = LADDER[level]
            by_prompt.setdefault(prompt, []).append(observation)
        for prompt, observations in by_prompt.items():
            cells.append(
                {
                    "scenarios": list(paths),
                    "planners": [_planner(model, prompt)],
                    "observations": observations,
                }
            )
    return cells


def _sweep(name: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "reps": 1,
        "validators": [NO_VALIDATOR],
        "latency_s": LATENCY_S,
        "shuffle_seed": SHUFFLE_SEED,
        "cells": cells,
    }


def llm_sweep(model: str, template: str, n: int, arm: str | None = None) -> dict[str, Any]:
    scenarios = {
        state: _scenarios(template, "families", "c", state, count)
        for state, count in family_counts(n).items()
    }
    return _sweep(f"e5-{arm or model}", ladder_cells(model, scenarios))


def scripted_sweep(template: str, n: int) -> dict[str, Any]:
    cells = [
        {
            "scenarios": _scenarios(template, directory, "c", state, n),
            "planners": [_planner(m) for m in SCRIPTED_MODELS],
            "observations": [observation],
        }
        for directory, state, observation in SCRIPTED_CELLS
    ]
    return _sweep(f"e5-{SCRIPTED_ARM}", cells)


def canary_llm_sweep(model: str, template: str) -> dict[str, Any]:
    scenarios = {s: _scenarios(template, "canary", "k", s, CANARY_FAMILIES) for s in CANARY_STATES}
    return _sweep(f"e5-canary-{template}-{model}", ladder_cells(model, scenarios))


def canary_scripted_sweep(template: str) -> dict[str, Any]:
    cell = {
        "scenarios": _scenarios(template, "canary", "k", "loud", CANARY_FAMILIES),
        "planners": [*(_planner(m) for m in SCRIPTED_MODELS), CAUTIOUS],
        "observations": ["O1"],
    }
    return _sweep(f"e5-canary-{template}-{SCRIPTED_ARM}", [cell])


def _header(lines: list[str]) -> str:
    return "".join(f"# {line}\n" if line else "#\n" for line in lines)


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def render(data: dict[str, Any], header: list[str]) -> str:
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=None, width=110)
    return _header(header) + body


def count(data: dict[str, Any], root: Path = ROOT) -> int:
    return len(manifest.build(manifest.SweepSpec(data), root))


def wave(
    template: str,
    n: int,
    arms: tuple[str, ...],
    gpt: str = GPT_ARM,
    root: Path = ROOT,
    sonnet55: str = SONNET55_ARM,
) -> dict[str, str]:
    """`{file name: YAML text}` of the wave sweeps; raises unless each has section 10.1's count. `gpt`
    and `sonnet55` are the registry entries those arms' smoke calls accepted; the file and sweep keep
    the arm's name."""
    if template not in TEMPLATES or n not in NS:
        raise ValueError(f"template {template!r}, N {n}: use one of {TEMPLATES} and one of {NS}")
    entries = {GPT_ARM: gpt, SONNET55_ARM: sonnet55}
    for arm, entry in entries.items():
        if entry not in SMOKED[arm]:
            raise ValueError(f"{arm} entry {entry!r}: use one of {', '.join(SMOKED[arm])}")
    unknown = set(arms) - set(ARMS)
    if unknown or not arms:
        raise ValueError(f"arms {sorted(unknown)}: choose from {', '.join(ARMS)}")
    out = {}
    for arm in ARMS:
        if arm not in arms:
            continue
        if arm == SCRIPTED_ARM:
            data, kind = scripted_sweep(template, n), "scripted"
            what = "the scripted DEXI-cadence control (design section 6), both cadences"
        else:
            model = entries.get(arm, arm)
            data, kind = llm_sweep(model, template, n, arm), "llm"
            what = f"LLM arm dexi/{model} at every ladder level of its state (design section 5)"
        episodes = count(data, root)
        if episodes != EXPECTED[n][kind]:
            raise AssertionError(f"{arm}: {episodes} episodes, design section 10.1 says {EXPECTED[n][kind]}")
        header = [
            f"E5 wave (freeze-v3): {what}.",
            f"Template {template}, N = {n}: {episodes} episodes. Written by scripts/e5_sweeps.py.",
        ]
        out[f"{arm}.yaml"] = render(data, header)
    return out


def canary(root: Path = ROOT) -> dict[str, str]:
    """`{file name: YAML text}` of the canary sweeps for both templates."""
    out = {}
    for template in TEMPLATES:
        for model in (*LLM_MODELS, *GPT_ENTRIES, *SONNET55_ENTRIES):
            data = canary_llm_sweep(model, template)
            what = f"dexi/{model} on k000-k005 at the 7 loud and 6 brief levels"
            out[f"{template}-{model}.yaml"] = _checked(data, "llm", what, template, root)
        data = canary_scripted_sweep(template)
        what = "the G-S cell (loud O1) at both cadences, and scripted-cautious at loud O1, on k000-k005"
        out[f"{template}-{SCRIPTED_ARM}.yaml"] = _checked(data, "scripted", what, template, root)
    return out


def _checked(data: dict[str, Any], kind: str, what: str, template: str, root: Path) -> str:
    episodes = count(data, root)
    if episodes != CANARY_EXPECTED[kind]:
        raise AssertionError(f"{data['name']}: {episodes} episodes, expected {CANARY_EXPECTED[kind]}")
    header = [
        f"E5 canary (freeze-v3 design, section 11 item 4), template {template}: {what}.",
        f"{episodes} episodes; flown only under the template A-1 picks. Written by scripts/e5_sweeps.py.",
    ]
    return render(data, header)


def write(files: dict[str, str], out: Path, check: bool) -> int:
    """Write the files, or with `check` compare them with what is in `out`: 1 on any difference."""
    if check:
        present = {p.name for p in out.glob("*.yaml")} if out.is_dir() else set()
        stale = sorted(present - set(files))
        changed = sorted(
            n for n, text in files.items() if not (out / n).is_file() or (out / n).read_text() != text
        )
        for name in changed + stale:
            print(f"{out / name}: {'not generated' if name in stale else 'differs from the generator'}")
        return 1 if changed or stale else 0
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text)
        print(out / name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wave", help="the wave sweeps for one template, N and arm set (A-7)")
    w.add_argument("--template", required=True, choices=TEMPLATES)
    w.add_argument("--n", required=True, type=int, choices=NS)
    w.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    w.add_argument("--gpt", default=GPT_ARM, choices=GPT_ENTRIES, help="the entry GPT's smoke call accepted")
    w.add_argument(
        "--sonnet55",
        default=SONNET55_ARM,
        choices=SONNET55_ENTRIES,
        help="the entry Sonnet 5.5's smoke call accepted",
    )
    w.add_argument("--out", type=Path, default=WAVE_OUT)
    w.add_argument("--check", action="store_true", help="compare with --out instead of writing")
    c = sub.add_parser("canary", help="the canary sweeps for both templates")
    c.add_argument("--out", type=Path, default=CANARY_OUT)
    c.add_argument("--check", action="store_true", help="compare with --out instead of writing")
    args = parser.parse_args(argv)
    if args.cmd == "wave":
        files = wave(args.template, args.n, tuple(args.arms), args.gpt, sonnet55=args.sonnet55)
    else:
        files = canary()
    return write(files, args.out, args.check)


if __name__ == "__main__":
    sys.exit(main())
