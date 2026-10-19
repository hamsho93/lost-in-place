"""`scripts/e5_label_sample.py`: one blind sample and its double-code over several E5 sweeps, each read
from its own runs prefix."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from e5_fixtures import load_script
from test_labels import SCORE, _attempt, _episode, _tracks

from lost_in_place import labels, manifest, report

ARMS = {
    "e5-gpt-5.6-sol": "dexi-gpt-5.6-sol.driver-dexi.prompt-dexi-v2",
    "e5-claude-sonnet-4-6": "dexi-claude-sonnet-4-6.driver-dexi.prompt-dexi-v2",
}


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script("e5_label_sample")


def _documents(shared: bool = False) -> dict[str, dict[str, Any]]:
    out = {}
    for sweep, arm in ARMS.items():
        planner = "dexi" if not shared else "same"
        entries = [
            {
                "scenario": f"s/b0{i}_loud.yaml",
                "scenario_id": f"b0{i}_loud",
                "rep": 0,
                "seed": i,
                "planner": planner,
                "validator": "none",
                "observation": "O1",
                "latency_s": 2.0,
                "planner_options": {"driver": "dexi", "prompt": "dexi-v2", "model": arm.split(".")[0][5:]},
            }
            for i in range(10)
        ]
        if shared:
            for e in entries:
                e["planner_options"] = {}
        out[f"s3://b/manifests/{sweep}.json"] = {"name": sweep, "runs_prefix": sweep, "entries": entries}
    return out


def _patch(monkeypatch: pytest.MonkeyPatch, documents: dict[str, dict[str, Any]], loaded: list[str]) -> None:
    by_root = {}
    for doc in documents.values():
        keys = sorted(manifest.Entry(**e).key for e in doc["entries"])
        arm = ARMS[doc["name"]]
        by_root[f"s3://b/runs/{doc['name']}"] = [
            _attempt(k, ("state_conditioned", "loud", arm, "none", "O1"), dict(SCORE)) for k in keys
        ]
    monkeypatch.setattr(manifest, "read_document", lambda uri: documents[uri])
    monkeypatch.setattr(report, "s3_attempts", lambda root: by_root[root])

    def loader(root: str):  # type: ignore[no-untyped-def]
        def load(a: report.Attempt) -> tuple[dict[str, Any], Any]:
            assert a in by_root[root]
            loaded.append(root)
            return _episode("b00_loud", True), _tracks()

        return load

    monkeypatch.setattr(labels, "loader", loader)


def test_one_sample_over_every_sweep_loads_each_packet_from_its_own_prefix(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, loaded = _documents(), []
    _patch(monkeypatch, documents, loaded)
    argv = ["s3://b/runs", "--out", str(tmp_path / "one"), "--out-2", str(tmp_path / "two"), "--no-plots"]
    assert script.main([*argv, *(x for uri in documents for x in ("--manifest", uri))]) == 0
    key = json.loads((tmp_path / "one" / "key.json").read_text())
    key_2 = json.loads((tmp_path / "two" / "key-2.json").read_text())
    assert key["episodes"] == 2 and key_2["episodes"] == 2
    assert {tuple(v["cell"])[2] for v in key["labels"].values()} == set(ARMS.values())
    assert sorted(v["group"] for v in key_2["labels"].values()) == ["arm", "matched"]
    assert sorted(set(loaded)) == sorted(f"s3://b/runs/{s}" for s in ARMS)


def test_episode_keys_shared_between_manifests_stop_it(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents = _documents(shared=True)
    _patch(monkeypatch, documents, [])
    argv = ["s3://b/runs", "--out", str(tmp_path / "one"), "--out-2", str(tmp_path / "two")]
    with pytest.raises(SystemExit, match="shared with an earlier manifest"):
        script.main([*argv, *(x for uri in documents for x in ("--manifest", uri))])
