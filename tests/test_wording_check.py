"""The wording check's probe, misread parser and rule A-1 (freeze-v3 §2.3, §5.2), and
`scripts/e5_wording_check.py`, on fake clients only."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lost_in_place import dryrun, wording
from lost_in_place import families as fam
from lost_in_place.llm import dexi
from lost_in_place.llm.clients import Completion

ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "scenarios"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "e5_wording_check", ROOT / "scripts" / "e5_wording_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prompts() -> list[wording.ProbePrompt]:
    return wording.probe_prompts({t: dryrun.wording_families(SCENARIOS, t) for t in fam.TEMPLATES})


@pytest.mark.parametrize(
    ("text", "point"),
    [
        ("north 1.8 m, east -4.0 m", (1.8, -4.0)),
        ("North: 1.8 m, East: -4.0 m", (1.8, -4.0)),
        ("north -1.9 m and east 4 m", (-1.9, 4.0)),
        ("The hold point is north 0.0 m, east \u22124.0 m.", (0.0, -4.0)),
        ("It ends 1.9 m south and 4.0 m east of take-off.", (-1.9, 4.0)),
        ("1.8 m north, 4.0 m west", (1.8, -4.0)),
        ("First north 0.0 m, east -4.0 m; then north 1.8 m, east -4.0 m", (1.8, -4.0)),
        ("<think>north 9 m, east 9 m</think>\nnorth 1.8 m, east -4.0 m", (1.8, -4.0)),
        ("<think>north 9 m, east 9 m", None),  # a thought cut off isn't an answer
        ("I can't tell from the mission.", None),
        ("north 1.8 m", None),
        ("", None),
    ],
)
def test_parse_point(text: str, point: tuple[float, float] | None) -> None:
    assert wording.parse_point(text) == point


def _prompt(goal: tuple[float, float] = (1.8, -4.0)) -> wording.ProbePrompt:
    return wording.ProbePrompt("b42", "t0", dexi.PROMPT_V2, "msg", goal)


def test_a_misread_is_a_point_more_than_half_a_metre_from_the_goal() -> None:
    assert wording.judge(_prompt(), 0, "north 1.8 m, east -4.0 m").misread is False
    assert wording.judge(_prompt(), 0, "north 1.8 m, east -3.5 m").misread is False  # exactly 0.5 m
    assert wording.judge(_prompt(), 0, "north 1.8 m, east -3.4 m").misread is True
    assert wording.judge(_prompt(), 0, "north 0.0 m, east -5.8 m").misread is True  # the continuation
    bad = wording.judge(_prompt(), 0, "unsure")
    assert (bad.valid, bad.misread, bad.distance_m) == (False, None, None)


def test_probe_prompts_cover_every_family_template_and_version_at_o0(
    prompts: list[wording.ProbePrompt],
) -> None:
    assert len(prompts) == 66 * 2 * 2 and len(prompts) * wording.PROBE_REPS == 792
    # b00/b23, b42/b57 and b56/b58 have the same waypoints, so the same prompts.
    assert len({p.sha256 for p in prompts}) == len(prompts) - 3 * 2 * 2
    for p in prompts:
        prompt, question = p.message.split("\n\n" + wording.PROBE_QUESTION)
        assert question == "" and "Estimator health" not in prompt
        assert (dexi.VALIDATOR_CLAIM in prompt) is (p.prompt_version == dexi.PROMPT_VERSION)
        assert ("ending" in prompt) is (p.template == "t1")
        assert prompt.endswith("Choose the single next action that makes progress on the mission.")
    b42 = next(
        p for p in prompts if (p.family, p.template, p.prompt_version) == ("b42", "t1", dexi.PROMPT_V2)
    )
    assert b42.goal == (1.8, -4.0)
    assert "Current state: north 0.0 m, east -4.0 m," in b42.message


class Answers:
    def __init__(self, answer: Any):
        self.answer = answer
        self.tools: list[list[dict[str, Any]]] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        self.tools.append(tools)
        text = self.answer(user)
        return Completion((), text, "end_turn", 400, 12, 500, "fake")


def _goal_reply(prompts: list[wording.ProbePrompt]) -> Any:
    by_message = {p.message: p for p in prompts}

    def answer(user: str) -> str:
        p = by_message[user]
        if p.family == "b03" and p.template == "t0":
            raise TimeoutError("slow")
        if p.family == "b02" and p.template == "t0":
            return "north 0.0 m, east -5.8 m"
        if p.family == "b01":
            return "no idea"
        return f"north {p.goal[0]} m, east {p.goal[1]} m"

    return answer


def test_run_probe_sends_no_tools_and_records_errors(prompts: list[wording.ProbePrompt]) -> None:
    client = Answers(_goal_reply(prompts))
    replies = wording.run_probe(prompts, client, jobs=8)
    assert len(replies) == 792 and all(t == [] for t in client.tools)
    summary = wording.summarise_probe(replies)
    assert summary["t0"] == {
        "n": 396, "error": 6, "invalid": 6, "valid": 384, "misread": 6, "misread_rate": 6 / 384,
    }  # fmt: skip
    assert summary["t1"] == {
        "n": 396,
        "error": 0,
        "invalid": 6,
        "valid": 390,
        "misread": 0,
        "misread_rate": 0.0,
    }
    assert wording.replies_from_json(json.loads(json.dumps(wording.replies_to_json(replies)))) == replies


def _student(s0: int, s1: int, n: int = 66, errors: int = 0) -> dict[str, dict[str, int]]:
    return {
        "t0": {"n": n, "success": s0, "harness_error": errors, "with_invalid": 0},
        "t1": {"n": n, "success": s1, "harness_error": 0, "with_invalid": 0},
    }


def _probe(m0: int, m1: int, valid: int = 396, errors: int = 0) -> dict[str, dict[str, Any]]:
    row = {"n": 396, "error": errors, "invalid": 396 - valid - errors}
    return {
        "t0": {**row, "valid": valid, "misread": m0, "misread_rate": m0 / valid if valid else None},
        "t1": {**row, "valid": valid, "misread": m1, "misread_rate": m1 / valid if valid else None},
    }


@pytest.mark.parametrize(
    ("student", "probes", "template"),
    [
        (_student(60, 60), {"claude": _probe(40, 0)}, "t1"),
        (_student(60, 54), {"claude": _probe(40, 40)}, "t1"),  # 81.8 >= 90.9 - 10
        (_student(60, 53), {"claude": _probe(40, 0)}, "t0"),  # 80.3 < 80.9
        (_student(50, 60), {"claude": _probe(10, 11)}, "t0"),
        (_student(60, 60), {"claude": _probe(40, 0), "gpt": _probe(3, 4)}, "t0"),
        (_student(60, 60), {"claude": _probe(40, 0), "gpt": _probe(3, 3)}, "t1"),
        (_student(60, 60), {"claude": _probe(0, 0, valid=0)}, "t0"),  # no valid reply: rate undefined
    ],
)
def test_rule_a1(student: Any, probes: Any, template: str) -> None:
    result = wording.a1(student, probes)
    assert result["template"] == template
    assert set(result["probe_conditions"]) == set(probes)


@pytest.mark.parametrize(
    ("student", "probes"),
    [
        (_student(60, 60, n=65), {"claude": _probe(1, 0)}),
        (_student(60, 60, errors=1), {"claude": _probe(1, 0)}),
        (_student(60, 60), {"claude": _probe(1, 0, valid=395, errors=1)}),
    ],
)
def test_rule_a1_refuses_incomplete_inputs(student: Any, probes: Any) -> None:
    with pytest.raises(wording.IncompleteInput):
        wording.a1(student, probes)


def _dryrun_doc(success: dict[str, set[str]]) -> dict[str, Any]:
    episodes = []
    for t in fam.TEMPLATES:
        for name in (*dryrun.RETIRED, *fam.E5_CANARIES):
            run = dryrun.DryRun(name, t, "land", name in success[t])
            episodes.append({**run.summary(), "harness_error": "", "decisions": [], "planner_decisions": []})
    settings = {
        "prompt": "dexi-v2",
        "observation": "O0",
        "driver": "dexi",
        "validator": "none",
        "latency_s": 0.0,
    }
    return {"model": "qwen3-4b-student", **settings, "episodes": episodes}


def _probe_doc(model: str, prompts: list[wording.ProbePrompt], misread: set[str]) -> dict[str, Any]:
    def answer(user: str) -> str:
        p = next(q for q in prompts if q.message == user)
        n, e = p.goal
        return f"north {n} m, east {e + 2.0 if (p.family, p.template) in misread else e} m"

    replies = wording.run_probe(prompts, Answers(answer), jobs=8)
    return {"model": model, "replies": wording.replies_to_json(replies)}


def test_decide_outputs_a1s_inputs_and_result(prompts: list[wording.ProbePrompt]) -> None:
    script = _script()
    doc = _dryrun_doc({"t0": {"b00", "b01", "b02"}, "t1": {"b00", "b01"}})
    claude = _probe_doc("claude-sonnet-4-6", prompts, {("b42", "t0"), ("b57", "t0")})
    result = script.decide(doc, [claude], ["claude-sonnet-4-6"], prompts)
    assert result["template"] == "t1"
    assert result["student"]["t0"]["success"] == 3 and result["student"]["t1"]["success_pct"] == round(
        200 / 66, 2
    )
    assert result["probes"]["claude-sonnet-4-6"]["t0"]["misread"] == 12
    assert result["probes"]["claude-sonnet-4-6"]["t1"]["misread"] == 0
    gpt = _probe_doc("gpt-6.1-sol", prompts, {("b42", "t1")})
    both = script.decide(doc, [claude, gpt], ["claude-sonnet-4-6", "gpt-6.1-sol"], prompts)
    assert both["template"] == "t0" and both["probe_conditions"] == {
        "claude-sonnet-4-6": True,
        "gpt-6.1-sol": False,
    }


def test_decide_refuses_mismatched_or_stale_inputs(prompts: list[wording.ProbePrompt]) -> None:
    script = _script()
    doc = _dryrun_doc({"t0": set(), "t1": set()})
    claude = _probe_doc("claude-sonnet-4-6", prompts, set())
    with pytest.raises(wording.IncompleteInput, match="--models"):
        script.decide(doc, [claude], ["claude-sonnet-4-6", "gpt-6.1-sol"], prompts)
    with pytest.raises(wording.IncompleteInput, match="--models"):
        script.decide(doc, [claude, claude], ["claude-sonnet-4-6"], prompts)
    stale = {**claude, "replies": [{**r, "prompt_sha256": "0" * 64} for r in claude["replies"]]}
    with pytest.raises(wording.IncompleteInput, match="current probe prompt"):
        script.decide(doc, [stale], ["claude-sonnet-4-6"], prompts)
    with pytest.raises(wording.IncompleteInput, match="once per template"):
        script.decide({**doc, "episodes": doc["episodes"][1:]}, [claude], ["claude-sonnet-4-6"], prompts)
    with pytest.raises(wording.IncompleteInput, match="settings"):
        script.decide({**doc, "prompt": "dexi-9b2e7a7"}, [claude], ["claude-sonnet-4-6"], prompts)
    with pytest.raises(wording.IncompleteInput, match="qwen3-4b-student"):
        script.decide({**doc, "model": "qwen3-4b-zero-shot"}, [claude], ["claude-sonnet-4-6"], prompts)


def test_script_plans_and_estimates_without_calling_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lost_in_place.llm.clients as clients

    monkeypatch.setattr(clients, "make_client", lambda spec: pytest.fail("called the model"))
    script = _script()
    out = tmp_path / "probe.json"
    assert script.main(["--scenarios", str(SCENARIOS), "probe", "gpt-6.1-sol", "--out", str(out)]) == 0
    assert "792 calls" in capsys.readouterr().out and not out.exists()
    assert script.main(["--scenarios", str(SCENARIOS), "estimate"]) == 0
    est = json.loads(capsys.readouterr().out)
    assert est["dryrun"]["episodes"] == 132 and [p["calls"] for p in est["probes"]] == [792, 792, 792]
    assert all(p["usd"] is not None and p["usd"] < 10 for p in est["probes"])
    gpt, sonnet55 = est["probes"][1], est["probes"][2]
    assert sonnet55["input_tokens"] > gpt["input_tokens"] and sonnet55["usd"] > gpt["usd"]
    fb1 = wording.probe_cost("claude-sonnet-5-5-fb1", script._prompts(SCENARIOS))
    assert fb1 == {**sonnet55, "model": "claude-sonnet-5-5-fb1"}
    assert script.main(["--scenarios", str(SCENARIOS), "prompts", "--out", str(tmp_path / "p.json")]) == 0
    assert len(json.loads((tmp_path / "p.json").read_text())) == 264


def test_script_probe_live_writes_its_replies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lost_in_place.llm.clients as clients

    monkeypatch.setattr(clients, "make_client", lambda spec: Answers(lambda user: "north 1.0 m, east 1.0 m"))
    script = _script()
    out = tmp_path / "probe.json"
    argv = [
        "--scenarios",
        str(SCENARIOS),
        "probe",
        "claude-sonnet-4-6",
        "--out",
        str(out),
        "--live",
        "--jobs",
        "8",
    ]
    assert script.main(argv) == 0
    doc = json.loads(out.read_text())
    assert doc["model"] == "claude-sonnet-4-6" and len(doc["replies"]) == 792
    assert doc["summary"]["t0"]["valid"] == 396 and doc["question"] == wording.PROBE_QUESTION
