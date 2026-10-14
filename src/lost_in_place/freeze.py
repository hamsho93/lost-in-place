"""The SPEC freeze record: one SHA-256 per frozen artifact (SPEC.md, "Freeze record").

- **SPEC.md** is hashed with the record's hash column blanked, so writing the record into the
  file doesn't change the file's own hash.
- **Scenario sets** are each set's `*.yaml` files sorted by name, their bytes concatenated.
- **The prompts** are a canonical JSON bundle of what an LLM arm is shown: both prompt versions,
  the system prompt, DEXI's prompt template, both tool lists, the chat templates, and both
  prompts rendered for one fixed observation at O0 and at O1. A change to the wording, the
  rendering or the tools changes it; a change to a comment doesn't.
- **Labelling** is the same kind of bundle: the labelling instructions, the model labeller's
  brief and its pinned model.
- **The image digest and the git SHA** are recorded as they are.
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
        if m and m["artifact"] in ROWS:
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


def _canonical(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()


def prompt_hash() -> str:
    return sha256(_canonical(prompt_bundle()))


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


def record(root: Path, design: Path | None, image_digest: str | None) -> dict[str, str]:
    """The record's values by row; rows with nothing to hash are left empty."""
    return dict(
        zip(
            ROWS,
            (
                spec_hash((root / SPEC).read_text()),
                sha256(design.read_bytes()) if design else "",
                scenario_set_hash(root / "scenarios" / "e1"),
                scenario_set_hash(root / "scenarios" / "e2"),
                prompt_hash(),
                labelling_hash(),
                image_digest or "",
                git_sha(root),
            ),
            strict=True,
        )
    )


def recorded(spec_text: str) -> dict[str, str]:
    """The values written in SPEC's freeze record."""
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    found = {}
    for line in lines[start:end]:
        m = _ROW.match(line)
        if m and m["artifact"] in ROWS:
            found[m["artifact"]] = m["value"].strip().strip("`")
    return found


def write_record(spec_text: str, values: dict[str, str]) -> str:
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    for i in range(start, end):
        m = _ROW.match(lines[i])
        if m and m["artifact"] in ROWS:
            value = values.get(m["artifact"], "")
            lines[i] = f"| {m['artifact']} | `{value}` |" if value else f"| {m['artifact']} | |"
    return "\n".join(lines)


def mismatches(spec_text: str, values: dict[str, str]) -> list[str]:
    """Rows whose written value differs from `values`. Rows with nothing to compare are skipped, and
    so is the git SHA: committing the record moves HEAD past the commit it names."""
    written = recorded(spec_text)
    return [
        f"{row}: recorded {written.get(row) or '(blank)'}, now {values[row]}"
        for row in ROWS
        if row != "Git SHA" and values.get(row) and written.get(row) != values[row]
    ]
