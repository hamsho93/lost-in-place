"""`scripted-rule-dexi` on the real ladder renders: the scripted client behind `DexiPlanner` at every level
of `dexi.LADDER`, on the E5 canary families' loud, brief and healthy files in both wording templates,
reads the legs, the health block and the moves flown from what the adapter actually renders."""

import asyncio
from pathlib import Path

import pytest

from lost_in_place.llm import dexi
from lost_in_place.llm.scripted_dexi import Health, ScriptedRuleClient, flown, health, legs, step_plan
from lost_in_place.planning import Action, EstimatorHealth, Observation, Outcome, Verdict, shows_health
from lost_in_place.scenario import Scenario, load_scenario

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = [f"k{i:03d}" for i in range(6)]
HEALTHY = EstimatorHealth(True, 0.31, 176.4, innovation_check_failing=False, heading_reference=False)
NO_FLOW = EstimatorHealth(True, 0.64, 0.0, innovation_check_failing=False, heading_reference=False)
NOT_VALID = EstimatorHealth(False, 0.9, 171.0, innovation_check_failing=True, heading_reference=False)
WORDS = {(1, 0): "forward", (-1, 0): "back", (0, 1): "right", (0, -1): "left"}


def _legs(scenario: Scenario) -> list[tuple[str, float]]:
    assert scenario.mission is not None
    (n1, e1), (n2, e2) = ((w.north_m, w.east_m) for w in scenario.mission.waypoints)
    out = []
    for dn, de in ((n1, e1), (n2 - n1, e2 - e1)):
        sign = (round(dn / abs(dn)) if dn else 0, round(de / abs(de)) if de else 0)
        out.append((WORDS[sign], round(abs(dn) + abs(de), 3)))
    return out


class Flight:
    """The adapter's loop without a simulator: each move arrives at its target, each hold is done."""

    def __init__(self, scenario: Scenario, level: str, cadence: str):
        observation, prompt = dexi.LADDER[level]
        self.planner = dexi.make_planner(
            f"rule-{cadence}", scenario, prompt=prompt, driver=dexi.DEXI_DRIVER, observation=observation
        )
        assert isinstance(self.planner.client, ScriptedRuleClient)
        assert scenario.mission is not None
        self.goal = scenario.mission.instruction
        self.shows = shows_health(observation)
        self.at = (0.0, 0.0)

    def observe(self, h: EstimatorHealth) -> Observation:
        return Observation(self.goal, *self.at, 1.5, 0.0, 90.0, health=h if self.shows else None)

    def step(self, h: EstimatorHealth) -> Action:
        action = asyncio.run(self.planner.propose(self.observe(h)))
        if action.name == "move":
            self.at = (action.args["north_m"], action.args["east_m"])
        status = "arrived" if action.name == "move" else "done"
        self.planner.outcome(Outcome(action, Verdict(True), status, self.observe(h), epochs=1))
        return action

    @property
    def prompt(self) -> str:
        return self.planner.decisions[-1].user_prompt


def _shown(h: EstimatorHealth) -> Health:
    return Health(flow_quality=float(f"{h.flow_quality:.0f}"), xy_valid=h.xy_valid)


@pytest.mark.parametrize("cadence", ["2m", "leg"])
@pytest.mark.parametrize("level", list(dexi.LADDER))
@pytest.mark.parametrize("template", ["t0", "t1"])
def test_the_rule_reads_every_ladder_render(template: str, level: str, cadence: str) -> None:
    for family in FAMILIES:
        for state in ("loud", "brief", "healthy"):
            scenario = load_scenario(
                ROOT / "scenarios" / "e5" / template / "canary" / f"{family}_{state}.yaml"
            )
            flight = Flight(scenario, level, cadence)
            first = flight.step(HEALTHY)
            assert legs(flight.prompt) == _legs(scenario), (family, state)
            plan = step_plan(flight.prompt, cadence)
            assert plan[0][0] == _legs(scenario)[0][0] and first.name == "move"
            assert health(flight.prompt) == (_shown(HEALTHY) if flight.shows else None)
            degraded = {"loud": NO_FLOW, "brief": NO_FLOW, "healthy": HEALTHY}[state]
            second = flight.step(degraded)
            assert flown(flight.prompt) == [
                ("take_off", "alt_m=1.5"),
                ("move", f"direction={plan[0][0]}, distance_m={plan[0][1]}"),
            ]
            if not flight.shows or state == "healthy":
                assert second.name == "move", (family, state, level)
                assert health(flight.prompt) == (_shown(HEALTHY) if flight.shows else None)
                continue
            assert health(flight.prompt) == _shown(NO_FLOW)
            assert (second.name, second.args) == ("hold", {"seconds": 2}), (family, state, level)
            third = flight.step(NO_FLOW if state == "loud" else HEALTHY)
            assert third.name == ("land" if state == "loud" else "move"), (family, state, level)


@pytest.mark.parametrize("level", [k for k, (obs, _) in dexi.LADDER.items() if shows_health(obs)])
def test_an_invalid_estimate_with_good_flow_stops_the_rule_at_every_health_level(level: str) -> None:
    for template in ("t0", "t1"):
        scenario = load_scenario(ROOT / "scenarios" / "e5" / template / "canary" / "k000_loud.yaml")
        flight = Flight(scenario, level, "2m")
        flight.step(HEALTHY)
        assert flight.step(NOT_VALID).name == "hold"
        assert health(flight.prompt) == _shown(NOT_VALID)
        assert flight.step(NOT_VALID).name == "land"
