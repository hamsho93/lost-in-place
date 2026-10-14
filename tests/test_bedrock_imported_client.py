import io
import json
from pathlib import Path
from typing import Any

import pytest

from lost_in_place.llm.clients import BedrockImportedClient, ToolCall, make_client
from lost_in_place.llm.prompt import tool_specs
from lost_in_place.llm.registry import ModelSpec, get_model

FIXTURES = Path(__file__).parent / "fixtures"
ARN = "arn:aws:bedrock:us-east-1:000000000000:imported-model/abc123def456"
SPEC = ModelSpec(
    name="qwen-imported-test",
    provider="bedrock_imported",
    model_id="qwen-test-import",
    chat_template="qwen3-thinking-2507",
    max_tokens=8192,
    temperature=0,
)


class ClientError(Exception):
    """Shaped like botocore's ClientError, which carries the service's error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Control:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def get_imported_model(self, modelIdentifier: str) -> dict[str, Any]:
        self.lookups.append(modelIdentifier)
        return {"modelArn": ARN}


class Runtime:
    """Stands in for bedrock-runtime: records each call and replays canned replies."""

    def __init__(self, *replies: dict[str, Any] | Exception):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({**kwargs, "body": json.loads(kwargs["body"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"body": io.BytesIO(json.dumps(reply).encode())}


def completion(text: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "object": "text_completion",
        "choices": [{"index": 0, "text": text, "finish_reason": finish}],
        "usage": {"prompt_tokens": 830, "completion_tokens": 2100},
    }


def client(runtime: Runtime, control: Control | None = None) -> BedrockImportedClient:
    return BedrockImportedClient(SPEC, runtime=runtime, control=control or Control(), sleep=lambda s: None)


MOVE = '<tool_call>\n{"name": "move", "arguments": {"north_m": 1.0, "east_m": 0.5}}\n</tool_call>'


@pytest.mark.parametrize(
    ("template", "fixture"),
    [
        ("qwen3-thinking-2507", "qwen3_thinking_render.txt"),
        ("qwen3-instruct-2507", "qwen3_instruct_render.txt"),
    ],
)
def test_prompt_matches_reference_rendering_byte_for_byte(template: str, fixture: str) -> None:
    request = json.loads((FIXTURES / "qwen3_thinking_request.json").read_text())
    spec = SPEC.model_copy(update={"chat_template": template})
    imported = BedrockImportedClient(spec, runtime=Runtime(), control=Control())
    rendered = imported.render(request["system"], request["user"], request["tools"])
    # Both captured with transformers 4.57.6 apply_chat_template; the Thinking one also matches
    # llama-server --jinja (b11561) via /apply-template.
    assert rendered == (FIXTURES / fixture).read_text()


def test_a_prompt_without_a_system_message_has_no_empty_system_turn() -> None:
    spec = SPEC.model_copy(update={"chat_template": "qwen3-instruct-2507"})
    rendered = BedrockImportedClient(spec, runtime=Runtime(), control=Control()).render("", "u", tool_specs())
    assert rendered.count("<|im_start|>system\n") == 1
    assert rendered.endswith("<|im_start|>user\nu<|im_end|>\n<|im_start|>assistant\n")


def test_a_model_that_does_not_think_answers_directly() -> None:
    spec = SPEC.model_copy(update={"chat_template": "qwen3-instruct-2507"})
    imported = BedrockImportedClient(spec, runtime=Runtime(completion(MOVE)), control=Control())
    result = imported.complete("", "u", tool_specs())
    assert result.tool_calls == (ToolCall(name="move", args={"north_m": 1.0, "east_m": 0.5}),)
    assert result.text == MOVE


def test_sends_the_raw_prompt_with_the_registry_settings() -> None:
    runtime = Runtime(completion("Head north-east.\n</think>\n\n" + MOVE))
    client(runtime).complete("system text", "user text", tool_specs())
    [call] = runtime.calls
    assert call["modelId"] == ARN
    assert call["body"]["max_tokens"] == 8192
    assert call["body"]["temperature"] == 0
    assert call["body"]["prompt"].endswith("<|im_start|>assistant\n<think>\n")
    assert "user text" in call["body"]["prompt"]


def test_reads_the_tool_call_after_the_thought() -> None:
    result = client(Runtime(completion("Head north-east.\n</think>\n\n" + MOVE))).complete(
        "s", "u", tool_specs()
    )
    assert result.tool_calls == (ToolCall(name="move", args={"north_m": 1.0, "east_m": 0.5}),)
    assert result.text.startswith("<think>\nHead north-east.")
    assert (result.stop_reason, result.input_tokens, result.output_tokens) == ("stop", 830, 2100)


def test_a_tool_call_inside_the_thought_is_not_an_answer() -> None:
    text = "Maybe " + MOVE + " but the estimate is bad, so refuse.\n</think>\n\n"
    result = client(Runtime(completion(text))).complete("s", "u", tool_specs())
    assert result.tool_calls == ()


def test_a_thought_cut_off_by_the_token_limit_has_no_answer() -> None:
    result = client(Runtime(completion("I could call " + MOVE + " and then", "length"))).complete(
        "s", "u", tool_specs()
    )
    assert result.tool_calls == ()
    assert result.stop_reason == "length"


def test_waits_for_a_cold_model_then_succeeds() -> None:
    runtime = Runtime(
        ClientError("ModelNotReadyException"),
        ClientError("ThrottlingException"),
        completion("ok\n</think>\n\n" + MOVE),
    )
    assert client(runtime).complete("s", "u", tool_specs()).tool_calls
    assert len(runtime.calls) == 3


def test_waits_out_a_slow_restore_then_gives_up() -> None:
    sleeps: list[float] = []
    slow = Runtime(*[ClientError("ModelNotReadyException")] * 30, completion("ok\n</think>\n\n" + MOVE))
    imported = BedrockImportedClient(SPEC, runtime=slow, control=Control(), sleep=sleeps.append)
    assert imported.complete("s", "u", tool_specs()).tool_calls
    assert sum(sleeps) > 27 * 60

    sleeps.clear()
    never = Runtime(*[ClientError("ModelNotReadyException")] * 50)
    imported = BedrockImportedClient(SPEC, runtime=never, control=Control(), sleep=sleeps.append)
    with pytest.raises(ClientError):
        imported.complete("s", "u", tool_specs())
    assert sum(sleeps) <= BedrockImportedClient.restore_wait_s
    assert sum(sleeps) > BedrockImportedClient.restore_wait_s - 60


def test_other_errors_are_not_retried() -> None:
    runtime = Runtime(ClientError("ValidationException"))
    with pytest.raises(ClientError):
        client(runtime).complete("s", "u", tool_specs())
    assert len(runtime.calls) == 1


def test_looks_the_arn_up_once_and_keeps_the_account_out_of_traces() -> None:
    control = Control()
    imported = client(
        Runtime(completion("a\n</think>\n" + MOVE), completion("b\n</think>\n" + MOVE)), control
    )
    first = imported.complete("s", "u", tool_specs())
    imported.complete("s", "u", tool_specs())
    assert control.lookups == ["qwen-test-import"]
    assert first.model_id == "qwen-test-import@abc123def456"
    assert "000000000000" not in first.model_id


def test_needs_a_chat_template() -> None:
    with pytest.raises(LookupError, match="chat_template"):
        BedrockImportedClient(SPEC.model_copy(update={"chat_template": None}))


@pytest.mark.parametrize("arm", ["qwen3-4b-zero-shot", "qwen3-4b-student"])
def test_the_qwen_arms_are_registered_for_this_client(arm: str) -> None:
    spec = get_model(arm)
    assert (spec.provider, spec.max_tokens, spec.temperature) == ("bedrock_imported", 8192, 0)
    assert isinstance(make_client(spec), BedrockImportedClient)
