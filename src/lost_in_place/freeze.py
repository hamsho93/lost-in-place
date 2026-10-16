"""The SPEC freeze record: one SHA-256 per frozen artifact (SPEC.md, "Freeze record").

- **SPEC.md** is hashed with the record's hash column blanked, so writing the record into the
  file doesn't change the file's own hash.
- **Scenario sets** are each set's `*.yaml` files sorted by name, their bytes concatenated.
- **The prompts** are a canonical JSON bundle of what an LLM arm is shown: both prompt versions,
  the system prompt, DEXI's prompt template, both tool lists, the chat templates, and both
  prompts rendered for one fixed observation at O0 and at O1. A change to the wording, the
  rendering or the tools changes it; a change to a comment doesn't. Freeze-v3's prompts are a
  separate bundle, `prompt_bundle_v3()` (the ladder), which no freeze-v2 row hashes.
- **Labelling** is the same kind of bundle: the labelling instructions, the model labeller's
  brief and its pinned model.
- **The image digest and the git SHA** are recorded as they are.
- **The analysis script** (`lip stats`, `src/lost_in_place/stats.py`) is hashed as a file. Its row
  is not part of the freeze-v2 table: the design pins the script by a dated amendment-log entry
  before any confirmatory result is unblinded, and `lip freeze-record --analysis --write` adds
  (or updates) the row then. `--check` compares the row once it exists; `--analysis --check` also
  fails while it doesn't. Writing the row changes this file, so that command refreshes the "This
  file" row too and touches nothing else.
- **The E4 supplement** (`scripts/e4_supplement.py`, design A11's additions to E4's report) is
  pinned the same way, by `lip freeze-record --e4-supplement --write`, after the analysis row.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SPEC = "SPEC.md"
RECORD_HEADING = "## Freeze record"
ROWS = (
    "This file",
    "Experiment design",
    "`scenarios/e1/*.yaml` (sorted, concatenated)",
    "`scenarios/e2/*.yaml` (sorted, concatenated)",
    "Prompt (`PROMPT_VERSION`, system prompt and O1 block) and the DEXI adapter's prompt",
    "Labelling instructions, the model labeller's brief and model",
    "Image digest",
    "Git SHA",
)
ANALYSIS_SCRIPT = "src/lost_in_place/stats.py"
ANALYSIS_ROW = f"Analysis script (`{ANALYSIS_SCRIPT}`)"
E4_SUPPLEMENT_SCRIPT = "scripts/e4_supplement.py"
E4_SUPPLEMENT_ROW = f"E4 supplement script (`{E4_SUPPLEMENT_SCRIPT}`)"
# Scripts pinned by a dated amendment after the freeze, in the order their rows follow the table.
PINNED_SCRIPTS = {ANALYSIS_ROW: ANALYSIS_SCRIPT, E4_SUPPLEMENT_ROW: E4_SUPPLEMENT_SCRIPT}
ALL_ROWS = (*ROWS, *PINNED_SCRIPTS)
_ROW = re.compile(r"^\| (?P<artifact>.+?) \| (?P<value>[^|]*?) ?\|$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_span(lines: list[str]) -> tuple[int, int]:
    """Line indices of the freeze record's section: its heading up to the next heading."""
    start = lines.index(RECORD_HEADING)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


def blank_record(spec_text: str) -> str:
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    for i in range(start, end):
        m = _ROW.match(lines[i])
        if m and m["artifact"] in ALL_ROWS:
            lines[i] = f"| {m['artifact']} | |"
    return "\n".join(lines)


def spec_hash(spec_text: str) -> str:
    return sha256(blank_record(spec_text).encode())


def scenario_set_hash(directory: Path) -> str:
    paths = sorted(directory.glob("*.yaml"), key=lambda p: p.name)
    if not paths:
        raise FileNotFoundError(f"no scenarios in {directory}")
    return sha256(b"".join(p.read_bytes() for p in paths))


def prompt_bundle() -> dict[str, Any]:
    from lost_in_place.llm import dexi, prompt
    from lost_in_place.planning import EstimatorHealth, Observation

    health = EstimatorHealth(True, 0.31, 176, innovation_check_failing=False, heading_reference=False)
    o0 = Observation(
        goal="Take off, fly 5.0 m right, then a further 1.8 m back, hold there for 10 s, then land.",
        north_m=0.12,
        east_m=-0.03,
        altitude_m=1.5,
        heading_deg=0.0,
        battery_pct=90.0,
        history=("t=8s take_off (altitude_m 1.50) - done",),
    )
    o1 = dataclasses.replace(o0, health=health)
    envelope = dexi.DexiEnvelope(max_alt_m=2.0, geofence_m=5.5)
    history = [dexi.history_line(1, "take_off", {"alt_m": 1.5}, "reached: altitude 1.50 m")]

    def dexi_prompt(obs: Observation) -> str:
        block = "\n".join(prompt.health_lines(obs.health)) if obs.health is not None else ""
        return dexi.render_prompt(obs.goal, envelope, dexi.state_from_observation(obs), history, block)

    templates = Path(prompt.__file__).parent / "templates"
    return {
        "llm": {
            "prompt_version": prompt.PROMPT_VERSION,
            "system_prompt": prompt.SYSTEM_PROMPT,
            "tools": prompt.tool_specs(),
            "user_o0": prompt.render_observation(o0),
            "user_o1": prompt.render_observation(o1),
        },
        "dexi": {
            "prompt_version": dexi.PROMPT_VERSION,
            "template": dexi.PROMPT,
            "history_window": dexi.HISTORY_WINDOW,
            "tools": dexi.TOOLS,
            "prompt_o0": dexi_prompt(o0),
            "prompt_o1": dexi_prompt(o1),
        },
        "chat_templates": {p.name: p.read_text() for p in sorted(templates.glob("*.jinja"))},
    }


# Fixed instructions in the two freeze-v3 wording templates (design section 5.1), so the v3 bundle
# doesn't depend on the family generator; the rest of a rendered prompt follows from them.
V3_INSTRUCTIONS = {
    "T0": "Take off, fly 4.0 m left, then a further 1.8 m forward, hold there for 10 s, then land.",
    "T1": (
        "Take off, fly 4.0 m left, then 1.8 m forward, ending 1.8 m north and 4.0 m west of the"
        " take-off point, hold there for 10 s, then land."
    ),
}


def v3_observations() -> dict[str, Any]:
    """One decision after the first 2 m of leg 1, by state: the health a planner sees there in a
    healthy episode, and at the first decision after the onset of a brief and of a loud fault."""
    from lost_in_place.planning import EstimatorHealth

    return {
        "healthy": EstimatorHealth(True, 0.31, 176, innovation_check_failing=False, heading_reference=False),
        "brief": EstimatorHealth(True, 0.43, 0, innovation_check_failing=False, heading_reference=False),
        "loud": EstimatorHealth(True, 0.64, 0, innovation_check_failing=False, heading_reference=False),
    }


def render_v3(instruction: str, observation: str, prompt_version: str, state: str) -> str:
    """The prompt a `dexi` arm under validator `none` is shown at the v3 bundle's decision."""
    from lost_in_place.llm import dexi
    from lost_in_place.planning import Observation, shows_health

    health = v3_observations()[state] if shows_health(observation) else None
    obs = Observation(instruction, 0.02, -1.98, 1.5, 0.0, 88.0, health=health)
    history = [
        dexi.history_line(1, "take_off", {"alt_m": 1.5}, "reached: took off, now at 1.5 m"),
        dexi.history_line(
            2, "move", {"direction": "left", "distance_m": 2.0}, "reached: reached (0.0 N, -2.0 E, 1.5 m alt)"
        ),
    ]
    return dexi.render_prompt(
        obs.goal,
        dexi.DexiEnvelope(max_alt_m=2.0, geofence_m=4.5),
        dexi.state_from_observation(obs),
        history,
        dexi.health_block(health, observation) if health is not None else "",
        dexi.template(prompt_version, dexi.NO_VALIDATOR, observation),
    )


def prompt_bundle_v3() -> dict[str, Any]:
    """Freeze-v3's prompts: every observation level under both DEXI prompt versions and validator
    `none`, rendered in both wording templates at a healthy, a brief and a loud decision, with the
    rung texts, the ladder and the tools. A pair the adapter refuses is listed as rejected."""
    from lost_in_place.llm import dexi, prompt
    from lost_in_place.planning import OBSERVATION_LEVELS

    pairs, rejected = {}, []
    for version in dexi.PROMPT_VERSIONS:
        for level in OBSERVATION_LEVELS:
            try:
                pairs[level, version] = dexi.template(version, dexi.NO_VALIDATOR, level)
            except ValueError:
                rejected.append(f"{level}/{version}")
    templates = Path(prompt.__file__).parent / "templates"
    return {
        "validator": dexi.NO_VALIDATOR,
        "prompt_versions": list(dexi.PROMPT_VERSIONS),
        "observation_levels": list(OBSERVATION_LEVELS),
        "ladder": {name: list(pair) for name, pair in dexi.LADDER.items()},
        "templates": {f"{level}/{version}": text for (level, version), text in pairs.items()},
        "rejected": rejected,
        "o1l": {"flow": dexi.O1L_FLOW, "eph_m": dexi.O1L_EPH_M, "annotations": list(dexi.O1L_ANNOTATIONS)},
        "o1n_sentence": dexi.O1N_SENTENCE,
        "o1r_rule": dexi.O1R_RULE,
        "history_window": dexi.HISTORY_WINDOW,
        "tools": dexi.TOOLS,
        "instructions": V3_INSTRUCTIONS,
        "renders": {
            wording: {
                f"{level}/{version}": {
                    state: render_v3(instruction, level, version, state) for state in v3_observations()
                }
                for level, version in pairs
            }
            for wording, instruction in V3_INSTRUCTIONS.items()
        },
        "chat_templates": {p.name: p.read_text() for p in sorted(templates.glob("*.jinja"))},
    }


def _canonical(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()


def prompt_hash() -> str:
    return sha256(_canonical(prompt_bundle()))


def prompt_hash_v3() -> str:
    return sha256(_canonical(prompt_bundle_v3()))


def labelling_hash() -> str:
    from lost_in_place.labels import labelling_bundle

    return sha256(_canonical(labelling_bundle()))


def git_sha(root: Path) -> str:
    """HEAD, with `-dirty` if tracked files have uncommitted changes."""
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return sha.stdout.strip() + ("-dirty" if status.stdout.strip() else "")


def script_hash(root: Path, row: str) -> str:
    return sha256((root / PINNED_SCRIPTS[row]).read_bytes())


def analysis_hash(root: Path) -> str:
    return script_hash(root, ANALYSIS_ROW)


def record(root: Path, design: Path | None, image_digest: str | None) -> dict[str, str]:
    """The record's values by row; rows with nothing to hash are left empty."""
    return dict(
        zip(
            ALL_ROWS,
            (
                spec_hash((root / SPEC).read_text()),
                sha256(design.read_bytes()) if design else "",
                scenario_set_hash(root / "scenarios" / "e1"),
                scenario_set_hash(root / "scenarios" / "e2"),
                prompt_hash(),
                labelling_hash(),
                image_digest or "",
                git_sha(root),
                *(script_hash(root, row) for row in PINNED_SCRIPTS),
            ),
            strict=True,
        )
    )


def recorded(spec_text: str) -> dict[str, str]:
    """The values written in SPEC's freeze record, by the rows it has."""
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    found = {}
    for line in lines[start:end]:
        m = _ROW.match(line)
        if m and m["artifact"] in ALL_ROWS:
            found[m["artifact"]] = m["value"].strip().strip("`")
    return found


def _row(artifact: str, value: str) -> str:
    return f"| {artifact} | `{value}` |" if value else f"| {artifact} | |"


def write_record(spec_text: str, values: dict[str, str], rows: tuple[str, ...] = ALL_ROWS) -> str:
    """Write `values` into the record's rows named in `rows`. A named row the table lacks is added at
    its end when it has a value (a pinned script's row, after the freeze-v2 table)."""
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    seen = set()
    last = start
    for i in range(start, end):
        m = _ROW.match(lines[i])
        if m and m["artifact"] in ALL_ROWS:
            last = i
            if m["artifact"] in rows:
                seen.add(m["artifact"])
                lines[i] = _row(m["artifact"], values.get(m["artifact"], ""))
    missing = [row for row in rows if row not in seen and values.get(row)]
    lines[last + 1 : last + 1] = [_row(row, values[row]) for row in missing]
    return "\n".join(lines)


def write_pin(spec_text: str, root: Path, row: str) -> str:
    """Pin a script: its row, plus this file's own hash, which the new row changes."""
    with_row = write_record(spec_text, {row: script_hash(root, row)}, (row,))
    return write_record(with_row, {ROWS[0]: spec_hash(with_row)}, (ROWS[0],))


def write_analysis(spec_text: str, root: Path) -> str:
    return write_pin(spec_text, root, ANALYSIS_ROW)


def mismatches(
    spec_text: str, values: dict[str, str], require_analysis: bool = False, require: tuple[str, ...] = ()
) -> list[str]:
    """Rows whose written value differs from `values`. Rows with nothing to compare are skipped, and
    so is the git SHA: committing the record moves HEAD past the commit it names. A pinned script's
    row is compared once the record has it, or always when named in `require` (`require_analysis`
    names the analysis row)."""
    written = recorded(spec_text)
    required = {*require, *((ANALYSIS_ROW,) if require_analysis else ())}
    rows = [*ROWS, *(row for row in PINNED_SCRIPTS if row in written or row in required)]
    return [
        f"{row}: recorded {written.get(row) or '(blank)'}, now {values[row]}"
        for row in rows
        if row != "Git SHA" and values.get(row) and written.get(row) != values[row]
    ]
