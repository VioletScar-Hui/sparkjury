"""守卫 Agent harness 的中层（M13）：三个存储、操作状态机、中断恢复、历史压缩、钩子。

中层最要命的错都是「悄悄错」：日志写坏一半当成没写、崩溃后把工具又跑了一遍（副作用发生两次）、
压缩之后模型再也看不到早期结论、被 hook 拦下的调用当成执行成功。所以这些用例全部盯着
*落盘之后可观察的事实*——文件里剩什么、下次启动时会不会重跑、模型实际收到哪些消息。

全部离线：模型是脚本替身，技能执行器是离线替身，不联网不起子进程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sparkjury.agent.ai import ScriptedProvider, Turn, text_turn, tool_turn
from sparkjury.agent.compact import (
    CompactionPolicy,
    Compactor,
    deterministic_summary,
    plan_compaction,
)
from sparkjury.agent.hooks import Hooks
from sparkjury.agent.loop import STOPPED_ABORTED, STOPPED_END_TURN, AgentLoop
from sparkjury.agent.ops import OperationKind, OperationLog, OperationStatus
from sparkjury.agent.runtime import AgentRuntime
from sparkjury.agent.session import SessionTree
from sparkjury.agent.store import Ledger, RunStore, StoreError, ValuesStore, atomic_write_json, read_jsonl
from sparkjury.agent.tools import OfflineSkillExecutor, ToolRegistry, ToolSpec, load_skill_tools
from sparkjury.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner(env={"COLUMNS": "220"})


def offline_registry(tmp_path: Path, replies: dict[str, str] | None = None):
    executor = OfflineSkillExecutor(replies or {})
    registry, skills = load_skill_tools(ROOT / "skills", executor=executor, workdir=tmp_path)
    return registry, skills, executor


def counting_registry(tmp_path: Path):
    """带计数的注册表：谁被执行了几次一问便知（用来证明「没有重复副作用」）。"""
    seen: list[str] = []

    def make(name: str):
        def handler(_args):
            seen.append(name)
            return f"{name} 的结果"
        return ToolSpec(name=name, description=f"测试用 {name}", parameters={"type": "object", "properties": {}},
                        handler=handler)

    registry = ToolRegistry([make("alpha"), make("beta"), make("gamma")])
    skills = []
    return registry, skills, seen


# ---------------------------------------------------------------- 存储


def test_values_store_replaces_atomically_and_reloads(tmp_path: Path):
    path = tmp_path / "values.json"
    store = ValuesStore(path)
    store.set("phase", "score")
    store.append("todos", {"id": 1})
    store.append("todos", {"id": 2})
    store.update({"turns": 3})
    assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "score"
    assert not list(tmp_path.glob("*.tmp")), "原子替换不该留下临时文件"
    again = ValuesStore(path)
    assert again.get("todos") == [{"id": 1}, {"id": 2}] and again.get("turns") == 3
    snapshot = again.snapshot()
    snapshot["phase"] = "改坏了"
    assert again.get("phase") == "score", "snapshot 必须是拷贝，不能把内部状态漏出去"


def test_values_store_refuses_to_pretend_state_is_empty(tmp_path: Path):
    """读不出来就报错。静默返回空表，等于把「状态丢了」伪装成「本来就没有」。"""
    path = tmp_path / "values.json"
    path.write_text("{坏掉的 JSON", encoding="utf-8")
    with pytest.raises(StoreError):
        ValuesStore(path)
    with pytest.raises(StoreError):
        ValuesStore(path).append("x", 1)


def test_ledger_totals_and_tolerates_torn_tail(tmp_path: Path):
    ledger = Ledger(tmp_path / "usage.jsonl")
    ledger.append({"turn": 1, "prompt_tokens": 100, "completion_tokens": 10})
    ledger.append({"turn": 2, "prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55})
    with (tmp_path / "usage.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"turn": 3, "prompt_tok')      # 被 kill 时写了一半
    rows = ledger.rows()
    assert [r["turn"] for r in rows] == [1, 2]
    assert ledger.torn_lines == [3]
    assert ledger.total() == {"prompt_tokens": 150, "completion_tokens": 15, "total_tokens": 55}


def test_session_load_skips_torn_line_but_reports_it(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    tree = SessionTree(path)
    tree.append("message", role="user", data={"text": "第一句"})
    tree.append("message", role="assistant", data={"text": "第二句"})
    with path.open("a", encoding="utf-8") as f:
        f.write('{"id":"e0003","kind":"mess')
    reloaded = SessionTree.load(path)
    assert len(reloaded) == 2 and reloaded.torn_lines == [3]
    assert [m["content"] for m in reloaded.messages()] == ["第一句", "第二句"]


def test_transaction_commits_all_or_nothing(tmp_path: Path):
    store = RunStore(tmp_path / "run-1")
    with store.transaction("turn-1") as tx:
        tx.ledger({"turn": 1, "prompt_tokens": 10})
        tx.entries({"id": "e0001", "kind": "note", "data": {"text": "一"}})
        tx.value("turns", 1)
    assert store.ledger.rows() == [{"turn": 1, "prompt_tokens": 10}]
    assert store.values.get("turns") == 1
    assert read_jsonl(store.paths.entries)[0][0]["id"] == "e0001"

    with pytest.raises(RuntimeError):
        with store.transaction("turn-2") as tx:
            tx.ledger({"turn": 2})
            tx.value("turns", 2)
            raise RuntimeError("提交前炸了")
    assert len(store.ledger.rows()) == 1, "事务体里抛异常就一条都不该写"
    assert store.values.get("turns") == 1


def test_atomic_write_json_replaces_in_place(tmp_path: Path):
    path = tmp_path / "m.json"
    atomic_write_json(path, {"a": 1})
    atomic_write_json(path, {"a": 2, "b": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "b": [1, 2]}
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------- 操作状态机


def test_operation_log_folds_append_only_records(tmp_path: Path):
    log = OperationLog(tmp_path / "ops.jsonl")
    op = log.accept(OperationKind.RUN, prompt_chars=12)
    assert op.status == OperationStatus.PENDING and op.unfinished
    log.drive(op, OperationStatus.RUNNING)
    log.drive(op, OperationStatus.RUNNING, turns=1, leaf="e0005")
    log.finish(op, OperationStatus.COMPLETED, turns=2)
    folded = log.operations()
    assert len(folded) == 1 and folded[0].status == OperationStatus.COMPLETED
    assert folded[0].records == 4                      # 一行一次状态变化，全留着
    assert folded[0].detail["turns"] == 2 and folded[0].detail["prompt_chars"] == 12
    assert log.unfinished() == [] and log.current().id == op.id


def test_operation_log_tracks_unfinished_and_allocates_ids(tmp_path: Path):
    log = OperationLog(tmp_path / "ops.jsonl")
    first = log.accept(OperationKind.RUN)
    log.finish(first, OperationStatus.COMPLETED)
    second = log.accept(OperationKind.RUN)
    log.drive(second, OperationStatus.RUNNING)
    assert first.id == "op-0001" and second.id == "op-0002"
    assert [op.id for op in log.unfinished()] == [second.id]
    assert log.pending_resume().id == second.id
    assert "running" in second.summary() and "op-0002" in second.summary()


def test_operation_log_rejects_unknown_kinds_and_bad_finish(tmp_path: Path):
    log = OperationLog(tmp_path / "ops.jsonl")
    with pytest.raises(ValueError):
        log.accept("teleport")
    op = log.accept(OperationKind.RUN)
    with pytest.raises(ValueError):
        log.finish(op, OperationStatus.RUNNING)


def test_operation_log_tolerates_torn_tail(tmp_path: Path):
    log = OperationLog(tmp_path / "ops.jsonl")
    op = log.accept(OperationKind.RUN)
    log.finish(op, OperationStatus.COMPLETED)
    with (tmp_path / "ops.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"op": "op-0002", "stat')
    assert [o.status for o in log.operations()] == [OperationStatus.COMPLETED]
    assert log.torn_lines == [3]


# ---------------------------------------------------------------- 中断恢复：不重复副作用


class Boom(Exception):
    """用来模拟「进程在某一步突然没了」。"""


def run_until_crash(tmp_path: Path, turns, *, crash_after_tool: str):
    """跑到某个工具刚执行完就断电：那之后的一律没发生。"""
    registry, skills, seen = counting_registry(tmp_path)
    hooks = Hooks()
    hooks.after_tool.append(lambda call, *_a: (_ for _ in ()).throw(Boom())
                            if call.name == crash_after_tool else None)
    loop = AgentLoop(ScriptedProvider(list(turns)), registry, SessionTree(tmp_path / "session.jsonl"),
                     skills=skills, hooks=hooks)
    try:
        loop.run("开始")
    except Boom:
        pass
    return loop, registry, seen


def test_resume_replays_finished_tools_without_rerunning_them(tmp_path: Path):
    """第一条工具已经拿到结果，第二条还没有：恢复时只跑第二条。"""
    loop, _registry, seen = run_until_crash(
        tmp_path, [tool_turn(("alpha", {}), ("beta", {}))], crash_after_tool="alpha")
    assert seen == ["alpha"]                     # 断电时 beta 还没轮到
    loop.provider = ScriptedProvider([text_turn("都做完了")])
    result = loop.resume()
    assert result.stopped == STOPPED_END_TURN and result.resumed
    assert seen == ["alpha", "beta"], "alpha 的结果已经在日志里，不能再执行一次"
    assert result.replayed_calls == 1
    # 结果还是原来那一条，没有被重写；只多了一条「重放」标记
    results = [e for e in loop.session.entries if e.kind == "tool_result"]
    assert [e.data["name"] for e in results] == ["alpha", "beta"]
    marks = [e for e in loop.session.entries if e.data.get("phase") == "tool_replay"]
    assert [m.data["name"] for m in marks] == ["alpha"]


def test_resume_refuses_to_rerun_tools_that_may_have_half_run(tmp_path: Path):
    """有「开始执行」标记却没有结果 = 状态未知：不自动重跑，交给模型决定。"""
    session_path = tmp_path / "session.jsonl"
    tree = SessionTree(session_path)
    tree.append("message", role="system", data={"text": "你是评测 Agent"})
    tree.append("message", role="user", data={"text": "开始"})
    tree.append("tool_call", data={"text": "", "calls": [{"id": "call_1", "name": "alpha", "arguments": "{}"}]})
    tree.append("note", data={"phase": "tool_start", "call_id": "call_1", "name": "alpha"})
    registry, skills, seen = counting_registry(tmp_path)
    loop = AgentLoop(ScriptedProvider([text_turn("看完了")]), registry, SessionTree.load(session_path),
                     skills=skills)
    result = loop.resume()
    assert seen == [], "状态未知的调用不许自动重跑"
    assert result.stopped == STOPPED_END_TURN
    outputs = [e.data["output"] for e in loop.session.entries if e.kind == "tool_result"]
    assert outputs and "完成情况未知" in outputs[0]


def test_resume_continues_when_the_last_thing_was_a_tool_result(tmp_path: Path):
    session_path = tmp_path / "session.jsonl"
    tree = SessionTree(session_path)
    tree.append("message", role="system", data={"text": "你是评测 Agent"})
    tree.append("message", role="user", data={"text": "开始"})
    tree.append("tool_call", data={"text": "", "calls": [{"id": "call_9", "name": "alpha", "arguments": "{}"}]})
    tree.append("tool_result", data={"call_id": "call_9", "name": "alpha", "output": "结果", "ok": True})
    registry, skills, seen = counting_registry(tmp_path)
    provider = ScriptedProvider([text_turn("接着说完")])
    loop = AgentLoop(provider, registry, SessionTree.load(session_path), skills=skills)
    result = loop.resume()
    assert result.stopped == STOPPED_END_TURN and result.text == "接着说完"
    assert seen == [] and len(provider.requests) == 1


def test_resume_does_not_ask_the_model_again_when_the_answer_is_already_there(tmp_path: Path):
    session_path = tmp_path / "session.jsonl"
    tree = SessionTree(session_path)
    tree.append("message", role="system", data={"text": "你是评测 Agent"})
    tree.append("message", role="user", data={"text": "开始"})
    tree.append("message", role="assistant", data={"text": "我给出的结论"})
    registry, skills, _seen = counting_registry(tmp_path)
    provider = ScriptedProvider([text_turn("不该被问到")])
    loop = AgentLoop(provider, registry, SessionTree.load(session_path), skills=skills)
    result = loop.resume()
    assert result.text == "我给出的结论" and result.turns == 0
    assert provider.requests == [], "回答已经在记录里，不该再问一次模型"


def test_run_reports_its_own_tools_as_executed_not_replayed(tmp_path: Path):
    """调用 id 不保证跨轮唯一（有的端点每轮都从 call_1 开始编）：同一轮内不许被当成重放。"""
    registry, skills, seen = counting_registry(tmp_path)
    turns = [tool_turn(("alpha", {})), tool_turn(("beta", {})), text_turn("结束")]
    loop = AgentLoop(ScriptedProvider(turns), registry, SessionTree(tmp_path / "s.jsonl"), skills=skills)
    result = loop.run("开始")
    # tool_turn 生成的 id 每轮都是 call_1：如果按「边跑边看日志」判断，beta 会被误判成重放
    assert seen == ["alpha", "beta"]
    assert result.replayed_calls == 0 and result.stopped == STOPPED_END_TURN


# ---------------------------------------------------------------- 运行时：原语与恢复


def simple_runtime(tmp_path: Path, turns, **kwargs) -> AgentRuntime:
    registry, skills, _seen = counting_registry(tmp_path)
    provider = ScriptedProvider(list(turns))
    return AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                        skills=skills, **kwargs)


def test_runtime_accept_drive_inspect_and_manifest(tmp_path: Path):
    runtime = simple_runtime(tmp_path, [tool_turn(("alpha", {})), text_turn("好了")])
    result = runtime.start("跑一下")
    manifest = json.loads(runtime.run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["operations"][0]["status"] == "completed"
    info = runtime.inspect()
    assert info["operations"][0]["kind"] == "run" and info["unfinished"] == []
    assert info["needs_resume"] is False and info["pending_tools"] == []
    assert info["values"]["last_result"]["turns"] == result.turns
    assert isinstance(info["usage"], dict) and isinstance(info["torn_lines"], dict)
    assert runtime.values.get("current_operation") is None


def test_runtime_abort_marks_operation_aborted(tmp_path: Path):
    hooks = Hooks()
    runtime = simple_runtime(tmp_path, [tool_turn(("alpha", {}), ("beta", {})), text_turn("不该到这儿")],
                             hooks=hooks)
    hooks.after_tool.append(lambda *_a: runtime.request_abort("测试中途叫停"))
    result = runtime.start("跑一下")
    assert result.stopped == STOPPED_ABORTED
    assert runtime.inspect()["operations"][0]["status"] == "aborted"
    assert runtime.values.get("last_abort", {}).get("reason") == "测试中途叫停"


def test_runtime_resume_from_a_killed_run(tmp_path: Path):
    runs = tmp_path / "runs"
    registry, skills, seen = counting_registry(tmp_path)
    # 第一次：第一个工具执行完就断电（操作留在 running）
    loop_provider = ScriptedProvider([tool_turn(("alpha", {}), ("beta", {}))])
    first = AgentRuntime(loop_provider, runs_dir=runs, workdir=tmp_path, registry=registry, skills=skills)
    op = first.accept(OperationKind.RUN)
    first.drive(OperationStatus.RUNNING)
    first.run.loop = AgentLoop(loop_provider, registry, first.session, skills=skills, on_turn=first._on_turn)
    first.operation = op
    # 手工模拟「跑了一条工具之后进程消失」：写会话、不写操作终态
    first.session.append("message", role="system", data={"text": "你是评测 Agent"})
    first.session.append("message", role="user", data={"text": "开始"})
    first.session.append("tool_call", data={"text": "", "calls": [
        {"id": "call_1", "name": "alpha", "arguments": "{}"},
        {"id": "call_2", "name": "beta", "arguments": "{}"}]})
    first.session.append("note", data={"phase": "tool_start", "call_id": "call_1", "name": "alpha"})
    first.session.append("tool_result", data={"call_id": "call_1", "name": "alpha", "output": "alpha 的结果",
                                              "ok": True})
    seen.append("alpha")

    # 第二次：重新打开这个 run 目录，接着跑
    second = AgentRuntime.reopen(first.run_dir, ScriptedProvider([text_turn("接着说完")]), workdir=tmp_path,
                                 registry=registry, skills=skills)
    assert second.inspect()["needs_resume"] is True
    info = second.inspect()
    assert info["unfinished"] == [op.id] and info["pending_tools"] == ["beta"]
    result = second.resume()
    assert result.resumed and result.stopped == STOPPED_END_TURN
    assert seen == ["alpha", "beta"], "alpha 已经跑过，恢复时只补 beta"
    assert result.replayed_calls == 1
    after = second.inspect()
    assert after["unfinished"] == [] and after["needs_resume"] is False
    assert after["operations"][0]["status"] == "completed"
    # 事件流是接着写的，不是被清空重来
    kinds = [json.loads(line)["kind"] for line in second.run.events_path.read_text(encoding="utf-8").splitlines()]
    assert kinds.count("run_end") == 1


def test_runtime_resume_abandons_stale_operations(tmp_path: Path):
    runtime = simple_runtime(tmp_path, [text_turn("好")])
    stale = runtime.ops.accept(OperationKind.RUN)
    runtime.ops.drive(stale, OperationStatus.RUNNING)
    runtime.ops.accept(OperationKind.RUN)
    assert len(runtime.inspect()["unfinished"]) == 2
    runtime.resume()
    statuses = {op["id"]: op["status"] for op in runtime.inspect()["operations"]}
    assert statuses[stale.id] == "aborted"
    assert runtime.inspect()["unfinished"] == []


def test_runtime_reopen_is_read_only(tmp_path: Path):
    runtime = simple_runtime(tmp_path, [text_turn("好")])
    runtime.start("跑一下")
    events_before = runtime.run.events_path.read_text(encoding="utf-8")
    reopened = AgentRuntime.reopen(runtime.run_dir)
    assert reopened.inspect()["operations"][0]["status"] == "completed"
    assert reopened.run.events_path.read_text(encoding="utf-8") == events_before, "只读查看不该动事件流"
    with pytest.raises(RuntimeError):
        reopened.start("不该跑起来")
    assert reopened.recover()["unfinished_ops"] == []


# ---------------------------------------------------------------- 压缩


def test_plan_compaction_keeps_the_tail_and_gives_up_when_pointless(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    for i in range(12):
        tree.append("message", role="user" if i % 2 == 0 else "assistant", data={"text": f"第 {i} 条"})
    plan = plan_compaction(tree, keep_tail_entries=4)
    assert plan is not None and plan.replaced == 8
    assert tree.get(plan.to_id).data["text"] == "第 8 条"
    short = SessionTree(tmp_path / "short.jsonl")
    short.append("message", role="user", data={"text": "只有两条"})
    short.append("message", role="assistant", data={"text": "确实"})
    assert plan_compaction(short, keep_tail_entries=4) is None


def test_compaction_replaces_old_messages_in_the_request_but_keeps_entries(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    tree.append("message", role="system", data={"text": "系统提示"})
    for i in range(8):
        tree.append("message", role="user" if i % 2 == 0 else "assistant", data={"text": f"第 {i} 条"})
    before = len(tree)
    compactor = Compactor(policy=CompactionPolicy(keep_tail_entries=3), use_model=False)
    entry = compactor.compact(tree, reason="测试")
    assert entry is not None and entry.kind == "compaction"
    assert len(tree) == before + 1, "原始记录一条不删，只是多了一条摘要"
    messages = tree.messages()
    assert messages[0]["content"].startswith("以下是这次会话更早部分的摘要")
    assert "第 7 条" in str(messages[-1]["content"])
    assert not any(m.get("content") == "第 0 条" for m in messages), "被压掉的不该再出现在请求里"
    assert tree.transcript().count("第 0 条") == 1, "回放里照样看得到原文"
    tree.append("message", role="user", data={"text": "压缩之后的新消息"})
    assert "压缩之后的新消息" in str(tree.messages()[-1]["content"])


def test_model_summary_is_used_and_falls_back_when_the_model_is_dead(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    for i in range(10):
        tree.append("message", role="user" if i % 2 == 0 else "assistant", data={"text": f"第 {i} 条"})
    good = Compactor(policy=CompactionPolicy(keep_tail_entries=2),
                     provider=ScriptedProvider([text_turn("背景与目标\n- 给 Agent 做体检")]), use_model=True)
    entry = good.compact(tree, reason="测试")
    assert entry.data["source"] == "model" and "给 Agent 做体检" in entry.data["summary"]

    tree2 = SessionTree(tmp_path / "s2.jsonl")
    for i in range(10):
        tree2.append("message", role="user" if i % 2 == 0 else "assistant", data={"text": f"第 {i} 条"})
    bad = Compactor(policy=CompactionPolicy(keep_tail_entries=2),
                    provider=ScriptedProvider([Turn(error="APIConnectionError: 端点没了")]), use_model=True)
    entry2 = bad.compact(tree2, reason="测试")
    assert entry2.data["source"] == "deterministic"
    assert entry2.data["summary"].startswith("（这是确定性兜底摘要")


def test_compaction_policy_needs_a_real_budget_and_something_to_compress(tmp_path: Path):
    policy = CompactionPolicy(max_prompt_tokens=1000, keep_tail_entries=3)
    assert policy.enabled
    assert policy.should_compact(prompt_tokens=2000, branch_len=10) is True
    assert policy.should_compact(prompt_tokens=500, branch_len=10) is False
    assert policy.should_compact(prompt_tokens=2000, branch_len=4) is False, "快贴到尾部了，不值得压"
    assert CompactionPolicy().should_compact(prompt_tokens=999999, branch_len=99) is False, "默认不开"


def test_loop_compacts_when_the_budget_is_blown(tmp_path: Path):
    registry, skills, _seen = counting_registry(tmp_path)
    second = tool_turn(("beta", {}))            # 这一轮报出超预算的 prompt_tokens，并继续要工具
    second.usage = {"prompt_tokens": 9000, "completion_tokens": 10}
    turns = [tool_turn(("alpha", {})), second, text_turn("第三轮")]
    provider = ScriptedProvider(turns)
    compactor = Compactor(policy=CompactionPolicy(max_prompt_tokens=1000, keep_tail_entries=1),
                          provider=ScriptedProvider([text_turn("摘要：前面在跑工具")]), use_model=True)
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills,
                     compactor=compactor)
    result = loop.run("开始")
    assert result.compactions >= 1
    assert any(e.kind == "compaction" for e in loop.session.entries)
    assert loop.session.messages()[0]["content"].startswith("以下是这次会话更早部分的摘要")


def test_compaction_records_an_operation_in_the_journal(tmp_path: Path):
    log = OperationLog(tmp_path / "ops.jsonl")
    tree = SessionTree(tmp_path / "s.jsonl")
    for i in range(10):
        tree.append("message", role="user", data={"text": f"第 {i} 条"})
    compactor = Compactor(policy=CompactionPolicy(keep_tail_entries=2), ops=log, use_model=False)
    compactor.compact(tree, reason="测试")
    ops = log.operations()
    assert [str(op.kind) for op in ops] == ["compaction"] and ops[0].status == "completed"
    assert ops[0].detail["replaced"] >= 1


# ---------------------------------------------------------------- 钩子


def test_hook_can_veto_a_tool_call(tmp_path: Path):
    registry, skills, seen = counting_registry(tmp_path)
    hooks = Hooks()
    hooks.before_tool.append(lambda call: False if call.name == "alpha" else None)
    loop = AgentLoop(ScriptedProvider([tool_turn(("alpha", {}), ("beta", {})), text_turn("好了")]),
                     registry, SessionTree(tmp_path / "s.jsonl"), skills=skills, hooks=hooks)
    result = loop.run("开始")
    assert result.stopped == STOPPED_END_TURN
    assert seen == ["beta"], "被拦下的调用不该真的执行"
    outputs = {e.data["name"]: e.data for e in loop.session.entries if e.kind == "tool_result"}
    assert outputs["alpha"]["ok"] is False and "被 hook 拦下" in outputs["alpha"]["output"]


def test_hook_can_veto_with_a_reason_and_observe_results(tmp_path: Path):
    registry, skills, _seen = counting_registry(tmp_path)
    seen_after: list[tuple[str, bool]] = []
    hooks = Hooks()
    hooks.before_tool.append(lambda call: "今天的预算用完了" if call.name == "alpha" else None)
    hooks.after_tool.append(lambda call, output, ok: seen_after.append((call.name, ok)))
    loop = AgentLoop(ScriptedProvider([tool_turn(("alpha", {}), ("beta", {})), text_turn("好了")]),
                     registry, SessionTree(tmp_path / "s.jsonl"), skills=skills, hooks=hooks)
    loop.run("开始")
    text = [e.data["output"] for e in loop.session.entries if e.kind == "tool_result"
            and e.data["name"] == "alpha"][0]
    assert text == "今天的预算用完了"
    assert seen_after == [("beta", True)]


def test_hook_can_rewrite_the_request(tmp_path: Path):
    registry, skills, _seen = counting_registry(tmp_path)
    provider = ScriptedProvider([text_turn("好")])
    hooks = Hooks()
    hooks.before_request.append(lambda messages, tools: messages.insert(0, {"role": "system", "content": "别忘了交付"}))
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills, hooks=hooks)
    loop.run("开始")
    assert provider.requests[0][0]["content"] == "别忘了交付"


def test_hooks_default_to_doing_nothing(tmp_path: Path):
    hooks = Hooks()
    assert hooks.any is False
    assert hooks.tool_verdict(tool_turn(("x", {})).tool_calls[0]) == (True, "")
    hooks.run_before_request([], [])
    hooks.run_after_tool(tool_turn(("x", {})).tool_calls[0], "out", True)
    hooks.run_after_turn(text_turn("t"), 1)


# ---------------------------------------------------------------- 命令行


def test_cli_ops_reports_operations_and_no_resume_needed(tmp_path: Path):
    runs = tmp_path / "runs"
    assert runner.invoke(app, ["agent", "run", "--demo", "--runs-dir", str(runs),
                               "--workdir", str(tmp_path)]).exit_code == 0
    run_dir = next(runs.glob("agent-*"))
    result = runner.invoke(app, ["agent", "ops", str(run_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operations"][0]["kind"] == "run"
    assert payload["operations"][0]["status"] == "completed"
    assert payload["needs_resume"] is False
    text_out = runner.invoke(app, ["agent", "ops", str(run_dir)])
    assert "不需要恢复" in text_out.output


def test_cli_resume_says_nothing_to_do_for_a_completed_run(tmp_path: Path):
    runs = tmp_path / "runs"
    runner.invoke(app, ["agent", "run", "--demo", "--runs-dir", str(runs), "--workdir", str(tmp_path)])
    run_dir = next(runs.glob("agent-*"))
    result = runner.invoke(app, ["agent", "resume", str(run_dir)])
    assert result.exit_code == 0
    assert "不需要恢复" in result.output


def test_cli_compact_dry_run_then_apply(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    tree = SessionTree(session)
    tree.append("message", role="system", data={"text": "系统提示"})
    for i in range(12):
        tree.append("message", role="user" if i % 2 == 0 else "assistant", data={"text": f"第 {i} 条"})
    dry = runner.invoke(app, ["agent", "compact", str(session), "--keep-tail", "3", "--json"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["applied"] is False
    assert len(SessionTree.load(session)) == 13, "试算不该写文件"
    applied = runner.invoke(app, ["agent", "compact", str(session), "--keep-tail", "3", "--apply", "--json"])
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    assert payload["applied"] is True and payload["source"] == "deterministic"
    assert len(SessionTree.load(session)) == 14
    messages = SessionTree.load(session).messages()
    assert messages[0]["content"].startswith("以下是这次会话更早部分的摘要")


def test_cli_compact_says_when_there_is_nothing_to_do(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    tree = SessionTree(session)
    tree.append("message", role="user", data={"text": "就一句"})
    result = runner.invoke(app, ["agent", "compact", str(session)])
    assert result.exit_code == 0 and "没什么可压的" in result.output


def test_agent_run_accepts_a_compaction_budget(tmp_path: Path):
    runs = tmp_path / "runs"
    result = runner.invoke(app, ["agent", "run", "--demo", "--runs-dir", str(runs), "--workdir", str(tmp_path),
                                 "--compact-budget", "10"])
    assert result.exit_code == 0, result.output
    manifest = json.loads((next(runs.glob("agent-*")) / "manifest.json").read_text(encoding="utf-8"))
    # demo 是脚本模型，没有真 usage；预算开关不该把 run 弄坏
    assert manifest["stopped"] == STOPPED_END_TURN
    assert "compactions" in manifest


def test_deterministic_summary_is_honest_about_being_a_fallback(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    tree.append("message", role="system", data={"text": "系统"})
    tree.append("message", role="user", data={"text": "问题"})
    tree.append("tool_result", data={"call_id": "c1", "name": "alpha", "output": "结果", "ok": True})
    tree.append("message", role="assistant", data={"text": "结论"})
    summary = deterministic_summary(tree.entries)
    assert summary.startswith("（这是确定性兜底摘要")
    for section in ("背景与目标", "已经做了什么", "结论与决定", "未决的问题"):
        assert section in summary
    assert "结论" in summary
