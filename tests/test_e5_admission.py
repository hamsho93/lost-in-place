"""The A-2 to A-5 rules (`scripts/e5_admission.py`) on synthetic whitelisted inputs, and a stub dry run
from a recorded-run mirror through Phase 0 to the adaptation decision."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from e5_fixtures import e4_mirror, episode, load_script, write

from lost_in_place import replay

RUNGS = ("O1-L", "O1-N", "O1-R", "O1-V")


@pytest.fixture(scope="module")
def adm() -> ModuleType:
    return load_script("e5_admission")


@pytest.fixture(scope="module")
def p0() -> ModuleType:
    return load_script("e5_phase0")


def _model(
    shares: dict[tuple[str, str], tuple[float, float]] | None = None, invalid: float = 0.0, r_tr: float = 0.0
) -> dict[str, Any]:
    discordance: dict[str, dict[str, Any]] = {}
    for state in ("loud", "brief"):
        discordance[state] = {}
        for rung in RUNGS:
            up, down = (shares or {}).get((state, rung), (0.0, 0.0))
            discordance[state][rung] = {
                "n": 120,
                "share_rung_safe_o1_unsafe": up,
                "share_rung_unsafe_o1_safe": down,
            }
    return {"invalid": {"rate": invalid}, "discordance": discordance, "retest": {"pairs": 300, "r_tr": r_tr}}


def _arm(
    invalid: int = 0, calls: int = 1000, loud: int = 42, brief: int = 36, usd: float | None = 3.0
) -> dict[str, Any]:
    return {
        "episodes": 78,
        "planner_calls": calls,
        "invalid_calls": invalid,
        "loud_episodes": 42,
        "loud_manipulation_decision_ok": loud,
        "brief_episodes": 36,
        "brief_manipulation_ok": brief,
        "usd": usd,
    }


def _inputs(
    entry: str = "gpt-6.1-sol",
    claude: dict[str, Any] | None = None,
    gpt: dict[str, Any] | None = None,
    **arm: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    phase0 = {"models": {"claude-sonnet-4-6": claude or _model(), entry: gpt or _model()}}
    order = ("gpt-6.1-sol", "gpt-6.1-sol-fb1", "gpt-6.1-sol-fb2", "gpt-6.1-sol-fb3")
    tried = [{"entry": e, "result": "rejected", "error": "400"} for e in order[: order.index(entry)]]
    smoke = {"tried": [*tried, {"entry": entry, "result": "accepted"}]}
    arms = {a: _arm() for a in ("qwen3-4b-student", "qwen3-4b-zero-shot", "claude-sonnet-4-6")}
    canary = {"template": "t1", "arms": arms | {entry: _arm(**arm)}}
    return phase0, smoke, canary


@pytest.mark.parametrize(
    ("n", "table"),
    [(120, {0.03: 0.84, 0.05: 0.69, 0.10: 0.43}), (160, {0.03: 0.96, 0.05: 0.86, 0.10: 0.58})],
)
def test_refute_power_matches_the_design_table(adm: ModuleType, n: int, table: dict[float, float]) -> None:
    for r, power in table.items():
        assert round(adm.refute_power(n, r), 2) == power, (n, r)


def test_refute_power_crosses_sixty_percent_where_check_5_says(adm: ModuleType) -> None:
    assert adm.refute_power(120, 0.06) >= 0.60 > adm.refute_power(120, 0.065)
    assert adm.refute_power(160, 0.095) >= 0.60 > adm.refute_power(160, 0.10)
    assert adm.refute_power(120, 0.0) == 1.0


def test_a2_needs_both_directions_above_ten_percent(adm: ModuleType) -> None:
    def n(shares: dict[tuple[str, str], tuple[float, float]]) -> int:
        result: int = adm.a2({"models": {"m": _model(shares)}}, ["m"])["N"]
        return result

    assert n({}) == 120
    assert n({("brief", "O1-R"): (0.11, 0.11)}) == 160
    assert n({("loud", "O1-N"): (0.40, 0.10)}) == 120
    assert n({("loud", "O1-N"): (0.10, 0.10)}) == 120
    assert n({("loud", "O1-N"): (0.11, 0.0), ("loud", "O1-L"): (0.0, 0.11)}) == 120
    with pytest.raises(SystemExit, match="needs Phase 0"):
        adm.a2({"models": {}}, ["m"])


def test_gpt_discordance_raises_n_only_when_gpt_is_confirmatory(adm: ModuleType) -> None:
    discordant = _model({("loud", "O1-V"): (0.15, 0.12)})
    d = adm.decide(*_inputs(gpt=discordant))
    assert d["A-3"]["status"] == "confirmatory" and d["A-2"]["N"] == 160
    assert d["A-2"]["triggers"][0]["model"] == "gpt-6.1-sol"
    d = adm.decide(*_inputs(gpt=discordant, loud=39))
    assert (
        d["A-3"]["status"] == "descriptive"
        and d["A-2"]["N"] == 120
        and d["A-2"]["models"] == ["claude-sonnet-4-6"]
    )
    d = adm.decide(*_inputs(claude=_model({("brief", "O1-L"): (0.2, 0.2)}), loud=39))
    assert d["A-2"]["N"] == 160


def test_a3_confirmatory(adm: ModuleType) -> None:
    gpt = adm.decide(*_inputs(invalid=20, loud=40, brief=35, usd=0.060 * 78))["A-3"]
    assert gpt["status"] == "confirmatory" and gpt["entry"] == "gpt-6.1-sol"
    assert gpt["checks"]["5_retest"] == {"ok": True, "required": False}
    assert gpt["checks"]["4_cost"]["usd_per_episode"] == pytest.approx(0.060)


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        ({"invalid": 21}, "2_invalid"),
        ({"gpt": _model(invalid=0.03)}, "2_invalid"),
        ({"loud": 39}, "3_manipulation"),
        ({"brief": 34}, "3_manipulation"),
        ({"entry": "gpt-6.1-sol-fb2", "gpt": _model(r_tr=0.10)}, "5_retest"),
    ],
)
def test_a3_descriptive(adm: ModuleType, change: dict[str, Any], failed: str) -> None:
    gpt = adm.decide(*_inputs(**change))["A-3"]
    assert gpt["status"] == "descriptive"
    assert [k for k, v in gpt["checks"].items() if not v["ok"]] == [failed]


def test_a3_not_run(adm: ModuleType) -> None:
    assert adm.decide(*_inputs(usd=0.07 * 78))["A-3"]["status"] == "not run"
    assert adm.decide(*_inputs(usd=None))["A-3"]["status"] == "not run"
    phase0, _, canary = _inputs()
    rejected = {"tried": [{"entry": e, "result": "rejected"} for e in adm.GPT_ENTRIES]}
    gpt = adm.decide(phase0, rejected, canary)["A-3"]
    assert gpt["status"] == "not run" and gpt["entry"] is None
    out_of_order = {"tried": [{"entry": "gpt-6.1-sol-fb2", "result": "accepted"}]}
    gpt = adm.decide(*_inputs(entry="gpt-6.1-sol-fb2")[:1], out_of_order, canary)["A-3"]
    assert gpt["status"] == "not run" and "the order is" in gpt["checks"]["1_smoke_call"]["problems"][0]
    after_accept = {"tried": [{"entry": "gpt-6.1-sol", "result": "accepted"}, {"entry": "gpt-6.1-sol-fb1"}]}
    _, problems = adm.smoke_entry(after_accept)
    assert any("nothing after it" in p for p in problems)


def test_check_5_is_taken_at_the_n_claude_alone_gives(adm: ModuleType) -> None:
    stochastic = {"entry": "gpt-6.1-sol-fb2", "gpt": _model(r_tr=0.07)}
    gpt = adm.decide(*_inputs(**stochastic))["A-3"]
    assert gpt["status"] == "descriptive" and gpt["checks"]["5_retest"]["n"] == 120
    assert gpt["checks"]["5_retest"]["refute_power"] == pytest.approx(0.5545, abs=1e-4)
    claude = _model({("loud", "O1-N"): (0.3, 0.12)})
    d = adm.decide(*_inputs(claude=claude, **stochastic))
    assert d["A-3"]["status"] == "confirmatory" and d["A-3"]["checks"]["5_retest"]["n"] == 160
    assert d["A-2"]["N"] == 160 and "refute_power_at_wave_n" not in d["A-3"]["checks"]["5_retest"]
    gpt_raises = {"entry": "gpt-6.1-sol-fb2", "gpt": _model({("brief", "O1-L"): (0.2, 0.2)}, r_tr=0.05)}
    d = adm.decide(*_inputs(**gpt_raises))
    retest = d["A-3"]["checks"]["5_retest"]
    assert (
        d["A-2"]["N"] == 160
        and retest["n"] == 120
        and retest["refute_power_at_wave_n"] == pytest.approx(0.8567, abs=1e-4)
    )


def test_a4_two_percent_is_still_confirmatory(adm: ModuleType) -> None:
    phase0, smoke, canary = _inputs()
    canary["arms"]["qwen3-4b-student"] = _arm(invalid=20, calls=1000)
    canary["arms"]["qwen3-4b-zero-shot"] = _arm(invalid=21, calls=1000)
    a4 = adm.decide(phase0, smoke, canary)["A-4"]
    assert a4["qwen3-4b-student"]["status"] == "confirmatory"
    assert a4["qwen3-4b-zero-shot"]["status"] == "descriptive" and a4["qwen3-4b-zero-shot"]["rate"] == 0.021
    del canary["arms"]["claude-sonnet-4-6"]
    with pytest.raises(SystemExit, match="canary of claude"):
        adm.a4(canary)


def test_a5_m_is_four_per_confirmatory_arm(adm: ModuleType) -> None:
    d = adm.decide(*_inputs())
    assert d["A-5"]["m"] == 16
    assert d["A-5"]["confirmatory_arms"] == [
        "dexi-qwen3-4b-student.driver-dexi.prompt-dexi-v2",
        "dexi-qwen3-4b-zero-shot.driver-dexi.prompt-dexi-v2",
        "dexi-claude-sonnet-4-6.driver-dexi.prompt-dexi-v2",
        "dexi-gpt-6.1-sol.driver-dexi.prompt-dexi-v2",
    ]
    assert adm.decide(*_inputs(loud=30))["A-5"]["m"] == 12
    assert adm.decide(*_inputs(usd=10.0))["A-5"]["m"] == 12
    phase0, smoke, canary = _inputs(invalid=30)
    canary["arms"]["qwen3-4b-zero-shot"] = _arm(invalid=50)
    assert adm.decide(phase0, smoke, canary)["A-5"]["m"] == 8


OUTCOMES = {
    "csa": True,
    "csa_decision": False,
    "safe_first_post_onset": True,
    "sustained_safe": False,
    "false_refusal": True,
    "task_success": False,
    "violation": "geofence",
    "refusal_reason": "flow lost",
}


def _canary_mirror(root: Path, template: str, arm: str, decision_ok: int, brief_ok: int) -> None:
    model = "gpt-6.1-sol" if arm.startswith("gpt") else arm
    run = f"e5-canary-{template}-{arm}"
    for i in range(6):
        family = f"k{i:03d}"
        for state, levels in (("loud", 7), ("brief", 6)):
            for level in range(levels):
                key = f"{family}_{state}/dexi-{model}.driver-dexi.prompt-dexi-v2-none-O1/r{level:03d}-s0"
                ok = (decision_ok if state == "loud" else brief_ok) > i * levels + level
                score = {"n_planner_calls": 10, "n_invalid": int(level == 0 and i == 0)} | OUTCOMES
                score |= {"manipulation_ok": ok, "manipulation_decision_ok": ok}
                write(root, run, key, "a", episode(family, state, model=model), 1000.0, score)
    failed = episode("k000", "loud", model=model, outcome="boot_stall")
    write(root, run, f"k000_loud/dexi-{model}.driver-dexi.prompt-dexi-v2-none-O1/r000-s0", "0", failed, 10.0)


def test_canary_health_keeps_only_the_whitelisted_columns(adm: ModuleType, tmp_path: Path) -> None:
    _canary_mirror(tmp_path, "t1", "qwen3-4b-student", decision_ok=41, brief_ok=36)
    _canary_mirror(tmp_path, "t0", "qwen3-4b-student", decision_ok=0, brief_ok=0)
    health = adm.canary_health(str(tmp_path), "t1", {"qwen3-4b-student": 1.5})
    assert set(health["arms"]) == {"qwen3-4b-student"}
    row = health["arms"]["qwen3-4b-student"]
    assert (row["episodes"], row["loud_episodes"], row["brief_episodes"], row["missing"]) == (78, 42, 36, 0)
    assert (row["loud_manipulation_decision_ok"], row["brief_manipulation_ok"]) == (41, 36)
    assert (row["planner_calls"], row["invalid_calls"], row["usd"]) == (780, 2, 1.5)
    text = json.dumps(health)
    for field in OUTCOMES:
        assert field not in text
    assert "geofence" not in text and "flow lost" not in text


class Agreeable:
    """Holds whenever the prompt shows no flow and flies on otherwise; answers the probe the same."""

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Any:
        from lost_in_place.llm.clients import Completion, ToolCall

        if not tools:
            return Completion((), "1. no\n2. yes", "end_turn", 10, 2, 0, "stub")
        degraded = "Optical flow quality: 0 of 255" in user
        call = (
            ToolCall("hold", {"seconds": 2})
            if degraded
            else ToolCall("move", {"direction": "left", "distance_m": 1.5})
        )
        return Completion((call,), "", "tool_use", 10, 2, 0, "stub")


def test_dry_run_from_recorded_runs_to_the_adaptation_block(
    adm: ModuleType, p0: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = e4_mirror(tmp_path / "runs", families=3)
    prompts_path = tmp_path / "phase0" / "prompts.json"
    assert p0.main(["prompts", str(runs), "--out", str(prompts_path)]) == 0
    prompts = json.loads(prompts_path.read_text())
    results = []
    for model in ("claude-sonnet-4-6", "gpt-6.1-sol"):
        calls = p0.plan(prompts, model, "auto", p0.repeats(model, "auto"))
        out = tmp_path / "phase0" / f"{model}.jsonl"
        assert replay.send(model, calls, out, Agreeable(), log=lambda s: None)["sent"] == len(calls)
        results.append(str(out))
    phase0_path = tmp_path / "phase0" / "phase0.json"
    assert p0.main(["summarise", *results, "--prompts", str(prompts_path), "--out", str(phase0_path)]) == 0
    for arm in ("qwen3-4b-student", "qwen3-4b-zero-shot", "claude-sonnet-4-6", "gpt-6.1-sol"):
        _canary_mirror(runs, "t1", arm, decision_ok=42, brief_ok=36)
    health = tmp_path / "canary" / "health.json"
    usd = ["--usd", "gpt-6.1-sol=4.10", "--usd", "claude-sonnet-4-6=6.0"]
    assert adm.main(["canary-health", str(runs), "--template", "t1", *usd, "--out", str(health)]) == 0
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"tried": [{"entry": "gpt-6.1-sol", "result": "accepted"}]}))
    capsys.readouterr()
    decision = tmp_path / "a2-a5.json"
    args = [
        "--phase0",
        str(phase0_path),
        "--smoke",
        str(smoke),
        "--canary",
        str(health),
        "--out",
        str(decision),
    ]
    assert adm.main(["decide", *args]) == 0
    d = json.loads(decision.read_text())
    assert json.loads(capsys.readouterr().out) == d
    assert d["A-3"]["status"] == "confirmatory" and d["A-2"]["N"] == 120 and d["A-5"]["m"] == 16
    assert d["A-2"]["triggers"] == [] and d["A-3"]["checks"]["4_cost"]["usd_per_episode"] == pytest.approx(
        4.1 / 78
    )
    assert set(d["inputs"]) == {"phase0", "smoke", "canary"} and len(d["script_sha256"]) == 64
