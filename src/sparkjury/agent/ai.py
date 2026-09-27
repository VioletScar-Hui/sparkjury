"""统一模型入口：一次调用拿到文本和工具调用，上层不用关心底下是哪家。

对应 pi 的 `pi-ai` 那一层。项目里已经有一个模型客户端 `judges/client.py`，但它只会
「问一句、答一句」：无工具、无多轮、无 usage。带工具的多轮对话是另一件事，所以单开
这一层，而不是把裁判客户端改造成四不像。

节点上四个 vLLM 端点固定，用短名字就能选中；云端或自建端点写
`--model http://host:port/v1#模型名`。`ScriptedProvider` 是离线替身：给一串写好的
回合按顺序返回，测试和 `--demo` 都靠它跑，不碰网络。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Collection

DEFAULT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class ModelSpec:
    """一个 OpenAI 兼容端点。密钥只从环境变量取，配置里绝不写明文。"""

    name: str
    model: str
    base_url: str
    api_key_env: str | None = None
    note: str = ""

    def api_key(self) -> str:
        if not self.api_key_env:
            return os.environ.get("OPENAI_API_KEY") or "EMPTY"
        return os.environ.get(self.api_key_env) or "EMPTY"


# 节点上常驻的四个端点（deploy/dgx/start_judges.sh 起的），短名字见左边。
NODE_ENDPOINTS: dict[str, ModelSpec] = {
    "judge-a": ModelSpec("judge-a", "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8", "http://127.0.0.1:8001/v1",
                         note="裁判 A；τ²-bench 的模拟用户现在也是它"),
    "judge-b": ModelSpec("judge-b", "nvidia/Nemotron-3.5-Lightning-30B-A3B-NVFP4", "http://127.0.0.1:8002/v1",
                         note="裁判 B，另一个家族"),
    "embed": ModelSpec("embed", "Qwen/Qwen3-Embedding-0.6B", "http://127.0.0.1:8003/v1",
                       note="聚类用的向量模型，只会算向量，不能当 agent 大脑"),
    "subject": ModelSpec("subject", "Qwen/Qwen3-8B", "http://127.0.0.1:8004/v1",
                         note="被评的那个小模型，默认让它当 agent 大脑"),
    "stepfun": ModelSpec("stepfun", "step-3.7-flash", "https://api.stepfun.com/v1",
                         api_key_env="STEPFUN_API_KEY", note="云端第三裁判家族"),
    "typesafe": ModelSpec("typesafe", "jev", "https://api.typesafe.cn/v1",
                          api_key_env="TYPESAFE_API_KEY", note="云端仲裁模型 Jev"),
}

DEFAULT_MODEL = "subject"

#: 能当 agent 大脑的短名。`embed` 不算：它只会算向量，拿它跑对话只会得到一堆错误。
CHAT_ENDPOINTS: tuple[str, ...] = tuple(name for name in NODE_ENDPOINTS if name != "embed")


def resolve_model(target: str | None = None) -> ModelSpec:
    """把 `judge-a` / `http://host:8000/v1#model-id` / 裸模型名统一成一个 ModelSpec。

    裸模型名走默认端点，`SPARKJURY_AGENT_BASE_URL` 可以整体把这个默认端点顶掉
    （本地开发时指向自己的小服务）。
    """
    target = target or os.environ.get("SPARKJURY_AGENT_MODEL") or DEFAULT_MODEL
    override = os.environ.get("SPARKJURY_AGENT_BASE_URL")
    if target in NODE_ENDPOINTS:
        spec = NODE_ENDPOINTS[target]
        return replace(spec, base_url=override) if override else spec
    if target.startswith(("http://", "https://")):
        url, _, model = target.partition("#")
        return ModelSpec(name=url, model=model or _model_from_url(url), base_url=url)
    base = override or NODE_ENDPOINTS[DEFAULT_MODEL].base_url
    return ModelSpec(name=target, model=target, base_url=base)


def _model_from_url(url: str) -> str:
    parts = [p for p in url.rstrip("/").split("/") if p]
    return parts[-2] if len(parts) >= 2 and parts[-1] == "v1" else (parts[-1] if parts else url)


@dataclass
class ToolCall:
    """模型要求执行的一次工具调用。arguments 已经解析成 dict，原始串保留下来备查。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_message_part(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False)}}


@dataclass
class Turn:
    """一次 assistant 回合：文本 + 想调的工具 + 用量。error 非空表示这次调用没成。

    `thinking` 单独存：Qwen3 系模型在节点上（vLLM 没开 reasoning parser）会把思考内容
    混在 content 里，直接当正文用的话，会话、卡片和结论里全是重复的思考过程。
    """

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    latency_ms: float = 0.0
    error: str | None = None
    salvaged: int = 0              # 这几个调用是从正文里捞回来的，不是服务端解析出来的


    @property
    def ok(self) -> bool:
        return self.error is None


class Provider:
    """所有模型入口的共同形状：给消息和工具表，回一个 Turn。"""

    spec: ModelSpec

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Turn:
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    """vLLM、StepFun、OpenAI 都吃同一套 chat.completions，所以一个实现就够。"""

    def __init__(self, spec: ModelSpec, *, temperature: float = 0.0, max_tokens: int = 1024,
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_retries: int = 1):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - openai 是硬依赖
            raise RuntimeError("pip install openai (or `uv sync`) to use the agent harness") from e
        self.spec = spec
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = OpenAI(base_url=spec.base_url, api_key=spec.api_key(), timeout=timeout_s,
                              max_retries=max_retries)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Turn:
        t0 = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.spec.model, "messages": messages,
            "temperature": self.temperature, "max_tokens": self.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - 端点掉线、模型不认识工具都算这一种失败
            return Turn(error=f"{type(e).__name__}: {e}", model=self.spec.model,
                        latency_ms=(time.perf_counter() - t0) * 1000)
        msg = resp.choices[0].message
        text, thinking = split_thinking(msg.content or "")
        calls = _parse_tool_calls(msg)
        salvaged = 0
        if not calls:
            # 服务端没解析出来，但模型可能把调用原样写进了正文：捞一次，别让它变成「说了没做」。
            allowed = _declared_tool_names(tools)
            calls, cleaned = salvage_tool_calls(text, allowed)
            if calls:
                salvaged = len(calls)
                text = cleaned
        return Turn(text=text, thinking=thinking, tool_calls=calls, salvaged=salvaged,
                    usage=_parse_usage(resp), model=self.spec.model,
                    latency_ms=(time.perf_counter() - t0) * 1000)


_THINK_RE = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.S | re.I)


def split_thinking(text: str) -> tuple[str, str]:
    """把思考块从正文里剥出来，返回 (正文, 思考)。

    节点上的 Qwen3-8B 走的是 `--tool-call-parser hermes`，没开 reasoning parser，于是
    `<think>…</think>` 是 content 的一部分。不剥掉的话，模型每次回答前面都挂着一大段
    自言自语，会话和结论都没法看。剥出来的思考仍然存进会话，调试时看得到。
    闭合标签缺失（模型被截断）也算思考块，不然半截思考会混进正文。
    """
    if not text:
        return "", ""
    blocks = [m.group(0) for m in _THINK_RE.finditer(text)]
    clean = _THINK_RE.sub("", text).strip()
    inner = [re.sub(r"^<think(?:ing)?>|</think(?:ing)?>$", "", b, flags=re.I).strip() for b in blocks]
    return clean, "\n".join(b for b in inner if b)


def _parse_tool_calls(msg: Any) -> list[ToolCall]:
    """vLLM 返回的 arguments 是 JSON 串，偶尔带多余空白或空串，坏掉就当空参数。"""
    out: list[ToolCall] = []
    for i, call in enumerate(getattr(msg, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        raw = (getattr(fn, "arguments", "") or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append(ToolCall(id=getattr(call, "id", None) or f"call_{i+1}",
                            name=getattr(fn, "name", "") or "", arguments=args, raw_arguments=raw))
    return out


def _parse_usage(resp: Any) -> dict[str, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    return {k: int(getattr(usage, k, 0) or 0)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, k, None) is not None}


#: 模型把工具调用写成正文时的两种形状：Hermes 那种 `<tool_call>{"name":…}</tool_call>`，
#: 以及 `<function=名字><parameter=参数>值</parameter></function>` 那种 XML 写法。
_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S | re.I)
_FUNC_RE = re.compile(r"<function\s*=\s*([A-Za-z0-9_.\-]+)\s*>(.*?)</function>", re.S | re.I)
_PARAM_RE = re.compile(r"<parameter\s*=\s*([A-Za-z0-9_.\-]+)\s*>(.*?)</parameter>", re.S | re.I)


def salvage_tool_calls(text: str, allowed: Collection[str] | None = None) -> tuple[list[ToolCall], str]:
    """从正文里把模型手写的工具调用捞出来，返回 (捞到的调用, 去掉这些块之后的正文)。

    为什么要这个兜底：一个 vLLM 端点只能配一种 `--tool-call-parser`（`start_judges.sh`
    里配的是 `hermes`），配不上的模型会把调用原样写进 content。节点上的 Nemotron 就是
    这样——它写的是 `<function=load_skill><parameter=name>…</parameter></function>`，
    服务端解析不了，于是 harness 这边看到的是「模型自言自语说要去调工具，然后就没有然后了」，
    run 以一句空回答收尾。同一个 harness 换一个模型就哑火，问题不在模型，在没人接它的话。

    三条自我约束，宁可漏捞也不能把示例当调用：形状必须写全（成对的标签，不是半截）、
    名字必须在本次真的声明过的工具表里（`allowed` 为空表示不校验，只给离线测试用）、
    捞出来的块要从正文里去掉，免得同一句话在会话里出现两遍。
    """
    if not text or "<" not in text:
        return [], text
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for match in _BLOCK_RE.finditer(text):
        got = _calls_in_block(match.group(1), allowed, len(calls))
        if got:
            calls.extend(got)
            spans.append(match.span())
    if not calls:                                 # 没有外层 <tool_call>，也认裸的 <function=…>
        for match in _FUNC_RE.finditer(text):
            got = _calls_in_block(match.group(0), allowed, len(calls))
            if got:
                calls.extend(got)
                spans.append(match.span())
    if not calls:
        return [], text
    kept, cursor = [], 0
    for start, end in sorted(spans):
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return calls, re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()


def _calls_in_block(block: str, allowed: Collection[str] | None, offset: int) -> list[ToolCall]:
    """一个块里可能写着一个 JSON，也可能写着若干个 `<function=…>`。"""
    out = _calls_from_json(block, allowed, offset)
    if out:
        return out
    for i, match in enumerate(_FUNC_RE.finditer(block)):
        name = match.group(1)
        if allowed is not None and name not in allowed:
            continue
        args: dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(match.group(2)):
            args[pm.group(1)] = _loose_value(pm.group(2))
        out.append(ToolCall(id=f"salvaged_{offset + i + 1}", name=name, arguments=args,
                            raw_arguments=json.dumps(args, ensure_ascii=False)))
    return out


def _calls_from_json(block: str, allowed: Collection[str] | None, offset: int) -> list[ToolCall]:
    """`{"name": …, "arguments": {…}}` 这种（Hermes 的字面写法）能认就认。"""
    start = block.find("{")
    if start < 0:
        return []
    try:
        payload = json.loads(block[start:])
    except json.JSONDecodeError:
        return []
    items = payload if isinstance(payload, list) else [payload]
    out: list[ToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(item.get("name") or fn.get("name") or "")
        if not name or (allowed is not None and name not in allowed):
            continue
        args = item.get("arguments") or fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"value": args}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append(ToolCall(id=f"salvaged_{offset + len(out) + 1}", name=name, arguments=args,
                            raw_arguments=json.dumps(args, ensure_ascii=False)))
    return out


def _loose_value(raw: str) -> Any:
    """`<parameter=args>["--db","x"]</parameter>` 里的值多半是 JSON，不是就当字符串。"""
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _declared_tool_names(tools: list[dict[str, Any]] | None) -> set[str] | None:
    """本次真的声明给模型的工具名。捞的时候拿它当白名单，免得把文档示例当成调用。"""
    if not tools:
        return None
    names = set()
    for item in tools:
        name = ((item.get("function") or {}).get("name")) if isinstance(item, dict) else None
        if name:
            names.add(str(name))
    return names or None


class ScriptedProvider(Provider):
    """离线替身：按脚本顺序回回合，不联网。测试与 `--demo` 用它。

    `requests` 把每次真正发出去的 messages 记下来，测试据此断言 system prompt 里
    只放了技能描述、工具结果有没有回灌。
    """

    def __init__(self, turns: list[Turn | Callable[[list[dict[str, Any]]], Turn]], spec: ModelSpec | None = None,
                 *, name: str = "scripted", repeat_last: bool = False):
        self.spec = spec or ModelSpec(name=name, model=name, base_url="scripted://offline")
        self._script = list(turns)
        self.repeat_last = repeat_last
        self.requests: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Turn:
        self.requests.append([dict(m) for m in messages])
        self.tools_seen.append(list(tools or []))
        if not self._script:
            if self.repeat_last and self.requests:
                return self._last_turn()
            return Turn(error="scripted provider ran out of turns", model=self.spec.model)
        step = self._script.pop(0)
        turn = step(messages) if callable(step) else step
        self._last = turn
        return turn

    def _last_turn(self) -> Turn:
        return getattr(self, "_last", Turn(error="scripted provider has no turn to repeat", model=self.spec.model))


def text_turn(text: str, *, thinking: str = "") -> Turn:
    return Turn(text=text, thinking=thinking)


def tool_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> Turn:
    """`tool_turn(("load_skill", {"name": "sparkjury-clean"}))` 这样写，省掉手搓 ToolCall。"""
    return Turn(text=text, tool_calls=[
        ToolCall(id=f"call_{i+1}", name=name, arguments=dict(args), raw_arguments=json.dumps(args, ensure_ascii=False))
        for i, (name, args) in enumerate(calls)
    ])


def describe_endpoints() -> list[tuple[str, str, str, str]]:
    """给 CLI 列端点用：(短名, 模型, 地址, 说明)。"""
    return [(k, s.model, s.base_url, s.note) for k, s in NODE_ENDPOINTS.items()]
