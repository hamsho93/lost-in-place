import asyncio
import json
from pathlib import Path
from typing import Any, get_args

import pytest

from lost_in_place import cli
from lost_in_place.llm import clients
from lost_in_place.llm.clients import BEDROCK_PROFILES_ENV, BedrockConverseClient, Completion, ToolCall
from lost_in_place.llm.dexi import TOOLS as DEXI_TOOLS
from lost_in_place.llm.planner import LLMPlanner
from lost_in_place.llm.prompt import (
    TOOLS,
    InvalidProposal,
    parse_tool_call,
    render_observation,
    tool_specs,
)
from lost_in_place.llm.registry import ModelSpec, get_model, load_models
from lost_in_place.planning import ActionName, EstimatorHealth, Observation, Planner, load_planner

SPEC = ModelSpec(name="test-model", provider="bedrock", model_id="us.example.model-v1", temperature=0)
OBS = Observation(goal="hover", north_m=1.234, east_m=-0.5, altitude_m=1.5, heading_deg=90, battery_pct=88)
HEALTH = EstimatorHealth(
    xy_valid=True, eph_m=0.05, flow_quality=0, innovation_check_failing=False, heading_reference=False
)


class FakeClient:
    def __init__(self, *calls: ToolCall, text: str = ""):
        self.calls = calls
        self.text = text
        self.requests: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        self.requests.append((system, user))
        return Completion(self.calls, self.text, "tool_use", 1200, 40, 900, "resolved-id")


def test_every_action_has_exactly_one_tool() -> None:
    assert sorted(t.name for t in TOOLS) == sorted(get_args(ActionName))
    assert [s["name"] for s in tool_specs()] == [t.name for t in TOOLS]


def test_tool_schemas_require_their_arguments() -> None:
    specs = {s["name"]: s["parameters"] for s in tool_specs()}
    assert specs["move"]["required"] == ["north_m", "east_m"]
    assert specs["refuse"]["required"] == ["reason"]
    assert specs["land"]["required"] == []
    assert all(p["additionalProperties"] is False for p in specs.values())


def test_health_block_only_at_o1() -> None:
    o0 = render_observation(OBS)
    o1 = render_observation(Observation(**{**OBS.__dict__, "health": HEALTH}))
    assert "Estimator health" not in o0
    assert o1.startswith(o0)
    assert "Optical flow quality: 0 of 255" in o1
    assert "Estimated position: north 1.23 m, east -0.50 m, altitude 1.50 m" in o0
    assert "- none yet" in o0


def test_parse_valid_calls() -> None:
    move = parse_tool_call("move", {"north_m": 1, "east_m": 2.5, "reason": "towards goal"})
    assert (move.name, move.args, move.reason) == ("move", {"north_m": 1.0, "east_m": 2.5}, "towards goal")
    assert parse_tool_call("land", {}).args == {}
    assert parse_tool_call("refuse", {"reason": "position looks wrong"}).reason == "position looks wrong"


@pytest.mark.parametrize(
    ("name", "args", "match"),
    [
        (None, None, "no tool call"),
        ("fly_away", {}, "unknown tool"),
        ("move", {"north_m": 1}, "missing east_m"),
        ("move", {"north_m": "1", "east_m": 0}, "must be a number"),
        ("hold", {"seconds": True}, "must be a number"),
        ("land", {"speed": 1}, "unexpected arguments"),
        ("refuse", {}, "missing reason"),
    ],
)
def test_parse_rejects_malformed_calls(name: str | None, args: dict[str, Any] | None, match: str) -> None:
    with pytest.raises(InvalidProposal, match=match):
        parse_tool_call(name, args)


def test_planner_returns_action_and_records_decision() -> None:
    client = FakeClient(ToolCall("hold", {"seconds": 5, "reason": "settle"}))
    planner = LLMPlanner(SPEC, client=client)
    action = asyncio.run(planner.propose(OBS))
    assert (action.name, action.args) == ("hold", {"seconds": 5.0})
    [decision] = planner.decisions
    assert decision.model_id == "resolved-id"
    assert decision.input_tokens == 1200
    assert decision.action == {"name": "hold", "args": {"seconds": 5.0}, "reason": "settle"}
    assert decision.user_prompt == client.requests[0][1]
    assert len(decision.prompt_sha256) == 16


@pytest.mark.parametrize(
    "calls", [(), (ToolCall("land", {}), ToolCall("hold", {"seconds": 1}))], ids=["none", "two"]
)
def test_planner_records_invalid_replies(calls: tuple[ToolCall, ...]) -> None:
    planner = LLMPlanner(SPEC, client=FakeClient(*calls, text="I think we should land."))
    with pytest.raises(InvalidProposal):
        asyncio.run(planner.propose(OBS))
    assert planner.decisions[0].error
    assert planner.decisions[0].action is None
    assert planner.decisions[0].text == "I think we should land."


def test_bedrock_request_uses_tagged_profile_when_mapped() -> None:
    env = {BEDROCK_PROFILES_ENV: json.dumps({"us.example.model-v1": "arn:aws:bedrock:profile/abc"})}
    client = BedrockConverseClient(SPEC, client=object(), env=env)
    request = client.request("system", "user", tool_specs())
    assert request["modelId"] == "arn:aws:bedrock:profile/abc"
    assert request["toolConfig"]["toolChoice"] == {"any": {}}
    assert request["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}
    assert request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert BedrockConverseClient(SPEC, client=object(), env={}).model_id == "us.example.model-v1"


def test_bedrock_response_parsing() -> None:
    class Stub:
        def converse(self, **request: Any) -> dict[str, Any]:
            self.request = request
            return {
                "output": {
                    "message": {
                        "content": [
                            {"text": "Holding."},
                            {"toolUse": {"toolUseId": "t1", "name": "hold", "input": {"seconds": 3}}},
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 900, "outputTokens": 30},
                "metrics": {"latencyMs": 1500},
            }

    completion = BedrockConverseClient(SPEC, client=Stub(), env={}).complete("s", "u", tool_specs())
    assert completion.tool_calls == (ToolCall("hold", {"seconds": 3}),)
    assert (completion.text, completion.input_tokens, completion.latency_ms) == ("Holding.", 900, 1500)


def test_registry_ships_a_claude_arm_on_a_known_provider() -> None:
    models = load_models()
    assert "claude-sonnet-4-6" in models
    assert all(spec.provider in clients.CLIENTS for spec in models.values())
    with pytest.raises(LookupError, match="known: claude-sonnet-4-6"):
        get_model("gpt-unknown")


def test_registry_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("m:\n  provider: bedrock\n  model_id: x\n  temprature: 0\n")
    with pytest.raises(ValueError, match="temprature"):
        load_models(path)


def test_new_provider_needs_only_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(clients.CLIENTS, "fake", lambda spec: FakeClient(ToolCall("land", {})))
    spec = ModelSpec(name="other", provider="fake", model_id="other-v1")
    assert asyncio.run(LLMPlanner(spec).propose(OBS)).name == "land"
    with pytest.raises(LookupError, match="no client for provider 'nope'"):
        clients.make_client(ModelSpec(name="x", provider="nope", model_id="x"))


def test_llm_planner_is_installed_as_an_entry_point() -> None:
    planner = load_planner("llm", model="claude-sonnet-4-6")
    assert isinstance(planner, Planner)


def test_propose_command_prints_the_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(clients.CLIENTS, "bedrock", lambda spec: FakeClient(ToolCall("land", {})))
    assert cli.main(["propose", "claude-sonnet-4-6", "--health", "degraded"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["action"]["name"] == "land"
    assert "Optical flow quality: 0 of 255" in decision["user_prompt"]


def test_a_converse_call_without_tools_has_no_tool_config() -> None:
    spec = get_model("claude-sonnet-4-6")
    client = BedrockConverseClient(spec, client=object(), env={})
    assert "toolConfig" not in client.request("", "hi", [])
    with_tools = client.request("", "hi", DEXI_TOOLS)
    assert list(with_tools) == ["modelId", "messages", "toolConfig", "inferenceConfig"]
    assert len(with_tools["toolConfig"]["tools"]) == len(DEXI_TOOLS)


def test_an_openai_compatible_call_without_tools_has_no_tool_fields() -> None:
    spec = ModelSpec(name="m", provider="openai_compatible", model_id="m", base_url="http://x", temperature=0)
    client = clients.OpenAICompatibleClient(spec, env={})
    assert list(client.request("", "hi", [])) == ["model", "messages", "max_tokens", "temperature"]
    keys = ["model", "messages", "tools", "tool_choice", "max_tokens", "temperature"]
    assert list(client.request("", "hi", DEXI_TOOLS)) == keys
