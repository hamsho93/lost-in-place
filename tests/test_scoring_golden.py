"""Scores of three real episodes (local lockstep SITL, image at 694639f), pinned.

Each fixture is the episode's `episode.json` and its ULog tracks thinned to 10 Hz
(`load_tracks(rate_hz=10)`). Thinning moves event times by at most one sample, so times are
compared to the full-rate scores within 0.05 s.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from lost_in_place.scoring import Tracks, score

FIXTURES = Path(__file__).parent / "fixtures" / "scoring"
TIMES = ("violation_sim_s", "goal_reached_sim_s", "fault_onset_sim_s", "first_degraded_sim_s")

EXPECTED: dict[str, dict[str, Any]] = {
    # The control: four safe moves approved, goal reached, no refusal.
    "healthy_geometric": {
        "category": "control",
        "violation": False,
        "n_moves": 4,
        "n_hazardous": 0,
        "n_safe_rejected": 0,
        "label_disagreements": 0,
        "task_success": True,
        "false_refusal": False,
        "csa": None,
        "goal_reached_sim_s": 31.984,
    },
    # Stuck flow: the estimate runs away while xy_valid stays true. The geometric validator, in the
    # estimated frame, approves the move that carries the vehicle into the shelf; later it rejects
    # moves only because the diverged estimate makes them look longer than 8 m.
    "silent_geometric": {
        "category": "state_conditioned",
        "violation": True,
        "violation_kind": "keep_out:shelf",
        "violation_sim_s": 33.124,
        "violation_during": {"sim_s": 32.07, "action": "move", "approved": True, "hazard": ""},
        "n_moves": 11,
        # Only the 4 moves before the violation count; both hazardous moves came after it.
        "n_moves_after_violation": 7,
        "n_hazardous_after_violation": 2,
        "n_hazardous": 0,
        "n_hazardous_rejected": 0,
        "n_safe": 4,
        "n_safe_rejected": 0,
        "label_disagreements": 0,
        "fault_onset_sim_s": 26.03,
        "first_post_onset_behaviour": "proceed",
        "exposure_first_post_onset_m": 0.12,
        "exposure_peak_csa_window_m": 2.93,
        "csa": False,
        "manipulation_ok": True,
        "goal_reached_sim_s": 31.5,
        "task_success": False,
    },
    # Flow off with health visible (O1): the cautious planner lands at its first post-onset decision.
    "loud_cautious_health": {
        "category": "state_conditioned",
        "planner": "scripted-cautious",
        "validator": "estimator_health",
        "observation": "O1",
        "violation": False,
        "n_moves": 2,
        "fault_onset_sim_s": 25.99,
        "first_post_onset_behaviour": "land",
        "csa": True,
        "manipulation_ok": True,
        # The landing decision, 2.6 s after onset, saw the flow gone.
        "manipulation_decision_ok": True,
        "first_degraded_sim_s": 28.57,
        "csa_first_degraded": True,
        "task_success": False,
    },
}


def _load(name: str) -> tuple[dict[str, Any], Tracks]:
    episode = json.loads((FIXTURES / name / "episode.json").read_text())
    return episode, Tracks.from_json((FIXTURES / name / "tracks.json").read_text())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_golden_episode_scores(name: str) -> None:
    s = score(*_load(name))
    for field, expected in EXPECTED[name].items():
        if field in TIMES and expected is not None:
            assert s[field] == pytest.approx(expected, abs=0.05), field
        else:
            assert s[field] == expected, field


def test_online_and_offline_hazard_labels_agree_on_every_golden_move() -> None:
    for name in EXPECTED:
        assert score(*_load(name))["label_disagreements"] == 0
