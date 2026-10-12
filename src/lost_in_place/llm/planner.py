"""A `Planner` backed by any model in the registry.

Each call to `propose` is one self-contained request: the observation (including its history)
is rendered into a single user message, so a decision depends only on what the benchmark
showed the model. Every decision is kept in `decisions` for the episode's trace.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from lost_in_place.llm.clients import ChatClient, make_client
from lost_in_place.llm.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    InvalidProposal,
    parse_tool_call,
    prompt_hash,
    render_observation,
    tool_specs,
)
from lost_in_place.llm.registry import ModelSpec, get_model
from lost_in_place.planning import Action, Observation


@dataclass
class Decision:
    model: str
    model_id: str
    prompt_version: str
    prompt_sha256: str
    user_prompt: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    action: dict[str, Any] | None = None
    error: str = ""


class LLMPlanner:
    def __init__(self, spec: ModelSpec, client: ChatClient | None = None):
        self.spec = spec
        self.client = client if client is not None else make_client(spec)
        self.decisions: list[Decision] = []

    async def propose(self, observation: Observation) -> Action:
        user = render_observation(observation)
        completion = await asyncio.to_thread(self.client.complete, SYSTEM_PROMPT, user, tool_specs())
        decision = Decision(
            model=self.spec.name,
            model_id=completion.model_id,
            prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_hash(SYSTEM_PROMPT, user),
            user_prompt=user,
            tool_calls=[{"name": c.name, "args": c.args} for c in completion.tool_calls],
            text=completion.text,
            stop_reason=completion.stop_reason,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=completion.latency_ms,
        )
        self.decisions.append(decision)
        try:
            if len(completion.tool_calls) > 1:
                raise InvalidProposal(f"{len(completion.tool_calls)} tool calls, expected one")
            call = completion.tool_calls[0] if completion.tool_calls else None
            action = parse_tool_call(call.name if call else None, call.args if call else None)
        except InvalidProposal as exc:
            decision.error = str(exc)
            raise
        decision.action = {"name": action.name, "args": action.args, "reason": action.reason}
        return action


def make_planner(model: str) -> LLMPlanner:
    """Entry point for `load_planner("llm", model=<name in models.yaml>)`."""
    return LLMPlanner(get_model(model))
