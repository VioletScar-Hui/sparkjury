"""守卫 Agent harness（M13）：模型能不能真的读到技能、调起工具、留下可回放的记录。

这一层全在「模型说要干什么」和「工具真的干了什么」之间，最容易出的错是悄悄错位：
提示词里塞了技能全文、工具结果没回灌给模型、中断之后会话缺一段、manifest 把失败写成成功。
所以这些用例都盯着可观察的结果——发给模型的请求、会话里留下的记录、事件流里的条目、
manifest 里的降级项——而不是内部实现长什么样。

全部离线：模型是脚本替身，技能执行器是离线替身，不联网、不起子进程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sparkjury.agent.ai import (
    ModelSpec,
    ScriptedProvider,
    Turn,
    resolve_model,
    split_thinking,
    text_turn,
    tool_turn,
)
from sparkjury.agent.cli import DEMO_PROMPT, demo_provider
from sparkjury.agent.loop import (
    STOPPED_ABORTED,
    STOPPED_END_TURN,
    STOPPED_ERROR,
    STOPPED_MAX_TURNS,
    AgentLoop,
    default_system_prompt,
)
from sparkjury.agent.runtime import AgentRuntime, new_run_id
from sparkjury.agent.session import SessionTree
from sparkjury.agent.tools import (
    OfflineSkillExecutor,
    SkillInfo,
    SkillRun,
    ToolRegistry,
    ToolSpec,
    load_skill_tools,
    load_skills,
    parse_skill_md,
)
from sparkjury.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner(env={"COLUMNS": "220"})


def offline_registry(tmp_path: Path, replies: dict[str, str] | None = None):
    """一套装了离线执行器的注册表，技能目录用仓库里真的那份。"""
    executor = OfflineSkillExecutor(replies or {})
    registry, skills = load_skill_tools(ROOT / "skills", executor=executor, workdir=tmp_path)
    return registry, skills, executor


def run_scripted(turns, tmp_path: Path, *, max_turns: int = 12, prompt: str = "跑一遍"):
    registry, skills, executor = offline_registry(tmp_path)
    provider = ScriptedProvider(list(turns))
    session = SessionTree(tmp_path / "session.jsonl")
    loop = AgentLoop(provider, registry, session, max_turns=max_turns, skills=skills)
    result = loop.run(prompt)
    return loop, provider, registry, session, result


# ---------------------------------------------------------------- 技能与注册表


def test_six_skills_parse_with_name_and_description():
    skills = load_skills(ROOT / "skills")
    assert [s.name for s in skills] == [
        "sparkjury-clean", "sparkjury-cluster", "sparkjury-evalset",
        "sparkjury-regress", "sparkjury-report", "sparkjury-score",
    ]
    for skill in skills:
        assert len(skill.description) > 40, f"{skill.name} 的描述太短"
        assert skill.body.startswith("---"), f"{skill.name} 的正文应当是完整的 SKILL.md"
        assert skill.run_py.is_file(), f"{skill.name} 缺 scripts/run.py"


def test_frontmatter_parser_handles_multiline_and_quotes(tmp_path: Path):
    path = tmp_path / "demo" / "SKILL.md"
    path.parent.mkdir()
    path.write_text('---\nname: demo-skill\ndescription: >\n  第一行\n  第二行\nlicense: MIT\n---\n正文\n',
                    encoding="utf-8")
    skill = parse_skill_md(path)
    assert skill.name == "demo-skill"
    assert skill.description == "第一行 第二行"
    assert skill.body.endswith("正文\n")


def test_registry_payload_is_valid_tool_schema(tmp_path: Path):
    registry, _skills, _executor = offline_registry(tmp_path)
    assert registry.names() == ["load_skill", "run_skill", "list_dir", "read_file"]
    for payload in registry.payload():
        assert payload["type"] == "function"
        fn = payload["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
    skill_enum = registry.get("load_skill").parameters["properties"]["name"]["enum"]
    assert skill_enum == [s.name for s in load_skills(ROOT / "skills")]


def test_system_prompt_lists_skills_but_not_their_bodies(tmp_path: Path):
    """按需加载：提示词里只有描述，SKILL.md 正文要模型自己调 load_skill 去取。"""
    registry, skills, _executor = offline_registry(tmp_path)
    prompt = default_system_prompt(registry, skills, workdir=tmp_path)
    for skill in skills:
        assert skill.name in prompt
        assert skill.description in prompt
        assert "## Steps" not in prompt
    assert "load_skill" in prompt and "run_skill" in prompt


def test_load_skill_returns_body_and_unknown_name_is_readable(tmp_path: Path):
    registry, _skills, _executor = offline_registry(tmp_path)
    body, ok = registry.call("load_skill", {"name": "sparkjury-score"})
    assert ok and "## Agreement rule" in body
    missing, ok2 = registry.call("load_skill", {"name": "sparkjury-nope"})
    assert not ok2 and "没有这个技能" in missing and "sparkjury-score" in missing


def test_run_skill_passes_args_through_and_shows_command(tmp_path: Path):
    registry, _skills, executor = offline_registry(tmp_path, {"sparkjury-score": "一致率 76.9%"})
    out, ok = registry.call("run_skill", {"name": "sparkjury-score",
                                          "args": ["--db", "runs/x.db", "--judges", "mock"]})
    assert ok and "一致率 76.9%" in out
    assert executor.seen == [("sparkjury-score", ["--db", "runs/x.db", "--judges", "mock"])]
    assert "scripts/run.py --db runs/x.db --judges mock" in out  # 回给模型的文本里带命令行


def test_file_tools_stay_inside_workdir(tmp_path: Path):
    registry, _skills, _executor = offline_registry(tmp_path)
    (tmp_path / "notes.txt").write_text("内部笔记", encoding="utf-8")
    assert "notes.txt" in registry.call("list_dir", {})[0]
    assert registry.call("read_file", {"path": "notes.txt"})[0] == "内部笔记"
    out, ok = registry.call("read_file", {"path": "../outside.txt"})
    assert not ok and "越出工作目录" in out


def test_tool_errors_come_back_as_text_not_exceptions():
    def boom(_args):
        raise RuntimeError("端点掉了")

    registry = ToolRegistry([ToolSpec(name="boom", description="总是失败", parameters={"type": "object", "properties": {}},
                                      handler=boom)])
    out, ok = registry.call("boom", {})
    assert not ok and "RuntimeError: 端点掉了" in out
    missing, ok2 = registry.call("nope", {})
    assert not ok2 and "没有这个工具" in missing
    assert [c.ok for c in registry.calls] == [False, False]


def test_failing_subprocess_executor_is_reported_not_raised(tmp_path: Path):
    registry, skills, _executor = offline_registry(tmp_path)

    def exploding(_skill: SkillInfo, _args) -> SkillRun:
        raise FileNotFoundError("python 不在")

    registry2, _skills2 = load_skill_tools(ROOT / "skills", executor=exploding, workdir=tmp_path)
    out, ok = registry2.call("run_skill", {"name": skills[0].name, "args": []})
    assert not ok and "FileNotFoundError" in out
    assert registry.get("run_skill") is not None


# ---------------------------------------------------------------- 会话树


def test_session_messages_follow_openai_shape(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    tree.append("message", role="system", data={"text": "系统"})
    tree.append("message", role="user", data={"text": "问题"})
    tree.append("tool_call", data={"text": "我先看看", "calls": [
        {"id": "call_1", "name": "load_skill", "arguments": '{"name": "sparkjury-score"}'}]})
    tree.append("tool_result", data={"call_id": "call_1", "name": "load_skill", "output": "说明书", "ok": True})
    tree.append("message", role="assistant", data={"text": "结论"})
    messages = tree.messages()
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[2]["tool_calls"][0]["function"]["name"] == "load_skill"
    assert messages[2]["content"] == "我先看看"
    assert messages[3]["tool_call_id"] == "call_1"


def test_session_branch_keeps_original_entries(tmp_path: Path):
    tree = SessionTree(tmp_path / "s.jsonl")
    tree.append("message", role="user", data={"text": "第一问"})
    first = tree.append("message", role="assistant", data={"text": "第一答"})
    tree.append("message", role="user", data={"text": "第二问"})
    tree.branch(first.id, text="改走另一条")
    tree.append("message", role="assistant", data={"text": "另一条答"})
    branch_texts = [m["content"] for m in tree.messages()]
    assert branch_texts == ["第一问", "第一答", "另一条答"]  # 分支标记本身不是消息
    assert len(tree) == 5  # 原来那条分支的记录一条没删
    assert [e.data["text"] for e in tree.entries if e.kind == "message" and e.role == "assistant"] == ["第一答", "另一条答"]


def test_session_jsonl_roundtrip(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    tree = SessionTree(path)
    tree.append("message", role="user", data={"text": "一"})
    tree.append("message", role="assistant", data={"text": "二"})
    reloaded = SessionTree.load(path)
    assert [e.to_json() for e in reloaded.entries] == [e.to_json() for e in tree.entries]
    assert reloaded.leaf_id == tree.leaf_id
    assert reloaded.messages() == tree.messages()
    with pytest.raises(ValueError):
        tree.append("teleport")


# ---------------------------------------------------------------- loop


def test_loop_runs_tools_then_ends_with_text(tmp_path: Path):
    turns = [
        tool_turn(("load_skill", {"name": "sparkjury-clean"}), text="先读说明书"),
        tool_turn(("run_skill", {"name": "sparkjury-clean", "args": ["--db", "runs/x.db"]})),
        text_turn("清洗完了，14 条里 1 条是环境问题。"),
    ]
    loop, provider, registry, session, result = run_scripted(turns, tmp_path)
    assert result.stopped == STOPPED_END_TURN and result.ok
    assert (result.turns, result.tool_calls) == (3, 2)
    assert result.text.startswith("清洗完了")
    assert [c.name for c in registry.calls] == ["load_skill", "run_skill"]
    assert session.entries[-1].role == "assistant"
    # 工具结果确实回灌给了模型：第三次请求里应当能看到工具返回的文本
    assert [m["role"] for m in provider.requests[2]] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert provider.requests[2][-1]["content"].startswith("$ python")


def test_loop_stops_on_provider_error_and_records_it(tmp_path: Path):
    turns = [tool_turn(("list_dir", {})), Turn(error="APIConnectionError: 端点没了")]
    loop, _provider, _registry, session, result = run_scripted(turns, tmp_path)
    assert result.stopped == STOPPED_ERROR and not result.ok
    assert result.error == "APIConnectionError: 端点没了"
    notes = [e for e in session.entries if e.kind == "note"]
    assert notes and "模型调用失败" in notes[-1].data["text"]


def test_loop_honours_max_turns(tmp_path: Path):
    endless = [tool_turn(("list_dir", {})) for _ in range(5)]
    _loop, provider, _registry, session, result = run_scripted(endless, tmp_path, max_turns=3)
    assert result.stopped == STOPPED_MAX_TURNS and result.turns == 3
    assert len(provider.requests) == 3
    assert "轮数上限" in session.entries[-1].data["text"]


def test_steering_lands_before_the_next_request(tmp_path: Path):
    """工具跑着的时候插一句话，下一次请求里必须看得到——pi 的 steering 语义。"""
    registry, skills, _executor = offline_registry(tmp_path)

    def steer_then_reply(_args):
        loop.steer("先别打分，数据源换成 node-real-samples")
        return "列完了"

    registry.register(ToolSpec(name="steer_me", description="插一句话", parameters={"type": "object", "properties": {}},
                               handler=steer_then_reply))
    provider = ScriptedProvider([tool_turn(("steer_me", {})), text_turn("收到，换数据源。")])
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills)
    result = loop.run("开始")
    assert result.stopped == STOPPED_END_TURN
    second_request = provider.requests[1]
    assert any(m["role"] == "user" and "node-real-samples" in str(m["content"]) for m in second_request)


def test_followup_starts_another_round(tmp_path: Path):
    registry, skills, _executor = offline_registry(tmp_path)

    def queue_followup(_args):
        loop.followup("再把结论写进证据卡片")
        return "列完了"

    registry.register(ToolSpec(name="queue_me", description="排一个 follow-up",
                               parameters={"type": "object", "properties": {}}, handler=queue_followup))
    provider = ScriptedProvider([tool_turn(("queue_me", {})), text_turn("第一轮说完了"),
                                 text_turn("卡片写好了")])
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills)
    result = loop.run("开始")
    assert result.stopped == STOPPED_END_TURN and result.turns == 3
    assert result.text == "卡片写好了"
    assert any(m["role"] == "user" and "证据卡片" in str(m["content"]) for m in provider.requests[2])


def test_abort_stops_the_run_without_losing_records(tmp_path: Path):
    registry, skills, _executor = offline_registry(tmp_path)

    def abort_here(_args):
        loop.abort()
        return "第一件干完了"

    registry.register(ToolSpec(name="abort_me", description="中断", parameters={"type": "object", "properties": {}},
                               handler=abort_here))
    provider = ScriptedProvider([tool_turn(("abort_me", {}), ("list_dir", {})), text_turn("不该走到这里")])
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills)
    result = loop.run("开始")
    assert result.stopped == STOPPED_ABORTED and not result.ok
    assert len(provider.requests) == 1  # 中断后没有再问模型
    assert loop.aborted is True


def test_loop_survives_keyboard_interrupt(tmp_path: Path):
    """Ctrl-C 落在模型调用里：按中断收尾，不当成崩溃。"""
    registry, skills, _executor = offline_registry(tmp_path)

    def interrupt(_messages, _tools=None):
        raise KeyboardInterrupt

    provider = ScriptedProvider([interrupt])
    loop = AgentLoop(provider, registry, SessionTree(tmp_path / "s.jsonl"), skills=skills)
    result = loop.run("开始")
    assert result.stopped == STOPPED_ABORTED


def test_loop_keeps_thinking_out_of_the_transcript(tmp_path: Path):
    """Qwen3 系模型的 <think> 段落：会话里留着，正文和结论里不能有。"""
    turns = [Turn(text="先清洗", thinking="我该先读说明书", tool_calls=[
        tool_turn(("list_dir", {})).tool_calls[0]]), text_turn("结论", thinking="复述一遍要求")]
    _loop, _provider, _registry, session, result = run_scripted(turns, tmp_path)
    assert result.text == "结论"
    assert all("<think>" not in str(e.data.get("text", "")) for e in session.entries)
    assert session.entries[2].data["thinking"] == "我该先读说明书"
    assert session.entries[-1].data["thinking"] == "复述一遍要求"
    assert all("<think>" not in str(m.get("content")) for m in session.messages())


def test_split_thinking_handles_closed_open_and_absent_blocks():
    assert split_thinking("<think>想一下</think>答案") == ("答案", "想一下")
    assert split_thinking("<thinking>想</thinking> 答案 ") == ("答案", "想")
    assert split_thinking("答案") == ("答案", "")
    assert split_thinking("") == ("", "")
    clean, thinking = split_thinking("前 <think>半截")
    assert clean == "前" and thinking == "半截"  # 模型被截断，半截思考不能当正文
    clean2, thinking2 = split_thinking("<think>一</think>正文<think>二</think>")
    assert (clean2, thinking2) == ("正文", "一\n二")


# ---------------------------------------------------------------- runtime 与 CLI


def test_runtime_writes_manifest_events_and_usage(tmp_path: Path):
    registry, skills, _executor = offline_registry(tmp_path)
    provider = ScriptedProvider([tool_turn(("list_dir", {})), text_turn("看完了")])
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry,
                           skills=skills)
    result = runtime.start("看看工作区")
    manifest = json.loads(runtime.run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["kind"] == "agent" and manifest["status"] == "ok"
    assert manifest["stopped"] == STOPPED_END_TURN and manifest["turns"] == 2
    # task 是 runtime 自己挂上去的（子 agent 那一层），注册表里本来那四个还在原位
    assert manifest["tools"] == ["load_skill", "run_skill", "list_dir", "read_file", "task"]
    assert len(manifest["skills"]) == 6
    # system + user + tool_call + 「开始执行」标记 + tool_result + 最终回答
    assert manifest["session"]["entries"] == 6
    assert manifest["degradations"] == []
    assert runtime.run.events_path.is_file() and runtime.run.session_path.is_file()
    assert runtime.run.ledger_path.is_file()
    rows = [json.loads(line) for line in runtime.run.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["turn"] for r in rows] == [1, 2]
    kinds = [json.loads(line)["kind"] for line in runtime.run.events_path.read_text(encoding="utf-8").splitlines()]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    # 中层：操作日志与 values 也落盘了，工具执行前留了标记（中断恢复靠它判断副作用有没有发生）
    assert [op["kind"] for op in manifest["operations"]] == ["run"]
    assert manifest["operations"][0]["status"] == "completed"
    assert (runtime.run_dir / "ops.jsonl").is_file()
    assert (runtime.run_dir / "values.json").is_file()
    phases = [e.data.get("phase") for e in runtime.session.entries]
    assert "tool_start" in phases
    assert result.session_path == str(runtime.run.session_path)


def test_runtime_marks_failed_tools_as_degraded(tmp_path: Path):
    registry = ToolRegistry([ToolSpec(name="boom", description="总是失败", parameters={"type": "object", "properties": {}},
                                      handler=lambda _args: (_ for _ in ()).throw(ValueError("坏掉了")))])
    provider = ScriptedProvider([tool_turn(("boom", {})), text_turn("它坏了")])
    runtime = AgentRuntime(provider, runs_dir=tmp_path / "runs", workdir=tmp_path, registry=registry)
    runtime.start("试试")
    manifest = json.loads(runtime.run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["degradations"] == ["tool boom failed"]


def test_run_id_is_unique_and_prefixed():
    first, second = new_run_id(), new_run_id()
    assert first.startswith("agent-") and first != second


def test_resolve_model_short_names_and_urls():
    assert resolve_model("subject").base_url == "http://127.0.0.1:8004/v1"
    assert resolve_model("judge-a").model.startswith("Qwen/Qwen3-30B")
    custom = resolve_model("http://127.0.0.1:9999/v1#my-model")
    assert (custom.base_url, custom.model) == ("http://127.0.0.1:9999/v1", "my-model")
    bare = resolve_model("Qwen/Qwen3-8B")
    assert bare.name == "Qwen/Qwen3-8B" and bare.base_url.endswith(":8004/v1")
    assert isinstance(ModelSpec("x", "y", "z").api_key(), str)


def test_cli_agent_tools_and_endpoints_are_listable():
    tools = runner.invoke(app, ["agent", "tools", "--json"])
    assert tools.exit_code == 0, tools.output
    payload = json.loads(tools.stdout)
    assert {t["function"]["name"] for t in payload["tools"]} == {"load_skill", "run_skill", "list_dir",
                                                                "read_file", "task"}
    assert len(payload["skills"]) == 6
    endpoints = runner.invoke(app, ["agent", "endpoints", "--json"])
    assert endpoints.exit_code == 0
    assert {row["name"] for row in json.loads(endpoints.stdout)} >= {"judge-a", "judge-b", "subject", "embed"}


def test_cli_demo_run_then_replay(tmp_path: Path):
    """整条闭环走一遍：模型自己读说明书、自己调技能、留下可回放的会话。"""
    runs = tmp_path / "runs"
    result = runner.invoke(app, ["agent", "run", "--demo", "--runs-dir", str(runs), "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    run_dirs = list(runs.glob("agent-*"))
    assert len(run_dirs) == 1
    session_path = run_dirs[0] / "session.jsonl"
    entries = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
    calls = [c for e in entries if e["kind"] == "tool_call" for c in e["data"]["calls"]]
    assert [c["name"] for c in calls] == ["load_skill", "run_skill", "run_skill", "run_skill"]
    assert [json.loads(c["arguments"])["name"] for c in calls[1:]] == [
        "sparkjury-clean", "sparkjury-score", "sparkjury-report"]
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stopped"] == STOPPED_END_TURN and manifest["degradations"] == []
    replay = runner.invoke(app, ["agent", "replay", str(session_path), "--limit", "5"])
    assert replay.exit_code == 0 and "e00" in replay.output


def test_cli_requires_prompt_without_demo():
    result = runner.invoke(app, ["agent", "run"])
    assert result.exit_code == 2
    assert "--demo" in result.output


def test_demo_provider_script_is_offline_and_complete():
    provider = demo_provider()
    assert isinstance(provider, ScriptedProvider)
    turns = [provider.complete([], None) for _ in range(5)]
    names = [c.name for t in turns for c in t.tool_calls]
    assert names == ["load_skill", "run_skill", "run_skill", "run_skill"]
    assert turns[-1].text and not turns[-1].tool_calls
    assert all(t.usage == {} for t in turns)  # 离线替身不编造 token 数
    assert DEMO_PROMPT
