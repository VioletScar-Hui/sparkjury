"""FastAPI backend (M8): runs, SSE timeline, card, traces, confirm, regress, DGX panel, static cockpit."""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sparkjury import __version__, pack as pack_mod
from sparkjury.api import dgx as dgx_mod
from sparkjury.api.runs import InvalidRunId, RunManager, check_run_id, resolve_within
from sparkjury.governance import DecisionLedger, LedgerError, card_target, decision_event, priority_target
from sparkjury.harness import EventKind, RunConfig, Stage
from sparkjury.models.cluster import clusters_payload
from sparkjury.report import build_card
from sparkjury.store import TraceStore

STATIC_DIR = Path(__file__).parent / "static"


class StartRun(BaseModel):
    demo: bool = False
    config_path: str | None = None
    run_id: str | None = None
    db: str | None = None
    stages: list[str] | None = None
    evalset_limit: int | None = None


class Confirm(BaseModel):
    cluster_id: int
    note: str = ""
    decided_by: str = "pm"


class Decision(BaseModel):
    """卡片上的拍板动作。

    `decision_kind` 只开三个「人对着卡片做」的动作：`accept_card`（就按系统给的先修这一类）、
    `override_priority`（换一类优先）、`reject_proposal`（这一类不修）。pack 层的动作
    （approve_pack_diff / thaw_pack）不走这个端点，它们改的是评测标准，不是这一轮的修复顺序。

    `taxonomy_id` / `to_rank` / `system_top` / `human_top` 可以不给：服务端按簇标签和
    `standards/label-taxonomy-map.yaml` 自己补（`human_top` 取被点的那一类，`system_top` 取系统排在第一的
    那一类）。补不出来的 `override_priority` 直接 400，不写一条会被消费端判 unmatched 的记录。
    """

    decision_kind: str
    actor: str = ""
    cluster_id: int | None = None
    taxonomy_id: str | None = None
    to_rank: int | None = None
    system_top: str | None = None
    human_top: str | None = None
    rationale: str = ""
    note: str = ""


#: 卡片上「人对着结果拍板」的三个动作。其余 decision_kind（approve_pack_diff / thaw_pack）改的是
#: 评测标准本身，属 pack 层流程，不走这个端点。
PM_DECISIONS = ("accept_card", "override_priority", "reject_proposal")


def create_app(runs_dir: str | Path = "runs", *, dgx_endpoints: list[dict[str, str]] | None = None, probe_endpoints: bool = True,
               token: str | None = None, config_root: str | Path | None = None,
               ledger_path: str | Path | None = None) -> FastAPI:
    """`token` (or env SPARKJURY_API_TOKEN) protects every route except /health: pass it as
    `Authorization: Bearer <token>` or `?token=<token>` (EventSource cannot set headers).

    没设 token 时只服务本机来的请求（见 `_is_local_client`）：节点手册的红线是「8888 和 9000 上对外
    提供的服务必须有鉴权」，所以「没配 token 就把服务敞开给公网」不能是一个允许的默认值。进程内的
    ASGI 调用（`TestClient`）没有网络对端，按本机算，测试和嵌入式用法照旧。

    `config_root`（默认当前工作目录）圈定 `POST /runs` 里 `config_path` 的允许范围：调用方只能指向
    服务进程自己目录树里的配置文件，不能拿这个字段当「读宿主机任意路径」的开关。

    `ledger_path` 是决策账本落盘位置，默认 `governance/events.jsonl`（仓库根，`SPARKJURY_LEDGER` 也能覆盖）：
    卡片上按下去的每一个动作都要留下一条能被治理层读走的记录，写不下去就报错，不静默吞掉。
    """
    app = FastAPI(title="SparkJury API", version=__version__)
    mgr = RunManager(runs_dir)
    app.state.manager = mgr
    api_token = token if token is not None else os.environ.get("SPARKJURY_API_TOKEN") or None
    app.state.token = api_token
    cfg_root = Path(config_root).resolve() if config_root is not None else Path.cwd()
    ledger = DecisionLedger(ledger_path)
    app.state.ledger = ledger

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if request.url.path in ("/health",):
            return await call_next(request)
        if api_token:
            auth = request.headers.get("authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
            if not given or not secrets.compare_digest(given, api_token):
                return JSONResponse({"detail": "unauthorized: pass Authorization: Bearer <token> or ?token="}, status_code=401)
        elif not _is_local_client(request):
            # 不靠"启动时打没打警告"来保证：不管这个 app 是怎么被起起来的（uvicorn 直接指向它、
            # 别人写脚本 import 它），公网来的请求一律拒掉。
            return JSONResponse(
                {"detail": "refusing unauthenticated access from a non-loopback client: set SPARKJURY_API_TOKEN (or serve --token)"},
                status_code=403,
            )
        return await call_next(request)

    # ---- helpers ----------------------------------------------------------

    def _run_dir_or_400(run_id: str) -> Path:
        """run_id -> runs_dir 下的目录；非法 run_id 报 400，不让它在文件系统上跳出去。"""
        try:
            return mgr.run_dir(run_id)
        except InvalidRunId as e:
            raise HTTPException(400, str(e)) from e

    def _manifest_or_404(run_id: str) -> dict[str, Any]:
        _run_dir_or_400(run_id)
        m = mgr.manifest(run_id)
        if not m:
            raise HTTPException(404, f"run {run_id} not found")
        return m

    def _config_path_or_400(config_path: str) -> Path:
        """config_path 只能指向 config_root 之内的真文件。"""
        try:
            p = resolve_within(cfg_root, config_path, "config_path")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not p.is_file():
            raise HTTPException(400, f"config not found: {config_path}")
        return p

    def _db_path_or_400(db: str) -> str:
        """调用方给的 db 必须在 runs_dir 之内。

        以前这个字段原样进 RunConfig，而 reset_db 的库会在开跑前被 unlink()：
        一个 POST /runs {"demo": true, "db": "<任意路径>"} 就能删掉宿主机上的任意文件。
        """
        try:
            return str(resolve_within(mgr.runs_dir, db, "db"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def _store(run_id: str) -> TraceStore:
        _manifest_or_404(run_id)
        db = mgr.db_path(run_id)
        if not db or not db.exists():
            raise HTTPException(409, f"run {run_id} has no database yet")
        return TraceStore(db)

    def _cluster_run_or_409(run_id: str):
        with _store(run_id) as store:
            run = store.get_cluster_run()
        if run is None:
            raise HTTPException(409, f"run {run_id} has no clusters yet; run the CLUSTER stage first")
        return run

    def _cluster_or_404(run, cluster_id: int):
        cl = next((c for c in run.clusters if c.cluster_id == cluster_id), None)
        if cl is None or cl.cluster_id == -1:
            raise HTTPException(404, f"cluster {cluster_id} not found")
        return cl

    def _system_top(run) -> str | None:
        """系统排第一的那个类目（= 卡片 recommendation 指的那一类）的 F 码。"""
        for c in sorted((x for x in run.clusters if x.cluster_id != -1), key=lambda x: x.rank):
            cat = pack_mod.category_for_label(c.label.value)
            if cat:
                return cat
        return None

    def _taxonomy_of(cluster) -> str | None:
        return pack_mod.category_for_label(cluster.label.value)

    def _with_taxonomy(d: dict[str, Any]) -> dict[str, Any]:
        """响应里的每个簇补一个 `taxonomy_id`（映射来自 `standards/label-taxonomy-map.yaml`）。

        只在响应里补：落盘的 `card.json` / `clusters.json` 仍然与 `Cluster` 字段一一对应，不给下游文件
        契约随手加字段。看板要显示类目、要发 override_priority 都需要这个码，缺了按钮就没法用。
        """
        for cl in d.get("clusters") or []:
            if isinstance(cl, dict) and cl.get("taxonomy_id") is None:
                cl["taxonomy_id"] = pack_mod.category_for_label(cl.get("label"))
        return d

    def _record_confirm(run_id: str, cl, note: str, decided_by: str) -> dict[str, Any]:
        """写 `confirm.json`（老接口留下的进度文件，看板拿它显示「已确认」）。"""
        m = mgr.manifest(run_id) or {}
        rec = {"run_id": run_id, "cluster_id": cl.cluster_id, "label": cl.label.value, "rank": cl.rank, "size": cl.size,
               "suggestion": cl.suggestion, "note": note, "decided_by": decided_by, "ts": time.time(),
               "next": f"apply the fix, re-run the same tasks, then: sparkjury regress --before {m.get('config', {}).get('db')} --after <new db> --gate"}
        p = mgr.run_dir(run_id) / "confirm.json"
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return rec

    def _append_decision(run_id: str, cl, kind: str, actor: str, *, taxonomy_id: str | None = None,
                         to_rank: int | None = None, system_top: str | None = None, human_top: str | None = None,
                         rationale: str = "", note: str = "") -> dict[str, Any]:
        extra: dict[str, Any] = {"cluster_id": cl.cluster_id, "label": cl.label.value, "rank": cl.rank, "size": cl.size}
        if note:
            extra["note"] = note
        ev = decision_event(run_id=run_id, decision_kind=kind, actor=actor,
                            target=(priority_target(taxonomy_id) if kind == "override_priority" else card_target(cl.cluster_id)),
                            skill="report", taxonomy_id=taxonomy_id, to_rank=to_rank, system_top=system_top,
                            human_top=human_top, rationale=rationale, extra=extra)
        try:
            return ledger.append(ev)
        except LedgerError as e:
            raise HTTPException(400, str(e)) from e

    # ---- runs -------------------------------------------------------------

    @app.post("/runs", status_code=202)
    def start_run(body: StartRun) -> dict[str, Any]:
        if body.demo:
            cfg = RunConfig.demo(run_id=body.run_id, db=None)
        elif body.config_path:
            cfg = RunConfig.from_toml(_config_path_or_400(body.config_path))
        else:
            raise HTTPException(400, "give demo=true or config_path")
        if body.run_id:
            try:
                cfg.run_id = check_run_id(body.run_id)
            except InvalidRunId as e:
                raise HTTPException(400, str(e)) from e
        if body.db:
            cfg.db = _db_path_or_400(body.db)
        if body.stages:
            try:
                cfg.stages = [Stage(s.upper()) for s in body.stages]
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if body.evalset_limit:
            cfg.evalset.limit = body.evalset_limit
        if body.demo and not body.db:
            cfg.db = str(Path(runs_dir) / cfg.run_id / "sparkjury.db")
        try:
            run_id = mgr.start(cfg)
        except InvalidRunId as e:
            raise HTTPException(400, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"run_id": run_id, "status": "running", "events": f"/runs/{run_id}/events"}

    @app.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        return mgr.list_runs()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        m = dict(_manifest_or_404(run_id))
        m.pop("traceback", None)
        m["running"] = mgr.is_running(run_id)
        return m

    @app.get("/runs/{run_id}/events")
    def stream_events(run_id: str, since: int = Query(0, ge=0), follow: bool = True):
        _manifest_or_404(run_id)

        def gen():
            last = since
            for ev in mgr.events(run_id, since):
                last = ev.seq
                yield _sse(ev.kind.value, ev.model_dump_json())
                if ev.kind == EventKind.RUN_END:
                    return
            q = mgr.subscribe(run_id) if follow else None
            if q is None:
                yield _sse("end", json.dumps({"run_id": run_id, "reason": "not running"}))
                return
            try:
                idle = 0.0
                while True:
                    try:
                        ev = q.get(timeout=1.0)
                    except queue.Empty:
                        idle += 1.0
                        if idle >= 15.0:
                            idle = 0.0
                            yield ": keepalive\n\n"
                        continue
                    if ev is None:
                        yield _sse("end", json.dumps({"run_id": run_id, "reason": "finished"}))
                        return
                    if ev.seq <= last:
                        continue
                    last = ev.seq
                    yield _sse(ev.kind.value, ev.model_dump_json())
                    if ev.kind == EventKind.RUN_END:
                        return
            finally:
                mgr.unsubscribe(run_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/runs/{run_id}/events.json")
    def events_json(run_id: str, since: int = 0) -> list[dict[str, Any]]:
        _manifest_or_404(run_id)
        return [e.model_dump(mode="json") for e in mgr.events(run_id, since)]

    # ---- results --------------------------------------------------------------

    @app.get("/runs/{run_id}/card")
    def get_card(run_id: str) -> dict[str, Any]:
        p = _run_dir_or_400(run_id) / "card" / "card.json"
        if p.exists():
            return _with_taxonomy(json.loads(p.read_text(encoding="utf-8")))
        with _store(run_id) as store:
            return _with_taxonomy(build_card(store, run_id=run_id).model_dump(mode="json"))

    @app.get("/runs/{run_id}/card.html", response_class=HTMLResponse)
    def get_card_html(run_id: str) -> str:
        p = _run_dir_or_400(run_id) / "card" / "card.html"
        if p.exists():
            return p.read_text(encoding="utf-8")
        from sparkjury.report import render_html

        with _store(run_id) as store:
            return render_html(build_card(store, run_id=run_id))

    @app.get("/runs/{run_id}/clusters")
    def get_clusters(run_id: str) -> dict[str, Any]:
        with _store(run_id) as store:
            run = store.get_cluster_run()
        if not run:
            return {"n_badcases": 0, "n_clusters": 0, "n_noise": 0, "clusters": []}
        return _with_taxonomy(clusters_payload(run))

    @app.get("/runs/{run_id}/traces")
    def list_traces(run_id: str, cluster: int | None = None, failed: bool | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with _store(run_id) as store:
            if cluster is not None:
                return [b.model_dump(mode="json") for b in store.list_badcases(cluster)]
            rows = []
            for t in store.list(success=(False if failed else None), limit=limit):
                pre = store.get_precheck(t.trace_id)
                dec = store.get_decision(t.trace_id)
                rows.append({
                    "trace_id": t.trace_id, "task_id": t.task_id, "trial": t.trial, "success": t.outcome.success,
                    "termination": t.outcome.termination_reason, "n_steps": t.metrics.n_steps, "n_tool_calls": t.metrics.n_tool_calls,
                    "env_failure": bool(pre and pre.is_env_failure), "final_scores": dec.scores if dec else None,
                    "degraded": dec.any_degraded if dec else None,
                })
            return rows

    @app.get("/runs/{run_id}/traces/{trace_id}")
    def get_trace(run_id: str, trace_id: str) -> dict[str, Any]:
        with _store(run_id) as store:
            t = store.get(trace_id)
            if not t:
                raise HTTPException(404, f"trace {trace_id} not found")
            pre, panel, dec = store.get_precheck(trace_id), store.get_panel_result(trace_id), store.get_decision(trace_id)
        return {
            "trace": t.model_dump(mode="json"), "transcript": t.transcript(),
            "precheck": pre.model_dump(mode="json") if pre else None,
            "panel": panel.model_dump(mode="json") if panel else None,
            "decision": dec.model_dump(mode="json") if dec else None,
        }

    @app.post("/runs/{run_id}/confirm")
    def confirm(run_id: str, body: Confirm) -> dict[str, Any]:
        """老的「先修这一类」入口：写 confirm.json，同时落一条 accept_card 决策事件。

        两件一起做是有意的：看板上那个按钮的语义就是「接受系统给的第一类」，治理层要看的正是这个动作。
        只写 confirm.json 的话，点了确认而账本里什么都没有，黄金决策池就一直是空的。
        """
        _manifest_or_404(run_id)
        run = _cluster_run_or_409(run_id)
        cl = _cluster_or_404(run, body.cluster_id)
        rec = _record_confirm(run_id, cl, body.note, body.decided_by)
        res = _append_decision(run_id, cl, "accept_card", body.decided_by or "pm", taxonomy_id=_taxonomy_of(cl), note=body.note)
        rec["event"] = res["event"]
        return rec

    @app.get("/runs/{run_id}/confirm")
    def get_confirm(run_id: str) -> dict[str, Any] | None:
        _manifest_or_404(run_id)
        p = mgr.run_dir(run_id) / "confirm.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    @app.post("/runs/{run_id}/decision", status_code=201)
    def post_decision(run_id: str, body: Decision) -> dict[str, Any]:
        """卡片拍板 -> 一条 append-only 决策事件（治理层消费：prioritize 的 override、govern 的投影）。"""
        _manifest_or_404(run_id)
        if body.decision_kind not in PM_DECISIONS:
            raise HTTPException(400, f"decision_kind 只能是 {PM_DECISIONS} 之一（卡片上的三个动作）；"
                                     "pack 层动作（approve_pack_diff / thaw_pack）改的是评测标准，不走这个端点")
        actor = (body.actor or os.environ.get("SPARKJURY_ACTOR") or "").strip()
        if not actor:
            raise HTTPException(400, "actor 必填：看板上填拍板人（或给服务设 SPARKJURY_ACTOR）。"
                                     "账本里的决策必须能追溯到人，不接受匿名写入")
        if body.cluster_id is None:
            raise HTTPException(400, "cluster_id 必填：决策要指到具体某一类问题")
        run = _cluster_run_or_409(run_id)
        cl = _cluster_or_404(run, body.cluster_id)
        taxonomy_id = body.taxonomy_id or _taxonomy_of(cl)
        if body.decision_kind == "override_priority":
            if not taxonomy_id:
                raise HTTPException(400, f"簇 {cl.cluster_id} 的标签 {cl.label.value} 没有 pack 类目映射："
                                         "override_priority 必须带 taxonomy_id，否则消费端把这条判 unmatched、"
                                         "排序不会变。先把 standards/label-taxonomy-map.yaml 补齐，或在请求里显式给 taxonomy_id")
            system_top = body.system_top or _system_top(run)
            human_top = body.human_top or taxonomy_id
            if system_top and system_top == human_top:
                raise HTTPException(400, f"system_top 与 human_top 都是 {human_top}：没有换类目，"
                                         "要「就按系统的来」用 accept_card")
            res = _append_decision(run_id, cl, body.decision_kind, actor, taxonomy_id=taxonomy_id,
                                   to_rank=body.to_rank or 1, system_top=system_top, human_top=human_top,
                                   rationale=body.rationale.strip(), note=body.note)
        else:
            res = _append_decision(run_id, cl, body.decision_kind, actor, taxonomy_id=taxonomy_id,
                                   rationale=body.rationale.strip(), note=body.note)
        out: dict[str, Any] = {"event": res["event"], "duplicate": res["duplicate"], "ledger": res["path"]}
        if body.decision_kind == "accept_card":
            out["confirm"] = _record_confirm(run_id, cl, body.note, actor)
        return out

    @app.get("/runs/{run_id}/regress")
    def regress(run_id: str, before: str = Query(..., description="run_id of the earlier run")) -> dict[str, Any]:
        from sparkjury.regress import compare

        _manifest_or_404(before)
        db_b, db_a = mgr.db_path(before), mgr.db_path(run_id)
        if not db_b or not db_b.exists() or not db_a or not db_a.exists():
            raise HTTPException(409, "both runs need a database")
        r = compare(db_b, db_a, before_label=before, after_label=run_id)
        d = r.model_dump(mode="json")
        d["verdict"] = r.verdict
        return d

    # ---- DGX & misc --------------------------------------------------------------

    @app.get("/dgx")
    def dgx() -> dict[str, Any]:
        return dgx_mod.snapshot(dgx_endpoints, probe=probe_endpoints)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "runs_dir": str(runs_dir), "auth": bool(api_token)}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        p = STATIC_DIR / "index.html"
        if not p.exists():
            return "<h1>SparkJury</h1><p>cockpit page missing</p>"
        return p.read_text(encoding="utf-8")

    return app


def _is_local_client(request: Request) -> bool:
    """请求是不是来自本机。

    `request.client` 由 ASGI 服务器按真实 socket 填，调用方改不了。不是 IP 的（`TestClient` 用的
    "testclient"）说明是进程内调用，根本没有网络对端，按本机算；IPv4-mapped 的
    `::ffff:127.0.0.1` 也算本机。
    """
    host = request.client.host if request.client else None
    if not host:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(addr.is_loopback or (mapped is not None and mapped.is_loopback))


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


app = create_app()
