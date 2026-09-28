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

EXPECTED = ["sparkjury-clean", "sparkjury-evalset", "sparkjury-score", "sparkjury-cluster", "sparkjury-report", "sparkjury-regress"]


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
    assert "6/6 skills valid" in r.stdout


@pytest.mark.parametrize("name", EXPECTED)
def test_skill_wrapper_scripts_invoke_cli_help(name, tmp_path):
    """Each wrapper must reach the CLI; --help exits 0 for the single-command wrappers."""
    script = SKILLS / name / "scripts" / "run.py"
    if name in ("sparkjury-clean", "sparkjury-score", "sparkjury-evalset"):
        pytest.skip("multi-command wrapper; covered by import + syntax check below")
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, cwd=ROOT)
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


# ---- 六个技能端到端（B 组通过条件：先红后绿）--------------------------------------------
# TEAM.md 第 2 轮 B 组点名过的自欺：validate_skills 只查格式，上面的 --help 用例只证明
# "能启动"。这一节按每份 SKILL.md 写下的承诺真跑六个封装，断言的是文档答应过的产出。
# 首次提交时有两条是红的（score 不消费 evalset / 被测模型可以坐上裁判席），修复后转绿。

SAMPLE = ROOT / "data" / "samples" / "tau2_retail_sample.json"


def _skill(name, *args, cwd, extra_env=None):
    import os
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(SKILLS / name / "scripts" / "run.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=cwd, env=env)


def _cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "sparkjury.cli", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    """clean + evalset 走完的库：后面每个用例从这里接力，链路只搭一次。"""
    base = tmp_path_factory.mktemp("skill-e2e")
    db = base / "store.db"
    r = _skill("sparkjury-clean", "--path", str(SAMPLE), "--source", "tau2", "--db", str(db), cwd=base)
    assert r.returncode == 0, r.stderr
    r = _skill("sparkjury-evalset", "--db", str(db), "--run-id", "e2e-set", "--limit", "3", cwd=base)
    assert r.returncode == 0, r.stderr
    return {"base": base, "db": db, "evalset": base / "runs" / "e2e-set" / "evalset.json"}


def test_e2e_clean_json_shape_and_idempotency(e2e):
    """SKILL.md 承诺：precheck --json 的 summary 四键 + flagged；重跑幂等（按 trace_id upsert）。"""
    import json as _json
    import sqlite3
    out = _cli("precheck", "--db", str(e2e["db"]), "--json", cwd=e2e["base"]).stdout
    d = _json.loads(out)
    assert {"n_checked", "n_env_failures", "n_scorable", "kinds"} <= set(d["summary"]) and isinstance(d["flagged"], list)
    n_before = sqlite3.connect(e2e["db"]).execute("select count(*) from traces").fetchone()[0]
    r = _skill("sparkjury-clean", "--path", str(SAMPLE), "--source", "tau2", "--db", str(e2e["db"]), cwd=e2e["base"])
    assert r.returncode == 0, r.stderr
    n_after = sqlite3.connect(e2e["db"]).execute("select count(*) from traces").fetchone()[0]
    assert n_after == n_before, "重跑 ingest 应按 trace_id upsert，不产生重复行"


def test_e2e_evalset_contract_files(e2e):
    """SKILL.md 承诺：runs/<id>/evalset.json 是 trace id 列表，manifest 记 n_traces / n_tasks。"""
    import json as _json
    ids = _json.loads(e2e["evalset"].read_text(encoding="utf-8"))
    assert isinstance(ids, list) and len(ids) == 3 and all(isinstance(i, str) for i in ids)
    m = _json.loads((e2e["base"] / "runs" / "e2e-set" / "manifest.json").read_text(encoding="utf-8"))
    st = m["stages"]["EVALSET"]
    assert st["status"] == "ok" and st["n_traces"] == 3 and st["n_tasks"] >= 1


def test_e2e_score_consumes_evalset(e2e, tmp_path):
    """evalset 的存在意义是「钉死这轮要判哪些 trace」。没有这条通路时，单独跑 score 会把
    整库都打一遍，evalset.json 成了只有 harness 才认的摆设——首次提交时这里是红的。"""
    import json as _json
    import shutil
    import sqlite3
    db = tmp_path / "sub.db"
    shutil.copy(e2e["db"], db)
    r = _cli("score", "--db", str(db), "--judges", "mock", "--evalset", str(e2e["evalset"]), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    scored = {row[0] for row in sqlite3.connect(db).execute("select distinct trace_id from verdicts")}
    assert scored == set(_json.loads(e2e["evalset"].read_text(encoding="utf-8")))
    # 空交集不许静默：给一份不相干的 evalset，必须红着退出而不是打零条还报成功
    bogus = tmp_path / "bogus.json"
    bogus.write_text('["no-such-trace"]', encoding="utf-8")
    r = _cli("score", "--db", str(db), "--judges", "mock", "--evalset", str(bogus), cwd=tmp_path)
    assert r.returncode != 0


def test_e2e_score_refuses_subject_model_on_panel(e2e, tmp_path):
    """SKILL.md 边界条款「被测 Agent 的模型不得坐上裁判席」在首次提交时是一句没有闸的文档。
    裁判席上有被测模型 = 自己给自己打分，必须拒绝；--allow-self-judge 是显式逃生口。"""
    import shutil
    import sqlite3
    db = tmp_path / "guard.db"
    shutil.copy(e2e["db"], db)
    subject = sqlite3.connect(db).execute(
        "select distinct agent_model from traces where agent_model is not null").fetchone()[0]
    toml = tmp_path / "panel.toml"
    toml.write_text(
        "[panel]\nscore_tolerance = 1\n\n[[panel.judges]]\nname = \"self\"\nkind = \"openai\"\n"
        f"model = \"{subject}\"\nbase_url = \"http://127.0.0.1:1/v1\"\n\n"
        "[[panel.judges]]\nname = \"other\"\nkind = \"openai\"\n"
        "model = \"some/other-judge\"\nbase_url = \"http://127.0.0.1:1/v1\"\n", encoding="utf-8")
    r = _cli("score", "--db", str(db), "--judges", str(toml), cwd=tmp_path)
    assert r.returncode != 0 and subject in (r.stdout + r.stderr)
    r = _cli("score", "--db", str(db), "--judges", "mock", cwd=tmp_path)
    assert r.returncode == 0, "mock 面板与被测模型无交集，不应被闸拦住"


@pytest.fixture(scope="module")
def e2e_scored(e2e):
    """score + arbitrate 走完的同一份库（mock 面板），cluster / report / regress 接着用。"""
    r = _skill("sparkjury-score", "--db", str(e2e["db"]), "--judges", "mock", cwd=e2e["base"])
    assert r.returncode == 0, r.stderr
    return e2e


def test_e2e_cluster_fields_and_offline_fallback(e2e_scored):
    """SKILL.md 承诺：每个簇带 label/size/share/severity/priority/代表/建议；embedding 服务
    不在时退回哈希向量并写明，而不是装作向量服务在线。"""
    import json as _json
    r = _skill("sparkjury-cluster", "--db", str(e2e_scored["db"]), "--min-cluster-size", "2", cwd=e2e_scored["base"])
    assert r.returncode == 0, r.stderr
    out = _cli("cluster", "--db", str(e2e_scored["db"]), "--min-cluster-size", "2", "--json",
               cwd=e2e_scored["base"]).stdout
    d = _json.loads(out)
    assert d["clusters"], "样本里有 badcase，聚不出簇说明链路断了"
    cl = d["clusters"][0]
    assert {"label", "size", "share", "severity", "priority", "representatives", "suggestion"} <= set(cl)
    assert 2 <= len(cl["representatives"]) <= 3
    assert "hash" in d["embedder"], "离线环境必须如实标注哈希兜底，而不是留空"


def test_e2e_report_three_files_and_disclaimer(e2e_scored):
    """SKILL.md 承诺：card.json/md/html 三件套，卡片永远带「非根因」免责声明。"""
    import json as _json
    out = e2e_scored["base"] / "card"
    r = _skill("sparkjury-report", "--db", str(e2e_scored["db"]), "--out", str(out), cwd=e2e_scored["base"])
    assert r.returncode == 0, r.stderr
    assert {(out / f).exists() for f in ("card.json", "card.md", "card.html")} == {True}
    card = _json.loads((out / "card.json").read_text(encoding="utf-8"))
    assert card["disclaimer"] and card["quality"]["judge_agreement_rate"] is not None


def test_e2e_regress_verdict_and_gate_exit(e2e_scored):
    """SKILL.md 承诺：同库对比判 unchanged；门禁默认只报告（rc=0），--gate 才把 FAIL 变成 rc=1。"""
    db = str(e2e_scored["db"])
    r = _skill("sparkjury-regress", "--before", db, "--after", db, cwd=e2e_scored["base"])
    assert r.returncode == 0 and "unchanged" in r.stdout, r.stdout + r.stderr
    r = _skill("sparkjury-regress", "--before", db, "--after", db, "--gate", cwd=e2e_scored["base"])
    assert r.returncode == 1, "同库 Δ=0 过不了 delta_min 门，--gate 下必须以 rc=1 拦住"
