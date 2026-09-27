"""子 agent：一个 run 可以把一件独立的事派出去，让另一个 run 去干，结果拿回来接着用。

对应 pi 里扩展注册工具那一路（`registerTool`）——这里不发明新机制，就是把 `task` 注册进同一张
工具表，模型看到的是一个普通工具，调用它的语法跟调 `run_skill` 一模一样。

子 agent 不是「再跑一次普通的 run」，它有四条自己的规矩：

- **有自己的 run 目录**：`runs/<父 run>/children/<子 run>/`，会话树、事件流、用量账本、manifest
  各一套。子 agent 干了什么，事后翻它自己的目录就能看清，不用在父会话里翻。
- **默认只深一层**。子 agent 的工具表里通常没有 `task`，免得两个 agent 互相派活派到天荒地老。
  要更深的树得显式给 `max_depth`。
- **权限沿用父的**。子 agent 不能借「我是个新 run」绕开审批和红线：同一个 `PermissionPolicy`
  实例绑给两边，拒签记录也落在同一本账上。
- **失败不吞**。子 agent 没正常结束（报错、被中断、轮数用尽）时，父 agent 拿到的是 `ToolError`，
  会进 manifest 的降级项；模型看到的是「它没干完，原因是这个」，而不是一段假装成功的总结。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sparkjury.agent.ai import CHAT_ENDPOINTS, NODE_ENDPOINTS, OpenAICompatProvider, Provider, ScriptedProvider
from sparkjury.agent.tools import ToolError, ToolSpec
from sparkjury.harness.events import EventKind

#: 子 run 落在父 run 目录下的哪个子目录里。
CHILDREN_DIR = "children"

#: 回给父 agent 的正文上限（父 agent 的上下文也是钱）。
TEXT_LIMIT = 800


@dataclass
class SubagentRecord:
    """一次派活的账。父 manifest 里列的就是它。"""

    run_id: str
    model: str
    prompt_chars: int
    stopped: str
    turns: int
    tool_calls: int
    text: str
    run_dir: str
    error: str | None = None
    degradations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.stopped == "end_turn"

    def to_dict(self) -> dict[str, Any]:
        text = self.text if len(self.text) <= TEXT_LIMIT else self.text[:TEXT_LIMIT] + "…"
        return {"run_id": self.run_id, "model": self.model, "prompt_chars": self.prompt_chars,
                "stopped": self.stopped, "turns": self.turns, "tool_calls": self.tool_calls,
                "text": text, "run_dir": self.run_dir, "error": self.error, "ok": self.ok,
                "degradations": list(self.degradations)}


class SubagentRunner:
    """把 `task` 这个工具接到父 runtime 上。父 runtime 建好之后才能构造它（要用它的目录）。"""

    def __init__(self, parent: Any, *, factory: Callable[[str | None], Provider] | None = None,
                 max_depth: int = 1, depth: int = 0, max_turns: int = 6,
                 permissions: Any | None = None):
        self.parent = parent
        self.factory = factory
        self.max_depth = max(0, int(max_depth))
        self.depth = int(depth)
        self.max_turns = max(1, int(max_turns))
        self.permissions = permissions if permissions is not None else getattr(parent, "permissions", None)
        self.records: list[SubagentRecord] = []

    # ------------------------------------------------------------ 能不能再派
    @property
    def can_spawn(self) -> bool:
        return self.depth < self.max_depth

    @property
    def children_dir(self) -> Path:
        return Path(self.parent.run_dir) / CHILDREN_DIR

    # ------------------------------------------------------------ 工具
    def tool_spec(self) -> ToolSpec:
        return task_tool_spec(self._handle)

    def _handle(self, args: dict[str, Any]) -> str:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            raise ToolError("task 少了 prompt：要子 Agent 干什么，得说明白。")
        model = args.get("model")
        return self.run(prompt, model=str(model) if model else None)

    @staticmethod
    def _check_model(model: str) -> None:
        """起子 run 之前先把 model 校验掉：名字不对、或者云端端点没配 key，都在这里说清楚。

        节点上真跑过一次才知道这一步不能省：Qwen3-8B 拿到「model：短名见 agent endpoints」
        这句话之后，自己编了 `claude` / `codex` / `default` 这些名字（大概是照着仓库里的
        `.claude`、`.codex` 目录猜的），每次都在端点那边换回一个 404，父 agent 连着试了五种，
        把 12 轮全烧光。后来又挑了 stepfun 这个云端端点，而它的 key 是空的，照样白跑一次子 run。
        名单外的名字、要 key 却没 key 的端点，当场说清楚，比让子 agent 去撞 404 / 401 便宜得多。
        """
        if not model or model.startswith(("http://", "https://")):
            return
        spec = NODE_ENDPOINTS.get(model)
        if spec is None:
            if "/" in model:      # 完整的模型 id（形如 Qwen/Qwen3-8B）照给，那是另一个端点的模型名
                return
            raise ToolError(f"没有这个端点短名：{model}。能用的只有 {', '.join(CHAT_ENDPOINTS)}；"
                            f"要连别的地址就写完整形式 http://host:port/v1#模型名。"
                            f"不要用别的名字重试——名字不对，重试多少次都是同一个 404。")
        if spec.api_key_env and not os.environ.get(spec.api_key_env):
            raise ToolError(f"{model} 是云端端点，要环境变量 {spec.api_key_env}，现在这个变量是空的。"
                            f"换 judge-a / judge-b / subject 这些本地端点，或者先让人把 key 配上。")
    # ------------------------------------------------------------ 派活
    def run(self, prompt: str, *, model: str | None = None, max_turns: int | None = None) -> str:
        """跑一个子 run，把它的结论回给父 agent；没干完就抛 ToolError，不美化。"""
        from sparkjury.agent.runtime import AgentRuntime, new_run_id

        if not self.can_spawn:
            raise ToolError(f"派活已经到最深的第 {self.depth} 层（上限 {self.max_depth} 层）："
                            f"这活得自己干，不要再往下派。")
        self._check_model(model or "")
        provider = self._provider(model)
        child_id = new_run_id("child")
        # 还剩多少层可以往下派。用「剩余深度」而不是绝对层数传给子 run，免得两边的口径对不上。
        remaining = self.max_depth - self.depth - 1
        child = AgentRuntime(
            provider, runs_dir=self.children_dir, run_id=child_id, workdir=self.parent.workdir,
            skills=self.parent.skills, max_turns=max_turns or self.max_turns,
            permissions=self.permissions, subagents=remaining > 0, subagent_factory=self.factory,
            subagent_max_depth=remaining,
        )
        bus = getattr(self.parent, "bus", None)
        spec_model = getattr(getattr(child, "provider", None), "spec", None)
        model_name = getattr(spec_model, "model", "") or "(未知)"
        if bus is not None:
            bus.publish(EventKind.STAGE_START, f"子 agent 开跑：{_one_line(prompt)}", stage="SUBAGENT",
                        child_run_id=child_id, model=model_name)
        record = SubagentRecord(run_id=child_id, model=model_name, prompt_chars=len(prompt),
                                stopped="", turns=0, tool_calls=0, text="", run_dir=str(child.run_dir))
        try:
            result = child.start(prompt)
        except Exception as e:  # noqa: BLE001 - 子 run 自己炸了，父 agent 要知道，不是把父 run 也带走
            record.error = f"{type(e).__name__}: {e}"
            record.stopped = "error"
            self.records.append(record)
            if bus is not None:
                bus.publish(EventKind.ERROR, f"子 agent {child_id} 起不来：{record.error}",
                            stage="SUBAGENT", child_run_id=child_id)
            raise ToolError(f"子 agent 没能开起来（{record.error}）。这次派活算失败，别当成它干完了。") from e
        record.stopped = str(result.stopped)
        record.turns = int(result.turns)
        record.tool_calls = int(result.tool_calls)
        record.error = result.error
        record.text = result.text or ""
        record.degradations = _child_degradations(child)
        self.records.append(record)
        if bus is not None:
            bus.publish(EventKind.STAGE_END if record.ok else EventKind.WARNING,
                        f"子 agent {child_id} {record.stopped}", stage="SUBAGENT",
                        child_run_id=child_id, turns=record.turns, tool_calls=record.tool_calls,
                        ok=record.ok)
        if not record.ok:
            raise ToolError(f"子 agent {child_id} 没干完（{record.stopped}"
                            f"{'：' + record.error if record.error else ''}）。它最后说的是："
                            f"{_one_line(record.text) or '（没说）'}"
                            f"\n产物在 {self._shown(record.run_dir)}，要看细节去翻它的 manifest.json。")
        return (f"子 agent {child_id} 干完了：{record.stopped}｜轮数 {record.turns}｜"
                f"工具调用 {record.tool_calls}\n它的回答：\n{record.text or '（没留下文字结论）'}\n"
                f"产物目录：{self._shown(record.run_dir)}")

    # ------------------------------------------------------------ 杂项
    def _provider(self, model: str | None) -> Provider:
        """子 agent 用哪个模型：给了 factory 就全由它定（model=None 表示「跟父一样」）。

        没给 factory 时，指定了 model 就照短名解析一个新端点；没指定就**另起一个**同款 provider，
        不把父那个对象直接交出去——provider 常常带自己的状态（脚本替身就是一条用完就没了的时间线），
        父子共用一份，子 agent 会把父的下一轮吃掉。
        """
        if self.factory is not None:
            return self.factory(model)
        if model:
            return _default_factory(model)
        parent_provider = getattr(self.parent, "provider", None)
        if parent_provider is None:
            raise ToolError("父 run 没有 provider，子 agent 起不来。")
        spec = getattr(parent_provider, "spec", None)
        if spec is None:
            raise ToolError("父 run 的 provider 没有 spec，子 agent 起不来。")
        if isinstance(parent_provider, ScriptedProvider):
            raise ToolError("脚本替身（--demo 与测试）只有一条固定时间线，父子的轮次会互相吃掉："
                            "要让脚本模型派活，得显式给 subagent_factory；"
                            "只想换个端点跑子 agent，用 task 的 model 参数。")
        return OpenAICompatProvider(spec) if _is_openai_compat(parent_provider) else parent_provider

    def report(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.records]

    def _shown(self, run_dir: str) -> str:
        """尽量给相对路径：绝对路径在不同机器上不是同一串字，写进会话就不好读。"""
        try:
            return Path(run_dir).resolve().relative_to(Path(self.parent.workdir).resolve()).as_posix()
        except (ValueError, AttributeError):
            return Path(run_dir).as_posix()


def task_tool_spec(handler: Callable[[dict[str, Any]], str]) -> ToolSpec:
    """task 工具的定义。handler 由 runner 给：只列清单的地方（`agent policy`、`agent tools`）
    不需要真的能跑，但需要看到「模型能调什么」里确实有这么一条。

    `model` 写成 enum 而不是一句「短名见 agent endpoints」：提示词里的「见某处」模型是不会去见的，
    它只会编一个看起来像的名字出来。
    """
    return ToolSpec(
        name="task",
        description="把一件独立的事派给一个子 Agent 去做，它有自己的会话和产物目录，干完把结论拿回来。"
                    "适合「跑一轮完整的体检」「单独查一件事」这种能一口气说到底的活；"
                    "需要一步步商量的活自己干，别派出去。",
        parameters={"type": "object", "properties": {
            "prompt": {"type": "string", "description": "要子 Agent 干的事，说清楚交什么、干到什么程度算完"},
            "model": {"type": "string", "enum": list(CHAT_ENDPOINTS),
                      "description": "用哪个端点跑子 Agent；不填就跟当前这次一样。"},
        }, "required": ["prompt"]},
        handler=handler, source="subagent",
    )


def _default_factory(name: str | None) -> Provider:
    from sparkjury.agent.ai import OpenAICompatProvider, resolve_model

    return OpenAICompatProvider(resolve_model(name))


def _is_openai_compat(provider: Provider) -> bool:
    """真端点才另起一个连接：客户端里带着 httpx 连接池，复制一份比共用一份更干净。"""
    from sparkjury.agent.ai import OpenAICompatProvider

    return isinstance(provider, OpenAICompatProvider)


def _child_degradations(child: Any) -> list[str]:
    """子 run 的降级项抄一份上来。父 manifest 要能一眼看出「子 agent 那次自己就降级过」。"""
    path = Path(child.run_dir) / "manifest.json"
    if not path.is_file():
        return []
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["子 run 的 manifest 读不出来（JSON 坏了）"]
    return [f"子 agent {child.run_id}: {item}" for item in data.get("degradations", [])]


def _one_line(text: str, limit: int = 80) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = ["CHILDREN_DIR", "SubagentRecord", "SubagentRunner", "task_tool_spec"]
