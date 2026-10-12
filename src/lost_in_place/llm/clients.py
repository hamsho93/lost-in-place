"""Model clients: one class per provider, all returning the same `Completion`.

To add a provider, write a class with `complete(system, user, tools) -> Completion` and register
a function that builds it from a `ModelSpec` in CLIENTS. Models on an existing provider only
need an entry in `models.yaml`.
"""

from __future__ import annotations

import json
import os
import time
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


CLIENTS: dict[str, Callable[[ModelSpec], ChatClient]] = {
    "bedrock": BedrockConverseClient.from_spec,
}


def make_client(spec: ModelSpec) -> ChatClient:
    if spec.provider not in CLIENTS:
        raise LookupError(f"model '{spec.name}': no client for provider '{spec.provider}'")
    return CLIENTS[spec.provider](spec)
