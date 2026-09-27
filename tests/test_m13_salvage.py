"""M13 从正文里捞工具调用：服务端没配对应的 tool-call parser 时的兜底。

起因是节点上实测：同一个 harness，`subject`（Qwen3-8B）和 `judge-a`（Qwen3-30B）都能正常
调 `load_skill`，换成 `judge-b`（Nemotron）就哑火——它把调用写成了
`<function=load_skill><parameter=name>…</parameter></function>` 这种 XML 正文，而端点配的是
`--tool-call-parser hermes`，解析不了就整段当 content 返回。harness 这边看到的是
「模型自言自语说要去调工具，然后就没有然后了」。这一组用例守的是兜底逻辑本身：
捞得到的要真的捞到，捞不到的不许硬捞。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sparkjury.agent.ai import ModelSpec, OpenAICompatProvider, Turn, salvage_tool_calls, tool_turn, text_turn
from sparkjury.agent.runtime import AgentRuntime
from sparkjury.agent.tools import OfflineSkillExecutor

# 节点上 Nemotron 真实的写法（原样抄回来的，连换行都在）
NEMOTRON_TEXT = """好的，我先读说明书。

<tool_call>
<function=load_skill>
<parameter=name>
sparkjury-score
</parameter>
</function>
</tool_call>
"""

HERMES_TEXT = ('<tool_call>{"name": "run_skill", "arguments": '
               '{"name": "sparkjury-cluster", "args": ["--db", "runs/x.db"]}}</tool_call>')


# ------------------------------------------------------------ 捞得到

def test_xml_shape_is_salvaged_and_removed_from_text():
    calls, text = salvage_tool_calls(NEMOTRON_TEXT, {"load_skill", "run_skill"})
    assert [(c.name, c.arguments) for c in calls] == [("load_skill", {"name": "sparkjury-score"})]
    assert text == "好的，我先读说明书。"          # 捞走之后正文干净，不会在会话里出现两遍
    assert calls[0].raw_arguments == '{"name": "sparkjury-score"}'


def test_hermes_json_shape_is_salvaged():
    calls, text = salvage_tool_calls(HERMES_TEXT, {"run_skill"})
    assert len(calls) == 1
    assert calls[0].name == "run_skill"
    assert calls[0].arguments == {"name": "sparkjury-cluster", "args": ["--db", "runs/x.db"]}
    assert calls[0].arguments["args"] == ["--db", "runs/x.db"]   # 列表原样是列表，不是字符串
    assert text == ""


def test_multiple_calls_in_one_block():
    text = ('<tool_call>\n<function=load_skill><parameter=name>a</parameter></function>\n'
            '<function=load_skill><parameter=name>b</parameter></function>\n</tool_call>')
    calls, _ = salvage_tool_calls(text, {"load_skill"})
    assert [c.arguments["name"] for c in calls] == ["a", "b"]
    assert len({c.id for c in calls}) == 2       # id 不重样，重放对账时才不会串


def test_bare_function_without_the_tool_call_wrapper():
    calls, text = salvage_tool_calls("<function=list_dir></function>", {"list_dir"})
    assert [c.name for c in calls] == ["list_dir"]
    assert calls[0].arguments == {}
    assert text == ""


def test_json_arguments_string_is_parsed():
    text = '<tool_call>{"name": "run_skill", "arguments": "{\\"name\\": \\"x\\"}"}</tool_call>'
    calls, _ = salvage_tool_calls(text, {"run_skill"})
    assert calls[0].arguments == {"name": "x"}


# ------------------------------------------------------------ 不许硬捞

def test_tool_name_outside_the_declared_list_is_left_alone():
    """文档里写的 `<function=…>` 示例不该被当成真的调用——名字不在本次工具表里就不捞。"""
    calls, text = salvage_tool_calls(NEMOTRON_TEXT, {"run_skill"})
    assert calls == []
    assert text == NEMOTRON_TEXT                  # 原样不动，正文里看到的就是模型真的说了什么


def test_plain_prose_is_untouched():
    prose = "我会先看 sparkjury-score 的说明书，然后跑一遍。"
    calls, text = salvage_tool_calls(prose, {"load_skill"})
    assert calls == [] and text == prose


def test_half_written_tags_are_not_salvaged():
    """模型被截断时会留下半截标签，硬捞只会捞出一个残废的调用。"""
    calls, text = salvage_tool_calls("<tool_call><function=load_skill>\n<parameter=name", {"load_skill"})
    assert calls == [] and text.startswith("<tool_call>")


def test_empty_text_is_a_no_op():
    assert salvage_tool_calls("", {"load_skill"}) == ([], "")


# ------------------------------------------------------------ 接到 provider 上

class _FakeMessage:
    def __init__(self, content: str, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResp:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(_FakeMessage(content))]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})()


class _FakeCompletions:
    def __init__(self, content: str, *, sink: dict):
        self._content, self._sink = content, sink

    def create(self, **kwargs):
        self._sink.update(kwargs)
        return _FakeResp(self._content)


def _provider_returning(content: str, sink: dict) -> OpenAICompatProvider:
    spec = ModelSpec(name="fake", model="fake-model", base_url="http://127.0.0.1:9/v1")
    provider = OpenAICompatProvider(spec)
    provider._client = type("C", (), {"chat": type("Ch", (), {
        "completions": _FakeCompletions(content, sink=sink)})()})()   # noqa: SLF001
    return provider


TOOLS_PAYLOAD = [{"type": "function", "function": {"name": "load_skill", "description": "x",
                                                   "parameters": {"type": "object"}}}]


def test_provider_salvages_when_the_endpoint_returns_plain_text():
    sink: dict = {}
    provider = _provider_returning(NEMOTRON_TEXT, sink)
    turn = provider.complete([{"role": "user", "content": "读一下 sparkjury-score"}], TOOLS_PAYLOAD)
    assert [c.name for c in turn.tool_calls] == ["load_skill"]
    assert turn.salvaged == 1
    assert turn.text == "好的，我先读说明书。"
    assert sink["tool_choice"] == "auto"          # 还是照常把工具表带上，没有另走一条路


def test_provider_leaves_unknown_tool_names_to_the_text():
    sink: dict = {}
    provider = _provider_returning("<tool_call><function=rm_rf><parameter=path>/</parameter></function></tool_call>", sink)
    turn = provider.complete([{"role": "user", "content": "x"}], TOOLS_PAYLOAD)
    assert turn.tool_calls == [] and turn.salvaged == 0
    assert "rm_rf" in turn.text                   # 没声明过的工具，原样留在正文里给人看


def test_provider_does_not_touch_a_normal_answer():
    sink: dict = {}
    provider = _provider_returning("我读完了，这个技能是给轨迹打分的。", sink)
    turn = provider.complete([{"role": "user", "content": "x"}], TOOLS_PAYLOAD)
    assert turn.tool_calls == [] and turn.salvaged == 0 and turn.text.startswith("我读完")


# ------------------------------------------------------------ 接到 loop 与 manifest 上

class _SalvagingProvider:
    """第一条回合把调用写成正文，第二条回合正常收尾——模拟节点上的 Nemotron。"""

    spec = ModelSpec(name="nemotron-ish", model="nemotron-ish", base_url="scripted://offline")

    def __init__(self):
        self.turns = [
            Turn(text="我去读说明书。<tool_call><function=load_skill>"
                      "<parameter=name>sparkjury-clean</parameter></function></tool_call>",
                 tool_calls=[], salvaged=0),
            text_turn("看完了，这个技能是清洗并导入轨迹的。"),
        ]

    def complete(self, messages, tools=None):
        from sparkjury.agent.ai import salvage_tool_calls as salvage
        turn = self.turns.pop(0)
        if turn.tool_calls or not turn.text:
            return turn
        calls, text = salvage(turn.text, {t["function"]["name"] for t in (tools or [])})
        if calls:
            turn = Turn(text=text, tool_calls=calls, salvaged=len(calls))
        return turn


def test_loop_records_the_salvage_and_the_manifest_says_so(tmp_path: Path):
    runtime = AgentRuntime(_SalvagingProvider(), runs_dir=tmp_path / "runs", workdir=tmp_path,
                           executor=OfflineSkillExecutor(), max_turns=4)
    result = runtime.start("读一下 sparkjury-clean 是干什么的")
    assert result.stopped == "end_turn"
    assert result.salvaged_calls == 1
    assert result.tool_calls == 1

    notes = [e.data for e in runtime.session.entries if e.kind == "note"
             and e.data.get("phase") == "tool_salvage"]
    assert notes and notes[0]["count"] == 1 and "捞回来" in notes[0]["text"]

    kinds = {e.kind for e in runtime.session.entries}
    assert {"tool_call", "tool_result"} <= kinds          # 捞回来的调用照样真的跑了

    manifest = json.loads((runtime.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["salvaged_tool_calls"] == 1
    assert any("recovered from assistant text" in d for d in manifest["degradations"])


def test_a_clean_run_records_no_salvage(tmp_path: Path):
    """正常走服务端解析的 run 不该多出这一栏——否则每份 manifest 都会挂一条假的。"""
    class _Clean:
        spec = ModelSpec(name="clean", model="clean", base_url="scripted://offline")

        def __init__(self):
            self.turns = [tool_turn(("load_skill", {"name": "sparkjury-clean"})),
                          text_turn("看完了。")]

        def complete(self, messages, tools=None):
            return self.turns.pop(0)

    runtime = AgentRuntime(_Clean(), runs_dir=tmp_path / "runs", workdir=tmp_path,
                           executor=OfflineSkillExecutor(), max_turns=4)
    result = runtime.start("读一下 sparkjury-clean")
    assert result.salvaged_calls == 0
    manifest = json.loads((runtime.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["salvaged_tool_calls"] == 0
    assert manifest["degradations"] == []


def test_scripted_turns_default_to_no_salvage():
    """离线替身不受影响：默认值就是 0，老测试不用改。"""
    assert tool_turn(("load_skill", {"name": "x"})).salvaged == 0
    assert text_turn("普通回答").salvaged == 0
