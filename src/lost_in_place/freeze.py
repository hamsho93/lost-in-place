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

Freeze-v3 (E5) has its own record, in `SPEC-v3.md` (`Freeze.V3`, `lip freeze-record --freeze v3`),
with the rows in `V3_ROWS`. It is written once, by the adaptation pull request, and every row is in
the table from the start: the analysis script is one of them, not a later pin. Besides hashes it
holds four adaptation outcomes, which are values: the wording template (A-1), N (A-2), the
confirmatory arms (A-3, A-4) and the wave-wide m (A-5, 4 times the arms). The scenario rows hash the
chosen template's directories. The probe-prompt row hashes the frozen texts that probes send
outside an arm's prompt (`PROBE_TEXTS`), apart from the v3 prompt bundle, so the bundle doesn't
depend on the probe modules. `tag_hashes()` is the hash list of the `freeze-v3-pre` tag message.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import importlib
import importlib.util
import json
import re
import subprocess
from collections.abc import Callable
from enum import StrEnum
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


def _blank(spec_text: str, rows: tuple[str, ...]) -> str:
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    for i in range(start, end):
        m = _ROW.match(lines[i])
        if m and m["artifact"] in rows:
            lines[i] = f"| {m['artifact']} | |"
    return "\n".join(lines)


def blank_record(spec_text: str) -> str:
    return _blank(spec_text, ALL_ROWS)


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


def _recorded(spec_text: str, rows: tuple[str, ...]) -> dict[str, str]:
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    found = {}
    for line in lines[start:end]:
        m = _ROW.match(line)
        if m and m["artifact"] in rows:
            found[m["artifact"]] = m["value"].strip().strip("`")
    return found


def recorded(spec_text: str) -> dict[str, str]:
    """The values written in SPEC's freeze record, by the rows it has."""
    return _recorded(spec_text, ALL_ROWS)


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


SPEC_V3 = "SPEC-v3.md"
DESIGN_V3 = "docs/prereg/freeze-v3-design.md"
E5_SCENARIOS = "scenarios/e5"
E5_TEMPLATES = ("t0", "t1")
E5_SETS = ("families", "canary", "timed")
E5_SWEEPS = "sweeps/e5"
FAMILY_COUNTS = (120, 160)
MODEL_REGISTRY = "src/lost_in_place/llm/models.yaml"
ANALYSIS_SCRIPT_V3 = "src/lost_in_place/stats_v3.py"
ADMISSION_SCRIPT = "scripts/e5_admission.py"
# Frozen text a probe sends besides an arm's prompt, by name: "<module>:<attribute>", or
# "<script path from the repo root>:<attribute>". Every such text must be listed before freeze-v3-pre.
PROBE_TEXTS = {"wording-check": "lost_in_place.wording:PROBE_QUESTION"}

DESIGN_ROW_V3 = f"Experiment design (`{DESIGN_V3}`)"
TEMPLATE_ROW = "Wording template (A-1)"
SCENARIO_ROWS_V3 = {s: f"`{E5_SCENARIOS}/<t>/{s}/*.yaml` (sorted, concatenated)" for s in E5_SETS}
PROMPT_ROW_V3 = "Prompt bundle v3 (`freeze.prompt_bundle_v3()`)"
PROBE_ROW_V3 = "Probe prompts v3 (`freeze.probe_bundle_v3()`)"
LABELLING_ROW = ROWS[5]
MODELS_ROW = f"Model registry (`{MODEL_REGISTRY}`)"
SWEEPS_ROW = f"`{E5_SWEEPS}/*.yaml` (sorted, concatenated)"
IMAGE_ROW = ROWS[6]
GIT_ROW = ROWS[7]
ANALYSIS_ROW_V3 = f"Analysis script (`{ANALYSIS_SCRIPT_V3}`)"
N_ROW = "Families (N)"
ARMS_ROW = "Confirmatory arms"
M_ROW = "Wave-wide m"
V3_ROWS = (
    ROWS[0],
    DESIGN_ROW_V3,
    TEMPLATE_ROW,
    *SCENARIO_ROWS_V3.values(),
    PROMPT_ROW_V3,
    PROBE_ROW_V3,
    LABELLING_ROW,
    MODELS_ROW,
    SWEEPS_ROW,
    IMAGE_ROW,
    GIT_ROW,
    ANALYSIS_ROW_V3,
    N_ROW,
    ARMS_ROW,
    M_ROW,
)


class Freeze(StrEnum):
    """A freeze and its record: freeze-v2 (E1-E4, `SPEC.md`) or freeze-v3 (E5, `SPEC-v3.md`)."""

    V2 = "v2"
    V3 = "v3"

    @property
    def spec(self) -> str:
        return SPEC if self is Freeze.V2 else SPEC_V3

    @property
    def rows(self) -> tuple[str, ...]:
        return ALL_ROWS if self is Freeze.V2 else V3_ROWS


class MissingArtifact(LookupError):
    """A frozen artifact that isn't on this checkout (yet)."""


def parse_arms(text: str) -> tuple[str, ...]:
    return tuple(sorted(arm.strip().strip("`") for arm in text.split(",") if arm.strip()))


@dataclasses.dataclass(frozen=True)
class Adaptation:
    """The adaptation outcomes the v3 record holds as values (design section 2.3): the wording
    template (A-1), N (A-2) and the confirmatory arms (A-3, A-4), whose count fixes m (A-5)."""

    template: str
    n: int
    arms: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.template not in E5_TEMPLATES:
            raise ValueError(f"template must be one of {', '.join(E5_TEMPLATES)}, got {self.template!r}")
        if self.n not in FAMILY_COUNTS:
            raise ValueError(f"N must be one of {', '.join(map(str, FAMILY_COUNTS))}, got {self.n}")
        if not self.arms:
            raise ValueError("no confirmatory arms")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError(f"duplicate confirmatory arm in {', '.join(self.arms)}")
        bad = [arm for arm in self.arms if not arm or any(c in arm for c in " `|,")]
        if bad:
            raise ValueError(f"not an arm key: {', '.join(map(repr, bad))}")

    @property
    def m(self) -> int:
        return 4 * len(self.arms)

    def values(self) -> dict[str, str]:
        return {
            TEMPLATE_ROW: self.template,
            N_ROW: str(self.n),
            ARMS_ROW: ", ".join(sorted(self.arms)),
            M_ROW: str(self.m),
        }


def spec_hash_v3(spec_text: str) -> str:
    return sha256(_blank(spec_text, V3_ROWS).encode())


def recorded_v3(spec_text: str) -> dict[str, str]:
    return _recorded(spec_text, V3_ROWS)


def adaptation_from_record(spec_text: str) -> Adaptation:
    """The adaptation outcomes written in the v3 record; raises if any is blank or inconsistent."""
    written = recorded_v3(spec_text)
    blank = [row for row in (TEMPLATE_ROW, N_ROW, ARMS_ROW, M_ROW) if not written.get(row)]
    if blank:
        raise ValueError(f"{SPEC_V3}'s freeze record has no value for {', '.join(blank)}")
    try:
        n, m = int(written[N_ROW]), int(written[M_ROW])
    except ValueError:
        raise ValueError(f"{N_ROW} and {M_ROW} must be integers in {SPEC_V3}'s freeze record") from None
    adaptation = Adaptation(written[TEMPLATE_ROW], n, parse_arms(written[ARMS_ROW]))
    if m != adaptation.m:
        raise ValueError(
            f"{M_ROW} is {m}, but 4 x {len(adaptation.arms)} confirmatory arms is {adaptation.m}"
        )
    return adaptation


def _load_attribute(root: Path, source: str) -> Any:
    where, _, attribute = source.rpartition(":")
    if where.endswith(".py"):
        path = root / where
        spec = importlib.util.spec_from_file_location(f"_frozen_{path.stem}", path)
        if spec is None or spec.loader is None or not path.is_file():
            raise MissingArtifact(f"no file {where}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        try:
            module = importlib.import_module(where)
        except ImportError as e:
            raise MissingArtifact(f"no module {where} ({e})") from e
    if not hasattr(module, attribute):
        raise MissingArtifact(f"{where} has no {attribute}")
    return getattr(module, attribute)


def probe_bundle_v3(root: Path) -> dict[str, Any]:
    """The frozen probe texts (`PROBE_TEXTS`), by name. `root` resolves the script sources."""
    return {name: _load_attribute(root, source) for name, source in PROBE_TEXTS.items()}


def probe_hash_v3(root: Path) -> str:
    return sha256(_canonical(probe_bundle_v3(root)))


def _file_hash(path: Path) -> str:
    if not path.is_file():
        raise MissingArtifact(f"no file {path}")
    return sha256(path.read_bytes())


def _set_hash(directory: Path) -> str:
    try:
        return scenario_set_hash(directory)
    except FileNotFoundError:
        raise MissingArtifact(f"no *.yaml files in {directory}") from None


def _spec_v3_hash(root: Path) -> str:
    path = root / SPEC_V3
    if not path.is_file():
        raise MissingArtifact(f"no file {path}")
    return spec_hash_v3(path.read_text())


def _git_sha_v3(root: Path) -> str:
    try:
        return git_sha(root)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise MissingArtifact(f"{root} is not a git checkout") from None


def _evaluate(items: dict[str, Callable[[], str]]) -> tuple[dict[str, str], dict[str, str]]:
    values, missing = {}, {}
    for label, compute in items.items():
        try:
            values[label] = compute()
        except MissingArtifact as e:
            values[label], missing[label] = "", str(e)
    return values, missing


def record_v3(
    root: Path, image_digest: str | None, adaptation: Adaptation | None, design: Path | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """The v3 record's values by row, and the rows whose artifact isn't there, with the reason. The
    image digest is empty when none is given; the scenario rows and the adaptation values are empty
    without an adaptation, which names the template."""
    items: dict[str, Callable[[], str]] = {row: str for row in V3_ROWS}
    items |= {
        ROWS[0]: functools.partial(_spec_v3_hash, root),
        DESIGN_ROW_V3: functools.partial(_file_hash, design or root / DESIGN_V3),
        PROMPT_ROW_V3: prompt_hash_v3,
        PROBE_ROW_V3: functools.partial(probe_hash_v3, root),
        LABELLING_ROW: labelling_hash,
        MODELS_ROW: functools.partial(_file_hash, root / MODEL_REGISTRY),
        SWEEPS_ROW: functools.partial(_set_hash, root / E5_SWEEPS),
        IMAGE_ROW: functools.partial(str, image_digest or ""),
        GIT_ROW: functools.partial(_git_sha_v3, root),
        ANALYSIS_ROW_V3: functools.partial(_file_hash, root / ANALYSIS_SCRIPT_V3),
    }
    if adaptation is not None:
        for name, row in SCENARIO_ROWS_V3.items():
            items[row] = functools.partial(_set_hash, root / E5_SCENARIOS / adaptation.template / name)
        items |= {row: functools.partial(str, value) for row, value in adaptation.values().items()}
    return _evaluate(items)


def write_record_v3(spec_text: str, values: dict[str, str]) -> str:
    """Write `values` into the v3 record. The table must already have every row of `V3_ROWS`."""
    lines = spec_text.split("\n")
    start, end = _record_span(lines)
    seen = set()
    for i in range(start, end):
        m = _ROW.match(lines[i])
        if m and m["artifact"] in V3_ROWS:
            seen.add(m["artifact"])
            lines[i] = _row(m["artifact"], values.get(m["artifact"], ""))
    absent = [row for row in V3_ROWS if row not in seen]
    if absent:
        raise ValueError(f"{SPEC_V3}'s record table has no row for {', '.join(absent)}")
    return "\n".join(lines)


def mismatches_v3(spec_text: str, values: dict[str, str]) -> list[str]:
    """Rows of the v3 record whose written value differs from `values`; as for freeze-v2, rows with
    nothing to compare and the git SHA are skipped."""
    written = recorded_v3(spec_text)
    return [
        f"{row}: recorded {written.get(row) or '(blank)'}, now {values[row]}"
        for row in V3_ROWS
        if row != GIT_ROW and values.get(row) and written.get(row) != values[row]
    ]


def tag_hashes(root: Path, image_digest: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """The `freeze-v3-pre` tag message's values (design section 2.1), by label, and the missing ones
    with the reason: the design, this spec, the v3 prompt bundle and probe prompts, each scenario
    directory of both templates, the model registry, the admission script and the image digest."""

    def digest() -> str:
        if not image_digest:
            raise MissingArtifact("no image digest (pass --image-digest)")
        return image_digest

    items: dict[str, Callable[[], str]] = {
        DESIGN_ROW_V3: functools.partial(_file_hash, root / DESIGN_V3),
        f"`{SPEC_V3}` (freeze record values blank)": functools.partial(_spec_v3_hash, root),
        PROMPT_ROW_V3: prompt_hash_v3,
        PROBE_ROW_V3: functools.partial(probe_hash_v3, root),
        **{
            f"`{E5_SCENARIOS}/{t}/{name}/*.yaml` (sorted, concatenated)": functools.partial(
                _set_hash, root / E5_SCENARIOS / t / name
            )
            for t in E5_TEMPLATES
            for name in E5_SETS
        },
        MODELS_ROW: functools.partial(_file_hash, root / MODEL_REGISTRY),
        f"Admission script (`{ADMISSION_SCRIPT}`)": functools.partial(_file_hash, root / ADMISSION_SCRIPT),
        IMAGE_ROW: digest,
    }
    return _evaluate(items)
