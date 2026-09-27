"""操作状态机：中层真正缺的那块。

M13 的循环只会「一路往前跑」，跑到哪算哪；进程一没了，谁也说不清上次是在跑、还是跑完了、
还是跑到一半被 kill 了。这一层把「一次 run」变成**一条可恢复的操作**：

- 只认三种操作：`run`（跑一轮对话）、`compaction`（压缩历史）、`navigation`（切换活动分支）。
- 日志只追加：一行一次状态变化，当前状态是折叠出来的，历史一行不删。
- `accept` 建操作、`drive` 推进它、`inspectExecution` 看现场——对应 pi 的原语。

为什么值得单开一层：中断恢复全靠它。没有操作日志，恢复时只能靠猜「上一条 tool_result
后面到底该不该再问一次模型」；有了它，恢复的起点就是「最后一个没结束的操作 + 会话树上的
叶子」，两者互相印证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from sparkjury.agent.store import append_jsonl, read_jsonl


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperationKind(StrEnum):
    RUN = "run"                 # 一轮对话
    COMPACTION = "compaction"   # 压缩历史
    NAVIGATION = "navigation"   # 切换活动分支


class OperationStatus(StrEnum):
    PENDING = "pending"         # 建了，还没开始
    RUNNING = "running"         # 正在跑
    COMPLETED = "completed"     # 正常结束
    FAILED = "failed"           # 出错结束
    ABORTED = "aborted"         # 被人或信号中断


TERMINAL_STATUSES = frozenset({OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ABORTED})


@dataclass
class Operation:
    """折叠出来的当前状态。`records` 是它在日志里出现了几次，用来判断是不是被反复推进。"""

    id: str
    kind: OperationKind
    status: OperationStatus = OperationStatus.PENDING
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    records: int = 1

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def unfinished(self) -> bool:
        return not self.finished

    def summary(self) -> str:
        bits = [f"{self.id} {self.kind}"]
        bits.append(str(self.status))
        if self.error:
            bits.append(f"错误：{self.error}")
        for key in ("turns", "tool_calls", "leaf", "reason", "prompt_chars"):
            if self.detail.get(key) not in (None, ""):
                bits.append(f"{key}={self.detail[key]}")
        return " ".join(bits)


class OperationLog:
    """只追加的操作日志。文件路径为 None 时纯内存（测试里用）。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.torn_lines: list[int] = []

    # ------------------------------------------------------------ 原语
    def accept(self, kind: OperationKind | str, **detail: Any) -> Operation:
        """建一条操作。这时它是 pending：durable 上已经存在，但还没开始跑。"""
        kind = OperationKind(kind)
        op = Operation(id=self._next_id(), kind=kind, status=OperationStatus.PENDING,
                       created_at=_now(), updated_at=_now(), detail=dict(detail))
        self._write(op, note="accept")
        return op

    def drive(self, op: Operation, status: OperationStatus | str, *, error: str | None = None,
              **detail: Any) -> Operation:
        """推进一条操作。detail 是累加的，不覆盖之前记下的东西。"""
        status = OperationStatus(status)
        op.status = status
        op.updated_at = _now()
        op.detail.update({k: v for k, v in detail.items() if v is not None})
        if error is not None:
            op.error = error
        op.records += 1
        self._write(op, note="drive")
        return op

    def finish(self, op: Operation, status: OperationStatus | str, *, error: str | None = None,
               **detail: Any) -> Operation:
        status = OperationStatus(status)
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finish 只接受终态，收到 {status}")
        return self.drive(op, status, error=error, **detail)

    # ------------------------------------------------------------ 读
    def records(self) -> list[dict[str, Any]]:
        if not self.path:
            return []
        rows, torn = read_jsonl(self.path)
        self.torn_lines = torn
        return rows

    def operations(self) -> list[Operation]:
        """把日志折叠成操作列表：同一 id 的多条记录合并成最后一次的状态。"""
        folded: dict[str, Operation] = {}
        order: list[str] = []
        for row in self.records():
            op_id = str(row.get("op", ""))
            if not op_id:
                continue
            if op_id not in folded:
                folded[op_id] = Operation(id=op_id, kind=OperationKind(row.get("kind", "run")),
                                           created_at=str(row.get("ts") or _now()),
                                           updated_at=str(row.get("ts") or _now()),
                                           status=OperationStatus(row.get("status", "pending")),
                                           detail={}, records=0)
                order.append(op_id)
            op = folded[op_id]
            op.status = OperationStatus(row.get("status", op.status))
            op.updated_at = str(row.get("ts") or op.updated_at)
            op.records += 1
            detail = row.get("detail")
            if isinstance(detail, dict):
                op.detail.update(detail)
            if row.get("error"):
                op.error = str(row["error"])
        return [folded[i] for i in order]

    def current(self) -> Operation | None:
        """最后一个操作（不管结没结束）。"""
        ops = self.operations()
        return ops[-1] if ops else None

    def unfinished(self) -> list[Operation]:
        """没结束的操作，按时间从早到晚。正常情况下最多一条。"""
        return [op for op in self.operations() if op.unfinished]

    def pending_resume(self) -> Operation | None:
        """该从哪条操作接着跑：最后一条没结束的。"""
        open_ops = self.unfinished()
        return open_ops[-1] if open_ops else None

    # ------------------------------------------------------------ 内部
    def _next_id(self) -> str:
        used = 0
        for op in self.operations():
            digits = "".join(ch for ch in op.id if ch.isdigit())
            used = max(used, int(digits) if digits else 0)
        return f"op-{used + 1:04d}"

    def _write(self, op: Operation, *, note: str) -> None:
        if not self.path:
            return
        append_jsonl(self.path, {"op": op.id, "kind": str(op.kind), "status": str(op.status), "ts": op.updated_at,
                                 "note": note, "detail": op.detail, "error": op.error})
