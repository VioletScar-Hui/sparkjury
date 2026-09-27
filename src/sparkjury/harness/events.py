"""Event bus: every state change of a run is an event, consumed by the CLI, the JSONL log and (M8) SSE."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    RUN_START = "run_start"
    RUN_END = "run_end"
    STAGE_START = "stage_start"
    STAGE_END = "stage_end"
    PROGRESS = "progress"
    DEGRADED = "degraded"
    WARNING = "warning"
    ERROR = "error"


class Event(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    seq: int = 0
    run_id: str
    stage: str | None = None
    kind: EventKind
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


Listener = Callable[[Event], None]


def _last_seq(path: Path) -> int:
    """读日志里最后一条的 seq。文件被写坏（半行）时退回 0，宁可重号一次也不炸。"""
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return 0
    if not lines:
        return 0
    try:
        return int(json.loads(lines[-1]).get("seq", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


class EventBus:
    def __init__(self, run_id: str, log_path: str | Path | None = None, *, append: bool = False):
        """`append=False`（默认）清空日志开一次新 run；`append=True` 接着上一次的事件流往后写。

        追加模式是给「中断后接着跑」用的：恢复一次已有的 run 时把事件流清掉，等于把上次
        跑到哪儿的证据一起删了。追加模式下 seq 从文件里最后一条接着数，不重号。
        """
        self.run_id = run_id
        self._listeners: list[Listener] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.events: list[Event] = []
        self._log = Path(log_path) if log_path else None
        if self._log:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            if append and self._log.is_file():
                self._seq = _last_seq(self._log)
            else:
                self._log.write_text("", encoding="utf-8")

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def publish(self, kind: EventKind, message: str = "", *, stage: str | None = None, **data: Any) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(seq=self._seq, run_id=self.run_id, stage=stage, kind=kind, message=message, data=data)
            self.events.append(ev)
            if self._log:
                with self._log.open("a", encoding="utf-8") as f:
                    f.write(ev.model_dump_json() + "\n")
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a listener must never break the run
                pass
        return ev

    @staticmethod
    def read_log(path: str | Path) -> list[Event]:
        p = Path(path)
        if not p.exists():
            return []
        return [Event.model_validate_json(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
