"""装配层：把模型、工具、会话树、事件流、操作日志和用量账本拼成一次能跑的 agent run。

对应 pi 的 `pi-durable`。M13 时这一层只做了「跑一次、写几个文件」；中层把它补成了
**可恢复的一层**：

- 一次 run 就是一条**操作**（`accept` 建、`drive` 推进、`inspectExecution` 看现场），
  操作日志只追加，历史一行不删；
- 三个存储齐了（只追加的会话树 / 可替换的 values / 只追加的用量账本），并带事务；
- `resume()` 从中断处接着跑：已经拿到结果的工具调用重放而不重跑，状态未知的交给模型决定。

没做的是 pi 那份规范里更重的东西：多进程同时操作同一个 run 的锁、跨机器的操作队列。
单机上「一个人接着跑上一个人的 run」是够用的。
"""

from __future__ import annotations

import json
import platform
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sparkjury import __version__
from sparkjury.agent.ai import Provider, Turn
from sparkjury.agent.compact import CompactionPolicy, Compactor
from sparkjury.agent.hooks import Hooks
from sparkjury.agent.loop import STOPPED_ABORTED, AgentLoop, LoopResult, default_system_prompt
from sparkjury.agent.ops import Operation, OperationKind, OperationLog, OperationStatus
from sparkjury.agent.session import SessionTree
from sparkjury.agent.store import RunStore, StoreError, atomic_write_json
from sparkjury.agent.tools import SkillInfo, ToolRegistry, load_skill_tools
from sparkjury.harness.events import EventBus


def new_run_id(prefix: str = "agent") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


@dataclass
class AgentRun:
    """一次 agent run 的把手：跑之前的东西、跑完的产物都在这里。"""

    run_id: str
    run_dir: Path
    session: SessionTree
    bus: EventBus
    provider: Provider
    registry: ToolRegistry
    skills: list[SkillInfo] = field(default_factory=list)
    ledger_path: Path | None = None
    loop: AgentLoop | None = None
    started_at: str = ""
    _t0: float = 0.0

    @property
    def session_path(self) -> Path:
        return self.run_dir / "session.jsonl"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"


class AgentRuntime:
    """把上面那堆零件装成一次 run，并负责落盘、推进操作、支持从中断处继续。"""

    def __init__(self, provider: Provider | None, *, runs_dir: str | Path = "runs", run_id: str | None = None,
                 run_dir: str | Path | None = None, reopen: bool = False,
                 workdir: str | Path | None = None, registry: ToolRegistry | None = None,
                 skills: Sequence[SkillInfo] = (), executor: Any | None = None,
                 skills_root: str | Path | None = None, max_turns: int = 12,
                 system_prompt: str | None = None, session_path: Path | None = None,
                 store: RunStore | None = None, compaction: CompactionPolicy | None = None,
                 hooks: Hooks | None = None, use_model_summary: bool = True):
        self.workdir = Path(workdir or Path.cwd()).resolve()
        if run_dir is not None:                      # 重新打开一次已有的 run：目录已经在那儿了
            self.run_dir = Path(run_dir)
            if reopen and not self.run_dir.is_dir():
                raise StoreError(f"run 目录不存在：{self.run_dir}")
            self.run_id = run_id or self._run_id_from_dir(self.run_dir)
        else:
            self.run_id = run_id or new_run_id()
            self.run_dir = Path(runs_dir) / self.run_id
        if not reopen:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        if registry is None:
            registry, skills = load_skill_tools(skills_root, executor=executor, workdir=self.workdir)
        self.registry = registry
        self.skills = list(skills)
        self.store = store or RunStore(self.run_dir)
        self.ops = OperationLog(self.store.paths.ops)
        self.values = self.store.values
        # 重新打开一个已有的 run 时事件流是接着写的：清空等于把上次跑到哪儿的证据删了
        self.bus = EventBus(self.run_id, self.store.paths.events, append=reopen)
        self.session = SessionTree.load(session_path or self.store.paths.entries)
        self.provider = provider
        self.max_turns = max_turns
        self.hooks = hooks or Hooks()
        self.system_prompt = system_prompt or default_system_prompt(registry, self.skills, workdir=self.workdir)
        self.compactor = Compactor(policy=compaction or CompactionPolicy(), provider=provider,
                                   bus=self.bus, ops=self.ops, use_model=use_model_summary)
        self.run = AgentRun(run_id=self.run_id, run_dir=self.run_dir, session=self.session, bus=self.bus,
                            provider=provider, registry=registry, skills=self.skills,
                            ledger_path=self.store.paths.ledger,
                            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.turns: list[dict[str, Any]] = []
        self.operation: Operation | None = None

    @staticmethod
    def _run_id_from_dir(run_dir: Path) -> str:
        """重新打开时 run_id 取目录名；manifest 里写着更准的就用 manifest 的。"""
        manifest = run_dir / "manifest.json"
        if manifest.is_file():
            try:
                return str(json.loads(manifest.read_text(encoding="utf-8")).get("run_id") or run_dir.name)
            except json.JSONDecodeError:
                pass
        return run_dir.name

    # ------------------------------------------------------------ 四个原语
    def accept(self, kind: OperationKind | str = OperationKind.RUN, **detail: Any) -> Operation:
        """建一条 durable 操作。`accept` 之后就存在了，哪怕进程立刻死掉也查得到。"""
        self.operation = self.ops.accept(kind, session_id=self.session.session_id,
                                         leaf=self.session.leaf_id, **detail)
        self.values.set("current_operation", self.operation.id)
        return self.operation

    def drive(self, status: OperationStatus | str, *, error: str | None = None, **detail: Any) -> Operation:
        """推进当前操作。结束态用形如 `drive(OperationStatus.COMPLETED)` 的写法即可。"""
        if self.operation is None:
            raise RuntimeError("还没有 accept 过任何操作")
        return self.ops.drive(self.operation, status, error=error, **detail)

    def request_abort(self, reason: str = "") -> None:
        """请求中断正在跑的那一轮。不抛异常、不删记录，只是把标志立起来。"""
        op = self.operation
        if op is not None and not op.finished:
            self.ops.drive(op, OperationStatus.ABORTED, reason=reason or "abort requested")
        if self.run.loop is not None:
            self.run.loop.abort()
        self.values.set("last_abort", {"reason": reason, "at": _now()})

    def inspect(self) -> dict[str, Any]:
        """看一眼现场：操作到哪了、会话停在哪、用量多少、有没有需要接着跑的活。

        给人和给脚本的是同一份事实：`sparkjury agent ops <run_dir>` 打的就是它。
        """
        ops = self.ops.operations()
        open_ops = [op for op in ops if op.unfinished]
        tail = self.session.path_to()[-1] if len(self.session) else None
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "session": {"id": self.session.session_id, "entries": len(self.session),
                        "leaf": self.session.leaf_id, "torn_lines": self.session.torn_lines,
                        "tail": (f"{tail.id} {tail.kind}" if tail else None)},
            "operations": [{"id": op.id, "kind": str(op.kind), "status": str(op.status),
                            "records": op.records, "detail": op.detail, "error": op.error} for op in ops],
            "unfinished": [op.id for op in open_ops],
            "pending_tools": [c.get("name") for c in self.session.pending_calls()],
            "usage": self.store.ledger.total(),
            "values": self.values.snapshot(),
            "needs_resume": bool(open_ops) or bool(self.session.pending_calls()),
            "torn_lines": self.store.recover()["torn_lines"],
        }

    # ------------------------------------------------------------ 跑
    def start(self, prompt: str | None, *, max_turns: int | None = None) -> LoopResult:
        """起一次 run：accept → drive(running) → 跑循环 → 收尾。"""
        self._require_provider()
        self.run._t0 = time.perf_counter()
        self.accept(OperationKind.RUN, prompt_chars=len(prompt or ""))
        self.drive(OperationStatus.RUNNING)
        loop = self._build_loop(max_turns)
        result = loop.run(prompt)
        self._settle(result)
        self.write_manifest(result)
        return result

    def resume(self, *, max_turns: int | None = None) -> LoopResult:
        """从中断处接着跑。

        没结束的操作会被收掉：正常跑完记 completed，仍然被中断记 aborted，出错记 failed。
        更早遗留的未完成操作一并标成 aborted——它们已经被这一次恢复取代了。
        """
        self._require_provider()
        self.run._t0 = time.perf_counter()
        pending_op = self.ops.pending_resume()
        stale = [op for op in self.ops.unfinished() if pending_op is not None and op.id != pending_op.id]
        for op in stale:
            self.ops.finish(op, OperationStatus.ABORTED, reason="被后一次恢复取代")
        self.operation = pending_op if pending_op is not None else self.accept(OperationKind.RUN, resumed=True)
        if pending_op is not None:
            self.ops.drive(self.operation, OperationStatus.RUNNING, resumed=True)
        loop = self._build_loop(max_turns)
        result = loop.resume()
        self._settle(result)
        self.write_manifest(result)
        return result

    @classmethod
    def reopen(cls, run_dir: str | Path, provider: Provider | None = None, **kwargs: Any) -> "AgentRuntime":
        """重新打开一次已有的 run：读回会话、values、操作日志，事件流改成追加。

        只看不跑（`inspect()`）不需要 provider；要 `resume()` 就得给一个。
        """
        return cls(provider, run_dir=run_dir, reopen=True, **kwargs)

    def _require_provider(self) -> None:
        """只读打开的 runtime（`reopen()` 不带 provider）不能跑，也不该在操作日志里留下痕迹。"""
        if self.provider is None:
            raise RuntimeError("这次 runtime 是以只读方式打开的，没有 provider，不能跑")

    def _build_loop(self, max_turns: int | None) -> AgentLoop:
        self._require_provider()
        loop = AgentLoop(self.provider, self.registry, self.session, bus=self.bus,
                         max_turns=max_turns or self.max_turns, system_prompt=self.system_prompt,
                         skills=self.skills, on_turn=self._on_turn, hooks=self.hooks,
                         compactor=self.compactor)
        self.run.loop = loop
        return loop

    def _settle(self, result: LoopResult) -> None:
        """按结果收操作。四种结束方式对应四种终态，不美化。"""
        if self.operation is not None:
            if result.error:
                self.ops.finish(self.operation, OperationStatus.FAILED, error=result.error,
                                turns=result.turns, tool_calls=result.tool_calls)
            elif result.stopped == STOPPED_ABORTED:
                self.ops.finish(self.operation, OperationStatus.ABORTED, turns=result.turns,
                                tool_calls=result.tool_calls, resumed=result.resumed)
            else:
                self.ops.finish(self.operation, OperationStatus.COMPLETED, turns=result.turns,
                                tool_calls=result.tool_calls, stopped=result.stopped,
                                resumed=result.resumed, replayed_calls=result.replayed_calls)
        self.values.update({"last_result": {"stopped": result.stopped, "turns": result.turns,
                                            "tool_calls": result.tool_calls,
                                            "resumed": result.resumed},
                            "session_leaf": self.session.leaf_id,
                            "current_operation": None})

    def _on_turn(self, turn: Turn, index: int) -> None:
        """每一轮往用量账本追加一行，并把进度写进操作日志和 values（中断后能看出跑到哪了）。"""
        model_name = turn.model or (self.provider.spec.model if self.provider else "")
        row = {"ts": _now(), "turn": index, "model": model_name,
               "latency_ms": round(turn.latency_ms, 1), "tool_calls": len(turn.tool_calls),
               "error": turn.error, **(turn.usage or {})}
        self.turns.append(row)
        self.store.ledger.append(row)
        if self.operation is not None and not self.operation.finished:
            self.ops.drive(self.operation, OperationStatus.RUNNING, turns=index,
                           leaf=self.session.leaf_id, prompt_tokens=turn.usage.get("prompt_tokens"))
        self.values.update({"turns": index, "session_leaf": self.session.leaf_id})

    # ------------------------------------------------------------ 收尾
    def write_manifest(self, result: LoopResult) -> Path:
        """写 manifest.json（原子替换）。降级项照实记：模型调用失败、工具失败、被中断都进。"""
        degradations = [f"tool {name} failed" for name in _failed_tools(self.run.registry)]
        if result.error:
            degradations.append(f"model call failed: {result.error}")
        if result.stopped == STOPPED_ABORTED:
            degradations.append("run aborted by caller")
        if result.replayed_calls:
            degradations.append(f"{result.replayed_calls} tool call(s) replayed from the journal on resume")
        ops = self.ops.operations()
        ledger_total = self.store.ledger.total()
        manifest = {
            "run_id": self.run_id,
            "kind": "agent",
            "status": "ok" if result.ok else result.stopped,
            "sparkjury_version": __version__,
            "python": sys.version.split()[0],
            "host": platform.node(),
            "started_at": self.run.started_at,
            "finished_at": _now(),
            "duration_s": round(time.perf_counter() - self.run._t0, 2),
            "session": {"id": self.session.session_id, "path": str(self.run.session_path),
                        "entries": len(self.session), "leaf": self.session.leaf_id,
                        "torn_lines": self.session.torn_lines},
            "models": {"agent": ({"name": self.provider.spec.name, "model": self.provider.spec.model,
                                  "base_url": self.provider.spec.base_url} if self.provider else None)},
            "tools": self.registry.names(),
            "skills": [s.name for s in self.skills],
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "stopped": result.stopped,
            "resumed": result.resumed,
            "replayed_calls": result.replayed_calls,
            "compactions": result.compactions,
            "usage": dict(result.usage) or ledger_total,
            "operations": [{"id": op.id, "kind": str(op.kind), "status": str(op.status),
                            "records": op.records, "detail": op.detail} for op in ops],
            "degradations": degradations,
        }
        atomic_write_json(self.store.paths.manifest, manifest)
        # 这里刻意不发事件：run_end 必须是事件流里最后一条。收尾之后还能再冒一条出来，
        # 消费 SSE 的看板就没法用「看到 run_end 就收工」判断一次 run 结束了。
        return self.store.paths.manifest

    # ------------------------------------------------------------ 恢复现场
    def recover(self) -> dict[str, Any]:
        """不跑任何东西，只报告现场（半行日志、未完成操作、待执行的工具）。"""
        report = self.store.recover()
        report["run_id"] = self.run_id
        report["unfinished_ops"] = [op.id for op in self.ops.unfinished()]
        report["pending_tools"] = [c.get("name") for c in self.session.pending_calls()]
        report["session_leaf"] = self.session.leaf_id
        return report


def _failed_tools(registry: ToolRegistry) -> list[str]:
    """注册表调用记录里失败过的工具名（同一工具只算一次，按首次出现排序）。"""
    failed: list[str] = []
    for record in registry.calls:
        if not record.ok and record.name not in failed:
            failed.append(record.name)
    return failed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(run_dir: str | Path) -> dict[str, Any]:
    """读一次 run 的 manifest。命令行和看板都走这条，省得各自拼路径。"""
    path = Path(run_dir) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))
