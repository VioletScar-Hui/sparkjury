"""agent loop：一次 run 就是一串 turn，直到模型不再要求调工具。

对应 pi 的 agent loop：消息进当前分支 → 组装请求（system prompt + 活动分支 + 工具表）
→ 模型回文本和工具调用 → 执行工具、把结果写回会话 = 一个 turn → 还有要求就再来一轮，
否则这个 run 结束。

三种打断方式，语义跟 pi 对齐：
- `steer(text)`：在当前 assistant turn 之后插入，下一个请求就能看到（工具跑着的时候插话）。
- `followup(text)`：等这一轮活干完、模型自己说完了，再作为新的一轮发过去。
- `abort()`：停掉当前 run，已经写进会话的记录一条不删。

这一层只管推进，不碰网络也不碰文件：模型从 Provider 来，工具从 ToolRegistry 来，
落到哪儿由 runtime 决定。

中层加进来的四件事都在这里落地：

- **钩子**：请求前、工具执行前（可拦下）、工具执行后、每轮结束四个挂点。
- **工具结果重放**：中断恢复时，已经记下结果的调用不再执行第二遍——副作用只发生一次。
- **执行前打标记**：工具刚开始执行就写一条 `tool_start`。标记和结果之间被打断的调用状态未知，
  恢复时不许想当然地重跑，交给模型决定。
- **压缩**：请求前问一句 Compactor，超预算就把老消息压成一条摘要。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from sparkjury.agent.ai import Provider, ToolCall, Turn
from sparkjury.agent.hooks import Hooks
from sparkjury.agent.session import SessionTree
from sparkjury.agent.tools import SkillInfo, ToolRegistry, describe_skills
from sparkjury.harness.events import EventBus, EventKind

STOPPED_END_TURN = "end_turn"
STOPPED_ABORTED = "aborted"
STOPPED_MAX_TURNS = "max_turns"
STOPPED_ERROR = "error"

DEFAULT_MAX_TURNS = 12


def default_system_prompt(registry: ToolRegistry, skills: Sequence[SkillInfo] = (), *,
                          workdir: str | Path | None = None, extra: str = "") -> str:
    """system prompt 里只放技能的一句话描述；正文让模型自己用 load_skill 去取。"""
    lines = [
        "你是 SparkJury 的评测 Agent，跑在团队的 DGX Spark 节点上。你的活是给别人的 Agent 做体检：",
        "把轨迹灌进库里、用裁判面板打分、分歧交给仲裁、把坏例子聚类、最后出证据卡片。",
        "",
        "你可以调用这些工具：",
        registry.describe(),
    ]
    if skills:
        lines += ["", "仓库里有这些技能（要看说明书就调 load_skill）：", describe_skills(skills)]
    if workdir:
        lines += ["", f"工作目录是 {workdir}，list_dir 和 read_file 只能在这个目录里活动。"]
    lines += [
        "",
        "干活规矩：",
        "- 不确定某个技能怎么用，先 load_skill 读它的说明书，不要凭猜编参数。",
        "- 按 SparkJury 流水线的顺序推进：清洗导入 → 定评测集 → 打分 → 出卡片。",
        "- 真跑命令用 run_skill，参数原样透传给命令行。",
        "- 工具报错就照实说清楚是哪一步失败了，不要绕过它假装成功。",
        "- 最后一次回答不要再调工具，直接用中文讲清楚：你做了什么、看到什么、结论是什么。",
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


@dataclass
class LoopResult:
    """一次 run 的结果。`stopped` 说明它是怎么停的，回放时先看这个。"""

    stopped: str
    turns: int = 0
    tool_calls: int = 0
    text: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    session_id: str = ""
    session_path: str | None = None
    resumed: bool = False          # 这次是从中断处接着跑的（没有重新起一条用户消息）
    compactions: int = 0
    replayed_calls: int = 0        # 恢复时重放而没有重新执行的工具调用数

    @property
    def ok(self) -> bool:
        return self.stopped in (STOPPED_END_TURN, STOPPED_MAX_TURNS) and not self.error


class AgentLoop:
    """把「模型说要干什么」和「工具真的干了什么」串起来的那只循环。"""

    def __init__(self, provider: Provider, registry: ToolRegistry, session: SessionTree, *,
                 bus: EventBus | None = None, max_turns: int = DEFAULT_MAX_TURNS,
                 system_prompt: str | None = None, skills: Sequence[SkillInfo] = (),
                 on_turn: Callable[[Turn, int], None] | None = None,
                 hooks: Hooks | None = None, compactor: Any | None = None,
                 durable_journal: bool = True):
        self.provider = provider
        self.registry = registry
        self.session = session
        self.bus = bus
        self.max_turns = max(1, int(max_turns))
        self.skills = list(skills)
        self.on_turn = on_turn
        self.hooks = hooks or Hooks()
        self.compactor = compactor
        self.durable_journal = durable_journal
        self.compactions = 0
        self.replayed_calls = 0
        self._last_prompt_tokens: int | None = None
        # 恢复用的两份快照：只在 run/resume 开始时取一次。工具调用 id 不保证跨轮唯一
        # （有的端点每轮都从 call_1 开始编），所以「这条调用的结果是不是早就有了」只能拿
        # 开跑那一刻的日志来判断，不能边跑边看——否则本轮刚写下的结果会被当成上一轮的重放。
        self._journal_results: dict[str, str] = {}
        self._journal_started: set[str] = set()
        self._used_call_ids: set[str] = set()
        self._system_prompt = system_prompt
        self._steering: list[str] = []
        self._followups: list[str] = []
        self._aborted = False
        self.usage: dict[str, int] = {}

    # ------------------------------------------------------------ 三个打断口
    def steer(self, text: str) -> None:
        self._steering.append(text)

    def followup(self, text: str) -> None:
        self._followups.append(text)

    def abort(self) -> None:
        self._aborted = True

    @property
    def aborted(self) -> bool:
        return self._aborted

    # ------------------------------------------------------------ 主循环
    def run(self, prompt: str | None = None, *, resumed: bool = False) -> LoopResult:
        self._aborted = False
        self._snapshot_journal()
        result = LoopResult(stopped=STOPPED_END_TURN, session_id=self.session.session_id,
                            session_path=str(self.session.path) if self.session.path else None,
                            resumed=resumed)
        self._ensure_system()
        if prompt:
            self.session.append("message", role="user", data={"text": prompt})
        self._publish(EventKind.RUN_START, "agent run 开始（从中断处继续）" if resumed else "agent run 开始",
                      model=self.provider.spec.model, base_url=self.provider.spec.base_url,
                      max_turns=self.max_turns, session_id=self.session.session_id, resumed=resumed)
        while True:
            if self._aborted:
                result.stopped = STOPPED_ABORTED
                self.session.append("note", data={"text": "run 被中断，已完成的记录保留", "level": "warn"})
                self._publish(EventKind.WARNING, "run 被中断")
                break
            if result.turns >= self.max_turns:
                result.stopped = STOPPED_MAX_TURNS
                self.session.append("note", data={"text": f"达到轮数上限 {self.max_turns}", "level": "warn"})
                self._publish(EventKind.WARNING, f"达到轮数上限 {self.max_turns}")
                break
            self._flush_steering()
            self._maybe_compact()
            messages = self.session.messages()
            tools_payload = self.registry.payload()
            self.hooks.run_before_request(messages, tools_payload)
            try:
                turn = self.provider.complete(messages, tools_payload)
            except KeyboardInterrupt:
                # Ctrl-C 不算异常退出：按中断处理，已写下的记录一条不动
                self._aborted = True
                continue
            result.turns += 1
            self._accumulate_usage(turn, result)
            if self.on_turn:
                self.on_turn(turn, result.turns)
            self.hooks.run_after_turn(turn, result.turns)
            if turn.error:
                result.stopped = STOPPED_ERROR
                result.error = turn.error
                self.session.append("note", data={"text": f"模型调用失败：{turn.error}", "level": "error"})
                self._publish(EventKind.ERROR, f"模型调用失败：{turn.error}",
                              model=self.provider.spec.model, turn=result.turns)
                break
            self._publish(EventKind.PROGRESS, f"第 {result.turns} 轮：模型回复",
                          phase="assistant", turn=result.turns, tool_calls=len(turn.tool_calls),
                          latency_ms=round(turn.latency_ms, 1))
            if turn.tool_calls:
                result.tool_calls += len(turn.tool_calls)
                self._record_tool_calls(turn)
                self._run_tools(turn.tool_calls)
                continue
            result.text = turn.text
            answer: dict[str, Any] = {"text": turn.text}
            if turn.thinking:
                answer["thinking"] = turn.thinking
            self.session.append("message", role="assistant", data=answer)
            if self._aborted:
                # 中断是在模型回答的这一轮里到达的。回答已经落盘（不删），但结束方式如实记成中断——
                # 记成 end_turn 会让人以为这次是自然跑完的。
                result.stopped = STOPPED_ABORTED
                self.session.append("note", data={"text": "这一轮回答期间收到中断请求，run 以中断收尾",
                                                  "level": "warn"})
                self._publish(EventKind.WARNING, "模型回答期间收到中断，run 以中断收尾")
                break
            if self._followups:
                text = self._followups.pop(0)
                self.session.append("message", role="user", data={"text": text, "followup": True})
                self._publish(EventKind.PROGRESS, "收到 follow-up，继续跑", phase="followup")
                continue
            if self._steering:
                # 插话是在这一轮回答期间到的。它出现在「这一轮之后」，所以不能就这么收尾——
                # 上面那圈循环开头会把它冲成一条 user 消息，这一轮白说的话就白说了。
                self._publish(EventKind.PROGRESS, "收到 steering，继续跑", phase="steering")
                continue
            result.stopped = STOPPED_END_TURN
            break
        result.compactions = self.compactions
        result.replayed_calls = self.replayed_calls
        self._publish(EventKind.RUN_END, f"agent run 结束：{result.stopped}", stopped=result.stopped,
                      turns=result.turns, tool_calls=result.tool_calls, usage=dict(self.usage),
                      error=result.error, compactions=self.compactions,
                      replayed_calls=self.replayed_calls)
        return result

    # ------------------------------------------------------------ 中断恢复
    def resume(self) -> LoopResult:
        """从中断处接着跑。不追加新的用户消息，先把手头的活收干净。

        三种情况分得很清楚，靠会话树自己说：

        - 最后一条是 `tool_call` 且还有没拿到结果的调用 → 先把它们执行掉（已经记过结果的
          重放、不重跑；只有开始标记没有结果的按「状态未知」处理，不重复副作用）。
        - 最后一条是 `tool_result` → 模型还欠一个回答，继续问。
        - 最后一条是 assistant 的回答 → 这一轮其实已经说完了，不再问模型，直接以 `end_turn` 收尾。
        """
        result = LoopResult(stopped=STOPPED_END_TURN, session_id=self.session.session_id,
                            session_path=str(self.session.path) if self.session.path else None,
                            resumed=True)
        self._aborted = False
        self._snapshot_journal()
        self._publish(EventKind.PROGRESS, "从中断处继续", phase="resume",
                      leaf=self.session.leaf_id, pending=len(self.session.pending_calls()))
        last_batch = next((e for e in reversed(self.session.path_to()) if e.kind == "tool_call"), None)
        if last_batch is not None:
            # 整批交回给 _run_tools：它按开跑时的日志判断哪些已经有结果（重放）、哪些状态未知、
            # 哪些真的要跑。这样「一批调用」的处理只有一处实现，恢复路径不会长出第二套判断。
            self._run_tools([ToolCall(id=str(c.get("id")), name=str(c.get("name", "")),
                                      arguments=_loads(c.get("arguments")),
                                      raw_arguments=str(c.get("arguments") or "{}"))
                             for c in (last_batch.data.get("calls") or [])])
        tail = self.session.path_to()[-1] if len(self.session) else None
        if tail is not None and tail.kind == "message" and tail.role == "assistant":
            # 回答已经落盘：这一轮没什么可继续的，把结果如实报回去
            result.text = str(tail.data.get("text", ""))
            result.turns = 0
            result.replayed_calls = self.replayed_calls
            self._publish(EventKind.RUN_END, "agent run 结束：end_turn（回答已在记录里，未重复调用模型）",
                          stopped=STOPPED_END_TURN, turns=0, resumed=True)
            return result
        return self.run(None, resumed=True)

    # ------------------------------------------------------------ 内部
    def _ensure_system(self) -> None:
        if len(self.session) and self.session.entries[0].role == "system":
            return
        text = self._system_prompt or default_system_prompt(self.registry, self.skills)
        if len(self.session):
            # 会话已经开了头（比如从文件恢复），补一条 system 到当前分支尾部不如直接记个说明
            self.session.append("note", data={"text": "会话已有内容，沿用文件里的记录；system prompt 未重新写入"})
            return
        self.session.append("message", role="system", data={"text": text})

    def _flush_steering(self) -> None:
        while self._steering:
            text = self._steering.pop(0)
            self.session.append("message", role="user", data={"text": text, "steering": True})
            self._publish(EventKind.PROGRESS, "插入 steering 消息", phase="steering")

    def _record_tool_calls(self, turn: Turn) -> None:
        data: dict[str, Any] = {
            "text": turn.text,
            "calls": [{"id": c.id, "name": c.name, "arguments": c.raw_arguments or _dumps(c.arguments)}
                      for c in turn.tool_calls],
        }
        if turn.thinking:  # 思考单独存：回放时看得到，正文和卡片里不出现
            data["thinking"] = turn.thinking
        self.session.append("tool_call", data=data)

    def _run_tools(self, calls: Sequence[ToolCall]) -> None:
        for call in calls:
            known = self._journal_results
            started = self._journal_started
            fresh = call.id not in self._used_call_ids
            if self._aborted:
                self.session.append("tool_result", data={"call_id": call.id, "name": call.name,
                                                         "output": "（run 已中断，未执行）", "ok": False})
                continue
            if call.id in known and fresh:   # 中断恢复：结果早就在日志里，重放而不再执行一次
                self._used_call_ids.add(call.id)
                self.replayed_calls += 1
                # 不重写一条 tool_result：那一份结果还在分支里，再写一遍会让模型看到两份同样的工具结果。
                # 只记一条「这条是重放的」标记，回放和验收都能看出跳过了一次执行。
                self.session.append("note", data={"phase": "tool_replay", "call_id": call.id,
                                                  "name": call.name})
                self._publish(EventKind.PROGRESS, f"工具 {call.name} 结果重放（未重复执行）",
                              phase="tool", tool=call.name, replayed=True)
                continue
            if call.id in started and fresh:
                # 有开始标记、没有结果：进程是在这个工具执行到一半时没的。重跑可能造成第二次副作用，
                # 所以不替模型做决定——如实记一条「状态未知」，让它自己选择要不要再来一次。
                self.session.append("tool_result", data={
                    "call_id": call.id, "name": call.name, "ok": False,
                    "output": f"（上一次执行到一半就中断了，{call.name} 的完成情况未知；"
                              f"为避免重复副作用没有自动重跑。需要的话请再调一次。）"})
                self._publish(EventKind.WARNING, f"工具 {call.name} 上一次执行状态未知，未自动重跑",
                              phase="tool", tool=call.name, uncertain=True)
                continue
            allowed, reason = self.hooks.tool_verdict(call)
            if not allowed:
                self.session.append("tool_result", data={"call_id": call.id, "name": call.name,
                                                         "output": reason, "ok": False})
                self._publish(EventKind.WARNING, f"工具 {call.name} 被 hook 拦下", phase="tool",
                              tool=call.name, ok=False, reason=reason)
                continue
            if self.durable_journal:        # 先记「开始执行」，再真的执行
                self.session.append("note", data={"phase": "tool_start", "call_id": call.id,
                                                  "name": call.name})
            self._used_call_ids.add(call.id)
            output, ok = self.registry.call(call.name, call.arguments)
            self.session.append("tool_result", data={"call_id": call.id, "name": call.name,
                                                     "output": output, "ok": ok})
            self.hooks.run_after_tool(call, output, ok)
            self._publish(EventKind.PROGRESS if ok else EventKind.WARNING,
                          f"工具 {call.name} {'完成' if ok else '失败'}", phase="tool", tool=call.name,
                          ok=ok, output_chars=len(output))
            if self._aborted:               # 工具里刚被中断（比如 hook 里 request_abort）
                self._publish(EventKind.WARNING, "工具执行后收到中断请求", phase="tool")

    def _snapshot_journal(self) -> None:
        """记下「开跑那一刻日志里已经有什么」。重放与状态未知的判断都只认这份快照。"""
        self._journal_results = self.session.results_by_call_id()
        self._journal_started = self.session.started_call_ids()
        self._used_call_ids = set()

    def _maybe_compact(self) -> None:
        """请求前问一句压缩器。触发条件是上一轮端点回的 prompt_tokens，不是估的字符数。"""
        if self.compactor is None:
            return
        entry = self.compactor.maybe_compact(self.session, prompt_tokens=self._last_prompt_tokens,
                                             prompt_chars=len(str(self.session.messages())))
        if entry is not None:
            self.compactions += 1
            self._publish(EventKind.PROGRESS, f"历史已压缩：{entry.data.get('replaced')} 条压成一条摘要",
                          phase="compaction", entry=entry.id, source=entry.data.get("source"))

    def _accumulate_usage(self, turn: Turn, result: LoopResult) -> None:
        for key, value in (turn.usage or {}).items():
            self.usage[key] = self.usage.get(key, 0) + int(value)
        result.usage = dict(self.usage)
        # 下一轮的预算判断用端点自己报的 prompt_tokens（真实值），没有就退回字符估算
        if turn.usage.get("prompt_tokens"):
            self._last_prompt_tokens = int(turn.usage["prompt_tokens"])

    def _publish(self, kind: EventKind, message: str, **data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(kind, message, stage="AGENT", **data)


def _dumps(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any) -> dict[str, Any]:
    """会话里存的工具参数是 JSON 串；坏了就当空参数，别让恢复流程卡在这儿。"""
    import json

    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
