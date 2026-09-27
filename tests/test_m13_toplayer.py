"""守卫 agent harness 的上层（M13）：权限怎么判、接口长什么样、派活派得出去也收得回来。

这一层盯的还是「悄悄错」，而且比前两层更容易悄悄错：

- 权限：被拦下的调用有没有真的没执行（而不是执行了再记一笔拒绝）、红线是不是在每种模式下都拦、
  无人值守放行的那几次有没有被数出来。
- 接口：`--print` 有没有混进别的东西、事件流每一行是不是都能 `json.loads`、
  RPC 跑着的时候插话和喊停还收不收得到。
- 子 agent：子 run 有没有自己的目录、父 manifest 里看不看得出子 agent 干了什么、
  子 agent 出事的时候父 run 会不会被一起带走或者反过来把失败写成成功。

全部离线：模型是脚本替身，技能执行器是离线替身，不联网、不起子进程。
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from sparkjury.agent.ai import ModelSpec, ScriptedProvider, Turn, text_turn, tool_turn
from sparkjury.agent.iface import (
    RPC_OPS,
    RpcSession,
    StreamWriter,
    event_payload,
    final_text,
    run_with_events,
)
from sparkjury.agent.perms import RED_LINES, Decision, PermissionMode, PermissionPolicy, Verdict
from sparkjury.agent.runtime import AgentRuntime
from sparkjury.agent.session import SessionTree
from sparkjury.agent.spawn import CHILDREN_DIR, SubagentRunner, task_tool_spec
from sparkjury.agent.tools import OfflineSkillExecutor, ToolError, ToolRegistry, ToolSpec, load_skill_tools

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 离线零件


def offline_registry(tmp_path: Path, replies: dict[str, str] | None = None):
    executor = OfflineSkillExecutor(replies or {})
    registry, skills = load_skill_tools(ROOT / "skills", executor=executor, workdir=tmp_path)
    return registry, skills, executor


def make_runtime(tmp_path: Path, turns=None, *, provider=None, permissions=None, subagents=True,
                 max_turns: int = 12, factory=None, subagent_max_depth: int = 1) -> AgentRuntime:
    registry, skills, _executor = offline_registry(tmp_path)
    provider = provider or ScriptedProvider(list(turns or []))
    return AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                        skills=skills, permissions=permissions, subagents=subagents,
                        max_turns=max_turns, subagent_factory=factory,
                        subagent_max_depth=subagent_max_depth)


def manifest_of(runtime: AgentRuntime) -> dict:
    return json.loads(runtime.run.manifest_path.read_text(encoding="utf-8"))


class RecordingExecutor(OfflineSkillExecutor):
    """离线执行器 + 一本调用账：用来证明「被拦下的调用真的没跑」。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, list[str]]] = []

    def __call__(self, skill, args):
        self.seen.append((skill.name, [str(a) for a in args]))
        return super().__call__(skill, args)


class BlockingProvider:
    """会卡住的模型替身：第一轮进来就等着，直到测试放行。用来测「跑着的时候插话/喊停」。"""

    def __init__(self, *, answer: str = "干完了", block_on: int = 1) -> None:
        self.spec = ModelSpec(name="blocking", model="blocking-1", base_url="blocking://test")
        self.answer = answer
        self.block_on = block_on
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests: list[list[dict]] = []
        self.calls = 0

    def complete(self, messages, tools=None):
        self.requests.append([dict(m) for m in messages])
        self.calls += 1
        if self.calls >= self.block_on:
            self.entered.set()
            self.release.wait(timeout=10)
        return text_turn(self.answer)


def build_rpc(tmp_path: Path, commands: list[dict], *, provider=None, permissions=None,
              turns=None, emit_events: bool = True):
    """喂一串命令给 RPC，返回 (退出码, 解析后的输出行)。"""
    registry, skills, _executor = offline_registry(tmp_path)
    provider = provider or ScriptedProvider(list(turns or [text_turn("好了")]))
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills, permissions=permissions)
    stdin = io.StringIO("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands))
    stdout = io.StringIO()
    session = RpcSession(runtime, stdin=stdin, stdout=stdout, emit_events=emit_events)
    code = session.serve()
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    return code, lines, runtime


# ---------------------------------------------------------------- 权限：判定


def test_plan_mode_lets_reads_through_and_refuses_writes():
    policy = PermissionPolicy(mode=PermissionMode.PLAN)
    readings = dict(policy.preview(["load_skill", "list_dir", "read_file"]))
    assert all(d.allow for d in readings.values())
    assert all(d.kind == str(Verdict.READONLY) for d in readings.values())
    write = policy.preview(["run_skill"])[0][1]
    assert not write.allow and write.kind == str(Verdict.PLAN)
    assert "计划模式" in write.reason and "run_skill" in write.reason


def test_safe_mode_without_approver_allows_but_books_it():
    policy = PermissionPolicy(mode=PermissionMode.SAFE)
    decision = policy.preview(["run_skill"])[0][1]
    assert decision.allow and decision.kind == str(Verdict.UNATTENDED)
    from sparkjury.agent.ai import ToolCall

    policy.check(ToolCall(id="c1", name="run_skill", arguments={"name": "sparkjury-clean"}))
    report = policy.report()
    assert report["mode"] == "safe" and report["checks"] == 1
    assert report["unattended"] == 1 and report["counts"]["unattended"] == 1
    assert report["records"][0]["tool"] == "run_skill"


def test_safe_mode_with_approver_records_who_said_yes_and_no():
    asked: list[str] = []

    def approver(_call, question: str) -> bool:
        asked.append(question)
        return "run_skill" not in question

    policy = PermissionPolicy(mode=PermissionMode.SAFE, approver=approver)
    from sparkjury.agent.ai import ToolCall

    yes = policy.check(ToolCall(id="c1", name="list_dir", arguments={}))
    assert yes.allow and yes.kind == str(Verdict.READONLY)

    from sparkjury.agent.ai import ToolCall as TC

    denied = policy.check(TC(id="c2", name="run_skill", arguments={"name": "sparkjury-score"}))
    assert not denied.allow and denied.kind == str(Verdict.DENIED)
    assert "拒绝了" in denied.reason
    assert any("run_skill" in q for q in asked), "问人的那句话里要写清楚要跑什么"


def test_redlines_are_refused_in_every_mode():
    from sparkjury.agent.ai import ToolCall

    for mode in (PermissionMode.PLAN, PermissionMode.SAFE, PermissionMode.YOLO):
        policy = PermissionPolicy(mode=mode)
        decision = policy.check(ToolCall(id="c1", name="run_skill",
                                         arguments={"name": "x", "args": ["--cmd", "sudo reboot now"]}))
        assert not decision.allow, f"{mode} 模式下红线没拦住"
        assert decision.kind == str(Verdict.REDLINE) and "红线" in decision.reason
        nested = policy.check(ToolCall(id="c2", name="run_skill",
                                       arguments={"name": "x", "args": {"host": "192.168.110.7"}}))
        assert not nested.allow, "嵌在字典里的内网地址也要拦"


def test_yolo_still_blocks_redlines_but_allows_writes():
    from sparkjury.agent.ai import ToolCall

    policy = PermissionPolicy(mode=PermissionMode.YOLO)
    ok = policy.check(ToolCall(id="c1", name="run_skill", arguments={"name": "sparkjury-clean"}))
    assert ok.allow and ok.kind == str(Verdict.ALLOWED)
    blocked = policy.check(ToolCall(id="c2", name="run_skill", arguments={"args": ["poweroff"]}))
    assert not blocked.allow and blocked.kind == str(Verdict.REDLINE)


def test_readonly_comes_from_the_tool_not_from_a_name_list():
    def handler(_args):  # pragma: no cover - 不会被调用
        return "ok"

    tools = ToolRegistry([
        ToolSpec(name="peek", description="只读的自定义工具", parameters={"type": "object", "properties": {}},
                 handler=handler, source="test", readonly=True),
        ToolSpec(name="poke", description="会改东西的自定义工具", parameters={"type": "object", "properties": {}},
                 handler=handler, source="test"),
    ])
    policy = PermissionPolicy(mode=PermissionMode.PLAN).bind(tools)
    verdicts = dict(policy.preview(["peek", "poke"]))
    assert verdicts["peek"].allow, "声明了 readonly 的工具在计划模式下也该放行"
    assert not verdicts["poke"].allow, "没声明的一律按会改东西处理"


def test_explicit_allow_and_deny_lists_win():
    policy = PermissionPolicy(mode=PermissionMode.PLAN, allow=["run_skill"], deny=["list_dir"])
    verdicts = dict(policy.preview(["run_skill", "list_dir"]))
    assert verdicts["run_skill"].allow and verdicts["run_skill"].kind == str(Verdict.ALLOWED)
    assert not verdicts["list_dir"].allow and verdicts["list_dir"].kind == str(Verdict.EXPLICIT_DENY)


def test_preview_does_not_touch_the_ledger():
    policy = PermissionPolicy()
    policy.preview(["run_skill", "list_dir"])
    report = policy.report()
    assert report["checks"] == 0 and report["records"] == []


def test_hook_returns_reason_text_for_the_model():
    from sparkjury.agent.ai import ToolCall

    hook = PermissionPolicy(mode=PermissionMode.PLAN).hook()
    assert hook(ToolCall(id="c1", name="load_skill", arguments={})) is True
    refusal = hook(ToolCall(id="c2", name="run_skill", arguments={}))
    assert isinstance(refusal, str) and refusal.strip(), "拦下时要把理由交回给模型，不能只说 no"
    assert "计划模式" in refusal


def test_redlines_cover_the_node_manual_rules():
    needles = {needle for needle, _why in RED_LINES}
    for required in ("reboot", "shutdown", "poweroff", "192.168.110."):
        assert required in needles, f"节点手册的红线少了 {required}"


# ---------------------------------------------------------------- 权限：接进循环


def test_plan_mode_run_never_executes_the_skill(tmp_path: Path):
    registry, skills, _exec = offline_registry(tmp_path)
    executor = RecordingExecutor()
    registry, skills = load_skill_tools(ROOT / "skills", executor=executor, workdir=tmp_path)
    permission = PermissionPolicy(mode=PermissionMode.PLAN)
    provider = ScriptedProvider([
        tool_turn(("run_skill", {"name": "sparkjury-clean", "args": ["--db", "x.db"]})),
        text_turn("没法动手，我先把要做的说清楚。"),
    ])
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills, permissions=permission)
    result = runtime.start("跑一遍体检")
    assert result.ok and executor.seen == [], "被拦下的调用绝不能真的执行"
    manifest = manifest_of(runtime)
    assert manifest["permissions"]["denied"] == 1
    assert any("denied by policy" in d for d in manifest["degradations"])


def test_denied_call_becomes_a_message_the_model_can_read(tmp_path: Path):
    registry, skills, _exec = offline_registry(tmp_path)
    permission = PermissionPolicy(mode=PermissionMode.PLAN)
    provider = ScriptedProvider([
        tool_turn(("run_skill", {"name": "sparkjury-clean", "args": []})),
        text_turn("好，我不跑了。"),
    ])
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills, permissions=permission)
    runtime.start("跑一遍")
    second_request = json.dumps(provider.requests[-1], ensure_ascii=False)
    assert "计划模式" in second_request, "拒绝的理由要回灌给模型，它才知道换个做法"
    assert "run_skill" in second_request


def test_policy_hook_runs_before_user_hooks(tmp_path: Path):
    seen: list[str] = []

    def nosy(call):
        seen.append(call.name)
        return None

    registry, skills, _exec = offline_registry(tmp_path)
    permission = PermissionPolicy(mode=PermissionMode.PLAN)
    from sparkjury.agent.hooks import Hooks

    hooks = Hooks(before_tool=[nosy])
    provider = ScriptedProvider([tool_turn(("run_skill", {"name": "sparkjury-clean", "args": []})),
                                 text_turn("停了")])
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills, permissions=permission, hooks=hooks)
    runtime.start("跑一遍")
    assert seen == [], "权限钩子排在最前面：已经拦下的调用不该再走到别的钩子里"


def test_unattended_writes_are_counted_in_the_manifest(tmp_path: Path):
    runtime = make_runtime(tmp_path, [
        tool_turn(("run_skill", {"name": "sparkjury-clean", "args": ["--db", "x.db"]})),
        text_turn("跑完了"),
    ], permissions=PermissionPolicy(mode=PermissionMode.SAFE))
    result = runtime.start("跑一遍")
    assert result.ok
    manifest = manifest_of(runtime)
    assert manifest["permissions"]["unattended"] == 1
    assert manifest["permissions"]["denied"] == 0
    assert not any("denied by policy" in d for d in manifest["degradations"]), "无人值守放行不是降级"


def test_subagents_can_be_switched_off(tmp_path: Path):
    runtime = make_runtime(tmp_path, [text_turn("没什么可派的")], subagents=False)
    assert "task" not in runtime.registry.names()
    assert runtime.subagents is None


def test_task_tool_is_registered_by_default(tmp_path: Path):
    runtime = make_runtime(tmp_path, [text_turn("好")])
    assert "task" in runtime.registry.names()
    spec = runtime.registry.get("task")
    assert "task" in runtime.system_prompt, "工具清单是在注册表定稿之后才写进提示词的"
    payload = spec.to_payload()
    assert payload["type"] == "function" and payload["function"]["parameters"]["required"] == ["prompt"]


# ---------------------------------------------------------------- 接口：print 与事件流


def test_final_text_prefers_result_then_falls_back_to_session(tmp_path: Path):
    runtime = make_runtime(tmp_path, [text_turn("这是结论")])
    result = runtime.start("跑")
    assert final_text(runtime, result) == "这是结论"

    class Fake:
        text = ""

    assert final_text(runtime, Fake()) == "这是结论", "端点没回文本时，退回会话里最后一条 assistant"


def test_event_payload_keeps_the_event_log_fields():
    from sparkjury.harness.events import EventKind

    class FakeEvent:
        ts = "2026-09-27T00:00:00+00:00"
        seq = 7
        run_id = "agent-x"
        stage = "AGENT"
        kind = EventKind.PROGRESS
        message = "工具 load_skill 完成"
        data = {"tool": "load_skill", "ok": True}

    payload = event_payload(FakeEvent())
    assert payload["type"] == "event" and payload["seq"] == 7 and payload["stage"] == "AGENT"
    assert payload["kind"] == "progress" and payload["data"]["tool"] == "load_skill"


def test_stream_writer_writes_one_parseable_line_per_payload():
    buffer = io.StringIO()
    writer = StreamWriter(buffer)
    writer.write({"text": "第一行\n第二行（换行也必须留着不折）", "n": 1})
    writer.write({"text": "ok"})
    lines = buffer.getvalue().splitlines()
    assert writer.lines == 2 and len(lines) == 2
    assert json.loads(lines[0])["text"].startswith("第一行\n")


def test_run_with_events_emits_run_start_and_a_final_result(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("list_dir", {})), text_turn("看完了")])
    buffer = io.StringIO()
    result = run_with_events(runtime, "看看工作区", writer=StreamWriter(buffer))
    assert result.ok
    lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
    kinds = [line["kind"] for line in lines if line["type"] == "event"]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert lines[-1]["type"] == "result" and lines[-1]["text"] == "看完了"
    assert runtime.bus._listeners == [], "收尾要把订阅摘掉，否则长驻进程里会越挂越多"  # noqa: SLF001


def test_run_with_events_never_mixes_plain_text_into_the_stream(tmp_path: Path):
    runtime = make_runtime(tmp_path, [text_turn("只有这段话")])
    buffer = io.StringIO()
    run_with_events(runtime, "说一句", writer=StreamWriter(buffer))
    for line in buffer.getvalue().splitlines():
        json.loads(line)  # 混进任何一行非 JSON，这里就炸


# ---------------------------------------------------------------- 接口：RPC


def test_rpc_ready_line_describes_the_session(tmp_path: Path):
    code, lines, _runtime = build_rpc(tmp_path, [{"op": "shutdown"}])
    assert code == 0
    ready = lines[0]
    assert ready["type"] == "ready" and ready["run_dir"] and "task" in ready["tools"]
    assert set(ready["ops"]) == set(RPC_OPS)


def test_rpc_prompt_runs_and_reports_the_result(tmp_path: Path):
    code, lines, _runtime = build_rpc(tmp_path, [{"op": "prompt", "text": "跑一遍"}, {"op": "shutdown"}],
                                      turns=[text_turn("跑完了")])
    assert code == 0
    kinds = [line["type"] for line in lines]
    assert "ack" in kinds and "result" in kinds
    result = [line for line in lines if line["type"] == "result"][0]
    assert result["stopped"] == "end_turn" and result["text"] == "跑完了"


def test_rpc_bad_json_does_not_kill_the_session(tmp_path: Path):
    registry, skills, _executor = offline_registry(tmp_path)
    runtime = AgentRuntime(ScriptedProvider([text_turn("ok")]), runs_dir=tmp_path / "runs", workdir=tmp_path,
                           registry=registry, skills=skills)
    stdin = io.StringIO('这不是 JSON\n{"op":"ping"}\n{"op":"shutdown"}\n')
    stdout = io.StringIO()
    assert RpcSession(runtime, stdin=stdin, stdout=stdout).serve() == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[1]["type"] == "error" and "JSON" in lines[1]["message"]
    assert any(line.get("op") == "ping" for line in lines), "坏命令之后还要能接着干活"


def test_rpc_unknown_op_lists_what_it_accepts(tmp_path: Path):
    _code, lines, _runtime = build_rpc(tmp_path, [{"op": "自爆"}, {"op": "shutdown"}])
    error = [line for line in lines if line["type"] == "error"][0]
    assert "自爆" in error["message"] and "prompt" in error["message"]


def test_rpc_inspect_and_policy_answer_with_facts(tmp_path: Path):
    policy = PermissionPolicy(mode=PermissionMode.PLAN)
    _code, lines, _runtime = build_rpc(tmp_path, [{"op": "inspect"}, {"op": "policy"}, {"op": "shutdown"}],
                                       permissions=policy)
    by_op = {line.get("op"): line for line in lines if line["type"] == "ack"}
    assert by_op["inspect"]["data"]["run_id"].startswith("agent-")
    assert by_op["inspect"]["data"]["operations"] == []
    assert by_op["policy"]["data"]["mode"] == "plan"


def test_rpc_steer_lands_in_the_next_request(tmp_path: Path):
    """跑着的时候插的话，要变成一轮新的追问，而不是等这次 run 结束就没了。"""
    provider = BlockingProvider(answer="第一轮说完了")
    registry, skills, _executor = offline_registry(tmp_path)
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills)
    stdout = io.StringIO()
    session = RpcSession(runtime, stdin=io.StringIO(), stdout=stdout)
    session._start_worker("跑一遍")            # noqa: SLF001
    assert provider.entered.wait(timeout=5)
    session._dispatch(json.dumps({"op": "steer", "text": "先别打分，数据源换了"}))  # noqa: SLF001
    provider.release.set()
    session._thread.join(timeout=10)          # noqa: SLF001
    assert len(provider.requests) >= 2, "插话之后应该再来一轮，不是就此收尾"
    sent = json.dumps(provider.requests[-1], ensure_ascii=False)
    assert "先别打分" in sent, "插话要作为一条 user 消息出现在下一个请求里"
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert any(line["type"] == "result" for line in lines)


def test_rpc_prompt_while_busy_becomes_steering(tmp_path: Path):
    provider = BlockingProvider()
    registry, skills, _executor = offline_registry(tmp_path)
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills)
    stdin, stdout = io.StringIO(), io.StringIO()
    session = RpcSession(runtime, stdin=stdin, stdout=stdout)
    session._start_worker("第一件事")          # 直接把一轮挂上去，模拟「正在跑」  # noqa: SLF001
    assert provider.entered.wait(timeout=5), "模型那一轮应该已经进去了"
    session._dispatch(json.dumps({"op": "prompt", "text": "顺便再看看日志"}))  # noqa: SLF001
    provider.release.set()
    session._thread.join(timeout=10)          # noqa: SLF001
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    ack = [line for line in lines if line["type"] == "ack" and line["op"] == "prompt"][0]
    assert ack["data"]["queued"] == "steer", "跑着的时候来的 prompt 不能又起一轮"


def test_rpc_abort_stops_the_running_turn(tmp_path: Path):
    provider = BlockingProvider(answer="不该出现的回答")

    def commands():
        yield {"op": "prompt", "text": "跑个长活"}

    registry, skills, _executor = offline_registry(tmp_path)
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills)
    stdin, stdout = io.StringIO(), io.StringIO()
    session = RpcSession(runtime, stdin=stdin, stdout=stdout)
    session._start_worker("跑个长活")          # noqa: SLF001
    assert provider.entered.wait(timeout=5)
    session._dispatch(json.dumps({"op": "abort", "reason": "别跑了"}))  # noqa: SLF001
    provider.release.set()
    session._thread.join(timeout=10)          # noqa: SLF001
    result = [json.loads(line) for line in stdout.getvalue().splitlines()
              if json.loads(line)["type"] == "result"][0]
    assert result["stopped"] == "aborted", "abort 之后这一轮要以中断收尾，不是假装跑完"


def test_rpc_steer_with_nothing_running_is_refused_not_swallowed(tmp_path: Path):
    _code, lines, _runtime = build_rpc(tmp_path, [{"op": "steer", "text": "在吗"}, {"op": "shutdown"}])
    ack = [line for line in lines if line["type"] == "ack" and line["op"] == "steer"][0]
    assert ack["ok"] is False and "没人接" in ack["data"]["reason"]


def test_rpc_shutdown_finishes_the_open_operation(tmp_path: Path):
    provider = BlockingProvider()
    registry, skills, _executor = offline_registry(tmp_path)
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills)
    stdin = io.StringIO('{"op":"prompt","text":"长活"}\n{"op":"shutdown"}\n')
    stdout = io.StringIO()
    # 这一轮的模型永远不放行：收工时的等待一定超时，测的正是「等不到就按中断收尾」。
    # 注意 abort 是协作式的——它喊停，但拦不住已经发出去、正等回包的那次模型调用，
    # 所以这里能保证的是「操作日志里不留悬着的操作」，不是「线程立刻就停了」。
    session = RpcSession(runtime, stdin=stdin, stdout=stdout, drain_timeout=0.5)
    code = session.serve()
    assert code == 0
    assert runtime.ops.unfinished() == [], "shutdown 之后操作日志里不能留下没结束的操作"
    statuses = [str(op.status) for op in runtime.ops.operations()]
    assert statuses[-1] == "aborted"
    provider.release.set()
    if session._thread is not None:            # noqa: SLF001
        session._thread.join(timeout=5)        # noqa: SLF001
    result = [json.loads(line) for line in stdout.getvalue().splitlines()
              if json.loads(line)["type"] == "result"][0]
    assert result["stopped"] == "aborted", "被喊停的那一轮要以中断收尾"


# ---------------------------------------------------------------- 子 agent


def test_task_tool_spec_is_a_valid_tool_definition():
    spec = task_tool_spec(lambda _args: "ok")
    payload = spec.to_payload()
    assert payload["function"]["name"] == "task"
    assert payload["function"]["parameters"]["required"] == ["prompt"]
    assert spec.readonly is False, "派活会改东西，不该在计划模式下放行"


def test_subagent_gets_its_own_run_directory(tmp_path: Path):
    turns = [
        tool_turn(("task", {"prompt": "单独查一下预检规则"})),
        text_turn("父 agent 收到子 agent 的结论了"),
    ]
    runtime = make_runtime(tmp_path, turns)
    child_turns = [text_turn("子 agent 查完了：10 条规则")]
    runtime.subagents.factory = lambda _model: ScriptedProvider(list(child_turns))
    result = runtime.start("跑一遍")
    assert result.ok
    assert len(runtime.subagents.records) == 1
    record = runtime.subagents.records[0]
    child_dir = Path(runtime.run_dir) / CHILDREN_DIR / record.run_id
    assert child_dir.is_dir(), "子 run 要落在父 run 的 children/ 下"
    for name in ("session.jsonl", "events.jsonl", "manifest.json", "usage.jsonl"):
        assert (child_dir / name).is_file(), f"子 run 少了 {name}"
    assert "子 agent 查完了" in Path(child_dir / "session.jsonl").read_text(encoding="utf-8")


def test_parent_sees_the_subagent_answer_and_its_own_manifest_records_it(tmp_path: Path):
    provider = ScriptedProvider([tool_turn(("task", {"prompt": "查一下"})), text_turn("收到")])
    runtime = make_runtime(tmp_path, provider=provider)
    runtime.subagents.factory = lambda _model: ScriptedProvider([text_turn("结论：没问题")])
    runtime.start("跑一遍")
    sent = json.dumps(provider.requests[-1], ensure_ascii=False)
    assert "结论：没问题" in sent, "子 agent 的结论要回到父 agent 的上下文里"
    manifest = manifest_of(runtime)
    assert len(manifest["subagents"]) == 1
    entry = manifest["subagents"][0]
    assert entry["ok"] is True and entry["stopped"] == "end_turn"
    assert entry["run_dir"].startswith(str(runtime.run_dir))
    assert not any("subagent" in d for d in manifest["degradations"]), manifest["degradations"]


def test_subagent_does_not_get_a_task_tool_by_default(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "往下再派一层"})), text_turn("好")])
    runtime.subagents.factory = lambda _model: ScriptedProvider([text_turn("我是子 agent")])
    runtime.start("跑一遍")
    child_dir = Path(runtime.run_dir) / CHILDREN_DIR / runtime.subagents.records[0].run_id
    child_manifest = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "task" not in child_manifest["tools"], "默认只深一层：子 agent 的工具表里不该有 task"


def test_depth_budget_can_be_raised_for_a_deeper_tree(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "派一层"})), text_turn("好")],
                           subagent_max_depth=2)
    runtime.subagents.factory = lambda _model: ScriptedProvider([text_turn("子 agent 干完了")])
    runtime.start("跑一遍")
    child_dir = Path(runtime.run_dir) / CHILDREN_DIR / runtime.subagents.records[0].run_id
    child_manifest = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "task" in child_manifest["tools"], "还有一层余量时，子 agent 也该能派活"


def test_exhausted_depth_refuses_instead_of_looping(tmp_path: Path):
    runner = SubagentRunner(_FakeParent(tmp_path), max_depth=0)
    assert not runner.can_spawn
    with pytest.raises(ToolError) as excinfo:
        runner.run("再派一层")
    assert "不要再往下派" in str(excinfo.value)


def test_subagent_failure_is_a_tool_error_not_a_silent_success(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下"})), text_turn("知道了")])
    runtime.subagents.factory = lambda _model: ScriptedProvider([Turn(error="端点掉线了", model="x")])
    result = runtime.start("跑一遍")
    assert result.ok, "子 agent 挂了不该把父 run 也带崩"
    manifest = manifest_of(runtime)
    assert manifest["subagents"][0]["ok"] is False
    assert any("subagent" in d for d in manifest["degradations"])
    sent = json.dumps(runtime.provider.requests[-1], ensure_ascii=False)
    assert "没干完" in sent and "端点掉线了" in sent


def test_child_run_degradations_are_copied_into_the_parent_manifest(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下"})), text_turn("知道了")])
    runtime.subagents.factory = lambda _model: ScriptedProvider([
        tool_turn(("run_skill", {"name": "没有这个技能", "args": []})), text_turn("技能名写错了")])
    runtime.start("跑一遍")
    manifest = manifest_of(runtime)
    assert any("子 agent" in d and "failed" in d for d in manifest["degradations"]), manifest["degradations"]


def test_children_inherit_the_permission_policy(tmp_path: Path):
    """子 agent 不能借「我是个新 run」把父这边的拒绝名单甩掉。"""
    policy = PermissionPolicy(mode=PermissionMode.SAFE, deny=["run_skill"])
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "让子 agent 去改点东西"})), text_turn("好")],
                           permissions=policy)
    runtime.subagents.factory = lambda _model: ScriptedProvider([
        tool_turn(("run_skill", {"name": "sparkjury-clean", "args": []})), text_turn("改不动")])
    runtime.start("跑一遍")
    assert runtime.subagents.records, "task 本身没被拒，子 agent 应该跑起来了"
    assert policy.report()["denied"] >= 1
    child_dir = Path(runtime.run_dir) / CHILDREN_DIR / runtime.subagents.records[0].run_id
    child_manifest = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
    assert child_manifest["permissions"]["denied"] == 1
    assert child_manifest["permissions"]["mode"] == "safe", "父的模式要跟着下去"


def test_plan_mode_refuses_delegation_too(tmp_path: Path):
    """派活也是会改东西的动作：计划模式下不该靠「派给别人」绕开只读。"""
    policy = PermissionPolicy(mode=PermissionMode.PLAN)
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "让别人去改"})), text_turn("好")],
                           permissions=policy)
    runtime.subagents.factory = lambda _model: ScriptedProvider([text_turn("我不该被叫起来")])
    result = runtime.start("跑一遍")
    assert result.ok
    assert runtime.subagents.records == [], "计划模式下列任务不该被派出去"
    assert manifest_of(runtime)["permissions"]["denied"] == 1


def test_task_model_parameter_is_an_enum_of_real_endpoints():
    """节点上真跑出来的教训：一句「短名见 agent endpoints」，模型会自己编名字。"""
    spec = task_tool_spec(lambda _args: "ok")
    enum = spec.parameters["properties"]["model"]["enum"]
    assert "subject" in enum and "judge-a" in enum
    assert "embed" not in enum, "向量端点不能当大脑用，别列进去勾引模型"
    assert "claude" not in enum and "codex" not in enum


def test_unknown_endpoint_name_is_refused_before_a_child_run_starts(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下", "model": "claude"})),
                                      text_turn("好")])
    runtime.subagents.factory = lambda _model: ScriptedProvider([text_turn("我不该被叫起来")])
    result = runtime.start("跑一遍")
    assert result.ok and runtime.subagents.records == []
    manifest = manifest_of(runtime)
    assert not any("subagent child" in d for d in manifest["degradations"]), manifest["degradations"]
    sent = json.dumps(runtime.provider.requests[-1], ensure_ascii=False)
    assert "没有这个端点短名：claude" in sent and "judge-a" in sent


def test_full_model_id_is_still_allowed(tmp_path: Path):
    runner = SubagentRunner(_FakeParent(tmp_path), max_depth=1)
    runner._check_model("Qwen/Qwen3-8B")          # noqa: SLF001 - 完整模型 id 不是「编出来的短名」
    runner._check_model("http://127.0.0.1:8001/v1#Qwen/Qwen3-30B-A3B-Instruct-2507-FP8")  # noqa: SLF001
    with pytest.raises(ToolError):
        runner._check_model("gjorewgj")            # noqa: SLF001


def test_cloud_endpoint_without_a_key_is_refused_up_front(tmp_path: Path, monkeypatch):
    """云端端点没配 key 时说清楚，别让子 agent 拿着空 key 去撞 401。"""
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    runner = SubagentRunner(_FakeParent(tmp_path), max_depth=1)
    with pytest.raises(ToolError) as excinfo:
        runner._check_model("stepfun")            # noqa: SLF001
    assert "STEPFUN_API_KEY" in str(excinfo.value) and "空" in str(excinfo.value)

    monkeypatch.setenv("STEPFUN_API_KEY", "sk-假的但非空")
    runner._check_model("stepfun")                # noqa: SLF001 - 配了就放行，连不连得上是另一回事


def test_failed_child_is_reported_once_not_three_times(tmp_path: Path):
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下"})), text_turn("知道了")])
    runtime.subagents.factory = lambda _model: ScriptedProvider([Turn(error="端点掉线了", model="x")])
    runtime.start("跑一遍")
    degradations = manifest_of(runtime)["degradations"]
    hits = [d for d in degradations if "subagent child" in d]
    assert len(hits) == 1, degradations


def test_task_without_prompt_is_refused_before_any_work(tmp_path: Path):
    runtime = make_runtime(tmp_path, [text_turn("好")])
    output, ok = runtime.registry.call("task", {})
    assert not ok and "prompt" in output


def test_subagent_model_can_be_switched_per_call(tmp_path: Path):
    asked: list[str | None] = []

    def factory(name):
        asked.append(name)
        spec = ModelSpec(name=name or "same-as-parent", model=name or "same-as-parent",
                         base_url="scripted://offline")
        return ScriptedProvider([text_turn(f"我是 {name}")], spec=spec)

    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下", "model": "judge-a"})),
                                      text_turn("好")])
    runtime.subagents.factory = factory
    runtime.start("跑一遍")
    assert asked == ["judge-a"], "task 里指定的端点要交给 factory 去解析"
    assert runtime.subagents.records[0].model == "judge-a"


def test_factory_sees_none_when_the_subagent_should_use_the_parent_endpoint(tmp_path: Path):
    asked: list[str | None] = []

    def factory(name):
        asked.append(name)
        return ScriptedProvider([text_turn("一样的端点")])

    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下"})), text_turn("好")])
    runtime.subagents.factory = factory
    runtime.start("跑一遍")
    assert asked == [None], "没指定 model 时 factory 收到 None，由它决定怎么跟父保持一致"


def test_scripted_parent_cannot_silently_share_its_timeline_with_a_child(tmp_path: Path):
    """脚本替身只有一条时间线：父子共用会让子 agent 把父的下一轮吃掉。宁可报错也不悄悄串味。"""
    runtime = make_runtime(tmp_path, [tool_turn(("task", {"prompt": "查一下"})), text_turn("好")])
    result = runtime.start("跑一遍")
    assert result.ok, "派活失败不该把父 run 带崩"
    sent = json.dumps(runtime.provider.requests[-1], ensure_ascii=False)
    assert "subagent_factory" in sent, "要给模型一句能照着改的话"


class _FakeParent:
    """只给 SubagentRunner 用到的那点东西：一个 run 目录和一个工作目录。"""

    def __init__(self, tmp_path: Path) -> None:
        self.run_dir = tmp_path / "runs" / "agent-fake"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workdir = tmp_path
        self.skills: list = []
        self.provider = None
        self.permissions = None


def test_decision_dataclass_is_plain_data():
    decision = Decision(True, "只读工具", Verdict.READONLY)
    assert decision.kind == "readonly" and decision.allow


def test_session_tree_is_untouched_by_the_top_layer(tmp_path: Path):
    """上层的三个新东西都不该改会话树的写入口径：一次 run 之后仍然只有一份 JSONL。"""
    runtime = make_runtime(tmp_path, [text_turn("就一句话")], permissions=PermissionPolicy())
    runtime.start("跑")
    tree = SessionTree.load(runtime.run.session_path)
    assert tree.torn_lines == [] and len(tree) >= 3
    assert all(isinstance(entry.data, dict) for entry in tree.entries)
