"""The model registry: `models.yaml` maps each planner arm to a provider and a pinned model."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    provider: str
    model_id: str
    region: str = "us-east-1"
    max_tokens: int = Field(512, gt=0)
    temperature: float | None = None
    tool_choice: Literal["any", "auto"] = "any"
    # OpenAI-compatible servers only: where the server is, the environment variable holding its
    # key (local servers need none), and extra request fields such as chat_template_kwargs.
    base_url: str | None = None
    api_key_env: str | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


def load_models(path: Path | None = None) -> dict[str, ModelSpec]:
    text = path.read_text() if path else files("lost_in_place.llm").joinpath("models.yaml").read_text()
    entries = yaml.safe_load(text) or {}
    return {name: ModelSpec(name=name, **entry) for name, entry in entries.items()}


def get_model(name: str, path: Path | None = None) -> ModelSpec:
    models = load_models(path)
    if name not in models:
        raise LookupError(f"no model '{name}'; known: {', '.join(sorted(models)) or 'none'}")
    return models[name]
