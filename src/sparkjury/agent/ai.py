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
from typing import Any, Callable

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
        return Turn(text=text, thinking=thinking, tool_calls=_parse_tool_calls(msg),
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
