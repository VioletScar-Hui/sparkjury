"""历史压缩：给会话树插一条摘要 entry，顶替它前面那些消息。

pi 的做法是「插一条 summary entry 替换后续请求里的老消息，原始 entry 保留」。这里照办，
理由也一样：原始记录是复盘和证据的来源，压缩只影响**下一次请求发给模型什么**，不改历史。

两个刻意的选择：

- 触发看**真实 token 数**（上一轮端点回的 `prompt_tokens`），不是估的字符数。端点上不回
  usage 时才退回落差很大的字符估算，并且把「估的」这件事写进事件里。
- 摘要优先让模型自己写（它才知道哪些是关键），模型叫不动就回落到确定性摘要——把每条
  entry 压成一行。回落不算失败，但会记一笔降级，收尾看得见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sparkjury.agent.session import Entry, SessionTree
from sparkjury.harness.events import EventBus, EventKind

SUMMARY_SECTIONS = ("背景与目标", "已经做了什么", "结论与决定", "未决的问题")

SUMMARY_PROMPT = """你负责压缩一段对话历史。下面是从旧到新的若干条记录，请把它压成一份交接说明，
让另一个模型读完就能接着干活。按这四个小标题写，每节不超过三行，只写记录里出现过的事实，
不要推测、不要补充建议：

背景与目标
已经做了什么
结论与决定
未决的问题
"""


@dataclass(frozen=True)
class CompactionPolicy:
    """什么时候值得压。

    `max_prompt_tokens` 为 0 表示不自动压缩（短会话没必要付这个代价）。
    """

    max_prompt_tokens: int = 0
    keep_tail_entries: int = 8
    fallback_max_chars: int = 24_000

    @property
    def enabled(self) -> bool:
        return self.max_prompt_tokens > 0

    def should_compact(self, *, prompt_tokens: int | None, branch_len: int, prompt_chars: int = 0) -> bool:
        """真正决策的地方。三个条件缺一不可：开了、有得压（尾部之外还有东西）、确实超了。"""
        if not self.enabled:
            return False
        if branch_len <= self.keep_tail_entries + 1:
            return False
        if prompt_tokens:
            return prompt_tokens > self.max_prompt_tokens
        if prompt_chars:
            return prompt_chars > self.fallback_max_chars
        return False


@dataclass
class CompactionPlan:
    """压缩计划：把 `from_id … to_id` 这一段换成一条摘要。`to_id` 指向的是保留的第一条。"""

    from_id: str
    to_id: str
    replaced: int
    reason: str = ""

    def describe(self) -> str:
        return f"压缩 {self.replaced} 条（{self.from_id} → {self.to_id}）：{self.reason or '超过预算'}"


def plan_compaction(session: SessionTree, *, leaf: str | None = None, keep_tail_entries: int = 8,
                    reason: str = "") -> CompactionPlan | None:
    """算一份压缩计划：保留末尾 `keep_tail_entries` 条原文，之前的部分进摘要。

    值得压的条件是「压缩后确实变短」——只剩两三条的时候插一条摘要反而更长，那种情况返回 None。
    """
    branch = session._compacted_branch(leaf)  # noqa: SLF001 - 分支口径只认 SessionTree 那一处
    head = branch[:-keep_tail_entries] if keep_tail_entries else list(branch)
    tail = branch[-keep_tail_entries:] if keep_tail_entries else []
    if not tail or not head or len(head) <= 1:
        # 空、或者只剩上一条摘要本身：这时候压完反而更长，不值得压
        return None
    return CompactionPlan(from_id=head[0].id, to_id=tail[0].id, replaced=len(head), reason=reason)


def deterministic_summary(entries: Sequence[Entry]) -> str:
    """不用模型的兜底摘要：每条压成一行，信息量低但绝不编造。"""
    lines = ["（这是确定性兜底摘要：模型没叫动，或者明确要求不调模型。）"]
    for section in SUMMARY_SECTIONS:
        lines.append(f"\n{section}")
        picked = [e for e in entries if _belongs(e, section)][:6]
        if not picked:
            lines.append("- （无）")
            continue
        for e in picked:
            lines.append(f"- [{e.id} {e.role or e.kind}] {e.summary(width=100)}")
    return "\n".join(lines)


def _belongs(entry: Entry, section: str) -> bool:
    """确定性的分节规则，按 entry 的形状归位，不看内容语义。"""
    if section == "背景与目标":
        return entry.kind == "message" and entry.role in ("system", "user")
    if section == "已经做了什么":
        return entry.kind in ("tool_call", "tool_result")
    if section == "结论与决定":
        return entry.kind == "message" and entry.role == "assistant"
    return entry.kind in ("note", "branch", "compaction")


def _render(entry: Entry) -> str:
    """喂给摘要模型的单条渲染。上一条摘要整段带上，不能截断——不然压第二次就开始丢东西。"""
    if entry.kind == "compaction":
        return f"[{entry.id} 上次的摘要] {entry.data.get('summary', '')}"
    return f"[{entry.id} {entry.role or entry.kind}] {entry.summary(width=400)}"


def model_summary(provider: Any, entries: Sequence[Entry], *, bus: EventBus | None = None) -> str | None:
    """让模型写摘要。叫不动就返回 None，由调用方回落。"""
    body = "\n".join(_render(entry) for entry in entries)
    messages = [{"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": body[:20_000]}]
    turn = provider.complete(messages, None)
    if turn.error:
        if bus:
            bus.publish(EventKind.DEGRADED, f"压缩摘要回落：模型没叫动（{turn.error}）",
                        stage="AGENT", phase="compaction", error=turn.error)
        return None
    text = (turn.text or "").strip()
    if not text:
        if bus:
            bus.publish(EventKind.DEGRADED, "压缩摘要回落：模型回了空内容", stage="AGENT", phase="compaction")
        return None
    return text


def apply_compaction(session: SessionTree, plan: CompactionPlan, summary: str, *,
                     source: str = "model") -> Entry:
    """把摘要写成一条 `compaction` entry。它挂在被保留的第一条之前，后面照常接。

    原始 entry 一条不删：`messages()` 只从最后一次压缩开始算，回放和证据都不受影响。
    """
    if session.get(plan.to_id) is None:
        raise KeyError(f"no such entry: {plan.to_id}")
    # 只追加：压缩记录是挂在分支末尾的一条普通 entry，它自己说清「我替掉了到 to 为止的那些」，
    # 切分交给 SessionTree._compacted_branch 在读的时候做，谁也不用回头改历史。
    return session.append("compaction", data={
        "summary": summary, "from": plan.from_id, "to": plan.to_id,
        "replaced": plan.replaced, "source": source, "reason": plan.reason,
    })


@dataclass
class Compactor:
    """把策略、模型、事件、操作日志串起来的那只。循环在每次请求前问它一句。"""

    policy: CompactionPolicy = field(default_factory=CompactionPolicy)
    provider: Any | None = None
    bus: EventBus | None = None
    ops: Any | None = None          # OperationLog；给了就记一条 compaction 操作
    use_model: bool = True
    applied: int = 0

    def maybe_compact(self, session: SessionTree, *, prompt_tokens: int | None = None,
                      prompt_chars: int = 0, leaf: str | None = None) -> Entry | None:
        if not self.policy.should_compact(prompt_tokens=prompt_tokens,
                                          branch_len=len(session._compacted_branch(leaf)),  # noqa: SLF001
                                          prompt_chars=prompt_chars):
            return None
        return self.compact(session, leaf=leaf, reason=f"prompt_tokens={prompt_tokens}")

    def compact(self, session: SessionTree, *, leaf: str | None = None, reason: str = "") -> Entry | None:
        plan = plan_compaction(session, leaf=leaf, keep_tail_entries=self.policy.keep_tail_entries, reason=reason)
        if plan is None:
            return None
        op = self.ops.accept("compaction", reason=plan.reason, replaced=plan.replaced) if self.ops else None
        if op is not None and self.ops is not None:
            self.ops.drive(op, "running")
        if self.bus:
            self.bus.publish(EventKind.PROGRESS, f"压缩历史：{plan.describe()}", stage="AGENT",
                             phase="compaction", replaced=plan.replaced)
        # 要压缩的原文 = 走到「保留的第一条」之前的所有 entry（含上一次的摘要记录，
        # 所以连着压两次不会把更早的内容丢掉）
        entries = session.path_to(plan.to_id)[:-1]
        summary, source = None, "deterministic"
        if self.use_model and self.provider is not None:
            summary = model_summary(self.provider, entries, bus=self.bus)
            if summary:
                source = "model"
        if not summary:
            summary = deterministic_summary(entries)
        entry = apply_compaction(session, plan, summary, source=source)
        self.applied += 1
        if op is not None and self.ops is not None:
            self.ops.finish(op, "completed", source=source, entry=entry.id)
        return entry
