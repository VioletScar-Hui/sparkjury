import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sparkjury.cli import app
from sparkjury.harness import EventBus, EventKind, Orchestrator, RunConfig, Stage, run_config
from sparkjury.harness.config import InputSpec
from sparkjury.judges import JudgeSpec, PanelConfig
from sparkjury.models.trace import TraceSource
from sparkjury.store import TraceStore

runner = CliRunner(env={"COLUMNS": "220"})


@pytest.fixture
def demo_cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = RunConfig.demo(run_id="t-demo")
    cfg.runs_dir = str(tmp_path / "runs")
    cfg.db = str(tmp_path / "runs" / "t-demo" / "db.sqlite")
    return cfg


def test_demo_run_end_to_end(demo_cfg):
    events = []
    manifest = run_config(demo_cfg, on_event=events.append)
    assert manifest["status"] == "ok" and manifest["run_id"] == "t-demo"
    st = manifest["stages"]
    assert list(st) == [s.value for s in Stage] and all(v["status"] == "ok" for v in st.values())
    assert st["INGEST"]["n_ingested"] == 14 and st["PRECHECK"]["n_env_failures"] == 1
    assert st["EVALSET"]["n_traces"] == 13 and st["SCORE"]["n_verdicts"] == 156
    assert st["ARBITRATE"]["n_dimensions"] == 52 and st["CLUSTER"]["n_badcases"] == 5
    assert st["REPORT"]["n_badcases"] == 5 and "Fix cluster #1" in st["REPORT"]["recommendation"]
    assert all(Path(f).exists() for f in st["REPORT"]["files"])
    # degradation recorded: no Jev key
    assert any(d["component"] == "jev" for d in manifest["degradations"])
    assert manifest["models"]["judges"] == {"judge_a": "mock-qwen", "judge_b": "mock-gemma", "judge_c": "mock-step"}
    # artefacts on disk
    rd = demo_cfg.run_dir
    assert (rd / "manifest.json").exists() and (rd / "events.jsonl").exists() and (rd / "evalset.json").exists()
    assert len(json.loads((rd / "evalset.json").read_text())) == 13
    # events: run + 7 stage pairs + progress + degraded
    kinds = [e.kind for e in events]
    assert kinds[0] == EventKind.RUN_START and kinds[-1] == EventKind.RUN_END
    assert kinds.count(EventKind.STAGE_START) == 7 and kinds.count(EventKind.STAGE_END) == 7
    assert sum(1 for e in events if e.kind == EventKind.PROGRESS and e.stage == "SCORE") == 13
    assert any(e.kind == EventKind.DEGRADED and e.stage == "ARBITRATE" for e in events)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert EventBus.read_log(rd / "events.jsonl")[-1].kind == EventKind.RUN_END
    # manifest also stored in the db runs table
    with TraceStore(demo_cfg.db) as store:
        assert store.get_run("t-demo")["stage"] == "ok"


def test_unconfigured_jev_is_recorded_as_a_cluster_degradation(demo_cfg):
    """簇标签退回启发式时必须留痕，不能只在 ARBITRATE 阶段记。

    这条曾经漏过：ARBITRATE 遇到没配 key 的 Jev 会写一条降级，CLUSTER 遇到同一个条件却什么都
    不写，而离线 demo 走的正是这条路——卡片上两个簇的 label_source 都是 heuristic，manifest 的
    degradations 里却只有 ARBITRATE 一条，读 manifest 的人会以为簇标签来自 Jev。
    AGENTS.md 的诚实降级规则要求每次都记，所以这里同时盯住记录和它对应的实际标签。
    """
    manifest = run_config(demo_cfg)
    cluster_deg = [d for d in manifest["degradations"] if d["stage"] == "CLUSTER" and d["component"] == "jev"]
    assert cluster_deg, "Jev 没配 key 时 CLUSTER 阶段没有记录降级"
    assert cluster_deg[0]["reason"] == "TYPESAFE_API_KEY not set"
    assert cluster_deg[0]["fallback"] == "heuristic cluster label"
    # 阶段结果里也要能一眼看出标签出自哪条路径
    st = manifest["stages"]["CLUSTER"]
    assert st["label_sources"]["jev"] == 0
    assert st["label_sources"]["heuristic"] == st["n_clusters"]

    # 降级记录要和卡片上的实际标签对得上，否则记了也是白记
    card = json.loads((demo_cfg.run_dir / "card" / "card.json").read_text(encoding="utf-8"))
    real = [c for c in card["clusters"] if c["cluster_id"] != -1]
    assert real and all(c["label_source"] == "heuristic" for c in real)


def test_unreachable_llm_judge_is_swapped_for_mock(demo_cfg):
    demo_cfg.panel = PanelConfig(judges=[
        JudgeSpec(name="judge_a", kind="mock", model="mock-qwen"),
        JudgeSpec(name="judge_b", kind="openai", model="gemma", base_url="http://127.0.0.1:9/v1"),
        JudgeSpec(name="judge_c", kind="mock", model="mock-step"),
    ])
    demo_cfg.stages = [Stage.INGEST, Stage.PRECHECK, Stage.EVALSET, Stage.SCORE]
    events = []
    m = run_config(demo_cfg, on_event=events.append)
    assert m["status"] == "ok"
    deg = [d for d in m["degradations"] if d["component"].startswith("judge judge_b")]
    assert deg and deg[0]["fallback"] == "mock judge"
    # 降级原因必须带具体异常（401 和网络不通的排查方向相反），不许只写 health check failed
    assert deg[0]["reason"].startswith("health check failed: ") and len(deg[0]["reason"]) > len("health check failed: ")
    assert m["models"]["judges"]["judge_b"] == "mock-fallback-for-gemma"
    assert m["stages"]["SCORE"]["judge_errors"] == 0
    assert any(e.kind == EventKind.DEGRADED and e.stage == "SCORE" for e in events)


def test_stage_failure_is_recorded_and_stops_the_run(demo_cfg):
    demo_cfg.inputs = [InputSpec(path="does/not/exist.json", source=TraceSource.TAU2)]
    events = []
    m = run_config(demo_cfg, on_event=events.append)
    assert m["status"] == "failed" and "FileNotFoundError" in m["error"]
    assert m["stages"]["INGEST"]["status"] == "failed" and "PRECHECK" not in m["stages"]
    assert any(e.kind == EventKind.ERROR for e in events) and events[-1].kind == EventKind.RUN_END
    assert (demo_cfg.run_dir / "manifest.json").exists()


def test_stage_subset_and_evalset_limit(demo_cfg):
    run_config(demo_cfg)                         # full run first
    cfg2 = demo_cfg.model_copy(deep=True)
    cfg2.run_id = "t-demo-rescore"
    cfg2.reset_db = False
    cfg2.stages = [Stage.EVALSET, Stage.SCORE, Stage.ARBITRATE]
    cfg2.evalset.limit = 4
    cfg2.evalset.task_ids = ["retail_task_002", "retail_task_004"]
    m = run_config(cfg2)
    assert m["status"] == "ok" and list(m["stages"]) == ["EVALSET", "SCORE", "ARBITRATE"]
    assert m["stages"]["EVALSET"]["n_traces"] == 4 and m["stages"]["EVALSET"]["n_tasks"] == 2
    assert m["stages"]["ARBITRATE"]["n_traces"] == 4


def test_config_from_toml(tmp_path):
    p = tmp_path / "run.toml"
    p.write_text(
        'run_id = "r1"\ndb = "x.db"\nstages = ["SCORE", "REPORT"]\n[[inputs]]\npath = "a.json"\nsource = "otel"\n'
        '[evalset]\nlimit = 3\n[panel]\nworkers = 2\n[[panel.judges]]\nname = "judge_a"\nkind = "mock"\n'
        '[arbiter]\njev = "off"\naudit_rate = 0.1\n[cluster]\nembedder = "hash"\nmin_cluster_size = 2\n[report]\ntitle = "T"\n',
        encoding="utf-8",
    )
    cfg = RunConfig.from_toml(p)
    assert cfg.run_id == "r1" and cfg.stages == [Stage.SCORE, Stage.REPORT]
    assert cfg.inputs[0].source == TraceSource.OTEL and cfg.evalset.limit == 3
    assert cfg.panel is not None and cfg.panel.workers == 2 and cfg.panel.judges[0].kind == "mock"
    assert cfg.arbiter.jev == "off" and cfg.arbiter.audit_rate == 0.1
    assert cfg.cluster.min_cluster_size == 2 and cfg.report.title == "T"
    example = RunConfig.from_toml(Path(__file__).resolve().parents[1] / "deploy" / "run.example.toml")
    assert example.panel is not None and len(example.panel.judges) == 3 and example.judge_healthcheck


def test_cli_run_runs_events(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["run", "--demo", "--run-id", "cli-demo", "--quiet"])
    assert r.exit_code == 0, r.output
    assert "REPORT ok" in r.output and "status: ok" in r.output and "recommendation:" in r.output
    r = runner.invoke(app, ["runs"])
    assert r.exit_code == 0 and "cli-demo" in r.output
    r = runner.invoke(app, ["events", "cli-demo", "--kind", "degraded"])
    assert r.exit_code == 0 and "jev" in r.output
    r = runner.invoke(app, ["events", "nope"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["run"])
    assert r.exit_code == 2


def test_partial_rerun_keeps_earlier_stages_in_manifest(demo_cfg):
    run_config(demo_cfg)
    cfg2 = demo_cfg.model_copy(deep=True)
    cfg2.reset_db = False
    cfg2.stages = [Stage.CLUSTER, Stage.REPORT]
    m = run_config(cfg2)
    assert list(m["stages"]) == [s.value for s in Stage]          # all seven, canonical order
    assert m["stages"]["INGEST"]["n_ingested"] == 14 and m["stages"]["SCORE"]["n_verdicts"] == 156
    assert m["models"]["judges"]["judge_a"] == "mock-qwen"        # model info survives
    assert m["stages"]["CLUSTER"]["n_badcases"] == 5 and m["previous_run_at"]


def test_reset_db_only_deletes_inside_runs_dir(tmp_path):
    """`db` 可以从配置文件或 API 请求体来，而 reset_db 以前对任意路径无条件 unlink()。"""
    (tmp_path / "runs" / "reset-guard").mkdir(parents=True)
    victim = tmp_path / "victim.db"
    victim.write_text("precious", encoding="utf-8")
    cfg = RunConfig(run_id="reset-guard", runs_dir=str(tmp_path / "runs"), db=str(victim),
                    reset_db=True, stages=[Stage.INGEST])
    m = Orchestrator(cfg).run()
    assert m["status"] == "failed" and "reset_db" in m["error"]   # 失败要写进清单，不能静默
    assert victim.read_text(encoding="utf-8") == "precious"       # 文件还在

    # runs_dir 之内的库照旧被重置
    inside = tmp_path / "runs" / "reset-ok" / "db.sqlite"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("stale", encoding="utf-8")
    cfg2 = RunConfig(run_id="reset-ok", runs_dir=str(tmp_path / "runs"), db=str(inside),
                     reset_db=True, stages=[Stage.INGEST])
    Orchestrator(cfg2).run()
    assert not inside.exists() or inside.read_bytes() != b"stale"
