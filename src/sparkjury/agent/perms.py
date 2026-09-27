"""权限层：一次工具调用到底放不放行，谁点的头，都留痕。

pi 的 harness 里没有权限系统——它的理由很直接：agent 跑在容器里，越界由沙箱兜。这里不一样，
SparkJury 的 agent 跑在队里那台大家共用的 DGX Spark 上，同一个 ssh 账号、同一棵工作树，
节点手册还列了几条硬红线（不许重启、不许改系统配置、不许探测内网）。所以这一层做的是
**处置与留痕**，不是一个能防住恶意代码的沙箱：

| 模式 | 只读工具 | 会改东西的工具 |
|---|---|---|
| `plan` | 放行 | 一律拒绝，理由回给模型，让它把要做的改动说清楚 |
| `safe`（默认） | 放行 | 有人在场就问一句；没人接手就放行，但在 manifest 里记成「无人值守放行」 |
| `yolo` | 放行 | 放行，仍然逐条记账 |

三种模式都拦红线。红线没有开关：那是节点使用手册定的规则，不是这次 run 的偏好。

「谁算只读」不靠这里猜：工具自己声明（`ToolSpec.readonly`），`bind()` 时从注册表读进来。
没声明的一律按「会改东西」处理——宁可多问一句，也不要自作主张。

`safe` 在没有批准通道时选择放行而不是拒绝，是因为这个 harness 的正常用法就是无人值守地跑
（节点上的 `run_tau2.sh` 那一类）。拒绝会把无人值守的用法整个堵死；而放行时把每一次都记下来，
事后在 manifest 的 `permissions` 里能数清楚有多少次是没人点头就做了的。**这笔账不能省。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence

from sparkjury.agent.ai import ToolCall

#: 节点使用手册里的硬红线。命中就拦，任何模式都没有豁免——包括 yolo。
RED_LINES: tuple[tuple[str, str], ...] = (
    ("reboot", "不许重启节点"),
    ("shutdown", "不许关机"),
    ("poweroff", "不许关机"),
    ("192.168.110.", "不许探测内网"),
    ("mkfs", "不许动文件系统"),
    ("dd if=", "不许裸写设备"),
    ("rm -rf /", "不许删根目录"),
)

#: 名字自带只读含义的工具。真正的来源是 `ToolSpec.readonly`，这里只是绑注册表之前的保守兜底。
BUILTIN_READONLY = frozenset({"load_skill", "list_dir", "read_file"})


class PermissionMode(StrEnum):
    """三种处置模式。计划模式是给演示和评审用的：模型只能看，改动必须先说出来。"""

    PLAN = "plan"
    SAFE = "safe"
    YOLO = "yolo"


class Verdict(StrEnum):
    """一次调用的处置结论。写进账本的就是这几个词，不额外造。"""

    READONLY = "readonly"          # 只读工具，直接放行
    ALLOWED = "allowed"            # 放行名单，或 yolo 模式
    APPROVED = "approved"          # 有人点头
    UNATTENDED = "unattended"      # 没有批准通道，放行但记账
    DENIED = "denied"              # 有人拒绝
    PLAN = "plan"                  # 计划模式：只读，不让改
    REDLINE = "redline"            # 踩红线
    EXPLICIT_DENY = "explicit_deny"


@dataclass(frozen=True)
class Decision:
    """放不放行、为什么、算哪一类。理由那句是给模型看的，要能照着改。"""

    allow: bool
    reason: str = ""
    verdict: Verdict = Verdict.ALLOWED

    @property
    def kind(self) -> str:
        return str(self.verdict)


Approver = Callable[[ToolCall, str], bool]


def _flatten(arguments: Any) -> list[str]:
    """把参数摊平成字符串列表：红线要能在嵌套的字典和列表里找到。"""
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif value is not None:
            out.append(str(value))

    walk(arguments)
    return out


@dataclass
class PermissionPolicy:
    """一次 run 的处置规则 + 账本。默认 `safe`。"""

    mode: PermissionMode | str = PermissionMode.SAFE
    allow: Sequence[str] = ()
    deny: Sequence[str] = ()
    approver: Approver | None = None
    redlines: Sequence[tuple[str, str]] = RED_LINES
    readonly: set[str] = field(default_factory=lambda: set(BUILTIN_READONLY))
    record_limit: int = 50
    checks: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mode = PermissionMode(str(self.mode))

    # ------------------------------------------------------------ 绑定工具表
    def bind(self, registry: Any) -> "PermissionPolicy":
        """从注册表读「谁只读」。工具自己声明，不在这里维护名单。"""
        for tool in registry.specs():
            if getattr(tool, "readonly", False):
                self.readonly.add(tool.name)
        return self

    # ------------------------------------------------------------ 判定
    def _decide(self, name: str, arguments: Any) -> Decision:
        args = arguments if isinstance(arguments, dict) else {}
        for needle, why in self.redlines:
            if any(needle in text for text in _flatten(args)):
                return Decision(False, f"这条调用踩了节点红线（{why}：参数里有「{needle}」）。"
                                        f"换成不碰它的做法，或者跟人说清楚为什么必须这么做。",
                                Verdict.REDLINE)
        if name in self.deny:
            return Decision(False, f"工具 {name} 在这次的拒绝名单里，换别的路走。", Verdict.EXPLICIT_DENY)
        if name in self.allow:
            return Decision(True, "在放行名单里", Verdict.ALLOWED)
        if name in self.readonly:
            return Decision(True, "只读工具", Verdict.READONLY)
        if self.mode is PermissionMode.PLAN:
            return Decision(False, f"现在是计划模式：只读工具能用，{name} 会改东西，不让执行。"
                                    f"把你的打算写成步骤说清楚，等有人点头再动手。", Verdict.PLAN)
        if self.mode is PermissionMode.YOLO:
            return Decision(True, "yolo 模式全放行（仍然逐条记账）", Verdict.ALLOWED)
        if self.approver is not None:
            question = f"要执行 {name}（{_brief(args)}），允许吗？"
            if bool(self.approver(ToolCall(id="", name=name, arguments=args), question)):
                return Decision(True, "有人点头", Verdict.APPROVED)
            return Decision(False, f"有人拒绝了这次 {name} 调用，别再试同一条路。", Verdict.DENIED)
        return Decision(True, "没有批准通道，按无人值守放行并记账", Verdict.UNATTENDED)

    def check(self, call: ToolCall) -> Decision:
        """判定一次工具调用，并记一笔账。循环里走的就是这条。"""
        decision = self._decide(call.name, call.arguments)
        self.checks += 1
        kind = str(decision.verdict)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if len(self.records) < self.record_limit:
            self.records.append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": call.name, "verdict": kind, "allowed": decision.allow,
                "args": _brief(call.arguments, limit=120),
            })
        return decision

    def hook(self) -> Callable[[ToolCall], "bool | str"]:
        """给 `Hooks.before_tool` 用：放行返回 True，拦下返回理由。"""

        def before_tool(call: ToolCall) -> "bool | str":
            decision = self.check(call)
            return True if decision.allow else decision.reason

        return before_tool

    # ------------------------------------------------------------ 报表
    def preview(self, names: Iterable[str]) -> list[tuple[str, Decision]]:
        """只看会怎么判，不记账。`sparkjury agent policy` 打的就是它。"""
        return [(name, self._decide(name, {})) for name in names]

    def report(self) -> dict[str, Any]:
        """进 manifest 的那份账：模式、次数、逐条记录。"""
        return {"mode": str(self.mode), "checks": self.checks, "counts": dict(self.counts),
                "unattended": self.counts.get(str(Verdict.UNATTENDED), 0),
                "denied": (self.counts.get(str(Verdict.DENIED), 0)
                           + self.counts.get(str(Verdict.PLAN), 0)
                           + self.counts.get(str(Verdict.EXPLICIT_DENY), 0)
                           + self.counts.get(str(Verdict.REDLINE), 0)),
                "records": list(self.records)}


def _brief(arguments: Any, limit: int = 60) -> str:
    """参数的一句话摘要。只用于记账和问人，不进提示词。"""
    if not isinstance(arguments, dict) or not arguments:
        return "无参数"
    parts = []
    for key, value in arguments.items():
        text = value if isinstance(value, str) else str(value)
        text = text.replace("\n", " ")
        if len(text) > 30:
            text = text[:30] + "…"
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


__all__ = ["BUILTIN_READONLY", "Decision", "PermissionMode", "PermissionPolicy", "RED_LINES",
           "Verdict"]
