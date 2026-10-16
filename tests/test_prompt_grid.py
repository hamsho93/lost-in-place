import asyncio
from pathlib import Path
from typing import Any

import pytest

from lost_in_place import freeze, prompt_grid
from lost_in_place.llm import dexi
from lost_in_place.llm.registry import load_models
from lost_in_place.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def grid() -> dict[str, Any]:
    return prompt_grid.render_grid(ROOT)


def test_the_grid_is_in_the_v3_bundle_and_deterministic(grid: dict[str, Any]) -> None:
    bundle = freeze.prompt_bundle_v3()
    assert bundle["render_grid"] == grid
    assert list(bundle)[-1] == "render_grid" and grid["prompts"] > 20_000


def test_the_grid_covers_every_design_level_and_bedrock_entry(grid: dict[str, Any]) -> None:
    assert set(grid["by_pair"]) == {f"{level}/{version}" for level, version in prompt_grid.pairs()}
    assert {f"{level}/{version}" for level, version in dexi.LADDER.values()} <= set(grid["by_pair"])
    bedrock = {n for n, s in load_models().items() if s.provider in ("bedrock", "bedrock_imported")}
    assert set(grid["wire"]) == bedrock


def test_the_grid_samples_both_templates_families_and_every_canary(grid: dict[str, Any]) -> None:
    for t in freeze.E5_TEMPLATES:
        canaries = sorted(p.name for p in (ROOT / freeze.E5_SCENARIOS / t / "canary").glob("k*_healthy.yaml"))
        families = sorted(
            p.name for p in (ROOT / freeze.E5_SCENARIOS / t / "families").glob("c*_healthy.yaml")
        )
        sampled = [
            s.rsplit("/", 1)[1] for s in grid["scenarios"] if s.startswith(f"{freeze.E5_SCENARIOS}/{t}/")
        ]
        assert sorted(sampled) == sorted([*families[:: prompt_grid.FAMILY_EVERY], *canaries])
    k000 = {
        t: load_scenario(ROOT / freeze.E5_SCENARIOS / t / "canary" / "k000_healthy.yaml").mission
        for t in freeze.E5_TEMPLATES
    }
    assert k000["t0"] is not None and k000["t1"] is not None
    assert k000["t0"].instruction != k000["t1"].instruction


def test_the_health_sweep_straddles_the_thresholds_and_the_rounding() -> None:
    sweep = prompt_grid.health_sweep()
    flows, ephs = {h.flow_quality for h in sweep}, {h.eph_m for h in sweep}
    assert {0, 49, 50, 159, 160, 255} <= flows and 49.5 in flows
    assert {0.69, 0.695, 0.7, 0.705, 0.71} <= ephs
    for field in ("xy_valid", "innovation_check_failing", "heading_reference"):
        assert {getattr(h, field) for h in sweep} == {True, False}
    assert len(sweep) == len(prompt_grid.FLOWS) * len(prompt_grid.EPHS) * 8


def test_the_histories_reach_every_result_and_the_elision() -> None:
    scenario = load_scenario(ROOT / freeze.E5_SCENARIOS / "t1" / "canary" / "k000_healthy.yaml")
    health = freeze.v3_observations()["loud"]
    requests = asyncio.run(prompt_grid._fly(scenario, "O1", "dexi-v2", prompt_grid.SCRIPTS["faults"], health))
    last = requests[-1][1]
    for text in (
        "(earlier actions elided: ",
        "-> REJECTED by validator: outside the geofence",
        "-> failed: stopped by the validator: outside the geofence",
        "-> overridden: interrupted: PX4 failsafe (land)",
        "-> timeout: did not reach heading",
        "-> timeout: did not reach target",
    ):
        assert text in last, text
    assert len(requests) == len(prompt_grid.SCRIPTS["faults"])
    long = asyncio.run(prompt_grid._fly(scenario, "O0", "dexi-v2", prompt_grid.SCRIPTS["long"], None))
    # Behind the last request: the take-off line and every move but the last.
    elided = len(prompt_grid.SCRIPTS["long"]) - dexi.HISTORY_WINDOW
    assert f"(earlier actions elided: {elided})" in long[-1][1]


def test_a_render_change_only_off_the_exemplars_changes_the_grid(
    grid: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    line = dexi.history_line

    def history_line(number: int, name: str | None, args: dict[str, Any], result: str) -> str:
        return line(number, name, args, result.replace("timeout:", "timed out:"))

    renders = freeze.prompt_bundle_v3()["renders"]
    monkeypatch.setattr(dexi, "history_line", history_line)
    changed = prompt_grid.render_grid(ROOT)
    assert changed["sha256"] != grid["sha256"]
    assert all(changed["by_pair"][p] != h for p, h in grid["by_pair"].items())
    monkeypatch.setattr(prompt_grid, "render_grid", lambda root: changed)
    assert freeze.prompt_bundle_v3()["renders"] == renders


def test_a_checkout_without_the_e5_families_has_no_bundle(tmp_path: Path) -> None:
    with pytest.raises(freeze.MissingArtifact, match="render grid: no E5 families"):
        freeze.prompt_bundle_v3(tmp_path)
