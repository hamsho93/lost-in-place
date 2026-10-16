"""Model clients: one class per provider, all returning the same `Completion`.

To add a provider, write a class with `complete(system, user, tools) -> Completion` and register
a function that builds it from a `ModelSpec` in CLIENTS. Models on an existing provider only
need an entry in `models.yaml`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
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
            **({"system": [{"text": system}]} if system else {}),
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


def _visible(text: str) -> str:
    """The reply without its reasoning. A thought cut off by the token limit never closes, and
    anything after its `<think>` is still reasoning, not an answer."""
    text = _THINKING.sub("", text)
    return text.split("<think>", 1)[0]


def _inline_tool_calls(text: str) -> list[ToolCall]:
    calls = []
    for block in _INLINE_TOOL_CALL.findall(_visible(text)):
        try:
            parsed = json.loads(block)
            calls.append(ToolCall(name=parsed["name"], args=_arguments(parsed.get("arguments"))))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return calls


def load_chat_template(name: str) -> Any:
    """A chat template from llm/templates/, compiled the way Hugging Face's apply_chat_template
    compiles it, so the prompt matches the one the model was trained on byte for byte."""
    import jinja2  # imported lazily, like boto3
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    def raise_exception(message: str) -> None:
        raise jinja2.exceptions.TemplateError(message)

    def tojson(value: Any, ensure_ascii: bool = False, indent: int | None = None, **kwargs: Any) -> str:
        return json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, **kwargs)

    source = files("lost_in_place.llm").joinpath("templates", f"{name}.jinja").read_text()
    env = ImmutableSandboxedEnvironment(
        trim_blocks=True, lstrip_blocks=True, extensions=["jinja2.ext.loopcontrols"]
    )
    env.filters["tojson"] = tojson
    env.globals["raise_exception"] = raise_exception
    return env.from_string(source)


# Bedrock's codes for a model that is scaling up from zero copies (about 1.5 min) or is busy.
_IMPORTED_RETRYABLE = {"ModelNotReadyException", "ThrottlingException", "ServiceUnavailableException"}


class BedrockImportedClient:
    """A model imported with Bedrock Custom Model Import, such as Qwen3. Bedrock has no Converse
    API for Qwen3 imports, so this renders the model's own chat template and sends the raw prompt
    with InvokeModel; the model answers with Qwen-style `<tool_call>` text.

    `model_id` is the imported model's name. Its ARN changes with every import, so it is looked up
    on the first call.

    Bedrock removes an idle import's copies after about 5 minutes, and restoring one took about
    27 minutes on Oct 14, 2026. The sim is paused while the planner thinks, so a call waits up to
    `restore_wait_s` rather than turning the episode into a harness failure."""

    restore_wait_s = 1800.0
    read_timeout_s = 600

    def __init__(
        self,
        spec: ModelSpec,
        runtime: Any = None,
        control: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not spec.chat_template:
            raise LookupError(f"model '{spec.name}': bedrock_imported needs a chat_template")
        self.spec = spec
        self.template = load_chat_template(spec.chat_template)
        self._runtime = runtime
        self._control = control
        self._sleep = sleep
        self._arn: str | None = None

    @classmethod
    def from_spec(cls, spec: ModelSpec) -> BedrockImportedClient:
        return cls(spec)

    def _client(self, service: str) -> Any:
        import boto3
        from botocore.config import Config

        config = Config(retries={"mode": "standard", "max_attempts": 1}, read_timeout=self.read_timeout_s)
        return boto3.client(service, region_name=self.spec.region, config=config)

    def arn(self) -> str:
        if self._arn is None:
            if self._control is None:
                self._control = self._client("bedrock")
            self._arn = str(self._control.get_imported_model(modelIdentifier=self.spec.model_id)["modelArn"])
        return self._arn

    def render(self, system: str, user: str, tools: list[dict[str, Any]]) -> str:
        messages = [{"role": "system", "content": system}] if system else []
        return str(
            self.template.render(
                messages=[*messages, {"role": "user", "content": user}],
                tools=[{"type": "function", "function": tool} for tool in tools],
                add_generation_prompt=True,
            )
        )

    def _invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._runtime is None:
            self._runtime = self._client("bedrock-runtime")
        waited = 0.0
        for attempt in itertools.count():
            try:
                response = self._runtime.invoke_model(
                    modelId=self.arn(),
                    body=json.dumps(body),
                    accept="application/json",
                    contentType="application/json",
                )
                payload: dict[str, Any] = json.loads(response["body"].read())
                return payload
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                delay = min(60.0, 5.0 * 2**attempt)
                if code not in _IMPORTED_RETRYABLE or waited + delay > self.restore_wait_s:
                    raise
                self._sleep(delay)
                waited += delay
        raise AssertionError("unreachable")

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        prompt = self.render(system, user, tools)
        body: dict[str, Any] = {"prompt": prompt, "max_tokens": self.spec.max_tokens}
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        started = time.monotonic()
        payload = self._invoke(body)
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = (payload.get("choices") or [{}])[0]
        text = choice.get("text") or ""
        # The template opens the thought; put it back so the text reads as the model wrote it.
        if prompt.endswith("<think>\n"):
            text = "<think>\n" + text
        usage = payload.get("usage") or {}
        return Completion(
            tool_calls=tuple(_inline_tool_calls(text)),
            text=text,
            stop_reason=choice.get("finish_reason") or "",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            # The import's id, not its ARN, which would put the account ID in every trace.
            model_id=f"{self.spec.model_id}@{self.arn().rsplit('/', 1)[-1]}",
        )


def _scripted(spec: ModelSpec) -> ChatClient:
    from lost_in_place.llm.scripted_dexi import ScriptedRuleClient  # it imports this module

    return ScriptedRuleClient.from_spec(spec)


CLIENTS: dict[str, Callable[[ModelSpec], ChatClient]] = {
    "bedrock": BedrockConverseClient.from_spec,
    "openai_compatible": OpenAICompatibleClient.from_spec,
    "bedrock_imported": BedrockImportedClient.from_spec,
    "scripted": _scripted,
}


def make_client(spec: ModelSpec) -> ChatClient:
    if spec.provider not in CLIENTS:
        raise LookupError(f"model '{spec.name}': no client for provider '{spec.provider}'")
    return CLIENTS[spec.provider](spec)
