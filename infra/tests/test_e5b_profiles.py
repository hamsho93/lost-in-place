import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from lost_in_place_infra.sim_stack import PROJECT_TAG_KEY, PROJECT_TAG_VALUE, bedrock_regions_from_registry

INFRA = Path(__file__).resolve().parents[1]
REGISTRY = INFRA.parent / "src" / "lost_in_place" / "llm" / "models.yaml"
E5B_MODELS = (
    "us.meta.llama3-3-70b-instruct-v1:0",
    "deepseek.v3.2",
    "mistral.mistral-large-3-675b-instruct",
)


@pytest.fixture(scope="module")
def profiles() -> dict[str, dict[str, Any]]:
    template = yaml.safe_load((INFRA / "e5b-profiles.yaml").read_text())
    resources = template["Resources"]
    assert {r["Type"] for r in resources.values()} == {"AWS::Bedrock::ApplicationInferenceProfile"}
    return {name: r["Properties"] for name, r in resources.items()}


def _source(props: dict[str, Any]) -> str:
    copy_from = props["ModelSource"]["CopyFrom"]["Fn::Sub"]
    assert isinstance(copy_from, str)
    return copy_from


def test_one_profile_per_e5b_model_in_us_east_2(profiles: dict[str, dict[str, Any]]) -> None:
    regions = bedrock_regions_from_registry(REGISTRY)
    assert {m: regions[m] for m in E5B_MODELS} == dict.fromkeys(E5B_MODELS, "us-east-2")
    sources = sorted(_source(p).rsplit("/", 1)[1] for p in profiles.values())
    assert sources == sorted(E5B_MODELS)


def test_scoped_ids_copy_their_system_profile_and_in_region_ids_their_model(
    profiles: dict[str, dict[str, Any]],
) -> None:
    for props in profiles.values():
        source = _source(props)
        model = source.rsplit("/", 1)[1]
        if model.startswith("us."):
            assert ":${AWS::AccountId}:inference-profile/" in source
        else:
            assert "::foundation-model/" in source
        assert source.startswith("arn:${AWS::Partition}:bedrock:${AWS::Region}:")


def test_every_profile_carries_the_budget_tag(profiles: dict[str, dict[str, Any]]) -> None:
    for props in profiles.values():
        tags = {t["Key"]: t["Value"] for t in props["Tags"]}
        assert tags[PROJECT_TAG_KEY] == PROJECT_TAG_VALUE
        assert props["InferenceProfileName"].startswith("LipE5b-")


def test_names_and_descriptions_pass_bedrocks_patterns(profiles: dict[str, dict[str, Any]]) -> None:
    # CloudFormation's early validation rejects the whole change set otherwise.
    for props in profiles.values():
        assert re.fullmatch(r"([0-9a-zA-Z:.][ _-]?)+", props["Description"])
        assert re.fullmatch(r"([0-9a-zA-Z][ _-]?)+", props["InferenceProfileName"])
