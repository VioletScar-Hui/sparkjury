import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sparkjury.api import create_app
from sparkjury.api import dgx as dgx_mod
from sparkjury.api.runs import InvalidRunId, RunManager, resolve_within
from sparkjury.governance import validate_event


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    # 决策账本落到 tmp：默认路径是仓库根的 governance/events.jsonl，测试不该往仓库里写
    app = create_app(tmp_path / "runs", probe_endpoints=False, ledger_path=tmp_path / "governance" / "events.jsonl")
    with TestClient(app) as c:
        yield c


def _wait(client, run_id, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = client.get(f"/runs/{run_id}").json()
        if not m.get("running") and m.get("status") in ("ok", "failed"):
            return m
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_start_demo_run_and_read_everything(client):
    r = client.post("/runs", json={"demo": True, "run_id": "api-1"})
    assert r.status_code == 202 and r.json()["run_id"] == "api-1"
    # duplicate while running (or finished quickly) -> 409 or accepted second run; only assert on a clearly running one
    m = _wait(client, "api-1")
    assert m["status"] == "ok" and m["stages"]["REPORT"]["status"] == "ok" and "traceback" not in m

    runs = client.get("/runs").json()
    assert runs[0]["run_id"] == "api-1" and runs[0]["status"] == "ok" and runs[0]["n_badcases"] == 5

    # SSE replay of a finished run ends with run_end
    with client.stream("GET", "/runs/api-1/events") as s:
        body = "".join(s.iter_text())
    assert body.startswith("event: run_start") and "event: degraded" in body and body.rstrip().endswith("}")
    assert body.count("event: stage_end") == 7 and "event: run_end" in body
    evs = client.get("/runs/api-1/events.json?since=0").json()
    assert evs[-1]["kind"] == "run_end" and evs[0]["seq"] == 1
    assert len(client.get("/runs/api-1/events.json?since=%d" % (evs[-1]["seq"] - 2)).json()) == 2

    card = client.get("/runs/api-1/card").json()
    assert card["totals"]["n_badcases"] == 5 and card["clusters"][0]["rank"] == 1
    html = client.get("/runs/api-1/card.html")
    assert html.status_code == 200 and "<!DOCTYPE html>" in html.text

    cl = client.get("/runs/api-1/clusters").json()
    assert cl["n_badcases"] == 5 and "badcases" not in cl and cl["clusters"][0]["label"]
    top = cl["clusters"][0]["cluster_id"]
    members = client.get(f"/runs/api-1/traces?cluster={top}").json()
    assert len(members) == cl["clusters"][0]["size"] and members[0]["feature_text"]

    rows = client.get("/runs/api-1/traces").json()
    assert len(rows) == 14 and sum(1 for x in rows if x["env_failure"]) == 1
    failed = client.get("/runs/api-1/traces?failed=true").json()
    assert len(failed) == 5

    d = client.get("/runs/api-1/traces/retail_task_002-t1").json()
    assert d["trace"]["task_id"] == "retail_task_002" and "modify_user_address" in d["transcript"]
    assert d["panel"] and len(d["panel"]["verdicts"]) == 12 and d["decision"]["arbitrations"]
    assert client.get("/runs/api-1/traces/nope").status_code == 404

    # PM confirmation
    assert client.get("/runs/api-1/confirm").json() is None
    c = client.post("/runs/api-1/confirm", json={"cluster_id": top, "note": "start here"}).json()
    assert c["cluster_id"] == top and "sparkjury regress" in c["next"]
    # 同一个按钮的两种入口都要落账本：老端点也得写一条 accept_card，否则点了确认账本里是空的
    assert c["event"]["payload"]["decision_kind"] == "accept_card" and c["event"]["payload"]["target"] == f"card:{top}"
    assert client.get("/runs/api-1/confirm").json()["note"] == "start here"
    assert client.post("/runs/api-1/confirm", json={"cluster_id": 999}).status_code == 404

    # regress against itself
    rg = client.get("/runs/api-1/regress?before=api-1").json()
    assert rg["verdict"] == "unchanged" and rg["delta_pass_rate"] == 0


def test_second_run_and_regress_between_runs(client):
    client.post("/runs", json={"demo": True, "run_id": "a"})
    _wait(client, "a")
    r = client.post("/runs", json={"demo": True, "run_id": "b", "stages": ["INGEST", "PRECHECK", "EVALSET", "SCORE", "ARBITRATE", "CLUSTER", "REPORT"], "evalset_limit": 6})
    assert r.status_code == 202
    m = _wait(client, "b")
    assert m["stages"]["EVALSET"]["n_traces"] == 6
    rg = client.get("/runs/b/regress?before=a").json()
    assert rg["n_tasks_common"] == 6 and rg["before_label"] == "a"
    assert client.get("/runs/b/regress?before=zzz").status_code == 404


def test_errors_and_misc(client, tmp_path):
    assert client.post("/runs", json={}).status_code == 400
    assert client.post("/runs", json={"config_path": "missing.toml"}).status_code == 400
    assert client.post("/runs", json={"demo": True, "stages": ["BOGUS"]}).status_code == 400
    assert client.get("/runs/none").status_code == 404
    assert client.get("/runs/none/card").status_code == 404
    assert client.get("/runs/none/events.json").status_code == 404
    h = client.get("/health").json()
    assert h["ok"] and h["version"]
    idx = client.get("/")
    assert idx.status_code == 200 and "SparkJury" in idx.text and "EventSource" in idx.text
    d = client.get("/dgx").json()
    assert "available" in d and "gpus" in d and d["endpoints"] == []


def test_dgx_probe_reports_down_endpoints(monkeypatch):
    eps = dgx_mod.probe_endpoints([{"name": "x", "url": "http://127.0.0.1:9/v1"}], timeout_s=0.3)
    assert eps[0]["ok"] is False and eps[0]["error"] and eps[0]["latency_ms"] >= 0
    monkeypatch.setattr(dgx_mod.shutil, "which", lambda _: None)
    g = dgx_mod.sample_gpus()
    assert g["available"] is False and "nvidia-smi" in g["reason"]


def test_dgx_sample_handles_unified_memory_na(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(dgx_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(dgx_mod.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 0, stdout="0, NVIDIA GB10, [N/A], [N/A], 3, [N/A]\n", stderr=""))
    monkeypatch.setattr(dgx_mod, "_system_memory_mb", lambda: (98000.0, 121000.0))
    g = dgx_mod.sample_gpus()
    assert g["available"] and g["gpus"][0]["unified_memory"] and g["gpus"][0]["mem_total_mb"] == 121000.0
    assert g["gpus"][0]["util_pct"] == 3.0 and g["gpus"][0]["temp_c"] is None


def test_cockpit_page_has_the_three_inspector_views_and_works_offline():
    """TEAM.md round 5, C group: verdicts side by side, regression, cluster drill-down, all on the board and offline."""
    from sparkjury.api.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for hook in ['data-tab="verdicts"', 'data-tab="clusters"', 'data-tab="regress"', 'id="trSel"', 'id="rgBefore"', 'id="clusters"']:
        assert hook in html, hook
    # each view reads the M8 endpoint it is built on
    assert '"/traces/"+enc(tid)' in html and '"/traces?cluster="+cid' in html and '"/regress?before="+enc(before)' in html
    # a replayed run_end must not reload the run (that looped forever)
    assert 'if(ev.kind==="run_end"&&live)' in html
    # offline: no external scripts, styles, fonts or images
    assert not re.search(r'(src|href)=["\']https?://', html), "cockpit must not load anything from the network"
    assert "<script src" not in html and "@import" not in html


# ---- 请求里的路径不能决定宿主机上删/写哪个文件 ------------------------------------------------
#
# 这些用例都是先对着旧代码跑红过的。注意 URL 里的 run_id 一直没被打穿过：Starlette 的 {run_id}
# 默认只匹配 [^/]+，斜杠进不来。真正能带路径的是请求体里的 run_id / db，那条路没有任何中间件拦。

BAD_RUN_IDS = ["../escape", "..", "a/b", "a\\b", "/etc/passwd", ".hidden", "x" * 200]


def test_start_run_rejects_run_id_that_escapes_runs_dir(client, tmp_path):
    """run_id 会被拼成 runs_dir/<run_id>/manifest.json，以前原样接受。"""
    for bad in BAD_RUN_IDS:
        r = client.post("/runs", json={"demo": True, "run_id": bad})
        assert r.status_code == 400, f"run_id={bad!r} 应为 400，实际 {r.status_code}"
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_start_run_rejects_db_outside_runs_dir(client, tmp_path):
    """db 以前原样进 RunConfig，而 reset_db 会在开跑前 unlink 它 —— 一个请求删掉任意文件。"""
    victim = tmp_path / "victim.db"
    victim.write_text("precious", encoding="utf-8")
    assert client.post("/runs", json={"demo": True, "db": str(victim)}).status_code == 400
    assert victim.read_text(encoding="utf-8") == "precious"
    # 用 .. 绕回来的写法一样拒
    r = client.post("/runs", json={"demo": True, "db": str(tmp_path / "runs" / ".." / "victim.db")})
    assert r.status_code == 400 and "db 必须位于" in r.text
    assert victim.read_text(encoding="utf-8") == "precious"


def test_manifest_reads_reject_illegal_run_id(tmp_path):
    """run_dir() 是所有读写的公共出口，校验放在那儿，读和写就都出不去 runs_dir。"""
    mgr = RunManager(tmp_path / "runs")
    assert mgr.run_dir("ok-1.2_3") == (tmp_path / "runs" / "ok-1.2_3").resolve()
    for bad in BAD_RUN_IDS + [""]:
        with pytest.raises(InvalidRunId):
            mgr.run_dir(bad)
        with pytest.raises(InvalidRunId):  # manifest 走 run_dir，读盘前就得拦住
            mgr.manifest(bad)
        with pytest.raises(InvalidRunId):
            mgr.events(bad)
        with pytest.raises(InvalidRunId):
            mgr.db_path(bad)


def test_resolve_within_keeps_paths_inside_the_root(tmp_path):
    root = tmp_path / "runs"
    (root / "sub").mkdir(parents=True)
    assert resolve_within(root, root / "sub" / "x.db", "db") == (root / "sub" / "x.db").resolve()
    for bad in [tmp_path / "out.db", root / ".." / "out.db", Path("/etc/hosts"), root / "sub" / ".." / ".." / "out.db"]:
        with pytest.raises(ValueError):
            resolve_within(root, bad, "db")
    link = root / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return  # Windows 建 symlink 要权限，建不了就只验上面那几条
    with pytest.raises(ValueError):  # symlink 也要展开后再比，不能只看字符串前缀
        resolve_within(root, link / "out.db", "db")


def test_config_path_must_live_under_the_config_root(tmp_path, monkeypatch):
    """config_path 以前只查"存在吗"，等于把「读宿主机任意路径」做成了一等公民。"""
    monkeypatch.delenv("SPARKJURY_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "cfgroot"
    root.mkdir()
    (root / "ok.toml").write_text('run_id = "from-cfg"\n', encoding="utf-8")
    outside = tmp_path / "outside.toml"
    outside.write_text('run_id = "outside"\n', encoding="utf-8")  # 内容完全合法，位置不合法
    with TestClient(create_app(tmp_path / "runs", probe_endpoints=False, config_root=root)) as c:
        r = c.post("/runs", json={"config_path": str(outside)})
        assert r.status_code == 400 and "config_path 必须位于" in r.text
        assert c.post("/runs", json={"config_path": str(root)}).status_code == 400  # 目录不算文件
        assert c.post("/runs", json={"config_path": "missing.toml"}).status_code == 400
        # 范围内的真文件照旧
        assert c.post("/runs", json={"config_path": str(root / "ok.toml"), "stages": ["BOGUS"]}).status_code == 400


def test_app_without_token_refuses_non_loopback_clients(tmp_path, monkeypatch):
    """没 token 时旧姿势是把服务敞开；现在非回环来源一律 403（红线：对外必须有鉴权）。"""
    monkeypatch.delenv("SPARKJURY_API_TOKEN", raising=False)
    open_app = create_app(tmp_path / "runs", probe_endpoints=False)
    with TestClient(open_app, client=("203.0.113.7", 51234)) as c:
        assert c.get("/runs").status_code == 403
        assert c.get("/").status_code == 403
        assert c.get("/health").status_code == 200  # 健康检查仍然公开，探活要用
    for local in ("127.0.0.1", "::1"):
        with TestClient(open_app, client=(local, 51234)) as c:
            assert c.get("/runs").status_code == 200, local
    # 配了 token 就是 401/放行那条路，不再是 403
    with TestClient(create_app(tmp_path / "runs", probe_endpoints=False, token="t"), client=("203.0.113.7", 51234)) as c:
        assert c.get("/runs").status_code == 401
        assert c.get("/runs?token=t").status_code == 200


# ---- 决策事件生产端（PR 接口 ③） ---------------------------------------------------------

def test_decision_endpoint_writes_a_governance_ledger(client, tmp_path):
    client.post("/runs", json={"demo": True, "run_id": "dec-1"})
    _wait(client, "dec-1")
    cls = client.get("/runs/dec-1/clusters").json()["clusters"]
    top, other = cls[0], cls[-1]
    assert re.match(r"^F\d\d$", top["taxonomy_id"])       # 响应里带 pack 类目：看板发 override 要用

    # actor 必填：账本里的决策必须能追溯到人
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "accept_card", "cluster_id": top["cluster_id"]}).status_code == 400
    # 未归类（cluster_id = -1）不是可拍板的对象
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "accept_card", "cluster_id": -1, "actor": "pm-li"}).status_code == 404
    # pack 层动作不走这个端点
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "thaw_pack", "cluster_id": top["cluster_id"], "actor": "pm-li"}).status_code == 400

    r = client.post("/runs/dec-1/decision", json={"decision_kind": "accept_card", "cluster_id": top["cluster_id"], "actor": "pm-li"})
    assert r.status_code == 201
    assert r.json()["confirm"]["cluster_id"] == top["cluster_id"]
    assert r.json()["event"]["payload"]["target"] == f"card:{top['cluster_id']}"

    # 换一类：必须带 taxonomy_id（匹配键）+ to_rank + system_top/human_top（校准投影用）
    r = client.post("/runs/dec-1/decision", json={"decision_kind": "override_priority", "cluster_id": other["cluster_id"],
                                                 "actor": "pm-li", "rationale": "先修这一类"})
    assert r.status_code == 201, r.text
    payload = r.json()["event"]["payload"]
    assert payload["taxonomy_id"] == other["taxonomy_id"] == payload["human_top"] != payload["system_top"]
    assert payload["to_rank"] == 1 and payload["target"] == f"priority:{other['taxonomy_id']}"
    # 选的还是系统给的第一名 -> 没有换类目，拒掉（否则会写进一条没有意义的 override）
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "override_priority", "cluster_id": top["cluster_id"],
                                                     "actor": "pm-li"}).status_code == 400
    # 不修：账本不接受没有理由的否决
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "reject_proposal", "cluster_id": other["cluster_id"],
                                                     "actor": "pm-li"}).status_code == 400
    assert client.post("/runs/dec-1/decision", json={"decision_kind": "reject_proposal", "cluster_id": other["cluster_id"],
                                                     "actor": "pm-li", "rationale": "成本高于收益"}).status_code == 201

    lines = [json.loads(x) for x in (tmp_path / "governance" / "events.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [e["event_id"] for e in lines] == ["evt-0001", "evt-0002", "evt-0003"]
    assert all(e["run_id"] == "dec-1" and e["skill"] == "report" for e in lines)
    for e in lines:
        validate_event(e)  # 消费端（PR#10 govern 的 _ledger.py）同一套规矩：target 必须带命名空间前缀
