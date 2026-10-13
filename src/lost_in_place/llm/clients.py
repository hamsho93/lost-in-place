"""Model clients: one class per provider, all returning the same `Completion`.

To add a provider, write a class with `complete(system, user, tools) -> Completion` and register
a function that builds it from a `ModelSpec` in CLIENTS. Models on an existing provider only
need an entry in `models.yaml`.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from lost_in_place.llm.registry import ModelSpec

# Set on the Batch job by the CDK stack: {system inference profile id: tagged application profile ARN},
# so Bedrock spend carries the project tag and counts against the project budget.
BEDROCK_PROFILES_ENV = "LIP_BEDROCK_PROFILES"


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    tool_calls: tuple[ToolCall, ...]
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model_id: str  # what was actually invoked, after any profile mapping


class ChatClient(Protocol):
    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion: ...


class BedrockConverseClient:
    """Any Bedrock model that supports tool use, through the Converse API."""

    def __init__(self, spec: ModelSpec, client: Any = None, env: Mapping[str, str] = os.environ):
        self.spec = spec
        profiles = json.loads(env.get(BEDROCK_PROFILES_ENV) or "{}")
        self.model_id: str = profiles.get(spec.model_id, spec.model_id)
        self._client = client

    @classmethod
    def from_spec(cls, spec: ModelSpec) -> BedrockConverseClient:
        return cls(spec)

    def _bedrock(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily so the core package doesn't require it
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.spec.region,
                config=Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=120),
            )
        return self._client

    def request(self, system: str, user: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        inference: dict[str, Any] = {"maxTokens": self.spec.max_tokens}
        if self.spec.temperature is not None:
            inference["temperature"] = self.spec.temperature
        return {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": {"json": t["parameters"]},
                        }
                    }
                    for t in tools
                ],
                "toolChoice": {self.spec.tool_choice: {}},
            },
            "inferenceConfig": inference,
        }

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        started = time.monotonic()
        response = self._bedrock().converse(**self.request(system, user, tools))
        content = response["output"]["message"]["content"]
        usage = response.get("usage", {})
        return Completion(
            tool_calls=tuple(
                ToolCall(name=block["toolUse"]["name"], args=dict(block["toolUse"]["input"]))
                for block in content
                if "toolUse" in block
            ),
            text="".join(block.get("text", "") for block in content),
            stop_reason=response.get("stopReason", ""),
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            latency_ms=int(response.get("metrics", {}).get("latencyMs", (time.monotonic() - started) * 1000)),
            model_id=self.model_id,
        )


# {model name in models.yaml: base URL}, overriding the registry's base_url, so the same arm can
# point at a server on a laptop, a tunnel or a hosted endpoint without editing models.yaml.
MODEL_ENDPOINTS_ENV = "LIP_MODEL_ENDPOINTS"

# Qwen-style tool calls that a server left in the message text instead of parsing them.
_INLINE_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)

Post = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload: dict[str, Any] = json.loads(response.read())
    return payload


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, urllib.error.URLError | TimeoutError | ConnectionError)


class OpenAICompatibleClient:
    """Any server with an OpenAI-style /chat/completions endpoint and tool calling: llama.cpp's
    llama-server, mlx_lm.server, vLLM, Ollama or a hosted endpoint."""

    attempts = 3
    timeout_s = 120.0

    def __init__(
        self,
        spec: ModelSpec,
        post: Post = _post_json,
        env: Mapping[str, str] = os.environ,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.spec = spec
        base_url = json.loads(env.get(MODEL_ENDPOINTS_ENV) or "{}").get(spec.name, spec.base_url)
        if not base_url:
            raise LookupError(f"model '{spec.name}': no base_url in models.yaml or {MODEL_ENDPOINTS_ENV}")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Content-Type": "application/json"}
        if spec.api_key_env:
            key = env.get(spec.api_key_env)
            if not key:
                raise LookupError(f"model '{spec.name}': {spec.api_key_env} is not set")
            self.headers["Authorization"] = f"Bearer {key}"
        self._post = post
        self._sleep = sleep

    @classmethod
    def from_spec(cls, spec: ModelSpec) -> OpenAICompatibleClient:
        return cls(spec)

    def request(self, system: str, user: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}] if system else []
        body: dict[str, Any] = {
            **self.spec.extra_body,
            "model": self.spec.model_id,
            "messages": [*messages, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": tool} for tool in tools],
            "tool_choice": "required" if self.spec.tool_choice == "any" else "auto",
            "max_tokens": self.spec.max_tokens,
        }
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        return body

    def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.attempts):
            try:
                return self._post(self.url, body, self.headers, self.timeout_s)
            except Exception as exc:
                if attempt + 1 == self.attempts or not _retryable(exc):
                    raise
                self._sleep(2.0**attempt)
        raise AssertionError("unreachable")

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        started = time.monotonic()
        payload = self._send(self.request(system, user, tools))
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        calls, unparsed = [], []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            args = function.get("arguments") or {}
            try:
                calls.append(ToolCall(name=function["name"], args=_arguments(args)))
            except (KeyError, ValueError):
                unparsed.append(json.dumps(function))
        if not calls and not unparsed:
            calls = _inline_tool_calls(text)
        if unparsed:
            text += "".join(f"\n[unparseable tool call] {raw}" for raw in unparsed)
        usage = payload.get("usage") or {}
        return Completion(
            tool_calls=tuple(calls),
            text=text,
            stop_reason=choice.get("finish_reason") or "",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            model_id=payload.get("model") or self.spec.model_id,
        )


def _arguments(raw: Any) -> dict[str, Any]:
    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw or {}
    if not isinstance(args, dict):
        raise ValueError("arguments are not an object")
    return args


def _inline_tool_calls(text: str) -> list[ToolCall]:
    calls = []
    for block in _INLINE_TOOL_CALL.findall(_THINKING.sub("", text)):
        try:
            parsed = json.loads(block)
            calls.append(ToolCall(name=parsed["name"], args=_arguments(parsed.get("arguments"))))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return calls


CLIENTS: dict[str, Callable[[ModelSpec], ChatClient]] = {
    "bedrock": BedrockConverseClient.from_spec,
    "openai_compatible": OpenAICompatibleClient.from_spec,
}


def make_client(spec: ModelSpec) -> ChatClient:
    if spec.provider not in CLIENTS:
        raise LookupError(f"model '{spec.name}': no client for provider '{spec.provider}'")
    return CLIENTS[spec.provider](spec)
