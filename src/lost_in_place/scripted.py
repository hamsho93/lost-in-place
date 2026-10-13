"""Scripted planner arms: fixed baselines for the manipulation check and the validator arms.

- `scripted-naive` flies the mission's waypoints, holds and lands, whatever the estimator says.
  If the validator rejects a move, it gives up on that waypoint and goes on to the next one, so
  one rejected leg is one rejected proposal rather than one per remaining planner call.
- `scripted-cautious` does the same, but lands as soon as the estimator health it is shown looks
  bad. At observation level O0 it is shown none, so it behaves like the naive arm; the gap
  between the two at O1 is what health information is worth to a planner that uses it.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from lost_in_place.planning import REJECTED, Action, EstimatorHealth, Observation
from lost_in_place.scenario import Mission

ARRIVED_M = 0.3


@dataclass(frozen=True)
class HealthLimits:
    max_eph_m: float = 1.0
    min_flow_quality: float = 50.0


class ScriptedNaive:
    def __init__(self, mission: Mission, think_s: float = 0.0):
        self.mission = mission
        # Wall seconds to wait before answering, to stand in for an LLM call when testing the harness.
        self.think_s = think_s
        self._leg = 0
        self._held = False
        self._moving_to: int | None = None

    def _next(self, obs: Observation) -> Action:
        if self._moving_to is not None and obs.history and REJECTED in obs.history[-1]:
            self._leg = max(self._leg, self._moving_to + 1)
        self._moving_to = None
        while self._leg < len(self.mission.waypoints):
            wp = self.mission.waypoints[self._leg]
            if math.hypot(wp.north_m - obs.north_m, wp.east_m - obs.east_m) > ARRIVED_M:
                self._moving_to = self._leg
                return Action(
                    "move", {"north_m": wp.north_m, "east_m": wp.east_m}, f"waypoint {self._leg + 1}"
                )
            self._leg += 1
        if not self._held and self.mission.hold_s > 0:
            self._held = True
            return Action("hold", {"seconds": self.mission.hold_s}, "hold at the last waypoint")
        return Action("land", {}, "mission done")

    async def think(self) -> None:
        if self.think_s > 0:
            await asyncio.sleep(self.think_s)

    async def propose(self, observation: Observation) -> Action:
        await self.think()
        return self._next(observation)


class ScriptedCautious(ScriptedNaive):
    def __init__(self, mission: Mission, limits: HealthLimits | None = None, think_s: float = 0.0):
        super().__init__(mission, think_s)
        self.limits = limits or HealthLimits()

    def concern(self, health: EstimatorHealth | None) -> str:
        if health is None:
            return ""
        if not health.xy_valid:
            return "position estimate invalid"
        if health.innovation_check_failing:
            return "estimator innovation check failing"
        if health.eph_m > self.limits.max_eph_m:
            return f"position uncertainty {health.eph_m:.2f} m"
        if health.flow_quality < self.limits.min_flow_quality:
            return f"optical flow quality {health.flow_quality:.0f}"
        return ""

    async def propose(self, observation: Observation) -> Action:
        await self.think()
        concern = self.concern(observation.health)
        if concern:
            return Action("land", {}, concern)
        return self._next(observation)


def make_naive(mission: Mission, think_s: str | float = 0.0) -> ScriptedNaive:
    return ScriptedNaive(mission, float(think_s))


def make_cautious(mission: Mission, think_s: str | float = 0.0) -> ScriptedCautious:
    return ScriptedCautious(mission, think_s=float(think_s))
