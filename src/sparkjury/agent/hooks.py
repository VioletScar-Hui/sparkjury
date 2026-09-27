"""钩子：循环上的几个挂点，给「不想改循环本身」的扩展留位置。

pi 的 harness 自己拥有工具表和 prompt 资源表，扩展通过注册表挂进去。这里最小化到四个挂点，
够用就行：

- `before_request`：请求发出前能改消息（注入一条系统提示、把敏感字段抹掉、加一句"别忘了交付"）。
- `before_tool`：**能拦下这次工具调用**。返回 False 就执行不了，循环会把它记成一次失败调用，
  模型看得到"被拦住了"和原因。人工确认、预算封顶、只读模式都靠这个。
- `after_tool`：执行完看一眼（审计、计数、把结果同步到别处）。
- `after_turn`：每一轮 assistant 回合结束（看用量、判断要不要停）。

钩子里抛异常不吞：钩子是调用方自己写的代码，出错就该看见，不能悄悄跳过之后当成没配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sparkjury.agent.ai import ToolCall, Turn

BeforeRequest = Callable[[list[dict[str, Any]], list[dict[str, Any]]], None]
BeforeTool = Callable[[ToolCall], "bool | str | None"]
AfterTool = Callable[[ToolCall, str, bool], None]
AfterTurn = Callable[[Turn, int], None]


@dataclass
class Hooks:
    """四个挂点的集合。默认全是空的——没配就是没配，不做任何事。"""

    before_request: list[BeforeRequest] = field(default_factory=list)
    before_tool: list[BeforeTool] = field(default_factory=list)
    after_tool: list[AfterTool] = field(default_factory=list)
    after_turn: list[AfterTurn] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.before_request or self.before_tool or self.after_tool or self.after_turn)

    def run_before_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
        for hook in self.before_request:
            hook(messages, tools)

    def tool_verdict(self, call: ToolCall) -> tuple[bool, str]:
        """问每个 before_tool 放不放行。返回 (放行?, 拒绝理由)。

        钩子可以返回 False（拒绝）、字符串（拒绝并带上理由）、None（不表态）。
        只要有一个拒绝就拒绝：拦的语义是保守的。
        """
        for hook in self.before_tool:
            verdict = hook(call)
            if verdict is None or verdict is True:
                continue
            if verdict is False:
                return False, f"被 hook 拦下：{call.name}"
            return False, str(verdict)
        return True, ""

    def run_after_tool(self, call: ToolCall, output: str, ok: bool) -> None:
        for hook in self.after_tool:
            hook(call, output, ok)

    def run_after_turn(self, turn: Turn, index: int) -> None:
        for hook in self.after_turn:
            hook(turn, index)
