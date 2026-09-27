import json
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
from sparkjury.models.cluster import ClusterRun
from sparkjury.models.report import EvidenceCard
from sparkjury.precheck import run_many
from sparkjury.regress import compare, render_markdown
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


def test_regress_gates_three_checks():
    """接口①：回归三判据是纯函数，直接喂 manifest/report 断言。

    铁律语义：pack hash 不同 = 新一轮评测不是回归 → FAIL；任一侧缺指纹（老 run）→
    UNKNOWN 不阻断但如实标注；Δpass 低于阈值 → FAIL；after 独有高严重簇 → FAIL。
    """
    from sparkjury.models.regress import ClusterChange, RegressionReport
    from sparkjury.regress.passk import compute_gates

    def _rep(**kw):
        return RegressionReport(before_label="b", after_label="a", n_tasks_common=6, k=3, **kw)

    rep = _rep(delta_pass_k=0.05,
               cluster_changes=[ClusterChange(label="loop", before=3, after=1, delta=-2)])
    m = lambda h: {"pack": {"frozen_hash": h}}

    g = compute_gates(m("a" * 64), m("a" * 64), rep)
    assert g["verdict"] == "PASS" and all(c["status"] == "PASS" for c in g["checks"])

    g = compute_gates(m("a" * 64), m("b" * 64), rep)
    assert g["verdict"] == "FAIL"
    assert next(c for c in g["checks"] if c["gate"] == "same_pack_hash")["status"] == "FAIL"

    g = compute_gates(None, m("a" * 64), rep)
    assert next(c for c in g["checks"] if c["gate"] == "same_pack_hash")["status"] == "UNKNOWN"

    rep2 = _rep(delta_pass_k=0.001,
                cluster_changes=[ClusterChange(label="policy_violation", before=0, after=2, delta=2)])
    g = compute_gates(m("a" * 64), m("a" * 64), rep2, after_severities={"policy_violation": 4.2})
    assert g["verdict"] == "FAIL"
    by = {c["gate"]: c["status"] for c in g["checks"]}
    assert by["primary_delta"] == "FAIL" and by["no_new_severe_cluster"] == "FAIL"
