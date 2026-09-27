"""Run manager: starts pipeline runs in background threads and fans events out to SSE subscribers."""

from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path
from typing import Any

from sparkjury.harness import Event, EventBus, EventKind, Orchestrator, RunConfig

# run_id 会被直接当成一层目录名拼进 runs_dir，所以只收单层名字。
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InvalidRunId(ValueError):
    """run_id 不是合法的单层目录名（带分隔符、`..`、以点开头、空、超长）。"""


def check_run_id(run_id: str) -> str:
    """校验 run_id 只能是一层目录名，返回原值。

    run_id 从 URL 和请求体两处进来，随后被拼成 `runs_dir/<run_id>/manifest.json`、`card/card.html`、
    `confirm.json`、`events.jsonl`。以前这里一个字符都不校验。URL 那条路碰巧是安全的：Starlette 的
    `{run_id}` 默认只匹配 `[^/]+`，斜杠根本进不来。但请求体那条路没有任何东西挡着，实测
    `POST /runs {"demo": true, "run_id": "../escape"}` 返回 202，并在 runs_dir 外面写下了
    manifest.json、events.jsonl、evalset.json、card/ 和 sparkjury.db。路由碰巧挡住不等于校验。

    这里刻意不做"清洗"（把 `/` 换成 `_` 之类）：那会让一个非法输入悄悄变成另一个合法 id，
    调用方以为自己在写 A，实际写到了 B。非法就是非法，报错让调用方自己改。
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise InvalidRunId(f"invalid run_id: {run_id!r} (只允许单层目录名：字母数字开头，可含 . _ -，最长 128)")
    return run_id


def resolve_within(base: str | Path, candidate: str | Path, what: str) -> Path:
    """把调用方给的路径归位到 base 之内，跳出去就报错，返回归位后的绝对路径。

    用 resolve() 而不是字符串前缀比较，因为它会展开 symlink：`runs/link -> /etc` 这种
    也能拦下来。相对路径按当前工作目录解释（不按 base），免得同一个相对路径在
    「相对谁」上产生第二套含义。
    """
    root = Path(base).resolve()
    p = Path(candidate).expanduser().resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"{what} 必须位于 {root} 之内，收到 {candidate}（归位后 {p}）")
    return p


class RunManager:
    def __init__(self, runs_dir: str | Path = "runs"):
        self.runs_dir = Path(runs_dir).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._live: dict[str, dict[str, Any]] = {}     # run_id -> {"events": [...], "subs": set[Queue], "done": bool, "thread": Thread}

    # ---- lifecycle ------------------------------------------------------------

    def start(self, cfg: RunConfig) -> str:
        # run_id 可能来自请求体或配置文件，进任何文件写操作之前先钉死成单层目录名
        run_id = check_run_id(cfg.run_id)
        with self._lock:
            if run_id in self._live and not self._live[run_id]["done"]:
                raise ValueError(f"run {run_id} is already running")
            state: dict[str, Any] = {"events": [], "subs": set(), "done": False, "thread": None, "manifest": None}
            self._live[run_id] = state
        cfg.runs_dir = str(self.runs_dir)
        orch = Orchestrator(cfg)
        orch.bus.subscribe(lambda ev: self._on_event(run_id, ev))

        def _target() -> None:
            try:
                state["manifest"] = orch.run()
            finally:
                state["done"] = True
                for q in list(state["subs"]):
                    q.put(None)

        t = threading.Thread(target=_target, name=f"sparkjury-run-{run_id}", daemon=True)
        state["thread"] = t
        t.start()
        return run_id

    def _on_event(self, run_id: str, ev: Event) -> None:
        state = self._live.get(run_id)
        if not state:
            return
        state["events"].append(ev)
        for q in list(state["subs"]):
            q.put(ev)

    def is_running(self, run_id: str) -> bool:
        s = self._live.get(run_id)
        return bool(s) and not s["done"]

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        s = self._live.get(run_id)
        if not s or not s["thread"]:
            return None
        s["thread"].join(timeout)
        return s["manifest"]

    # ---- reads ----------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        """run_id -> runs_dir 下的一层目录。

        manifest / events / card / card.html / confirm.json 全都从这里拼路径，所以校验放在这一处：
        守住这里，读和写就都出不去 runs_dir 了。
        """
        return self.runs_dir / check_run_id(run_id)

    def manifest(self, run_id: str) -> dict[str, Any] | None:
        s = self._live.get(run_id)
        if s and s["manifest"]:
            return s["manifest"]
        p = self.run_dir(run_id) / "manifest.json"
        if not p.exists():
            if s:  # running, no manifest yet
                return {"run_id": run_id, "status": "running", "stages": {}, "degradations": []}
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        seen = set()
        for p in sorted(self.runs_dir.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            seen.add(m.get("run_id", p.parent.name))
            out.append(_summary(m))
        for run_id, s in self._live.items():
            if run_id not in seen:
                out.insert(0, {"run_id": run_id, "status": "running", "duration_s": None, "degradations": 0, "pass_rate": None, "n_badcases": None, "recommendation": None})
        return out

    def events(self, run_id: str, since: int = 0) -> list[Event]:
        s = self._live.get(run_id)
        evs = list(s["events"]) if s else EventBus.read_log(self.run_dir(run_id) / "events.jsonl")
        return [e for e in evs if e.seq > since]

    def subscribe(self, run_id: str) -> "queue.Queue[Event | None] | None":
        s = self._live.get(run_id)
        if not s or s["done"]:
            return None
        q: queue.Queue = queue.Queue()
        s["subs"].add(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        s = self._live.get(run_id)
        if s:
            s["subs"].discard(q)

    def db_path(self, run_id: str) -> Path | None:
        m = self.manifest(run_id)
        if not m:
            return None
        db = (m.get("config") or {}).get("db")
        return Path(db) if db else None


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    rep = m.get("stages", {}).get("REPORT", {})
    return {
        "run_id": m.get("run_id"), "status": m.get("status"), "duration_s": m.get("duration_s"),
        "degradations": len(m.get("degradations", [])), "pass_rate": rep.get("pass_rate"), "pass_k": rep.get("pass_k"),
        "n_badcases": rep.get("n_badcases"), "recommendation": rep.get("recommendation"),
        "models": m.get("models", {}), "title": ((m.get("config") or {}).get("report") or {}).get("title"),
    }
