import subprocess
import sys
from pathlib import Path

import pytest

from sparkjury.adapters.nat import nat_item_to_trace
from sparkjury.integrations.nat_eval import SparkJuryEvaluatorCore
from sparkjury.models.trace import Role

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
sys.path.insert(0, str(ROOT / "scripts"))
from validate_skills import parse_frontmatter, validate_skill  # noqa: E402

EXPECTED = ["sparkjury-clean", "sparkjury-evalset", "sparkjury-score", "sparkjury-cluster",
            "sparkjury-report", "sparkjury-regress", "sparkjury-arbitrate", "sparkjury-calibrate",
            "sparkjury-prioritize", "sparkjury-clarify", "sparkjury-govern"]


# ---- skills -------------------------------------------------------------------------

def test_six_skills_exist_and_validate():
    dirs = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())
    assert dirs == sorted(EXPECTED)
    for name in EXPECTED:
        assert validate_skill(SKILLS / name) == [], name


def test_skill_frontmatter_content():
    fm, body = parse_frontmatter((SKILLS / "sparkjury-score" / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == "sparkjury-score" and fm["license"] == "MIT"
    assert "three-judge" in fm["description"] and "Use when" in fm["description"]
    assert fm["metadata"]["product"] == "sparkjury" and fm["metadata"]["version"] == "0.1.0"
    assert "## Steps" in body and "sparkjury score" in body and "## Edge cases" in body
    for name in EXPECTED:
        card = (SKILLS / name / "skill-card.md").read_text(encoding="utf-8")
        assert "Data handling" in card and "Risk level" in card


def test_validator_catches_bad_skill(tmp_path):
    d = tmp_path / "Bad--Name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad--Name\ndescription: x\n---\nbody\n", encoding="utf-8")
    errs = validate_skill(d)
    assert any("bad name" in e for e in errs) and any("run.py" in e for e in errs) and any("skill-card" in e for e in errs)
    (d / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    assert validate_skill(d) == ["missing frontmatter"]


def test_validate_script_runs():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_skills.py"), str(SKILLS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{len(EXPECTED)}/{len(EXPECTED)} skills valid" in r.stdout


@pytest.mark.parametrize("name", EXPECTED)
def test_skill_wrapper_scripts_invoke_cli_help(name, tmp_path):
    """Each wrapper must reach the CLI; --help exits 0 for the single-command wrappers."""
    script = SKILLS / name / "scripts" / "run.py"
    if name in ("sparkjury-clean", "sparkjury-score", "sparkjury-evalset"):
        pytest.skip("multi-command wrapper; covered by import + syntax check below")
    # 治理 skill 的 --help 是中文（仓库语言约定）；Windows 的 locale 编码是 cp1252/GBK，
    # text=True 会用它解码子进程的 UTF-8 输出直接 UnicodeDecodeError——显式 utf-8。
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "Usage" in r.stdout or "usage" in r.stdout.lower()


def test_wrapper_scripts_compile():
    import py_compile

    for name in EXPECTED:
        py_compile.compile(str(SKILLS / name / "scripts" / "run.py"), doraise=True)


# ---- NAT adapter ------------------------------------------------------------------------

def _nat_item(with_tool_error=False):
    """A workflow_output.json-style row with serialized IntermediateSteps."""
    steps = [
        {"payload": {"event_type": "LLM_START", "name": "qwen3-8b", "UUID": "u1", "data": {"input": "Where is order #W1?"}}},
        {"payload": {"event_type": "LLM_END", "name": "qwen3-8b", "UUID": "u1",
                     "data": {"output": {"content": "", "tool_calls": [{"id": "c1", "name": "get_order_details", "args": {"order_id": "#W1"}}]}}}},
        {"payload": {"event_type": "TOOL_START", "name": "get_order_details", "UUID": "t1", "data": {"input": {"order_id": "#W1"}}}},
        {"payload": {"event_type": "TOOL_END", "name": "get_order_details", "UUID": "t1",
                     "data": {"output": "Error: backend unavailable (503)" if with_tool_error else '{"status": "shipped"}'}}},
        {"payload": {"event_type": "LLM_END", "name": "qwen3-8b", "UUID": "u2", "data": {"output": "Your order #W1 has shipped."}}},
    ]
    return {"id": "7", "question": "Where is order #W1?", "answer": "shipped", "generated_answer": "Your order #W1 has shipped.",
            "intermediate_steps": steps}


def test_nat_item_to_trace_shapes_steps():
    t = nat_item_to_trace(_nat_item())
    assert t.trace_id == "nat-7" and t.task_id == "7" and t.agent_model == "qwen3-8b"
    roles = [s.role for s in t.steps]
    assert roles == [Role.USER, Role.ASSISTANT, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    assert t.steps[1].tool_calls[0].name == "get_order_details" and t.steps[1].tool_calls[0].arguments == {"order_id": "#W1"}
    assert t.steps[2].tool_calls[0].call_id == "t1" and t.tool_result_for("t1").content.startswith("{")
    assert t.metrics.n_tool_calls == 2 and t.metrics.n_tool_errors == 0
    assert t.outcome.gold["expected"] == "shipped" and t.outcome.success is None  # not an exact match


def test_nat_item_object_style_and_errors():
    class Obj:  # mimics EvalInputItem
        id = 3
        input_obj = "cancel #W2"
        expected_output_obj = "cancelled"
        output_obj = "cancelled"
        trajectory = [type("S", (), {"payload": type("P", (), {"event_type": "IntermediateStepType.LLM_END", "name": "m", "UUID": "x",
                                                                   "data": type("D", (), {"input": None, "output": "cancelled"})()})()})()]

    t = nat_item_to_trace(Obj())
    assert t.task_id == "3" and t.outcome.success is True and [s.role for s in t.steps] == [Role.USER, Role.ASSISTANT]
    te = nat_item_to_trace(_nat_item(with_tool_error=True))
    assert te.metrics.n_tool_errors == 1


# ---- evaluator core ------------------------------------------------------------------------

def test_evaluator_core_scores_items(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    core = SparkJuryEvaluatorCore(judges="mock", jev="off")
    score, reasoning = core.evaluate_item(_nat_item())
    assert 0.0 <= score <= 1.0 and set(reasoning["scores"]) == {"outcome", "tool_use", "efficiency", "safety"}
    assert reasoning["env_failure"] is False and reasoning["sources"]["outcome"] in ("panel", "local", "panel_fallback")
    assert reasoning["judges"] == {"judge_a": "mock-qwen", "judge_b": "mock-gemma", "judge_c": "mock-step"}
    # subset of dimensions
    core2 = SparkJuryEvaluatorCore(judges="mock", jev="off", dimensions=["safety"])
    s2, r2 = core2.evaluate_item(_nat_item())
    assert set(r2["scores"]) == {"safety"} and s2 == r2["scores"]["safety"] / 4
    avg, outs = core.evaluate_items([_nat_item(), _nat_item()])
    assert len(outs) == 2 and outs[0]["id"] == "7" and abs(avg - score) < 1e-9


def test_evaluator_core_env_failure_scores_zero():
    item = _nat_item(with_tool_error=True)
    # two consecutive outages on the same tool -> precheck tool_unavailable
    item["intermediate_steps"] += [
        {"payload": {"event_type": "TOOL_START", "name": "get_order_details", "UUID": "t2", "data": {"input": {"order_id": "#W1"}}}},
        {"payload": {"event_type": "TOOL_END", "name": "get_order_details", "UUID": "t2", "data": {"output": "Error: connection reset"}}},
    ]
    score, reasoning = SparkJuryEvaluatorCore(judges="mock", jev="off").evaluate_item(item)
    assert score == 0.0 and reasoning["env_failure"] and "tool_unavailable" in reasoning["kinds"]


def test_nat_plugin_files_are_consistent():
    reg = (ROOT / "nat" / "nat_sparkjury" / "src" / "nat_sparkjury" / "register.py").read_text(encoding="utf-8")
    assert 'name="sparkjury"' in reg and "@register_evaluator" in reg and "SparkJuryEvaluatorCore" in reg
    py = (ROOT / "nat" / "nat_sparkjury" / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."nat.components"]' in py and 'nat_sparkjury = "nat_sparkjury.register"' in py
    yml = (ROOT / "nat" / "configs" / "sparkjury_eval.yml").read_text(encoding="utf-8")
    assert "_type: sparkjury" in yml and "_type: trajectory" in yml and "profiler:" in yml
    import py_compile
    py_compile.compile(str(ROOT / "nat" / "nat_sparkjury" / "src" / "nat_sparkjury" / "register.py"), doraise=True)
