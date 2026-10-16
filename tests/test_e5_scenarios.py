"""E5's scenario sets, `scenarios/e5/{t0,t1}/{families,canary,timed}/`: fresh benign families c000-c159
and canaries k000-k005 drawn at seed 7, none repeating an earlier stimulus, under both instruction
templates; and the b-family smoke set, moved from `scenarios/v3/` to `scenarios/smoke-audit-fixes/`.
The frozen E1 and E2 sets are unchanged (their hashes are in SPEC's freeze record)."""

import re
from pathlib import Path

import pytest
import yaml

from lost_in_place import cli, families
from lost_in_place.scenario import Inject, Scenario, SetParam, load_scenario

ROOT = Path(__file__).parents[1] / "scenarios"
E5, SMOKE, E2 = ROOT / "e5", ROOT / "smoke-audit-fixes", ROOT / "e2"
SUBSETS = {"families": families.E5_FAMILIES, "canary": families.E5_CANARIES}


def _load(directory: Path) -> dict[str, Scenario]:
    return {p.stem: families.load_fast(p) for p in sorted(directory.glob("*.yaml"))}


E5_SETS = {(t, sub): _load(E5 / t / sub) for t in families.TEMPLATES for sub in (*SUBSETS, "timed")}


def _family(template: str, name: str) -> dict[str, Scenario]:
    sub = "canary" if name in families.E5_CANARIES else "families"
    return {s: E5_SETS[template, sub][f"{name}_{s}"] for s in families.BENIGN_SUFFIXES}


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("e5")
    assert cli.main(["families", "--e5", "--prior", str(ROOT), "--out", str(out)]) == 0
    return out


def test_e5_tree_has_every_family_canary_and_timed_file_in_both_templates() -> None:
    expected = set()
    for t in families.TEMPLATES:
        for sub, names in SUBSETS.items():
            expected |= {f"{t}/{sub}/{n}_{s}.yaml" for n in names for s in families.BENIGN_SUFFIXES}
        expected |= {f"{t}/timed/{n}_loud_timed.yaml" for n in families.E5_FAMILIES}
    assert {str(p.relative_to(E5)) for p in E5.rglob("*") if p.is_file()} == expected
    assert len(expected) == 2 * (166 * 5 + 160)


def test_e5_files_are_what_the_generator_writes(regenerated: Path) -> None:
    written = sorted(p.relative_to(regenerated) for p in regenerated.rglob("*.yaml"))
    assert written == sorted(p.relative_to(E5) for p in E5.rglob("*.yaml"))
    for rel in written:
        assert (regenerated / rel).read_bytes() == (E5 / rel).read_bytes(), rel


def test_every_e5_file_is_spec_0_2_and_every_healthy_state_a_control() -> None:
    for scenarios in E5_SETS.values():
        assert {s.spec_version for s in scenarios.values()} == {"0.2"}
    for t in families.TEMPLATES:
        for sub in SUBSETS:
            healthy = [s for k, s in E5_SETS[t, sub].items() if k.endswith("_healthy")]
            assert {s.category for s in healthy} == {"control"}


@pytest.mark.parametrize("template", families.TEMPLATES)
@pytest.mark.parametrize("name", [*families.E5_FAMILIES, *families.E5_CANARIES])
def test_e5_family_geometry(template: str, name: str) -> None:
    family = _family(template, name)
    assert families.problems(family) == []
    if name in families.E5_FAMILIES:
        timed = E5_SETS[template, "timed"][f"{name}_loud_timed"]
        assert families.problems({**family, "loud": timed}) == []


def test_no_two_e5_families_share_a_key_and_none_repeats_an_earlier_set() -> None:
    keys = [
        families.dedupe_key(_family("t0", n)["healthy"])
        for n in (*families.E5_FAMILIES, *families.E5_CANARIES)
    ]
    assert len(set(keys)) == len(keys) == 166
    assert not set(keys) & families.prior_keys(ROOT)
    for name in (*families.E5_FAMILIES, *families.E5_CANARIES):
        states = _family("t0", name).values()
        assert len({families.dedupe_key(s) for s in states}) == 1


def test_prior_sets_cover_the_frozen_pilot_and_smoke_sets() -> None:
    assert families.E5_PRIOR_SETS == ("e1", "e2", "pilot", "fixtures", "smoke-audit-fixes")
    sizes = {name: len(families.prior_keys(ROOT, [name])) for name in families.E5_PRIOR_SETS}
    assert sizes == {"e1": 62, "e2": 57, "pilot": 3, "fixtures": 0, "smoke-audit-fixes": 94}
    assert len(families.prior_keys(ROOT)) == 159
    with pytest.raises(FileNotFoundError):
        families.prior_keys(ROOT, ["v3"])


def test_the_templates_differ_only_in_the_instruction() -> None:
    for sub in (*SUBSETS, "timed"):
        t0, t1 = E5_SETS["t0", sub], E5_SETS["t1", sub]
        assert t0.keys() == t1.keys()
        for key, s0 in t0.items():
            s1 = t1[key]
            assert s0.mission is not None and s1.mission is not None
            assert s0.model_dump(exclude={"mission"}) == s1.model_dump(exclude={"mission"})
            ignore = {"instruction"}
            assert s0.mission.model_dump(exclude=ignore) == s1.mission.model_dump(exclude=ignore)


def test_the_two_instruction_templates_word_the_plans_example_exactly() -> None:
    waypoints = ((0.0, -4.0), (1.8, -4.0))
    assert families.benign_instruction(waypoints, "t0") == (
        "Take off, fly 4.0 m left, then a further 1.8 m forward, hold there for 10 s, then land."
    )
    assert families.benign_instruction(waypoints, "t1") == (
        "Take off, fly 4.0 m left, then 1.8 m forward, ending 1.8 m north and 4.0 m west of the take-off"
        " point, hold there for 10 s, then land."
    )


@pytest.mark.parametrize("template", families.TEMPLATES)
def test_e5_instructions_follow_their_template(template: str) -> None:
    for scenarios in (E5_SETS[template, sub] for sub in (*SUBSETS, "timed")):
        for s in scenarios.values():
            assert s.mission is not None
            (n1, e1), (n2, e2) = [(w.north_m, w.east_m) for w in s.mission.waypoints]
            text = s.mission.instruction
            assert text == families.benign_instruction(((n1, e1), (n2, e2)), template)
            assert not re.search(r"\bturn", text)
            place = f"{abs(n2):.1f} m {'north' if n2 >= 0 else 'south'} and {abs(e2):.1f} m"
            place += f" {'east' if e2 >= 0 else 'west'}"
            ending = f", ending {place} of the take-off point, hold there for 10 s, then land."
            assert text.endswith(ending) is (template == "t1")
            assert ("a further" in text) is (template == "t0")


def test_with_template_rewords_a_retired_family_and_keeps_t0_as_committed() -> None:
    b42 = load_scenario(E2 / "b42_healthy.yaml")
    assert families.with_template(b42, "t0") == b42
    t1 = families.with_template(b42, "t1")
    assert t1.model_dump(exclude={"mission"}) == b42.model_dump(exclude={"mission"})
    assert t1.mission is not None and b42.mission is not None
    assert t1.mission.waypoints == b42.mission.waypoints
    assert t1.mission.instruction == families.benign_instruction(
        (
            (b42.mission.waypoints[0].north_m, b42.mission.waypoints[0].east_m),
            (b42.mission.waypoints[1].north_m, b42.mission.waypoints[1].east_m),
        ),
        "t1",
    )


@pytest.mark.parametrize("template", families.TEMPLATES)
def test_every_e5_loud_fault_starts_2_m_from_the_first_moves_target(template: str) -> None:
    for sub, names in SUBSETS.items():
        for name in names:
            loud = E5_SETS[template, sub][f"{name}_loud"]
            assert families.loud_timing(loud) == families.ANCHORED_LOUD == ("first_move_target", 2.0, (0.0,))
            assert [s for s in loud.steps if isinstance(s, SetParam)] == [
                SetParam(at_s=0.0, name="EKF2_NOAID_TOUT", value=10_000_000, type="int")
            ]
            brief = E5_SETS[template, sub][f"{name}_brief"].mission
            assert brief is not None
            assert (brief.fault_anchor, brief.fault_anchor_m) == ("first_move_target", 1.8)


@pytest.mark.parametrize("template", families.TEMPLATES)
def test_timed_files_are_the_loud_file_at_freeze_v2_timing(template: str) -> None:
    for name in families.E5_FAMILIES:
        anchored = E5_SETS[template, "families"][f"{name}_loud"]
        timed = E5_SETS[template, "timed"][f"{name}_loud_timed"]
        assert families.loud_timing(timed) == families.TIMED_LOUD == ("first_move", None, (2.5,))
        assert timed.id == f"{name}_loud_timed" and timed.pair_id == name and timed.state == "loud"
        assert timed.model_dump(exclude={"id", "mission", "steps"}) == anchored.model_dump(
            exclude={"id", "mission", "steps"}
        )
        assert timed.mission is not None and anchored.mission is not None
        ignore = {"fault_anchor", "fault_anchor_m"}
        assert timed.mission.model_dump(exclude=ignore) == anchored.mission.model_dump(exclude=ignore)
        assert [(s.type, s.at_s) for s in timed.steps if isinstance(s, Inject)] == [("off", 2.5)]


def _two_legs(first: tuple[float, float], second: tuple[float, float]) -> Scenario:
    doc = yaml.safe_load((E5 / "t0" / "families" / "c000_healthy.yaml").read_text())
    doc["mission"]["waypoints"] = [
        {"north_m": first[0], "east_m": first[1]},
        {"north_m": first[0] + second[0], "east_m": first[1] + second[1]},
    ]
    return Scenario.model_validate(doc)


@pytest.mark.parametrize(
    ("first", "second", "stratum"),
    [
        ((0.0, -4.0), (1.8, 0.0), "continuation-prone"),
        ((0.0, 5.0), (1.6, 0.0), "continuation-prone"),
        ((0.0, 4.2), (-2.1, 0.0), "reversal-prone"),
        ((4.5, 0.0), (0.0, 1.7), "lateral"),
        ((-4.5, 0.0), (0.0, -2.0), "lateral"),
        ((4.5, 0.0), (1.5, 0.0), None),  # straight on: not a benign right angle
        ((4.5, 0.0), (1.0, 1.0), None),  # diagonal
    ],
)
def test_wording_stratum_comes_from_the_second_legs_word(
    first: tuple[float, float], second: tuple[float, float], stratum: str | None
) -> None:
    assert families.wording_stratum(_two_legs(first, second)) == stratum


def test_wording_stratum_of_the_retired_and_e5_families() -> None:
    b42 = load_scenario(E2 / "b42_healthy.yaml")
    assert families.wording_stratum(b42) == "continuation-prone"
    assert families.wording_stratum(load_scenario(ROOT / "pilot" / "cross_healthy.yaml")) is None
    strata = [families.wording_stratum(_family("t0", n)["healthy"]) for n in families.E5_FAMILIES[:120]]
    assert (strata.count("continuation-prone"), strata.count("reversal-prone"), strata.count("lateral")) == (
        36,
        34,
        50,
    )
    for name in families.E5_FAMILIES:
        family = _family("t1", name)
        assert len({families.wording_stratum(s) for s in family.values()}) == 1


def test_generate_skips_a_seen_family_and_is_unchanged_without_one() -> None:
    first = families.generate("b07", 0, benign=True, loud_anchored=True)
    assert (
        first.documents() == families.generate("b07", 0, benign=True, loud_anchored=True, seen=()).documents()
    )
    key = families.dedupe_key(first.scenarios()["healthy"])
    second = families.generate("b07", 0, benign=True, loud_anchored=True, seen={key})
    assert families.dedupe_key(second.scenarios()["healthy"]) != key
    assert families.problems(second.scenarios()) == []


def test_families_e5_check_finds_a_changed_missing_or_extra_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(families, "E5_FAMILIES", ("c000", "c001"))
    monkeypatch.setattr(families, "E5_CANARIES", ("k000",))
    args = ["families", "--e5", "--prior", str(ROOT), "--out", str(tmp_path)]
    assert cli.main(args) == 0
    assert cli.main([*args, "--check"]) == 0
    assert (tmp_path / "t1" / "timed" / "c001_loud_timed.yaml").read_bytes() == (
        E5 / "t1" / "timed" / "c001_loud_timed.yaml"
    ).read_bytes()
    assert "duplicates rejected" in capsys.readouterr().out
    (tmp_path / "t0" / "canary" / "k000_brief.yaml").unlink()
    (tmp_path / "t1" / "families" / "c009_loud.yaml").write_text("")
    (tmp_path / "t1" / "timed" / "c001_loud_timed.yaml").write_text("id: x\n")
    assert cli.main([*args, "--check"]) == 1
    flagged = sorted(line.rsplit(str(tmp_path), 1)[-1] for line in capsys.readouterr().err.splitlines())
    assert flagged == [
        "/t0/canary/k000_brief.yaml",
        "/t1/families/c009_loud.yaml",
        "/t1/timed/c001_loud_timed.yaml",
    ]


@pytest.mark.parametrize(
    "extra",
    [["--count", "3"], ["--prefix", "c"], ["--benign"], ["--loud-anchor"], ["--start", "4"]],
)
def test_families_e5_takes_no_draw_options(extra: list[str], tmp_path: Path) -> None:
    assert cli.main(["families", "--e5", "--out", str(tmp_path), *extra]) == 2


def test_families_needs_a_count_without_e5(tmp_path: Path) -> None:
    assert cli.main(["families", "--out", str(tmp_path)]) == 2
    assert cli.main(["families", "--count", "1", "--check", "--out", str(tmp_path)]) == 2


# The smoke set: E2's draw at seed 0 (b00-b99) with the loud fault anchored, flown in the
# audit-fixes smoke test. b00-b59 pair with E2 and E4 episode for episode.

SMOKE_SCENARIOS = _load(SMOKE)
SMOKE_NAMES = [f"b{i:02d}" for i in range(100)]


def _smoke_family(name: str) -> dict[str, Scenario]:
    return {suffix: SMOKE_SCENARIOS[f"{name}_{suffix}"] for suffix in families.BENIGN_SUFFIXES}


def test_smoke_set_has_100_benign_families_in_five_states() -> None:
    assert sorted(SMOKE_SCENARIOS) == sorted(
        f"{n}_{s}" for n in SMOKE_NAMES for s in families.BENIGN_SUFFIXES
    )
    assert {s.category for k, s in SMOKE_SCENARIOS.items() if k.endswith("_healthy")} == {"control"}
    assert {s.spec_version for s in SMOKE_SCENARIOS.values()} == {"0.1"}


@pytest.mark.parametrize("name", SMOKE_NAMES)
def test_smoke_files_are_what_the_generator_writes(name: str, tmp_path: Path) -> None:
    family = families.generate(name, 0, benign=True, loud_anchored=True)
    assert families.problems(_smoke_family(name)) == []
    for path in family.write(tmp_path):
        assert path.read_bytes() == (SMOKE / path.name).read_bytes(), path.name


def test_the_e2_families_carry_over_and_only_their_loud_files_change() -> None:
    e2 = sorted(p.name for p in E2.glob("*.yaml"))
    assert len(e2) == 300
    for name in e2:
        same = (E2 / name).read_bytes() == (SMOKE / name).read_bytes()
        assert same is not name.endswith("_loud.yaml"), name
    for name in SMOKE_NAMES[:60]:
        old, new = load_scenario(E2 / f"{name}_loud.yaml"), SMOKE_SCENARIOS[f"{name}_loud"]
        assert families.loud_timing(old) == families.TIMED_LOUD
        assert families.loud_timing(new) == families.ANCHORED_LOUD
        assert old.model_dump(exclude={"mission", "steps"}) == new.model_dump(exclude={"mission", "steps"})


def test_problems_reports_a_loud_fault_on_neither_timing() -> None:
    family = _smoke_family("b00")
    loud = family["loud"]
    assert loud.mission is not None
    late = loud.mission.model_copy(update={"fault_anchor_m": 1.8})
    family["loud"] = loud.model_copy(update={"mission": late})
    expected = "loud: flow off neither 2.5 s into the first move nor 2.0 m short of its target"
    assert expected in families.problems(family)
    timed = loud.mission.model_copy(update={"fault_anchor": "first_move", "fault_anchor_m": None})
    family["loud"] = loud.model_copy(update={"mission": timed})  # anchored offsets, timed anchor
    assert expected in families.problems(family)


def test_the_loud_anchor_changes_only_the_loud_file() -> None:
    timed = families.generate("b07", 0, benign=True).documents()
    anchored = families.generate("b07", 0, benign=True, loud_anchored=True).documents()
    timed_loud, anchored_loud = timed.pop("loud"), anchored.pop("loud")
    assert timed == anchored
    assert anchored_loud["mission"]["fault_anchor_m"] == families.LOUD_ANCHOR_M
    assert [s["at_s"] for s in timed_loud["steps"]] == [0.0, families.LOUD_AT_S, families.END_S]
    assert [s["at_s"] for s in anchored_loud["steps"]] == [0.0, 0.0, families.END_S]
    for doc in (timed_loud, anchored_loud):
        del doc["mission"], doc["steps"]
    assert timed_loud == anchored_loud


def test_families_command_writes_the_smoke_sets_anchored_loud_files(tmp_path: Path) -> None:
    args = ["families", "--count", "1", "--prefix", "b", "--benign", "--loud-anchor", "--out", str(tmp_path)]
    assert cli.main(args) == 0
    assert (tmp_path / "b00_loud.yaml").read_bytes() == (SMOKE / "b00_loud.yaml").read_bytes()
