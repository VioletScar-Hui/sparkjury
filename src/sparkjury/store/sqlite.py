"""SQLite-backed store for traces and run manifests.

Single file, zero ops. The full Trace is stored as JSON; a few columns are
extracted for filtering and stats.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sparkjury.models.arbitration import Arbitration, TraceDecision
from sparkjury.models.cluster import BadCase, Cluster, ClusterRun
from sparkjury.models.precheck import PrecheckFlag, PrecheckResult
from sparkjury.models.trace import Trace
from sparkjury.models.verdict import DimensionAgreement, PanelResult, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id            TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    domain              TEXT,
    task_id             TEXT NOT NULL,
    trial               INTEGER NOT NULL DEFAULT 0,
    agent_model         TEXT,
    success             INTEGER,            -- 1 / 0 / NULL
    reward              REAL,
    termination_reason  TEXT,
    n_steps             INTEGER,
    n_tool_calls        INTEGER,
    n_tool_errors       INTEGER,
    duration_s          REAL,
    ingested_at         TEXT NOT NULL,
    json                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_task ON traces(task_id, trial);
CREATE INDEX IF NOT EXISTS idx_traces_success ON traces(success);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    manifest    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    trace_id    TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    judge       TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    model       TEXT,
    score       INTEGER,
    label       TEXT,
    error       TEXT,
    latency_ms  REAL,
    json        TEXT NOT NULL,
    scored_at   TEXT NOT NULL,
    PRIMARY KEY (trace_id, judge, dimension)
);
CREATE INDEX IF NOT EXISTS idx_verdicts_trace ON verdicts(trace_id);

CREATE TABLE IF NOT EXISTS panel (
    trace_id        TEXT PRIMARY KEY REFERENCES traces(trace_id) ON DELETE CASCADE,
    disagreements   TEXT NOT NULL,      -- comma-separated dimensions needing arbitration
    agreement       TEXT NOT NULL,      -- JSON list[DimensionAgreement]
    scored_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arbitration (
    trace_id        TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    dimension       TEXT NOT NULL,
    final_score     INTEGER,
    final_label     TEXT,
    source          TEXT NOT NULL,
    degraded        INTEGER NOT NULL,
    audit_sampled   INTEGER NOT NULL,
    audit_disagrees INTEGER,
    json            TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    PRIMARY KEY (trace_id, dimension)
);

CREATE TABLE IF NOT EXISTS badcases (
    trace_id    TEXT PRIMARY KEY REFERENCES traces(trace_id) ON DELETE CASCADE,
    task_id     TEXT NOT NULL,
    cluster_id  INTEGER,
    severity    REAL NOT NULL,
    failed_dims TEXT NOT NULL,
    json        TEXT NOT NULL,
    built_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id  INTEGER PRIMARY KEY,
    rank        INTEGER NOT NULL,
    label       TEXT NOT NULL,
    size        INTEGER NOT NULL,
    priority    REAL NOT NULL,
    json        TEXT NOT NULL,
    built_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precheck (
    trace_id        TEXT PRIMARY KEY REFERENCES traces(trace_id) ON DELETE CASCADE,
    is_env_failure  INTEGER NOT NULL,
    kinds           TEXT NOT NULL,      -- comma-separated PrecheckKind values
    flags           TEXT NOT NULL,      -- JSON list[PrecheckFlag]
    checked_at      TEXT NOT NULL
);
"""


@dataclass
class Stats:
    n_traces: int = 0
    n_tasks: int = 0
    trials_per_task: dict[int, int] = field(default_factory=dict)  # k -> number of tasks with k trials
    n_with_gold: int = 0
    pass_rate: float | None = None  # mean over traces with gold (= pass^1)
    pass_k: dict[int, float] = field(default_factory=dict)  # k -> fraction of tasks passing all k trials (prefix estimator, legacy)
    pass_k_comb: dict[int, float] = field(default_factory=dict)  # k -> combinatorial C(c,k)/C(n,k), τ-bench estimator (reliability floor)
    pass_at_k: dict[int, float] = field(default_factory=dict)    # k -> at least one success in k draws (capability ceiling)
    avg_steps: float | None = None
    avg_tool_calls: float | None = None
    avg_tool_errors: float | None = None
    avg_duration_s: float | None = None
    termination: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    agent_models: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_traces": self.n_traces,
            "n_tasks": self.n_tasks,
            "trials_per_task": self.trials_per_task,
            "n_with_gold": self.n_with_gold,
            "pass_rate": self.pass_rate,
            "pass_k": self.pass_k,
            "pass_k_comb": self.pass_k_comb,
            "pass_at_k": self.pass_at_k,
            "avg_steps": self.avg_steps,
            "avg_tool_calls": self.avg_tool_calls,
            "avg_tool_errors": self.avg_tool_errors,
            "avg_duration_s": self.avg_duration_s,
            "termination": self.termination,
            "sources": self.sources,
            "agent_models": self.agent_models,
        }


class TraceStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TraceStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- traces -----------------------------------------------------------

    def upsert_traces(self, traces: Iterable[Trace]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for t in traces:
            if t.metrics.n_steps == 0 and t.steps:
                t.compute_metrics()
            rows.append(
                (
                    t.trace_id,
                    t.source.value,
                    t.domain,
                    t.task_id,
                    t.trial,
                    t.agent_model,
                    None if t.outcome.success is None else int(t.outcome.success),
                    t.outcome.reward,
                    t.outcome.termination_reason,
                    t.metrics.n_steps,
                    t.metrics.n_tool_calls,
                    t.metrics.n_tool_errors,
                    t.metrics.duration_s,
                    now,
                    t.model_dump_json(),
                )
            )
        with self._conn:
            self._conn.executemany(
                """INSERT INTO traces (trace_id, source, domain, task_id, trial, agent_model,
                     success, reward, termination_reason, n_steps, n_tool_calls, n_tool_errors,
                     duration_s, ingested_at, json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trace_id) DO UPDATE SET
                     source=excluded.source, domain=excluded.domain, task_id=excluded.task_id,
                     trial=excluded.trial, agent_model=excluded.agent_model, success=excluded.success,
                     reward=excluded.reward, termination_reason=excluded.termination_reason,
                     n_steps=excluded.n_steps, n_tool_calls=excluded.n_tool_calls,
                     n_tool_errors=excluded.n_tool_errors, duration_s=excluded.duration_s,
                     ingested_at=excluded.ingested_at, json=excluded.json""",
                rows,
            )
        return len(rows)

    def get(self, trace_id: str) -> Trace | None:
        row = self._conn.execute("SELECT json FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
        return Trace.model_validate_json(row["json"]) if row else None

    def list(
        self,
        *,
        task_id: str | None = None,
        success: bool | None = None,
        limit: int | None = None,
    ) -> list[Trace]:
        sql = "SELECT json FROM traces"
        cond, args = [], []
        if task_id is not None:
            cond.append("task_id=?")
            args.append(task_id)
        if success is not None:
            cond.append("success=?")
            args.append(int(success))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY task_id, trial"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [Trace.model_validate_json(r["json"]) for r in self._conn.execute(sql, args)]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])

    def export_jsonl(self, out: str | Path) -> int:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with out.open("w", encoding="utf-8") as f:
            for r in self._conn.execute("SELECT json FROM traces ORDER BY task_id, trial"):
                f.write(r["json"] + "\n")
                n += 1
        return n

    # ---- stats ------------------------------------------------------------

    def stats(self) -> Stats:
        rows = self._conn.execute(
            "SELECT task_id, trial, success, n_steps, n_tool_calls, n_tool_errors, duration_s, "
            "termination_reason, source, agent_model FROM traces"
        ).fetchall()
        st = Stats(n_traces=len(rows))
        if not rows:
            return st

        by_task: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_task.setdefault(r["task_id"], []).append(r)
        st.n_tasks = len(by_task)
        st.trials_per_task = dict(Counter(len(v) for v in by_task.values()))

        gold = [r for r in rows if r["success"] is not None]
        st.n_with_gold = len(gold)
        if gold:
            st.pass_rate = sum(r["success"] for r in gold) / len(gold)
            # pass^k: task passes iff all of its first k trials succeed
            max_k = max(len(v) for v in by_task.values())
            for k in range(1, max_k + 1):
                eligible = [v for v in by_task.values() if len(v) >= k and all(x["success"] is not None for x in v[:k])]
                if eligible:
                    st.pass_k[k] = sum(all(x["success"] for x in sorted(v, key=lambda x: x["trial"])[:k]) for v in eligible) / len(eligible)
                    # 组合估计（τ-bench 口径，用全部 trial 而非前 k 个，方差更小）：
                    # pass^k = mean C(c,k)/C(n,k)（可靠性下限）；pass@k = mean 1-C(n-c,k)/C(n,k)（能力上限）。
                    # 二者一起报=给模型一个区间；区间宽度就是稳定性信号。旧 pass_k 保留（首 k 个 trial 口径）。
                    from math import comb
                    pk, pak = [], []
                    for v in eligible:
                        n, c = len(v), sum(bool(x["success"]) for x in v)
                        pk.append(comb(c, k) / comb(n, k))
                        pak.append(1.0 - (comb(n - c, k) / comb(n, k) if n - c >= k else 0.0))
                    st.pass_k_comb[k] = sum(pk) / len(pk)
                    st.pass_at_k[k] = sum(pak) / len(pak)

        def _avg(col: str) -> float | None:
            vals = [r[col] for r in rows if r[col] is not None]
            return (sum(vals) / len(vals)) if vals else None

        st.avg_steps = _avg("n_steps")
        st.avg_tool_calls = _avg("n_tool_calls")
        st.avg_tool_errors = _avg("n_tool_errors")
        st.avg_duration_s = _avg("duration_s")
        st.termination = dict(Counter(r["termination_reason"] or "unknown" for r in rows))
        st.sources = dict(Counter(r["source"] for r in rows))
        st.agent_models = dict(Counter(r["agent_model"] or "unknown" for r in rows))
        return st

    # ---- precheck ---------------------------------------------------------

    def put_precheck(self, results: Iterable[PrecheckResult]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                r.trace_id,
                int(r.is_env_failure),
                ",".join(k.value for k in r.kinds),
                json.dumps([f.model_dump(mode="json") for f in r.flags], ensure_ascii=False),
                now,
            )
            for r in results
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT INTO precheck (trace_id, is_env_failure, kinds, flags, checked_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET is_env_failure=excluded.is_env_failure, kinds=excluded.kinds, "
                "flags=excluded.flags, checked_at=excluded.checked_at",
                rows,
            )
        return len(rows)

    def get_precheck(self, trace_id: str) -> PrecheckResult | None:
        row = self._conn.execute("SELECT trace_id, flags FROM precheck WHERE trace_id=?", (trace_id,)).fetchone()
        if not row:
            return None
        return PrecheckResult(trace_id=row["trace_id"], flags=[PrecheckFlag.model_validate(f) for f in json.loads(row["flags"])])

    def list_precheck(self, *, env_failure: bool | None = None) -> list[PrecheckResult]:
        sql = "SELECT trace_id, flags FROM precheck"
        args: list[Any] = []
        if env_failure is not None:
            sql += " WHERE is_env_failure=?"
            args.append(int(env_failure))
        sql += " ORDER BY trace_id"
        return [
            PrecheckResult(trace_id=r["trace_id"], flags=[PrecheckFlag.model_validate(f) for f in json.loads(r["flags"])])
            for r in self._conn.execute(sql, args)
        ]

    def scorable_traces(self) -> list[Trace]:
        """Traces that passed precheck (or were never prechecked) — the input to judges."""
        sql = (
            "SELECT t.json FROM traces t LEFT JOIN precheck p ON p.trace_id = t.trace_id "
            "WHERE p.trace_id IS NULL OR p.is_env_failure = 0 ORDER BY t.task_id, t.trial"
        )
        return [Trace.model_validate_json(r["json"]) for r in self._conn.execute(sql)]

    def precheck_summary(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT is_env_failure, kinds FROM precheck").fetchall()
        kinds: Counter[str] = Counter()
        for r in rows:
            for k in filter(None, r["kinds"].split(",")):
                kinds[k] += 1
        return {
            "n_checked": len(rows),
            "n_env_failures": sum(r["is_env_failure"] for r in rows),
            "n_scorable": len(rows) - sum(r["is_env_failure"] for r in rows),
            "kinds": dict(kinds),
        }

    # ---- verdicts / panel -------------------------------------------------

    def put_panel_results(self, results: Iterable[PanelResult]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        vrows, prows = [], []
        n = 0
        for r in results:
            n += 1
            for v in r.verdicts:
                vrows.append((v.trace_id, v.judge, v.dimension.value, v.model, v.score, v.label, v.error,
                              v.latency_ms, v.model_dump_json(), now))
            prows.append((r.trace_id, ",".join(d.value for d in r.disagreements),
                          json.dumps([a.model_dump(mode="json") for a in r.agreement], ensure_ascii=False), now))
        with self._conn:
            self._conn.executemany(
                "INSERT INTO verdicts (trace_id, judge, dimension, model, score, label, error, latency_ms, json, scored_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trace_id, judge, dimension) DO UPDATE SET "
                "model=excluded.model, score=excluded.score, label=excluded.label, error=excluded.error, "
                "latency_ms=excluded.latency_ms, json=excluded.json, scored_at=excluded.scored_at",
                vrows,
            )
            self._conn.executemany(
                "INSERT INTO panel (trace_id, disagreements, agreement, scored_at) VALUES (?,?,?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET disagreements=excluded.disagreements, "
                "agreement=excluded.agreement, scored_at=excluded.scored_at",
                prows,
            )
        return n

    def get_panel_result(self, trace_id: str) -> PanelResult | None:
        prow = self._conn.execute("SELECT agreement FROM panel WHERE trace_id=?", (trace_id,)).fetchone()
        if not prow:
            return None
        verdicts = [Verdict.model_validate_json(r["json"]) for r in
                    self._conn.execute("SELECT json FROM verdicts WHERE trace_id=? ORDER BY dimension, judge", (trace_id,))]
        agreement = [DimensionAgreement.model_validate(a) for a in json.loads(prow["agreement"])]
        return PanelResult(trace_id=trace_id, verdicts=verdicts, agreement=agreement)

    def list_panel_results(self, *, needs_arbitration: bool | None = None) -> list[PanelResult]:
        sql = "SELECT trace_id FROM panel"
        if needs_arbitration is True:
            sql += " WHERE disagreements != ''"
        elif needs_arbitration is False:
            sql += " WHERE disagreements = ''"
        sql += " ORDER BY trace_id"
        out = []
        for r in self._conn.execute(sql):
            pr = self.get_panel_result(r["trace_id"])
            if pr:
                out.append(pr)
        return out

    def verdict_summary(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT judge, dimension, score, error, latency_ms FROM verdicts").fetchall()
        prows = self._conn.execute("SELECT disagreements FROM panel").fetchall()
        by_judge: dict[str, dict[str, Any]] = {}
        for r in rows:
            j = by_judge.setdefault(r["judge"], {"n": 0, "errors": 0, "score_sum": 0, "score_n": 0, "latency_sum": 0.0, "latency_n": 0})
            j["n"] += 1
            if r["error"]:
                j["errors"] += 1
            if r["score"] is not None:
                j["score_sum"] += r["score"]
                j["score_n"] += 1
            if r["latency_ms"] is not None:
                j["latency_sum"] += r["latency_ms"]
                j["latency_n"] += 1
        judges = {
            k: {
                "n_verdicts": v["n"],
                "n_errors": v["errors"],
                "mean_score": (v["score_sum"] / v["score_n"]) if v["score_n"] else None,
                "mean_latency_ms": (v["latency_sum"] / v["latency_n"]) if v["latency_n"] else None,
            }
            for k, v in by_judge.items()
        }
        dis: Counter[str] = Counter()
        for r in prows:
            for d in filter(None, r["disagreements"].split(",")):
                dis[d] += 1
        n_traces = len(prows)
        n_dis = sum(1 for r in prows if r["disagreements"])
        return {
            "n_traces_scored": n_traces,
            "n_verdicts": len(rows),
            "n_needing_arbitration": n_dis,
            "agreement_rate": ((n_traces - n_dis) / n_traces) if n_traces else None,
            "disagreements_by_dimension": dict(dis),
            "judges": judges,
        }

    # ---- arbitration ------------------------------------------------------

    def put_decisions(self, decisions: Iterable[TraceDecision]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        n = 0
        for d in decisions:
            n += 1
            for a in d.arbitrations:
                rows.append((a.trace_id, a.dimension.value, a.final_score, a.final_label, a.source.value,
                             int(a.degraded), int(a.audit_sampled),
                             None if a.audit_disagrees is None else int(a.audit_disagrees),
                             a.model_dump_json(), now))
        with self._conn:
            self._conn.executemany(
                "INSERT INTO arbitration (trace_id, dimension, final_score, final_label, source, degraded, "
                "audit_sampled, audit_disagrees, json, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(trace_id, dimension) DO UPDATE SET final_score=excluded.final_score, "
                "final_label=excluded.final_label, source=excluded.source, degraded=excluded.degraded, "
                "audit_sampled=excluded.audit_sampled, audit_disagrees=excluded.audit_disagrees, "
                "json=excluded.json, decided_at=excluded.decided_at",
                rows,
            )
        return n

    def get_decision(self, trace_id: str) -> TraceDecision | None:
        rows = self._conn.execute("SELECT json FROM arbitration WHERE trace_id=? ORDER BY dimension", (trace_id,)).fetchall()
        if not rows:
            return None
        return TraceDecision(trace_id=trace_id, arbitrations=[Arbitration.model_validate_json(r["json"]) for r in rows])

    def list_decisions(self) -> list[TraceDecision]:
        ids = [r["trace_id"] for r in self._conn.execute("SELECT DISTINCT trace_id FROM arbitration ORDER BY trace_id")]
        return [d for d in (self.get_decision(i) for i in ids) if d]

    def arbitration_summary(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT trace_id, dimension, source, degraded, audit_sampled, audit_disagrees, final_score, final_label FROM arbitration"
        ).fetchall()
        by_source = Counter(r["source"] for r in rows)
        n_traces = len({r["trace_id"] for r in rows})
        audited = [r for r in rows if r["audit_sampled"]]
        return {
            "n_traces": n_traces,
            "n_dimensions": len(rows),
            "by_source": dict(by_source),
            "n_degraded": sum(r["degraded"] for r in rows),
            "n_audited_dimensions": len(audited),
            "n_audit_disagreements": sum(1 for r in audited if r["audit_disagrees"]),
            "n_outcome_fail": sum(1 for r in rows if r["dimension"] == "outcome" and r["final_label"] == "fail"),
            "mean_final_score": (statistics.fmean([r["final_score"] for r in rows if r["final_score"] is not None])
                                 if any(r["final_score"] is not None for r in rows) else None),
        }

    # ---- badcases / clusters ---------------------------------------------

    def put_cluster_run(self, run: ClusterRun) -> None:
        """Replace the previous clustering entirely (clusters are recomputed each run)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("DELETE FROM clusters")
            self._conn.execute("DELETE FROM badcases")
            self._conn.executemany(
                "INSERT INTO badcases (trace_id, task_id, cluster_id, severity, failed_dims, json, built_at) VALUES (?,?,?,?,?,?,?)",
                [(b.trace_id, b.task_id, b.cluster_id, b.severity, ",".join(d.value for d in b.failed_dimensions),
                  b.model_dump_json(), now) for b in run.badcases],
            )
            self._conn.executemany(
                "INSERT INTO clusters (cluster_id, rank, label, size, priority, json, built_at) VALUES (?,?,?,?,?,?,?)",
                [(c.cluster_id, c.rank, c.label.value, c.size, c.priority, c.model_dump_json(), now) for c in run.clusters],
            )
            self.put_run("cluster:latest", "CLUSTER", {
                "n_badcases": run.n_badcases, "n_clusters": run.n_clusters, "n_noise": run.n_noise,
                "embedder": run.embedder, "method": run.method,
            })

    def get_cluster_run(self) -> ClusterRun | None:
        meta = self.get_run("cluster:latest")
        if not meta:
            return None
        clusters = [Cluster.model_validate_json(r["json"]) for r in self._conn.execute("SELECT json FROM clusters ORDER BY rank")]
        badcases = [BadCase.model_validate_json(r["json"]) for r in self._conn.execute("SELECT json FROM badcases ORDER BY trace_id")]
        m = meta["manifest"]
        return ClusterRun(n_badcases=m["n_badcases"], n_clusters=m["n_clusters"], n_noise=m["n_noise"],
                          embedder=m["embedder"], method=m["method"], clusters=clusters, badcases=badcases)

    def list_badcases(self, cluster_id: int | None = None) -> list[BadCase]:
        sql, args = "SELECT json FROM badcases", []
        if cluster_id is not None:
            sql += " WHERE cluster_id=?"
            args.append(cluster_id)
        return [BadCase.model_validate_json(r["json"]) for r in self._conn.execute(sql + " ORDER BY severity DESC, trace_id", args)]

    # ---- runs -------------------------------------------------------------

    def put_run(self, run_id: str, stage: str, manifest: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, created_at, stage, manifest) VALUES (?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET stage=excluded.stage, manifest=excluded.manifest",
                (run_id, datetime.now(timezone.utc).isoformat(), stage, json.dumps(manifest, ensure_ascii=False)),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "stage": row["stage"],
            "manifest": json.loads(row["manifest"]),
        }
