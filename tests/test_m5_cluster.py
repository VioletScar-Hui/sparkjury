import json

import httpx
import pytest
from typer.testing import CliRunner

from sparkjury.adapters.otel import load_otel
from sparkjury.adapters.tau2 import load_tau2
from sparkjury.arbiter import Arbiter, JevClient
from sparkjury.cli import app
from sparkjury.cluster import HashingEmbedder, build_badcase, cluster_badcases, is_badcase, label_clusters
from sparkjury.cluster.embed import cosine
from sparkjury.cluster.group import _threshold
from sparkjury.judges import Panel, PanelConfig
from sparkjury.models.arbitration import Arbitration, DecisionSource, TraceDecision
from sparkjury.models.cluster import BadCase, FailureLabel
from sparkjury.models.verdict import Dimension
from sparkjury.precheck import run_many
from sparkjury.store import TraceStore

runner = CliRunner(env={"COLUMNS": "220"})


@pytest.fixture
def scored_db(tmp_path, tau2_path, otel_path):
    db = tmp_path / "c.db"
    with TraceStore(db) as store:
        store.upsert_traces(load_tau2(tau2_path) + load_otel(otel_path))
        store.put_precheck(run_many(store.list()))
        panel = Panel.from_config(PanelConfig.mock())
        store.put_panel_results(panel.score_many(store.scorable_traces()))
        arb = Arbiter(jev=None, local_judge=panel.judges[0], audit_rate=0)
        store.put_decisions(arb.decide_many([(store.get(p.trace_id), p) for p in store.list_panel_results()]))
    return db


# ---- badcase selection -----------------------------------------------------------------

def _dec(tid, outcome="pass", **scores):
    arbs = [Arbitration(trace_id=tid, dimension=Dimension.OUTCOME, final_score=4 if outcome == "pass" else 0,
                        final_label=outcome, source=DecisionSource.PANEL)]
    for d in (Dimension.TOOL_USE, Dimension.EFFICIENCY, Dimension.SAFETY):
        arbs.append(Arbitration(trace_id=tid, dimension=d, final_score=scores.get(d.value, 4), source=DecisionSource.PANEL))
    return TraceDecision(trace_id=tid, arbitrations=arbs)


def test_is_badcase_thresholds():
    assert is_badcase(_dec("a")) == []
    assert is_badcase(_dec("b", outcome="fail")) == [Dimension.OUTCOME]
    assert is_badcase(_dec("c", tool_use=1)) == [Dimension.TOOL_USE]
    assert is_badcase(_dec("d", tool_use=2)) == []
    assert is_badcase(_dec("e", safety=2)) == [Dimension.SAFETY]          # safety is stricter
    assert is_badcase(_dec("f", outcome="fail", safety=0, efficiency=1)) == [Dimension.OUTCOME, Dimension.EFFICIENCY, Dimension.SAFETY]


def test_build_badcase_from_sample_store(scored_db):
    with TraceStore(scored_db) as store:
        bcs = {}
        for d in store.list_decisions():
            b = build_badcase(store.get(d.trace_id), d, store.get_panel_result(d.trace_id))
            if b:
                bcs[b.trace_id] = b
    assert set(bcs) == {"retail_task_001-t2", "retail_task_002-t1", "retail_task_002-t2", "retail_task_004-t0", "b000000000000001"}
    nc = bcs["retail_task_001-t2"]
    assert nc.failed_dimensions == [Dimension.SAFETY] and nc.severity == 3.0 and 6 in nc.evidence_steps
    assert "cancel_pending_order" in nc.feature_text and "confirmation" in nc.feature_text
    wt = bcs["retail_task_002-t1"]
    assert Dimension.OUTCOME in wt.failed_dimensions and Dimension.SAFETY in wt.failed_dimensions
    assert wt.severity >= 5.0 and "modify_user_address" in wt.excerpt


# ---- embeddings ------------------------------------------------------------------------

def test_hashing_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder(dim=128)
    a, b = e.embed(["cancel_pending_order without confirmation", "cancel_pending_order without confirmation"])
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-9
    c = e.embed(["fabricated delivery status never read the order"])[0]
    assert cosine(a, c) < cosine(a, b)


def test_threshold_clustering_groups_similar_and_marks_singletons():
    e = HashingEmbedder()
    vecs = e.embed([
        "wrong tool modify_user_address repeated after the user objected",
        "wrong tool modify_user_address repeated after the user objected loop",
        "fabricated status claim not backed by any tool result",
    ])
    labels = _threshold(vecs, 0.55)
    assert labels[0] == labels[1] == 0 and labels[2] == -1


# ---- clustering + labelling end to end ------------------------------------------------------

def _badcases(scored_db):
    with TraceStore(scored_db) as store:
        out = []
        for d in store.list_decisions():
            b = build_badcase(store.get(d.trace_id), d, store.get_panel_result(d.trace_id))
            if b:
                out.append(b)
    return out


def test_cluster_and_heuristic_labels(scored_db):
    bcs = _badcases(scored_db)
    clusters, method = cluster_badcases(bcs, HashingEmbedder(), min_cluster_size=2, method="auto")
    assert method in ("hdbscan", "threshold")
    label_clusters(clusters, bcs, jev=None)
    assert all(b.cluster_id is not None for b in bcs)
    # the two wrong-tool loops of task_002 land together, labelled as wrong_tool or loop
    by_trace = {b.trace_id: b.cluster_id for b in bcs}
    assert by_trace["retail_task_002-t1"] == by_trace["retail_task_002-t2"] != -1
    pair = next(c for c in clusters if "retail_task_002-t1" in c.member_trace_ids)
    assert pair.label in (FailureLabel.WRONG_TOOL, FailureLabel.LOOP) and pair.label_source == "heuristic"
    assert pair.rank <= 2 and pair.priority >= 12.0   # 2 traces x severity >= 6
    real = [c for c in clusters if c.cluster_id != -1]
    assert [c.priority for c in real] == sorted((c.priority for c in real), reverse=True)
    assert pair.summary and pair.suggestion and pair.representatives[0].excerpt
    # ranks are contiguous and the noise bucket is last
    ranks = [c.rank for c in clusters]
    assert ranks == list(range(1, len(clusters) + 1))
    if any(c.cluster_id == -1 for c in clusters):
        assert clusters[-1].cluster_id == -1 and clusters[-1].label_source == "none"


def test_cluster_forced_hdbscan_falls_back_when_too_small(scored_db):
    bcs = _badcases(scored_db)[:2]
    clusters, method = cluster_badcases(bcs, HashingEmbedder(), min_cluster_size=5, method="auto")
    assert method == "threshold" and sum(c.size for c in clusters) == 2


def test_empty_input():
    assert cluster_badcases([], HashingEmbedder()) == ([], "none")


def test_jev_labels_clusters_and_falls_back(scored_db):
    bcs = _badcases(scored_db)
    clusters, _ = cluster_badcases(bcs, HashingEmbedder(), min_cluster_size=2)
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["criteria"] = body["questions"]["label"]["criteria"]
        seen["state"] = body["state"]
        return httpx.Response(200, json={"answers": {"label": {"type": "choice", "choice": "wrong_tool", "confidence": 0.71, "probabilities": {}}}})

    jev = JevClient(api_key="k", transport=httpx.MockTransport(handler))
    counts = label_clusters(clusters, bcs, jev)
    real = [c for c in clusters if c.cluster_id != -1]
    assert all(c.label == FailureLabel.WRONG_TOOL and c.label_source == "jev" and c.label_confidence == 0.71 for c in real)
    assert counts == {"jev": len(real), "heuristic": 0, "n_jev_failed": 0}
    assert set(seen["criteria"]) == {l.value for l in FailureLabel} and "Representative evidence" in seen["state"]
    # Jev down -> heuristic labels，同时如实回报「本来想用 Jev、实际没用上」的簇有几个
    bad = JevClient(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    counts = label_clusters(clusters, bcs, bad)
    assert all(c.label_source == "heuristic" for c in real)
    assert counts == {"jev": 0, "heuristic": len(real), "n_jev_failed": len(real)}


# ---- store + CLI -------------------------------------------------------------------------

def test_store_and_cli(scored_db, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    r = runner.invoke(app, ["cluster", "--db", str(scored_db), "--min-cluster-size", "2", "--json"])
    assert r.exit_code == 0, r.output
    # 降级提示走 stderr；typer 的 CliRunner 把两股流混进 r.output，所以这里只认 r.stdout
    assert "fall back to heuristic" in r.stderr
    data = json.loads(r.stdout)
    assert data["n_badcases"] == 5 and data["n_clusters"] >= 1 and data["embedder"].startswith("hashing")
    with TraceStore(scored_db) as store:
        run = store.get_cluster_run()
        assert run is not None and run.n_badcases == 5 and len(run.badcases) == 5
        top = run.clusters[0]
        assert top.rank == 1 and len(store.list_badcases(top.cluster_id)) == top.size
    r = runner.invoke(app, ["cluster", "--db", str(scored_db), "--min-cluster-size", "2"])
    assert r.exit_code == 0, r.output
    assert "clustered 5 badcases" in r.output and "Suggestion:" in r.output
    # embedding server unreachable -> automatic fallback to hashing
    r = runner.invoke(app, ["cluster", "--db", str(scored_db), "--embedder", "openai", "--embed-base-url", "http://127.0.0.1:9/v1", "--min-cluster-size", "2"])
    assert r.exit_code == 0, r.output
    assert "falling back to hashing" in r.output
    r = runner.invoke(app, ["cluster", "--db", str(scored_db.parent / "none.db")])
    assert r.exit_code == 1


def test_cli_cluster_writes_clusters_json(scored_db, tmp_path):
    """clusters.json 是给下游（prioritize / regress 新簇检测）的文件契约：字段与 Cluster 一致，不带逐条 badcase。"""
    out = tmp_path / "out" / "clusters.json"
    r = runner.invoke(app, ["cluster", "--db", str(scored_db), "--min-cluster-size", "2", "--out", str(out)])
    assert r.exit_code == 0, r.output
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["n_badcases"] == 5 and d["clusters"] and "badcases" not in d
    top = d["clusters"][0]
    assert {"cluster_id", "label", "size", "share", "severity", "priority", "label_source", "member_trace_ids",
            "failed_dimension_counts"} <= set(top)
    assert len(top["member_trace_ids"]) == top["size"]


def test_category_map_prefers_pack_runtime_label(tmp_path):
    """pack 自带 runtime_label 时用它（v0.2 起是权威），pack 没给该字段才落到兜底映射文件。"""
    from sparkjury import pack as pack_mod

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "taxonomy.yaml").write_text(
        "meta:\n  version: 0.2.0\ncategories:\n"
        "  - id: F06\n    name: 冗余与空转\n    runtime_label: loop\n"
        "  - id: F01\n    name: 工具选择错误\n", encoding="utf-8")
    pack_mod.clear_cache()
    assert pack_mod.pack_label_map(pack_dir) == {"loop": "F06"}
    assert pack_mod.category_for_label("loop", pack=pack_dir) == "F06"          # pack 说了算
    # pack 没给 runtime_label 的标签，落到 standards/label-taxonomy-map.yaml
    assert pack_mod.category_for_label("wrong_tool", pack=pack_dir) == "F01"
    assert pack_mod.category_for_label("other", pack=pack_dir) == "F99"
    assert pack_mod.category_for_label("no_such_label", pack=pack_dir) is None
    pack_mod.clear_cache()
