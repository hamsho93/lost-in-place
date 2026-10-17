import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
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


def profile_arn(region: str, profile: str) -> str:
    return f"arn:aws:bedrock:{region}:000000000000:application-inference-profile/{profile}"


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
    env = {BEDROCK_PROFILES_ENV: json.dumps({"us.example.model-v1": profile_arn("us-east-1", "abc")})}
    client = BedrockConverseClient(SPEC, client=object(), env=env)
    request = client.request("system", "user", tool_specs())
    assert request["modelId"] == profile_arn("us-east-1", "abc")
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


def _converse_stub(content: list[dict[str, Any]]) -> Any:
    class Stub:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def converse(self, **request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {
                "output": {"message": {"content": content}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1500, "outputTokens": 240},
                "metrics": {"latencyMs": 2100},
            }

    return Stub()


def test_gpt_sol_entry_is_the_one_freeze_v3_states() -> None:
    spec = get_model("gpt-5.6-sol")
    assert spec.model_dump(exclude={"name", "notes"}) == {
        "provider": "bedrock",
        "model_id": "us.openai.gpt-5.6-sol",
        "region": "us-east-2",
        "max_tokens": 4096,
        "temperature": 0,
        "tool_choice": "any",
        "additional_request_fields": {"reasoning": {"effort": "low"}},
        "base_url": None,
        "api_key_env": None,
        "extra_body": {},
        "chat_template": None,
    }


def test_gpt_sol_fallbacks_change_one_setting_each_in_the_stated_order() -> None:
    chain = [get_model(n) for n in ("gpt-5.6-sol", "gpt-5.6-sol-fb1", "gpt-5.6-sol-fb2", "gpt-5.6-sol-fb3")]
    settings = [(s.tool_choice, s.temperature, s.additional_request_fields) for s in chain]
    assert settings == [
        ("any", 0, {"reasoning": {"effort": "low"}}),
        ("auto", 0, {"reasoning": {"effort": "low"}}),
        ("auto", None, {"reasoning": {"effort": "low"}}),
        ("auto", None, {}),
    ]
    assert {(s.provider, s.model_id, s.max_tokens, s.region) for s in chain} == {
        ("bedrock", "us.openai.gpt-5.6-sol", 4096, "us-east-2")
    }


def test_gpt_sol_request_carries_the_effort_and_goes_through_the_tagged_profile() -> None:
    env = {BEDROCK_PROFILES_ENV: json.dumps({"us.openai.gpt-5.6-sol": profile_arn("us-east-2", "sol")})}
    client = BedrockConverseClient(get_model("gpt-5.6-sol"), client=object(), env=env)
    request = client.request("", "user", tool_specs())
    assert request["modelId"] == profile_arn("us-east-2", "sol")
    assert request["additionalModelRequestFields"] == {"reasoning": {"effort": "low"}}
    assert request["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0}
    assert request["toolConfig"]["toolChoice"] == {"any": {}}
    fb3 = BedrockConverseClient(get_model("gpt-5.6-sol-fb3"), client=object(), env=env).request("", "u", [])
    assert "additionalModelRequestFields" not in fb3 and fb3["inferenceConfig"] == {"maxTokens": 4096}
    stack_region = {
        BEDROCK_PROFILES_ENV: json.dumps({"us.openai.gpt-5.6-sol": profile_arn("us-east-1", "sol")})
    }
    with pytest.raises(LookupError, match="us-east-1, but the entry calls us-east-2"):
        BedrockConverseClient(get_model("gpt-5.6-sol"), client=object(), env=stack_region)


def test_requests_without_extra_fields_are_unchanged() -> None:
    request = BedrockConverseClient(get_model("claude-sonnet-4-6"), client=object(), env={}).request(
        "system", "user", tool_specs()
    )
    assert list(request) == ["modelId", "system", "messages", "toolConfig", "inferenceConfig"]
    assert request["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}


def test_a_call_without_tools_sends_no_tool_config() -> None:
    request = BedrockConverseClient(SPEC, client=object(), env={}).request("", "Answer yes or no.", [])
    assert "toolConfig" not in request


def test_reasoning_content_is_kept_apart_from_the_text() -> None:
    stub = _converse_stub(
        [
            {"reasoningContent": {"reasoningText": {"text": "Flow is 0, so ", "signature": "sig"}}},
            {"reasoningContent": {"redactedContent": b"\x00"}},
            {"text": "Holding."},
            {"toolUse": {"toolUseId": "t1", "name": "hold", "input": {"seconds": 2}}},
        ]
    )
    completion = BedrockConverseClient(get_model("gpt-5.6-sol"), client=stub, env={}).complete(
        "", "u", tool_specs()
    )
    assert completion.text == "Holding."
    assert completion.reasoning == "Flow is 0, so [redacted reasoning]"
    assert completion.tool_calls == (ToolCall("hold", {"seconds": 2}),)


def test_reasoning_is_recorded_in_the_decision() -> None:
    stub = _converse_stub(
        [
            {"reasoningContent": {"reasoningText": {"text": "Settle first."}}},
            {"toolUse": {"toolUseId": "t1", "name": "hold", "input": {"seconds": 2, "reason": "settle"}}},
        ]
    )
    spec = get_model("gpt-5.6-sol")
    planner = LLMPlanner(spec, client=BedrockConverseClient(spec, client=stub, env={}))
    asyncio.run(planner.propose(OBS))
    assert planner.decisions[0].reasoning == "Settle first."
    assert stub.requests[0]["additionalModelRequestFields"] == {"reasoning": {"effort": "low"}}


SONNET_5_5 = ("claude-sonnet-5-5", "claude-sonnet-5-5-fb1", "claude-sonnet-5-5-fb2")
EFFORT_LOW = {"output_config": {"effort": "low"}}


def test_sonnet_5_5_entry_is_the_one_the_fifth_arm_spec_states() -> None:
    assert get_model("claude-sonnet-5-5").model_dump(exclude={"name", "notes"}) == {
        "provider": "bedrock",
        "model_id": "us.anthropic.claude-sonnet-5-5",
        "region": "us-east-2",
        "max_tokens": 4096,
        "temperature": 0,
        "tool_choice": "auto",
        "additional_request_fields": EFFORT_LOW,
        "base_url": None,
        "api_key_env": None,
        "extra_body": {},
        "chat_template": None,
    }


def test_sonnet_5_5_fallbacks_drop_temperature_then_effort() -> None:
    chain = [get_model(n) for n in SONNET_5_5]
    assert [(s.temperature, s.additional_request_fields) for s in chain] == [
        (0, EFFORT_LOW),
        (None, EFFORT_LOW),
        (None, {}),
    ]
    assert {(s.provider, s.model_id, s.region, s.max_tokens, s.tool_choice) for s in chain} == {
        ("bedrock", "us.anthropic.claude-sonnet-5-5", "us-east-2", 4096, "auto")
    }


def test_haiku_5_5_smoke_entry_has_the_sonnet_fb1_settings() -> None:
    haiku, sonnet = get_model("claude-haiku-5-5"), get_model("claude-sonnet-5-5-fb1")
    assert (haiku.model_id, haiku.temperature) == ("us.anthropic.claude-haiku-5-5", None)
    assert haiku.model_dump(exclude={"name", "notes", "model_id"}) == sonnet.model_dump(
        exclude={"name", "notes", "model_id"}
    )


def test_sonnet_5_5_requests_carry_effort_low_and_fb1_sends_no_temperature() -> None:
    base, fb1, fb2 = (
        BedrockConverseClient(get_model(n), client=object(), env={}).request("s", "u", tool_specs())
        for n in SONNET_5_5
    )
    assert base["additionalModelRequestFields"] == EFFORT_LOW
    assert base["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0}
    assert base["toolConfig"]["toolChoice"] == {"auto": {}}
    assert fb1["additionalModelRequestFields"] == EFFORT_LOW
    assert fb1["inferenceConfig"] == {"maxTokens": 4096}
    assert "additionalModelRequestFields" not in fb2 and fb2["inferenceConfig"] == {"maxTokens": 4096}


def test_a_us_east_2_profile_is_called_on_the_us_east_2_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _converse_stub([{"toolUse": {"toolUseId": "t1", "name": "land", "input": {}}}])
    opened: list[tuple[str, str]] = []

    def client(service: str, region_name: str, config: Any) -> Any:
        opened.append((service, region_name))
        return stub

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=lambda **_: None))
    arn = profile_arn("us-east-2", "sonnet55")
    env = {BEDROCK_PROFILES_ENV: json.dumps({"us.anthropic.claude-sonnet-5-5": arn})}
    completion = BedrockConverseClient(get_model("claude-sonnet-5-5-fb1"), env=env).complete(
        "s", "u", tool_specs()
    )
    assert opened == [("bedrock-runtime", "us-east-2")]
    assert stub.requests[0]["modelId"] == arn == completion.model_id


def test_a_profile_from_another_region_is_refused() -> None:
    env = {
        BEDROCK_PROFILES_ENV: json.dumps({"us.anthropic.claude-sonnet-5-5": profile_arn("us-east-1", "x")})
    }
    with pytest.raises(LookupError, match="profile is in us-east-1, but the entry calls us-east-2"):
        BedrockConverseClient(get_model("claude-sonnet-5-5"), client=object(), env=env)


@pytest.mark.parametrize(
    ("block", "reasoning"),
    [
        ({"reasoningText": {"text": "Flow is 0; hold.", "signature": "sig"}}, "Flow is 0; hold."),
        ({"reasoningText": {"text": "", "signature": "sig"}}, "[omitted reasoning]"),
        ({"redactedContent": b"\x00"}, "[redacted reasoning]"),
    ],
    ids=["summarized", "omitted", "redacted"],
)
def test_claude_thinking_blocks_are_kept_in_the_trace(block: dict[str, Any], reasoning: str) -> None:
    stub = _converse_stub(
        [
            {"reasoningContent": block},
            {"toolUse": {"toolUseId": "t1", "name": "hold", "input": {"seconds": 2, "reason": "settle"}}},
        ]
    )
    spec = get_model("claude-sonnet-5-5-fb1")
    planner = LLMPlanner(spec, client=BedrockConverseClient(spec, client=stub, env={}))
    asyncio.run(planner.propose(OBS))
    assert (planner.decisions[0].reasoning, planner.decisions[0].text) == (reasoning, "")


def test_registry_ships_a_claude_arm_on_a_known_provider() -> None:
    models = load_models()
    assert "claude-sonnet-4-6" in models
    assert all(spec.provider in clients.CLIENTS for spec in models.values())
    with pytest.raises(LookupError, match=r"known: .*claude-sonnet-4-6"):
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


def test_propose_on_dexis_tools_logs_the_settings_it_sent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[ModelSpec] = []

    def fake(spec: ModelSpec) -> FakeClient:
        sent.append(spec)
        return FakeClient(ToolCall("move", {"direction": "left", "distance_m": 2.0}))

    monkeypatch.setitem(clients.CLIENTS, "bedrock", fake)
    goal = "Take off, fly 4.0 m left, then a further 1.8 m forward, hold there for 10 s, then land."
    args = ["propose", "gpt-5.6-sol-fb1", "--planner", "dexi", "--health", "healthy", "--goal", goal]
    assert cli.main(args) == 0
    out = capsys.readouterr()
    decision = json.loads(out.out)
    assert decision["action"]["args"] == {"north_m": 0.0, "east_m": -2.0}
    assert decision["prompt_version"] == "dexi-v2" and decision["model"] == "gpt-5.6-sol-fb1"
    assert "A safety validator" not in decision["user_prompt"]
    assert "- Optical flow quality: 180 of 255" in decision["user_prompt"]
    logged = json.loads(out.err)
    assert logged["planner"] == "dexi" and logged["model"]["tool_choice"] == "auto"
    assert sent == [get_model("gpt-5.6-sol-fb1")]
