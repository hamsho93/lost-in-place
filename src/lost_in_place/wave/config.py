"""The wave configuration: arms, their sweeps, caps, cost sources and the stop-rule thresholds.

Arms are registered here, not in code. A wave arm is either a Batch arm, which the submitter tops up
from a sweep's manifest, or an external arm (comprehension probes, the blind labeller), which only
has a metering window and a cost cap. The packaged `e5.yaml` is the E5 wave; `--config` replaces it.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ARM_NAME = re.compile(r"[a-z0-9][a-z0-9.-]*")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Price(Strict):
    """US dollars per million tokens, as billed for the arm's tagged inference profile."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)

    def cost(self, input_tokens: float, output_tokens: float) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


class Select(Strict):
    """A subset of a sweep's manifest, so one sweep can be flown as two arms (the scripted gate cells
    first, the rest later). Entries match when their scenario id matches `scenario_regex` and their
    observation is in `observations` (any, when empty); `invert` takes the complement."""

    scenario_regex: str = ".*"
    observations: tuple[str, ...] = ()
    invert: bool = False

    def matches(self, scenario_id: str, observation: str) -> bool:
        hit = re.search(self.scenario_regex, scenario_id) is not None and (
            not self.observations or observation in self.observations
        )
        return hit != self.invert


class After(Strict):
    """A start condition: `arm` has settled at least `fraction` of its episodes."""

    arm: str
    fraction: float = Field(1.0, gt=0, le=1)


class CapRule(Strict):
    """Raise or lower the in-flight cap once another arm has settled (zero-shot: 39, then 64 when
    Claude ends)."""

    after: After
    cap: int = Field(gt=0)


class Arm(Strict):
    name: str
    kind: Literal["batch", "external"] = "batch"
    # Batch arms: the sweep YAML whose manifest the arm flies, and optionally a subset of it.
    sweep: str | None = None
    select: Select | None = None
    cap: int = Field(0, ge=0)
    cap_rules: tuple[CapRule, ...] = ()
    ramp_step: int | None = Field(None, gt=0)
    start_after: tuple[After, ...] = ()
    needs_gate: bool = False
    # Cost sources: a Bedrock system inference profile, reached through the stack's tagged copy of
    # it, priced per token; or a Custom Model Import, priced per copy-minute. Compute is metered for
    # every Batch arm from its jobs' run time.
    bedrock_model: str | None = None
    price: Price | None = None
    import_name: str | None = None
    # US dollars: S-7's cap at the base N, the planned median and P90 (projections before any
    # measurement, and the October rule).
    cap_usd: float | None = Field(None, gt=0)
    estimate_usd: float = Field(0.0, ge=0)
    p90_usd: float = Field(0.0, ge=0)
    # Section 10.4 and S-8: the arm runs last, only if the month-fit sum passes, and pauses when its
    # rolling cost per episode exceeds `s8_factor` x its canary's. Month-fit arms are summed in the
    # order listed (GPT-6.1 Sol, then Claude Sonnet 5.5).
    month_fit: bool = False
    canary_usd_per_episode: float | None = Field(None, gt=0)
    probe_usd: float = Field(0.0, ge=0)
    # An arm whose cost is already inside another arm's projection (GPT's probe, in `probe_usd`).
    counted_in: str | None = None
    # The arm's models.yaml entry, whose `region` is where its tokens are metered; S-0 checks that the
    # entry equals `pinned_model` field by field (the smoke call's accepted config).
    model: str | None = None
    pinned_model: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> Arm:
        if not ARM_NAME.fullmatch(self.name):
            raise ValueError(f"arm name {self.name!r}: use lower case, digits, '.' and '-'")
        if self.kind == "batch" and (not self.sweep or self.cap <= 0):
            raise ValueError(f"arm {self.name}: a batch arm needs a sweep and a positive cap")
        if self.kind == "external" and (self.sweep or self.cap):
            raise ValueError(f"arm {self.name}: an external arm has no sweep or cap")
        if self.bedrock_model and self.price is None:
            raise ValueError(f"arm {self.name}: bedrock_model needs a price")
        if self.pinned_model is not None and not self.model:
            raise ValueError(f"arm {self.name}: pinned_model needs the models.yaml key in `model`")
        return self


class Month(Strict):
    """A budget month. `stop_usd` is the metered hard stop (S-9). With `budget_actual`, the month's
    estimate is the AWS Budgets actual plus the meter since `budget_lag_h` before its last update
    (October, which started before the meter); otherwise the meter alone. With `job_p90_check`, an arm
    starts only if the estimate plus its P90 stays under the stop. `arms` limits which arms may run.
    A month without `stop_usd` has no approved budget yet: nothing is submitted in it, and a month-fit
    arm can't be deferred to it."""

    stop_usd: float | None = Field(None, gt=0)
    budget_actual: bool = False
    job_p90_check: bool = False
    arms: tuple[str, ...] | None = None


class Rules(Strict):
    """Stop-rule thresholds (plan section 10.5)."""

    harness_window: int = 100
    harness_max_rate: float = 0.03
    consecutive_link_failures: int = 3
    invalid_window: int = 100
    invalid_max_rate: float = 0.02
    throttle_window_s: int = 900
    throttle_max_rate: float = 0.01
    throttle_cap_cut: int = 5
    warm_timeout_s: int = 1800
    idle_copy_s: int = 600
    s7_first: int = 100
    s7_every: int = 250
    s8_window: int = 100
    s8_factor: float = 1.5
    gpt_admission_usd_per_episode: float = 0.060
    month_fit_factor: float = 1.15
    deadman_s: int = 300


class Wave(Strict):
    name: str
    n_families: int = Field(gt=0)
    base_n_families: int = 120
    cap_factor_above_base: float = 1.25
    stack: str = "LostInPlaceSim"
    job_definition_output: str = "SweepJobDefinition"
    ledger_prefix: str
    queries_log: str
    manifests_prefix: str = "manifests"
    runs_prefix: str = "runs"
    poll_s: int = Field(60, gt=0)
    topup_min: int = Field(5, gt=0)
    array_max: int = Field(10, ge=2)
    max_in_flight: int = Field(64, gt=0)
    max_submissions_per_episode: int = Field(3, gt=0)
    # Section 10.1 rates: $0.05718 per CMU-minute x 2 CMUs per copy; spot compute per vCPU-hour.
    copy_minute_usd: float = 0.114
    vcpu_hour_usd: float = 0.025
    episode_vcpus: int = 2
    other_usd_per_day: float = 0.5
    fixed_remaining_usd: float = 0.0
    # The Budgets actual lags 18-24 h behind its LastUpdatedTime, so the meter is added from this
    # long before it (double-counting the overlap, which errs towards stopping).
    budget_lag_h: float = 24.0
    months: dict[str, Month]
    # Section 10.4: a month-fit arm that doesn't fit is dropped if the next month has no approved
    # budget by this date.
    deferral_deadline: str | None = None
    rules: Rules = Rules()
    preflight_commands: tuple[tuple[str, ...], ...] = ()
    gate_command: tuple[str, ...] | None = None
    arms: tuple[Arm, ...]

    @model_validator(mode="after")
    def _check(self) -> Wave:
        names = [a.name for a in self.arms]
        if len(set(names)) != len(names):
            raise ValueError("duplicate arm names")
        for arm in self.arms:
            refs = [r.arm for r in [*arm.start_after, *(c.after for c in arm.cap_rules)]]
            for ref in [*refs, *([arm.counted_in] if arm.counted_in else [])]:
                if ref not in names:
                    raise ValueError(f"arm {arm.name}: unknown arm {ref!r}")
        for key in self.months:
            if not re.fullmatch(r"\d{4}-\d{2}", key):
                raise ValueError(f"month {key!r}: use YYYY-MM")
        return self

    def arm(self, name: str) -> Arm:
        for arm in self.arms:
            if arm.name == name:
                return arm
        raise LookupError(f"no arm {name!r}; configured: {', '.join(a.name for a in self.arms)}")

    def cap_usd(self, arm: Arm) -> float | None:
        if arm.cap_usd is None:
            return None
        scale = self.cap_factor_above_base if self.n_families > self.base_n_families else 1.0
        return arm.cap_usd * scale

    def sharing_import(self, import_name: str) -> list[Arm]:
        return [a for a in self.arms if a.import_name == import_name]

    def month_fit_arms(self) -> list[Arm]:
        return [a for a in self.arms if a.month_fit]

    def outside_month_fit(self) -> list[Arm]:
        """Arms whose remaining cost counts as a fixed item in the month-fit sum."""
        fit = {a.name for a in self.arms if a.month_fit}
        return [a for a in self.arms if a.name not in fit and a.counted_in not in fit]


def load(path: Path | None = None) -> Wave:
    text = path.read_text() if path else files("lost_in_place.wave").joinpath("e5.yaml").read_text()
    return Wave.model_validate(yaml.safe_load(text))
