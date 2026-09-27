"""会话树：一次对话就是一行一条的 JSONL，每条记录带一个 parent 指针。

对应 pi 的会话存储。要点只有两条：**只追加**（原始记录永不改写，回放才有意义），
**带 parent**（当前停在哪条 entry 决定活动分支，从更早那条继续就是另开一个分支，
不需要复制文件）。

一条 entry 长这样：

    {"id":"e0007","parent":"e0006","kind":"tool_result","role":null,"ts":"...","data":{...}}

kind 有六种：`message`（system/user/assistant 的文本）、`tool_call`（一次 assistant
回合里要求调的所有工具）、`tool_result`（一个工具的执行结果）、`note`（harness 自己
记的事，比如被中断、某个工具刚开始执行）、`branch`（从哪条分出来的标记）、
`compaction`（历史压缩的摘要，顶替它前面那些消息）。把它翻译成模型吃的 messages
是 `messages()` 的活，翻译规则就是 OpenAI 那套：一轮里的多个工具调用合成一条
assistant 消息，工具结果一条对一个 `tool_call_id`。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

KINDS = ("message", "tool_call", "tool_result", "note", "branch", "compaction")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    id: str
    kind: str
    ts: str = field(default_factory=_now)
    parent: str | None = None
    role: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "parent": self.parent, "kind": self.kind, "role": self.role,
                           "ts": self.ts, "data": self.data}, ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Entry":
        return cls(id=str(raw["id"]), kind=str(raw.get("kind", "note")), ts=str(raw.get("ts") or _now()),
                   parent=raw.get("parent"), role=raw.get("role"), data=dict(raw.get("data") or {}))

    def summary(self, width: int = 72) -> str:
        """一行摘要，给回放和调试用。"""
        if self.kind == "message":
            text = str(self.data.get("text", ""))
        elif self.kind == "tool_call":
            names = ", ".join(c.get("name", "?") for c in self.data.get("calls", []))
            text = f"要求调用：{names}"
            if self.data.get("text"):
                text = f"{self.data['text']} | {text}"
        elif self.kind == "tool_result":
            text = f"{self.data.get('name', '?')} → " + str(self.data.get("output", ""))
        elif self.kind == "branch":
            text = f"从 {self.data.get('from')} 分出新分支"
        elif self.kind == "compaction":
            text = (f"更早的 {self.data.get('replaced', '?')} 条已压成摘要"
                    f"（来源 {self.data.get('source', '?')}）：" + str(self.data.get("summary", "")))
        elif self.kind == "note" and self.data.get("phase") == "tool_start":
            text = f"开始执行 {self.data.get('name', '?')}"
        else:
            text = str(self.data.get("text", ""))
        text = " ".join(text.split())
        return text if len(text) <= width else text[:width] + "…"


class SessionTree:
    """只追加的会话树。文件路径为 None 时纯内存（测试里用）。"""

    def __init__(self, path: Path | None = None, *, session_id: str | None = None):
        self.path = Path(path) if path else None
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._entries: list[Entry] = []
        self._leaf: str | None = None
        # 崩溃时最后一行可能只写了一半：读的时候跳过它，但要记下来（静默丢掉和「本来就没有」是两回事）
        self.torn_lines: list[int] = []

    # ------------------------------------------------------------ 读
    @property
    def entries(self) -> list[Entry]:
        return list(self._entries)

    @property
    def leaf_id(self) -> str | None:
        return self._leaf

    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self._entries if e.id == entry_id), None)

    def path_to(self, leaf: str | None = None) -> list[Entry]:
        """从根走到指定 entry（默认当前叶子）。parent 断了就停在能走到的地方。"""
        cur = self.get(leaf or self._leaf or "")
        chain: list[Entry] = []
        while cur is not None:
            chain.append(cur)
            cur = self.get(cur.parent) if cur.parent else None
        return list(reversed(chain))

    def leaves(self) -> list[Entry]:
        parents = {e.parent for e in self._entries if e.parent}
        return [e for e in self._entries if e.id not in parents]

    def messages(self, leaf: str | None = None) -> list[dict[str, Any]]:
        """把这条分支翻译成模型吃的一串消息。

        压缩过的分支：从最后一次 `compaction` 开始算，它前面那些 entry 不再逐条发给模型，
        由这一条摘要顶上——但它们在文件里一条没少，回放时照样看得到。
        """
        branch = self._compacted_branch(leaf)
        out: list[dict[str, Any]] = []
        for e in branch:
            if e.kind == "compaction":
                out.append({"role": "system",
                            "content": "以下是这次会话更早部分的摘要（原文仍在 session.jsonl 里）：\n"
                                       + str(e.data.get("summary", ""))})
                continue
            if e.kind == "message" and e.role:
                out.append({"role": e.role, "content": str(e.data.get("text", ""))})
            elif e.kind == "tool_call":
                calls = e.data.get("calls") or []
                if not calls:
                    continue
                out.append({"role": "assistant", "content": e.data.get("text") or None,
                            "tool_calls": [{"id": c.get("id") or f"call_{i+1}", "type": "function",
                                            "function": {"name": c.get("name", ""),
                                                         "arguments": c.get("arguments", "{}")}}
                                           for i, c in enumerate(calls)]})
            elif e.kind == "tool_result":
                out.append({"role": "tool", "tool_call_id": e.data.get("call_id") or "",
                            "content": str(e.data.get("output", ""))})
        return out

    def _compacted_branch(self, leaf: str | None = None) -> list[Entry]:
        """「模型实际看到的」那条分支：摘要打头，后面接没被压掉的原文。

        实现上不重排任何 entry（entry 是只追加的，parent 指针不能改），而是每次读的时候
        按最后一次压缩记录里的 `to` 切一刀：`to` 之前的部分由那条摘要顶上，`to` 及其之后的
        原文照旧。切完把中间那些历史摘要记录滤掉，免得同一段被 summarise 两遍。
        """
        branch = self.path_to(leaf)
        last = next((e for e in reversed(branch) if e.kind == "compaction"), None)
        if last is None:
            return branch
        to_id = last.data.get("to")
        start = next((i for i, e in enumerate(branch) if e.id == to_id), None)
        if start is None:      # 记录里指的那条不在了（文件被手工改过）：退回整条分支，不猜
            return branch
        tail = [e for e in branch[start:] if e.kind != "compaction"]
        return [last, *tail]

    def results_by_call_id(self, leaf: str | None = None) -> dict[str, str]:
        """这条分支上已经拿到的工具结果：call_id → 输出。

        中断恢复靠它：下次启动时，凡是这里有了的调用都不再执行第二遍。
        """
        out: dict[str, str] = {}
        for e in self.path_to(leaf):
            if e.kind == "tool_result" and e.data.get("call_id"):
                out[str(e.data["call_id"])] = str(e.data.get("output", ""))
        return out

    def started_call_ids(self, leaf: str | None = None) -> set[str]:
        """已经有「开始执行」标记的调用。

        标记在结果之前写：所以在标记和结果之间被打断的那些调用，无从判断它到底跑没跑完——
        恢复时不能想当然地重跑（可能重复副作用），得当成「状态未知」交给模型决定。
        """
        return {str(e.data.get("call_id")) for e in self.path_to(leaf)
                if e.kind == "note" and e.data.get("phase") == "tool_start" and e.data.get("call_id")}

    def pending_calls(self, leaf: str | None = None) -> list[dict[str, Any]]:
        """最后一条 tool_call 里还没拿到结果的调用（按原顺序）。"""
        branch = self.path_to(leaf)
        done = self.results_by_call_id(leaf)
        for entry in reversed(branch):
            if entry.kind != "tool_call":
                continue
            calls = entry.data.get("calls") or []
            return [c for c in calls if str(c.get("id")) not in done]
        return []

    def transcript(self, limit: int | None = None) -> str:
        """人类可读回放：`[e0003 assistant] …`。默认走当前分支，`limit` 只看最后几条。"""
        branch = self.path_to()
        if limit:
            branch = branch[-limit:]
        return "\n".join(f"[{e.id} {e.role or e.kind}] {e.summary()}" for e in branch)

    # ------------------------------------------------------------ 写
    def append(self, kind: str, *, role: str | None = None, data: dict[str, Any] | None = None,
               parent: str | None = None) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"unknown entry kind: {kind}（只认 {', '.join(KINDS)}）")
        entry = Entry(id=f"e{len(self._entries) + 1:04d}", kind=kind, parent=parent if parent is not None else self._leaf,
                      role=role, data=dict(data or {}))
        self._entries.append(entry)
        self._leaf = entry.id
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        return entry

    def branch(self, from_id: str, *, text: str = "") -> Entry:
        """把活动分支切到 `from_id`，后面的记录接在那条后面。原始 entry 一条不动。"""
        if self.get(from_id) is None:
            raise KeyError(f"no such entry: {from_id}")
        self._leaf = from_id
        return self.append("branch", data={"from": from_id, "text": text})

    # ------------------------------------------------------------ 存取
    @classmethod
    def load(cls, path: Path | str, *, session_id: str | None = None) -> "SessionTree":
        path = Path(path)
        tree = cls(path, session_id=session_id)
        if not path.is_file():
            return tree
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                tree._entries.append(Entry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError):
                tree.torn_lines.append(lineno)   # 崩溃时写了一半的最后一行：跳过，但不装作没发生
        tree._leaf = tree._entries[-1].id if tree._entries else None
        return tree

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SessionTree(id={self.session_id}, entries={len(self._entries)}, leaf={self._leaf})"
