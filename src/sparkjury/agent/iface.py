"""三种非交互接口：一行文本、一行一条 JSON、以及长驻的 RPC。

pi 的 coding agent 有五种接口（interactive / print / JSON / RPC / SDK），共用同一套 agent 与会话。
这里对上三种，够这个仓库用：

- **print**：跑完只把最后那段回答吐到 stdout，`$(uv run sparkjury agent run -p ... --print)`
  能直接拿去做后续处理，屏幕上不会混进表格和颜色。
- **event stream（`--events`）**：每冒一条事件写一行 JSON（NDJSON），看板、管道、日志采集都能吃。
  事件就是 M7 `EventBus` 上那套，字段一个没改——`stage` 固定 `AGENT` 或 `SUBAGENT`。
- **RPC（`sparkjury agent rpc`）**：进程常驻，stdin 一行一条 JSON 命令（prompt / steer / followup /
  abort / inspect / policy / ping / shutdown），stdout 一行一条 JSON 事件与回执。跑 run 的活放在
  工作线程里，所以**跑着的时候也读得到命令**——这是 RPC 跟 print 的区别：它是个能中途插话的长驻进程。

写 stdout 的地方都要过一把锁：RPC 里事件来自工作线程、回执来自读命令的线程，两个线程同时写一行
会写岔。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any

from sparkjury.agent.runtime import AgentRuntime

#: RPC 认识的命令。认不出来的如实回一条 error，不是当没看见。
RPC_OPS = ("prompt", "steer", "followup", "abort", "inspect", "policy", "ping", "shutdown")


def final_text(runtime: AgentRuntime, result: Any) -> str:
    """这次 run 最后的回答正文。print 接口就靠它。"""
    text = getattr(result, "text", "") or ""
    if text:
        return text
    for entry in reversed(runtime.session.entries):   # 端点没回文本时，退回会话里最后一条 assistant
        if entry.kind == "message" and entry.role == "assistant":
            return str(entry.data.get("text", ""))
    return ""


def event_payload(event: Any) -> dict[str, Any]:
    """一条事件 → 一行 JSON。字段跟 `events.jsonl` 里的对齐，不另起一套。"""
    stage = getattr(event, "stage", None)
    return {"type": "event", "ts": getattr(event, "ts", ""), "seq": getattr(event, "seq", 0),
            "run_id": getattr(event, "run_id", ""), "stage": "" if stage is None else str(stage),
            "kind": str(getattr(event, "kind", "")), "message": getattr(event, "message", ""),
            "data": dict(getattr(event, "data", None) or {})}


def result_payload(runtime: AgentRuntime, result: Any, status: str = "result") -> dict[str, Any]:
    """一次 run 的收尾行：结束方式、轮数、工具调用、最后那段话。"""
    return {"type": status, "run_id": runtime.run_id, "stopped": getattr(result, "stopped", ""),
            "turns": getattr(result, "turns", 0), "tool_calls": getattr(result, "tool_calls", 0),
            "text": final_text(runtime, result), "error": getattr(result, "error", None),
            "run_dir": str(runtime.run_dir)}


class StreamWriter:
    """一行一条 JSON 的写出器。加锁是因为 RPC 里两个线程都在往同一个 stdout 写。"""

    def __init__(self, stream: IO[str] | None = None):
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()
        self.lines = 0

    def write(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.stream.write(text + "\n")
            self.stream.flush()
            self.lines += 1


class EventStream:
    """事件流的订阅者：把 bus 上冒出来的每条事件写成一行 JSON。"""

    def __init__(self, writer: StreamWriter, *, kinds: tuple[str, ...] = ()):
        self.writer = writer
        self.kinds = kinds
        self.count = 0

    def __call__(self, event: Any) -> None:
        if self.kinds and str(getattr(event, "kind", "")) not in self.kinds:
            return
        self.writer.write(event_payload(event))
        self.count += 1


def run_with_events(runtime: AgentRuntime, prompt: str | None, *, writer: StreamWriter,
                    max_turns: int | None = None) -> Any:
    """跑一次 run，全程把事件按行推出去，收尾补一行 result。"""
    stream = EventStream(writer)
    runtime.bus.subscribe(stream)
    try:
        result = runtime.start(prompt, max_turns=max_turns)
    finally:
        runtime.bus.unsubscribe(stream)
    writer.write(result_payload(runtime, result))
    return result


@dataclass
class RpcSession:
    """常驻进程：stdin 读命令，stdout 写事件与回执。一次只跑一轮，跑着的时候还能插话。"""

    runtime: AgentRuntime
    stdin: IO[str] = field(default_factory=lambda: sys.stdin)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    emit_events: bool = True
    max_turns: int | None = None
    #: 收到 shutdown / EOF 之后，等手上那一轮跑完再退。等超时了就按中断收尾，不把操作悬在那儿。
    drain_timeout: float = 30.0
    #: 插话时等循环出现的上限：命令到了、循环还没建好的那一瞬间不算「没人接」。
    interject_wait: float = 1.0

    def __post_init__(self) -> None:
        self.writer = StreamWriter(self.stdout)
        self._busy = threading.Event()
        self._thread: threading.Thread | None = None
        self.handled = 0

    # ------------------------------------------------------------ 状态
    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def _stream(self) -> EventStream:
        if not hasattr(self, "_events"):
            self._events = EventStream(self.writer)
        return self._events

    # ------------------------------------------------------------ 主循环
    def serve(self) -> int:
        """读命令直到 EOF 或 shutdown。返回进程退出码。"""
        if self.emit_events:
            self.runtime.bus.subscribe(self._stream())
        spec = self.runtime.provider.spec if self.runtime.provider else None
        self.writer.write({"type": "ready", "run_id": self.runtime.run_id,
                           "run_dir": str(self.runtime.run_dir),
                           "model": getattr(spec, "model", ""), "endpoint": getattr(spec, "name", ""),
                           "tools": self.runtime.registry.names(), "ops": list(RPC_OPS)})
        try:
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                self.handled += 1
                if self._dispatch(line):
                    break
        finally:
            self._drain()
            if self.emit_events and hasattr(self, "_events"):
                self.runtime.bus.unsubscribe(self._events)
        return 0

    def _drain(self) -> None:
        """收工时把手上那一轮等完。等不到就按中断收尾——操作日志里不能留下悬着的操作。"""
        if self._thread is None or not self.busy:
            return
        self._thread.join(timeout=self.drain_timeout)
        if self.busy:
            self.runtime.request_abort("rpc 收工：等这一轮超时了")
            self._thread.join(timeout=5)

    def _dispatch(self, line: str) -> bool:
        """处理一行命令。返回 True 表示该收工了。"""
        try:
            command = json.loads(line)
        except json.JSONDecodeError as e:
            self.writer.write({"type": "error", "op": "?", "message": f"这行不是 JSON：{e}"})
            return False
        if not isinstance(command, dict):
            self.writer.write({"type": "error", "op": "?", "message": "命令要是一个 JSON 对象"})
            return False
        op = str(command.get("op", ""))
        text = str(command.get("text", ""))
        if op == "prompt":
            return self._on_prompt(text)
        if op in ("steer", "followup"):
            return self._on_interject(op, text)
        if op == "abort":
            self.runtime.request_abort(text or "rpc abort")
            self.writer.write({"type": "ack", "op": "abort", "ok": True})
            return False
        if op == "inspect":
            self.writer.write({"type": "ack", "op": "inspect", "ok": True,
                               "data": self.runtime.inspect()})
            return False
        if op == "policy":
            policy = getattr(self.runtime, "permissions", None)
            self.writer.write({"type": "ack", "op": "policy", "ok": policy is not None,
                               "data": policy.report() if policy is not None else {}})
            return False
        if op == "ping":
            self.writer.write({"type": "ack", "op": "ping", "ok": True, "data": {"busy": self.busy}})
            return False
        if op == "shutdown":
            self.writer.write({"type": "ack", "op": "shutdown", "ok": True})
            return True
        self.writer.write({"type": "error", "op": op or "?", "message":
                           f"不认识的命令：{op or '（空的）'}。可用的是：{', '.join(RPC_OPS)}"})
        return False

    # ------------------------------------------------------------ 三种命令
    def _on_prompt(self, text: str) -> bool:
        if not text:
            self.writer.write({"type": "error", "op": "prompt", "message": "prompt 少了 text 字段"})
            return False
        if self.busy:      # 跑着的时候来的 prompt 当插话：不能又起一轮，那会写坏同一份会话
            loop = self._await_loop()
            if loop is not None:
                loop.steer(text)
            self.writer.write({"type": "ack", "op": "prompt", "ok": True,
                               "data": {"queued": "steer", "reason": "上一轮还在跑，这条当插话处理"}})
            return False
        self._start_worker(text)
        self.writer.write({"type": "ack", "op": "prompt", "ok": True, "data": {"queued": "run"}})
        return False

    def _on_interject(self, op: str, text: str) -> bool:
        loop = self._await_loop()
        if loop is None or not self.busy:
            self.writer.write({"type": "ack", "op": op, "ok": False,
                               "data": {"reason": "现在没有在跑的轮次，插话没人接"}})
            return False
        (loop.followup if op == "followup" else loop.steer)(text)
        self.writer.write({"type": "ack", "op": op, "ok": True})
        return False

    def _await_loop(self) -> Any:
        """等循环建好。命令到了、循环还没建起来的那几毫秒不算「没人接」。"""
        deadline = time.monotonic() + max(0.0, self.interject_wait)
        while True:
            loop = self.runtime.run.loop
            if loop is not None or not self.busy or time.monotonic() >= deadline:
                return loop
            time.sleep(0.005)

    def _start_worker(self, text: str) -> None:
        def work() -> None:
            try:
                result = self.runtime.start(text, max_turns=self.max_turns)
                self.writer.write(result_payload(self.runtime, result))
            except Exception as e:  # noqa: BLE001 - RPC 不能因为一轮跑挂就退出
                self.writer.write({"type": "error", "op": "prompt",
                                   "message": f"{type(e).__name__}: {e}"})
            finally:
                self._busy.clear()

        self._busy.set()
        self._thread = threading.Thread(target=work, name="sparkjury-rpc-run", daemon=True)
        self._thread.start()


def read_rpc_line(raw: str) -> dict[str, Any]:
    """给测试和别的调用方用的一层薄壳：把一行文本变成命令对象，非法就抛 ValueError。"""
    command = json.loads(raw)
    if not isinstance(command, dict):
        raise ValueError("命令要是一个 JSON 对象")
    return command


__all__ = ["EventStream", "RPC_OPS", "RpcSession", "StreamWriter", "event_payload", "final_text",
           "read_rpc_line", "result_payload", "run_with_events"]
