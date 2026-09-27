import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sparkjury.adapters.otel import load_otel
from sparkjury.adapters.tau2 import parse_tau2
from sparkjury.arbiter import Arbiter
from sparkjury.cli import app
from sparkjury.cluster import HashingEmbedder, build_badcase, cluster_badcases, label_clusters
from sparkjury.judges import Panel, PanelConfig
from sparkjury.judges.pairwise import MockPairwiseJudge, compare_with_swap
from sparkjury.models.cluster import Cluster, ClusterRun, FailureLabel
from sparkjury.models.report import EvidenceCard
from sparkjury.precheck import run_many
from sparkjury.regress import compare, evaluate_gates, new_severe_clusters, render_markdown
from sparkjury.report import build_card, render_html, render_json, render_markdown as card_md, write_card
from sparkjury.store import TraceStore

runner = CliRunner(env={"COLUMNS": "220"})


def run_pipeline(db: Path, tau2_data: dict, otel_path: Path | None = None) -> None:
    """ingest -> precheck -> score -> arbitrate -> cluster, all offline."""
    with TraceStore(db) as store:
        traces = parse_tau2(tau2_data, raw_ref="mem")
        if otel_path:
            traces += load_otel(otel_path)
        store.upsert_traces(traces)
        store.put_precheck(run_many(store.list()))
        panel = Panel.from_config(PanelConfig.mock())
        store.put_panel_results(panel.score_many(store.scorable_traces()))
        arb = Arbiter(jev=None, local_judge=panel.judges[0], audit_judge=panel.judges[-1], audit_rate=0.3)
        store.put_decisions(arb.decide_many([(store.get(p.trace_id), p) for p in store.list_panel_results()]))
        bcs = []
        for d in store.list_decisions():
            b = build_badcase(store.get(d.trace_id), d, store.get_panel_result(d.trace_id))
            if b:
                bcs.append(b)
        clusters, method = cluster_badcases(bcs, HashingEmbedder(), min_cluster_size=2)
        label_clusters(clusters, bcs, None)
        store.put_cluster_run(ClusterRun(n_badcases=len(bcs), n_clusters=sum(1 for c in clusters if c.cluster_id != -1),
                                         n_noise=sum(c.size for c in clusters if c.cluster_id == -1),
                                         embedder="hashing-512", method=method, clusters=clusters, badcases=bcs))


@pytest.fixture
def tau2_data(tau2_path):
    return json.loads(Path(tau2_path).read_text(encoding="utf-8"))


@pytest.fixture
def before_db(tmp_path, tau2_data, otel_path):
    db = tmp_path / "before.db"
    run_pipeline(db, tau2_data, otel_path)
    return db


@pytest.fixture
def after_db(tmp_path, tau2_data, otel_path):
    """Same run, but task_004 trial 0 no longer hallucinates (prompt was fixed) and task_001 trial 2 now confirms."""
    import copy

    data = copy.deepcopy(tau2_data)
    sims = {(s["task_id"], s["trial"]): s for s in data["simulations"]}
    good4 = sims[("retail_task_004", 1)]
    fixed = sims[("retail_task_004", 0)]
    fixed["messages"] = copy.deepcopy(good4["messages"])
    fixed["reward_info"] = copy.deepcopy(good4["reward_info"])
    good1 = sims[("retail_task_001", 0)]
    sims[("retail_task_001", 2)]["messages"] = copy.deepcopy(good1["messages"])
    db = tmp_path / "after.db"
    run_pipeline(db, data, otel_path)
    return db


# ---- evidence card ------------------------------------------------------------------

def test_build_card_totals_and_clusters(before_db):
    with TraceStore(before_db) as store:
        card = build_card(store, run_id="r1", title="Retail eval")
    t, q = card.totals, card.quality
    assert t.n_traces == 14 and t.n_tasks == 6 and t.n_env_failures == 1 and t.env_kinds == {"tool_unavailable": 1}
    assert t.n_scored == 13 and t.n_badcases == 5 and t.n_clusters >= 1
    assert abs(q.pass_rate - 9 / 14) < 1e-9 and abs(q.pass_k[3] - 0.25) < 1e-9
    assert q.agent_model == "openai/qwen3-8b" and q.judge_agreement_rate is not None
    assert q.decisions_by_source["panel"] > 0 and q.n_outcome_fail == 4
    assert set(q.mean_scores) == {"outcome", "tool_use", "efficiency", "safety"}
    top = card.clusters[0]
    assert top.rank == 1 and top.size >= 2 and top.suggestion and top.representatives
    rep = top.representatives[0]
    assert rep.excerpt and rep.failed_dimensions and rep.opinions and rep.decision_sources
    assert "Fix cluster #1 first" in card.recommendation and "pass^3" in card.recommendation
    assert "not root-cause" in card.disclaimer


def test_card_renders_all_formats(before_db, tmp_path):
    with TraceStore(before_db) as store:
        card = build_card(store)
    js = render_json(card)
    assert EvidenceCard.model_validate_json(js).totals.n_badcases == 5
    md = card_md(card)
    assert md.startswith("# SparkJury evidence card") and "## Recommendation" in md and "### #1" in md and "```" in md
    html = render_html(card)
    assert "<!DOCTYPE html>" in html and "Recommendation" in html and card.clusters[0].label.value in html
    files = write_card(card, tmp_path / "out")
    assert [f.suffix for f in files] == [".json", ".md", ".html"] and all(f.exists() for f in files)


def test_card_on_empty_store(tmp_path):
    with TraceStore(tmp_path / "e.db") as store:
        card = build_card(store)
    assert card.totals.n_traces == 0 and card.clusters == [] and "No badcases" in card.recommendation
    assert "No badcases" in card_md(card)


# ---- regression ---------------------------------------------------------------------

def test_regress_same_store_is_unchanged(before_db):
    r = compare(before_db, before_db)
    assert r.verdict == "unchanged" and r.delta_pass_k == 0 and r.delta_pass_rate == 0
    assert r.fixed_tasks == [] and r.broken_tasks == [] and all(c.delta == 0 for c in r.cluster_changes)
    assert r.k == 1  # otel tasks have one trial, so the common k is 1


def test_regress_detects_improvement(before_db, after_db):
    r = compare(before_db, after_db, before_label="v1", after_label="v2")
    assert r.verdict == "improved" and r.delta_pass_rate is not None and r.delta_pass_rate > 0
    assert [t.task_id for t in r.fixed_tasks] == ["retail_task_004"] and r.broken_tasks == []
    assert r.n_badcases_after < r.n_badcases_before
    assert r.mean_scores_after["safety"] > r.mean_scores_before["safety"]
    md = render_markdown(r)
    assert md.startswith("# Regression report: improved") and "retail_task_004" in md and "## Cluster changes" in md
    # reversed direction is a regression
    rr = compare(after_db, before_db)
    assert rr.verdict == "regressed" and [t.task_id for t in rr.broken_tasks] == ["retail_task_004"]


def test_pairwise_swap_consistency(before_db, after_db):
    with TraceStore(before_db) as b, TraceStore(after_db) as a:
        tb, ta = b.get("retail_task_004-t0"), a.get("retail_task_004-t0")
    winner, consistent, why = compare_with_swap(MockPairwiseJudge(), tb, ta)
    assert winner == "after" and consistent and "scores" in why
    winner, consistent, _ = compare_with_swap(MockPairwiseJudge(), ta, ta)
    assert winner == "tie" and consistent

    class Biased:  # always picks A: the swap must expose it as inconsistent
        name = "biased"

        def compare(self, x, y):
            return "A", "first is better"

    winner, consistent, why = compare_with_swap(Biased(), tb, ta)
    assert winner == "tie" and not consistent and "inconsistent" in why


def test_regress_with_pairwise(before_db, after_db):
    r = compare(before_db, after_db, pairwise=MockPairwiseJudge())
    assert len(r.pairwise) == 14 and r.pairwise_summary.get("after", 0) >= 2
    assert all(p.consistent for p in r.pairwise) and "inconsistent" not in r.pairwise_summary


# ---- CLI -------------------------------------------------------------------------------

def test_cli_report_and_regress(before_db, after_db, tmp_path):
    out = tmp_path / "cardout"
    r = runner.invoke(app, ["report", "--db", str(before_db), "--out", str(out), "--title", "Retail eval"])
    assert r.exit_code == 0, r.output
    assert "5 badcases" in r.output and (out / "card.html").exists() and (out / "card.md").exists()
    assert "Retail eval" in (out / "card.md").read_text(encoding="utf-8")

    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(after_db), "--pairwise", "mock", "--out", str(tmp_path / "reg.md")])
    assert r.exit_code == 0, r.output
    assert "regression improved" in r.output and (tmp_path / "reg.md").exists() and "pairwise:" in r.output

    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(after_db), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["fixed_tasks"][0]["task_id"] == "retail_task_004"


# ---- 回归门禁：pack 身份 / 提升阈值 / 新严重簇（PR 接口 ①） ----------------------------------

def _manifest_with_hash(db: Path, pack_hash: str | None) -> None:
    """run 的 manifest 就放在库旁边：pack 身份是这么确认的（一个 run 一个目录）。"""
    (db.parent / "manifest.json").write_text(json.dumps({"run_id": db.stem, "pack_hash": pack_hash}), encoding="utf-8")


def test_gates_refuse_when_pack_hashes_differ(before_db, after_db, tmp_path):
    """两侧 pack hash 不同 = 两套标准评的两轮，不是回归：拒绝对比，退出码 2。"""
    # 每个 run 一个目录：manifest.json 就放在库旁边，两个库放同一个目录里就分不出是哪一轮了
    b1, b2 = tmp_path / "run-a" / "sparkjury.db", tmp_path / "run-b" / "sparkjury.db"
    for src, dst in ((before_db, b1), (after_db, b2)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    _manifest_with_hash(b1, "a" * 64)
    _manifest_with_hash(b2, "b" * 64)
    g = evaluate_gates(None, before_db=b1, after_db=b2)
    assert g.verdict == "REFUSED" and g.exit_code == 2 and g.same_pack is False
    assert "pack hash differs" in g.failures[0].detail

    r = runner.invoke(app, ["regress", "--before", str(b1), "--after", str(b2)])
    assert r.exit_code == 2 and "regress refused" in r.output


def test_gates_skip_pack_identity_without_manifest(before_db, after_db):
    """老库（run 目录里没有 manifest.json）只是无法确认身份，不该变成不可对比。"""
    g = evaluate_gates(None, before_db=before_db, after_db=after_db)
    assert g.verdict == "PASS" and g.same_pack is None
    assert g.items[0].status == "skip" and "no pack_hash" in g.items[0].detail


def test_gates_tie_fails_the_improvement_threshold(before_db):
    """同一份库前后对比：提升 0 < delta_min —— 没提升就不算回归，这是铁律的核心。"""
    _manifest_with_hash(before_db, "c" * 64)
    rep = compare(before_db, before_db)
    g = evaluate_gates(rep, before_db=before_db, after_db=before_db)
    assert g.verdict == "FAIL" and g.same_pack is True
    fail = {i.name: i for i in g.failures}
    assert fail["primary_metric"].source == "pack" and "delta_min 0.02" in fail["primary_metric"].detail
    # 阈值真的来自参数/pack，不是写死的：放到底就没得挑
    assert evaluate_gates(rep, before_db=before_db, after_db=before_db, delta_min=0.0).verdict == "PASS"


def test_cli_gate_flag_only_changes_the_exit_code(before_db):
    """默认只报不改退出码（CI/certificate 里同库对比不该突然变成失败），--gate 才拦。"""
    _manifest_with_hash(before_db, "c" * 64)
    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db)])
    assert r.exit_code == 0 and "gates FAIL" in r.output
    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db), "--gate"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db), "--json", "--gate"])
    payload = json.loads(r.stdout)
    assert r.exit_code == 1 and payload["verdict"] == "unchanged" and payload["gates"]["verdict"] == "FAIL"


def test_cli_json_writes_out_and_serialises_verdict(before_db, tmp_path):
    """--json 会提前 return 导致 --out 静默不写；verdict 是 property，以前也不在 JSON 里。"""
    out = tmp_path / "reg-json.md"
    r = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db), "--json", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists() and "pass^" in out.read_text(encoding="utf-8")
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "unchanged" and payload["gates"]["items"]


def _cluster_run(pairs) -> ClusterRun:
    clusters = [Cluster(cluster_id=i, rank=i + 1, label=FailureLabel(lab), label_source="heuristic", size=size,
                        share=0.5, severity=sev, priority=1.0, member_trace_ids=[f"t{i}"])
                for i, (lab, sev, size) in enumerate(pairs)]
    return ClusterRun(n_badcases=sum(p[2] for p in pairs), n_clusters=len(clusters), n_noise=0,
                      embedder="hashing-512", method="threshold", clusters=clusters)


def test_new_severe_cluster_detection():
    """新 = 这个标签 before 一条都没有；严重 = severity 到阈值。两者都满足才阻断。"""
    before = _cluster_run([("wrong_tool", 4.0, 3)])
    after = _cluster_run([("wrong_tool", 4.0, 3), ("policy_violation", 4.2, 1), ("premature_stop", 1.0, 2)])
    assert new_severe_clusters(before, after, 3.0) == [("policy_violation", 4.2, 1)]
    assert new_severe_clusters(before, after, 5.0) == []
    # 同一类问题变多不算「新簇」：那是主指标和簇变化表要讲的事
    assert new_severe_clusters(before, _cluster_run([("wrong_tool", 6.0, 9)]), 3.0) == []


def test_gates_fail_on_a_newly_appeared_severe_cluster(before_db, after_db):
    _manifest_with_hash(before_db, "d" * 64)
    _manifest_with_hash(after_db, "d" * 64)
    with TraceStore(after_db) as store:
        run = store.get_cluster_run()
        assert run is not None and run.clusters
        run.clusters.append(Cluster(cluster_id=99, rank=len(run.clusters) + 1, label=FailureLabel.POLICY_VIOLATION,
                                    label_source="heuristic", size=1, share=0.1, severity=4.2, priority=9.0,
                                    member_trace_ids=run.clusters[0].member_trace_ids[:1]))
        store.put_cluster_run(run)
    rep = compare(before_db, after_db)
    g = evaluate_gates(rep, before_db=before_db, after_db=after_db, delta_min=0.0)
    assert g.verdict == "FAIL"
    fail = {i.name: i for i in g.failures}
    assert "policy_violation" in fail["new_severe_cluster"].detail and fail["new_severe_cluster"].source == "default"
    # 阈值调高就放行：这条门禁的能量来自参数，不是常量
    assert evaluate_gates(rep, before_db=before_db, after_db=after_db, delta_min=0.0, severe_min=5.0).verdict == "PASS"


def test_cli_gate_output_stays_ascii_for_windows_consoles(before_db, tmp_path):
    """Windows CI 的控制台是 cp1252：rich 往真控制台写中文会直接 UnicodeEncodeError。

    六技能封装是把 CLI 当子进程调的，所以这条链路上任何非 ASCII 的控制台输出都会让 Windows 红
    （门禁输出第一版写了中文，就是这么红的）。这里把「regress 的输出必须能用 cp1252 编码」钉住：
    正常出报告、门禁 FAIL、pack 身份拒绝三条路都过一遍。
    """
    _manifest_with_hash(before_db, "c" * 64)
    ok = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db)])
    assert ok.exit_code == 0 and "gates FAIL" in ok.output
    ok.output.encode("cp1252")          # 有中文就抛 UnicodeEncodeError
    js = runner.invoke(app, ["regress", "--before", str(before_db), "--after", str(before_db), "--json"])
    js.stdout.encode("cp1252")

    b1, b2 = tmp_path / "run-a" / "sparkjury.db", tmp_path / "run-b" / "sparkjury.db"
    for src, dst in ((before_db, b1), (before_db, b2)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    _manifest_with_hash(b1, "a" * 64)
    _manifest_with_hash(b2, "b" * 64)
    refused = runner.invoke(app, ["regress", "--before", str(b1), "--after", str(b2)])
    assert refused.exit_code == 2
    refused.output.encode("cp1252")
