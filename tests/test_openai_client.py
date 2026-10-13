import json
import urllib.error
from typing import Any

import pytest

from lost_in_place.llm import clients
from lost_in_place.llm.clients import MODEL_ENDPOINTS_ENV, OpenAICompatibleClient, ToolCall
from lost_in_place.llm.prompt import tool_specs
from lost_in_place.llm.registry import ModelSpec

SPEC = ModelSpec(
    name="qwen-test",
    provider="openai_compatible",
    model_id="qwen3-4b-test",
    base_url="http://127.0.0.1:8081/v1/",
    temperature=0,
    tool_choice="auto",
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


class Server:
    """Stands in for the HTTP call: records each request and replays canned replies."""

    def __init__(self, *replies: dict[str, Any] | Exception):
        self.replies = list(replies)
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float
    ) -> dict[str, Any]:
        self.requests.append((url, body, headers))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(content: str = "", tool_calls: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 25},
        **extra,
    }


def function_call(name: str, arguments: Any) -> dict[str, Any]:
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}


def client(
    server: Server, spec: ModelSpec = SPEC, env: dict[str, str] | None = None
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(spec, post=server, env=env or {}, sleep=lambda s: None)


def test_request_shape() -> None:
    server = Server(reply(tool_calls=[function_call("land", "{}")]))
    client(server).complete("be careful", "where now?", tool_specs())
    url, body, headers = server.requests[0]
    assert url == "http://127.0.0.1:8081/v1/chat/completions"
    assert "Authorization" not in headers
    assert body["model"] == "qwen3-4b-test"
    assert body["messages"] == [
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": "where now?"},
    ]
    assert body["tools"][0] == {"type": "function", "function": tool_specs()[0]}
    assert (body["tool_choice"], body["temperature"], body["max_tokens"]) == ("auto", 0, 512)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_system_prompt_sends_one_user_message() -> None:
    server = Server(reply(tool_calls=[function_call("land", "{}")]))
    client(server).complete("", "the whole prompt", tool_specs())
    assert server.requests[0][1]["messages"] == [{"role": "user", "content": "the whole prompt"}]


def test_tool_calls_with_string_or_object_arguments() -> None:
    server = Server(
        reply(
            "Holding.",
            [function_call("hold", json.dumps({"seconds": 3})), function_call("land", {"reason": "done"})],
            model="served-alias",
        )
    )
    completion = client(server).complete("s", "u", tool_specs())
    assert completion.tool_calls == (ToolCall("hold", {"seconds": 3}), ToolCall("land", {"reason": "done"}))
    assert (completion.text, completion.stop_reason) == ("Holding.", "tool_calls")
    assert (completion.input_tokens, completion.output_tokens) == (700, 25)
    assert completion.model_id == "served-alias"


def test_unparseable_arguments_are_kept_in_the_text_not_guessed() -> None:
    server = Server(reply(tool_calls=[function_call("move", "{north_m: 2")]))
    completion = client(server).complete("s", "u", tool_specs())
    assert completion.tool_calls == ()
    assert "[unparseable tool call]" in completion.text and "north_m: 2" in completion.text


def test_inline_tool_call_fallback_ignores_thinking() -> None:
    text = (
        '<think>maybe <tool_call>{"name": "land", "arguments": {}}</tool_call></think>\n'
        '<tool_call>\n{"name": "move", "arguments": {"north_m": 1, "east_m": 2}}\n</tool_call>'
    )
    completion = client(Server(reply(text))).complete("s", "u", tool_specs())
    assert completion.tool_calls == (ToolCall("move", {"north_m": 1, "east_m": 2}),)
    assert completion.model_id == "qwen3-4b-test"


def test_endpoint_override_and_api_key() -> None:
    spec = SPEC.model_copy(update={"base_url": None, "api_key_env": "QWEN_KEY"})
    env = {MODEL_ENDPOINTS_ENV: json.dumps({"qwen-test": "https://example.test/v1"}), "QWEN_KEY": "k"}
    server = Server(reply(tool_calls=[function_call("land", "{}")]))
    client(server, spec, env).complete("s", "u", tool_specs())
    url, _, headers = server.requests[0]
    assert url == "https://example.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k"


def test_missing_endpoint_or_key_fails_at_construction() -> None:
    with pytest.raises(LookupError, match="no base_url"):
        client(Server(), SPEC.model_copy(update={"base_url": None}))
    with pytest.raises(LookupError, match="QWEN_KEY is not set"):
        client(Server(), SPEC.model_copy(update={"api_key_env": "QWEN_KEY"}))


def test_retries_transient_errors_only() -> None:
    busy = urllib.error.HTTPError("u", 503, "busy", {}, None)  # type: ignore[arg-type]
    server = Server(busy, urllib.error.URLError("refused"), reply(tool_calls=[function_call("land", "{}")]))
    assert client(server).complete("s", "u", tool_specs()).tool_calls == (ToolCall("land", {}),)
    assert len(server.requests) == 3

    bad = urllib.error.HTTPError("u", 400, "bad request", {}, None)  # type: ignore[arg-type]
    server = Server(bad)
    with pytest.raises(urllib.error.HTTPError):
        client(server).complete("s", "u", tool_specs())
    assert len(server.requests) == 1


def test_registered_as_a_provider() -> None:
    assert clients.CLIENTS["openai_compatible"] == OpenAICompatibleClient.from_spec
