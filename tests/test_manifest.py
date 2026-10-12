from pathlib import Path

import pytest

from lost_in_place import manifest
from lost_in_place.manifest import Entry, SweepSpec

ROOT = Path(__file__).parents[1]


def _spec(**overrides: object) -> SweepSpec:
    data = {
        "name": "t",
        "scenarios": ["scenarios/pilot/cross_*.yaml"],
        "reps": 2,
        "validators": ["none", "geometric"],
    }
    return SweepSpec({**data, **overrides})


def test_the_e1_pilot_sweep_crosses_every_arm() -> None:
    entries = manifest.build(SweepSpec.load(ROOT / "sweeps" / "e1-pilot.yaml"), ROOT)
    assert len(entries) == 9 * 10 * 3
    assert len({e.key for e in entries}) == len(entries)
    assert {e.validator for e in entries} == {"none", "geometric", "estimator_health"}


def test_matched_scenarios_and_arms_share_seeds() -> None:
    entries = manifest.build(_spec(), ROOT)
    assert len(entries) == 3 * 2 * 2
    seeds: dict[int, set[int]] = {}
    for e in entries:
        seeds.setdefault(e.rep, set()).add(e.seed)
    assert all(len(s) == 1 for s in seeds.values())  # healthy, loud, silent and both validators
    assert seeds[0] != seeds[1]
    assert manifest.episode_seed("cross", 0) == next(iter(seeds[0]))


def test_seeds_are_stable_across_runs() -> None:
    assert manifest.episode_seed("cross", 0) == 1638029371


def test_order_is_shuffled_but_reproducible() -> None:
    a, b = manifest.build(_spec(), ROOT), manifest.build(_spec(), ROOT)
    assert a == b
    assert a != sorted(a, key=lambda e: e.key)
    assert manifest.build(_spec(shuffle_seed=1), ROOT) != a


def test_manifest_round_trips_and_records_the_spec() -> None:
    spec = _spec()
    entries = manifest.build(spec, ROOT)
    text = manifest.dumps(spec, entries)
    assert manifest.loads(text) == entries
    assert '"spec_sha256"' in text


def test_episode_key_names_the_cell_and_seed() -> None:
    e = Entry("scenarios/pilot/cross_loud.yaml", "cross_loud", 7, 123, validator="geometric")
    assert e.key == "cross_loud/scripted-naive-geometric-O0/r007-s123"


def test_unknown_sweep_keys_and_empty_globs_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        _spec(seeds=3)
    with pytest.raises(ValueError, match="no scenarios"):
        manifest.build(_spec(scenarios=["nope/*.yaml"]), ROOT)


def test_arrays_are_split_at_the_batch_limit() -> None:
    assert manifest.array_chunks(25_000) == [(0, 10_000), (10_000, 10_000), (20_000, 5_000)]
    assert manifest.array_chunks(270) == [(0, 270)]


@pytest.mark.parametrize(
    ("outcomes", "settled"),
    [
        ([], False),
        (["boot_stall"], False),
        (["boot_stall", "completed"], True),
        (["error: OSError", "sim_stall: no clock", "timeout"], True),
        (["inject_failed: denied", "pending"], False),
    ],
)
def test_an_episode_is_settled_by_a_result_or_three_attempts(outcomes: list[str], settled: bool) -> None:
    assert manifest.settled(outcomes) is settled
