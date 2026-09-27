"""工具注册表：模型能调什么、怎么调、描述怎么进提示词。

对应 pi 的「harness 拥有工具与 prompt 资源注册表」那一句。这里只有两类工具：

1. 六个技能（`skills/sparkjury-*/`）。技能正文**不进** system prompt，只进一句描述；
   模型想看细节就调 `load_skill`，想跑就调 `run_skill`。这就是 pi 的按需加载。
2. 两个本地只读工具 `list_dir` / `read_file`，给模型看工作区里有什么。

`run_skill` 的默认执行器是一个受控子进程：python 跑 `skills/<技能>/scripts/run.py`，
参数由模型给，超时和输出长度都有限制。测试和 `--demo` 换成离线执行器，不落地任何命令。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sparkjury.harness.config import PKG_ROOT

SKILLS_ROOT = PKG_ROOT / "skills"
MAX_OUTPUT_CHARS = 4000
DEFAULT_SKILL_TIMEOUT_S = 180.0


class ToolError(RuntimeError):
    """工具自己判定这次调用不成立（技能名写错、文件不存在……）。

    抛出去而不是回一段「友好提示」：注册表会把它记成一次失败调用，事件流里是 WARNING，
    manifest 的 degradations 里也看得见。模型照样能读到这句话并自己纠正。
    """


@dataclass(frozen=True)
class SkillInfo:
    """一个技能包：名字、一句话描述（进提示词）、正文（按需加载）。"""

    name: str
    description: str
    path: Path
    body: str = ""

    @property
    def run_py(self) -> Path:
        return self.path / "scripts" / "run.py"


def parse_skill_md(path: Path) -> SkillInfo:
    """读 SKILL.md 的 frontmatter。只认 name / description 两个键，其余原样跳过。

    这里不做完整 YAML：技能文件是仓库自己的，格式固定，引一个 YAML 依赖不值得。
    多行写法（`description: >` 后面跟缩进行）也认，防止哪天有人把描述拆成两行。
    """
    text = path.read_text(encoding="utf-8")
    name = ""
    description = ""
    if text.startswith("---"):
        block = text.split("---", 2)[1]
        lines = block.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            i += 1
            if line.startswith((" ", "\t")) or not line.strip():
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key not in ("name", "description"):
                continue
            if value in (">", "|", ">-", "|-", ""):
                collected: list[str] = []
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                    collected.append(lines[i].strip())
                    i += 1
                value = " ".join(p for p in collected if p)
            value = value.strip().strip('"').strip("'")
            if key == "name" and not name:
                name = value
            elif key == "description" and not description:
                description = value
    if not name:
        name = path.parent.name
    return SkillInfo(name=name, description=description, path=path.parent, body=text)


def load_skills(root: Path | None = None) -> list[SkillInfo]:
    """仓库里所有技能，按名字排序。找不到技能目录时返回空表而不是抛错。"""
    root = Path(root or SKILLS_ROOT)
    if not root.is_dir():
        return []
    return sorted((parse_skill_md(p) for p in root.glob("*/SKILL.md")), key=lambda s: s.name)


# ---------------------------------------------------------------- 工具与注册表


@dataclass
class ToolSpec:
    """一个模型可调用的工具。parameters 是 JSON Schema，直接进 OpenAI 的 tools 字段。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    source: str = "builtin"
    #: 会不会改东西。权限层照这个判：True 直接放行，False 要过审批或至少记一笔。
    #: 默认 False 是故意的——没声明的工具按「会改东西」处理，宁可多问一句。
    readonly: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class CallRecord:
    """一次工具调用留下的记录，manifest 里的降级项就是从这里挑的。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    output_chars: int = 0


class ToolRegistry:
    """注册表的全部职责：列清单给模型、按名字执行、把异常变成模型看得懂的文本。

    工具抛异常不往上炸：agent loop 里一个工具失败不该让整个 run 死掉，模型应该看到
    「这一步失败了，原因是……」然后自己决定要不要换条路。这正是 pi 里工具结果是
    普通消息的原因。
    """

    def __init__(self, tools: Iterable[ToolSpec] | None = None):
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools or []:
            self.register(tool)
        self.calls: list[CallRecord] = []

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def payload(self) -> list[dict[str, Any]]:
        return [t.to_payload() for t in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[str, bool]:
        """执行一个工具，返回 (给模型看的文本, 是否成功)。每次调用都记一笔，收尾好用。"""
        args = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            output, ok = f"没有这个工具：{name}。可用的是：{', '.join(self.names())}", False
        else:
            try:
                output, ok = tool.handler(args), True
            except ToolError as e:  # 工具自己判定的「这次调用不成立」，文本直接给模型看
                output, ok = str(e), False
            except Exception as e:  # noqa: BLE001 - 工具内部炸了，交回给模型
                output, ok = f"{type(e).__name__}: {e}", False
        self.calls.append(CallRecord(name=name, arguments=args, ok=ok, output_chars=len(output)))
        return output, ok

    def describe(self) -> str:
        """给 system prompt 的紧凑清单：名字 + 一句话，不含正文。"""
        return "\n".join(f"- {t.name}: {t.description}" for t in self._tools.values())


# ---------------------------------------------------------------- 执行器


@dataclass
class SkillRun:
    rc: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SubprocessSkillExecutor:
    """真执行：python skills/<技能>/scripts/run.py <模型给的参数>。

    不用 shell，参数以列表交给 subprocess，模型给不出重定向或管道这类惊喜。
    """

    def __init__(self, workdir: Path | None = None, *, python: str | None = None,
                 timeout_s: float = DEFAULT_SKILL_TIMEOUT_S):
        self.workdir = Path(workdir or Path.cwd())
        self.python = python or sys.executable
        self.timeout_s = timeout_s

    def __call__(self, skill: SkillInfo, args: Sequence[str]) -> SkillRun:
        cmd = [self.python, str(skill.run_py), *[str(a) for a in args]]
        try:
            proc = subprocess.run(cmd, cwd=str(self.workdir), capture_output=True, text=True,
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return SkillRun(rc=124, stdout="", stderr=f"技能超时（{self.timeout_s:.0f}s）", command=cmd)
        return SkillRun(rc=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "", command=cmd)


class OfflineSkillExecutor:
    """离线替身：不跑命令，回一段写死的输出。`--demo` 与测试用它。"""

    def __init__(self, replies: dict[str, str] | None = None, default: str = "已执行（离线替身，未真的跑命令）"):
        self.replies = replies or {}
        self.default = default
        self.seen: list[tuple[str, list[str]]] = []

    def __call__(self, skill: SkillInfo, args: Sequence[str]) -> SkillRun:
        self.seen.append((skill.name, [str(a) for a in args]))
        return SkillRun(rc=0, stdout=self.replies.get(skill.name, self.default), stderr="",
                        command=["offline", skill.name, *[str(a) for a in args]])


# ---------------------------------------------------------------- 注册表装配


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出被截断，共 {len(text)} 字符）"


def load_skill_tools(root: Path | None = None, *, executor: Callable[[SkillInfo, Sequence[str]], SkillRun] | None = None,
                     workdir: Path | None = None) -> tuple[ToolRegistry, list[SkillInfo]]:
    """装好一整套工具：六个技能 + 两个只读文件工具。返回 (注册表, 技能清单)。"""
    skills = load_skills(root)
    by_name = {s.name: s for s in skills}
    names = sorted(by_name)
    runner = executor or SubprocessSkillExecutor(workdir)
    base_dir = Path(workdir or Path.cwd()).resolve()

    def load_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", ""))
        skill = by_name.get(name)
        if skill is None:
            raise ToolError(f"没有这个技能：{name}。可用的是：{', '.join(names) or '（一个都没有）'}")
        return _truncate(skill.body)

    def run_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", ""))
        skill = by_name.get(name)
        if skill is None:
            raise ToolError(f"没有这个技能：{name}。可用的是：{', '.join(names) or '（一个都没有）'}")
        raw_args = args.get("args") or []
        if isinstance(raw_args, str):
            raw_args = raw_args.split()
        cli_args = [str(a) for a in raw_args]
        run = runner(skill, cli_args)
        try:  # 技能根目录可以不在仓库里（测试用临时目录），那时就写绝对路径
            shown = skill.run_py.relative_to(skill.path.parent.parent)
        except ValueError:
            shown = skill.run_py
        # 一律用正斜杠：Windows 上 Path 会渲染成反斜杠，于是「模型看到的命令行」跟着宿主系统变，
        # 提示词和会话记录在不同平台上就不是同一串字了（CI 的 windows job 就是这么抓到的）。
        shown = shown.as_posix()
        head = f"$ python {shown} {' '.join(cli_args)}".rstrip()
        tail = f"退出码 {run.rc}"
        body = _truncate((run.stdout + ("\n" + run.stderr if run.stderr else "")).strip())
        return f"{head}\n{tail}\n{body}" if body else f"{head}\n{tail}"

    def list_dir(args: dict[str, Any]) -> str:
        target = _safe_path(base_dir, str(args.get("path", ".")))
        if not target.is_dir():
            raise ToolError(f"不是一个目录：{target}")
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        listed = "\n".join(f"{'[dir] ' if p.is_dir() else '      '}{p.name}" for p in items[:200])
        return listed or "（空目录）"

    def read_file(args: dict[str, Any]) -> str:
        target = _safe_path(base_dir, str(args.get("path", "")))
        if not target.is_file():
            raise ToolError(f"不是一个文件：{target}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return _truncate(text)

    registry = ToolRegistry([
        ToolSpec(
            name="load_skill",
            description="读一个技能包的完整说明书（SKILL.md 正文）。想知道某个技能该怎么用、参数怎么写，先调它。",
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "description": "技能名", "enum": names},
            }, "required": ["name"]},
            handler=load_skill, source="skills", readonly=True,
        ),
        ToolSpec(
            name="run_skill",
            description="执行一个技能包（skills/<技能>/scripts/run.py），参数直接透传给 SparkJury 命令行。"
                        "比如 {\"name\": \"sparkjury-score\", \"args\": [\"--db\", \"runs/x.db\", \"--judges\", \"mock\"]}。",
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "description": "技能名", "enum": names},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "透传给该技能的命令行参数，例如 [\"--db\", \"runs/sparkjury.db\"]"},
            }, "required": ["name"]},
            handler=run_skill, source="skills",
        ),
        ToolSpec(
            name="list_dir",
            description="列工作目录下的文件。路径相对工作目录，不能跑到外面去。",
            parameters={"type": "object", "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
                        "required": []},
            handler=list_dir, source="local", readonly=True,
        ),
        ToolSpec(
            name="read_file",
            description="读工作目录下的一个文本文件（只读，路径不能越出工作目录）。",
            parameters={"type": "object", "properties": {"path": {"type": "string", "description": "相对路径"}},
                        "required": ["path"]},
            handler=read_file, source="local", readonly=True,
        ),
    ])
    return registry, skills


def _safe_path(base: Path, relative: str) -> Path:
    """把模型给的相对路径钉在工作目录里，越界就抛错——这一条不靠模型自觉。"""
    if not relative:
        raise ValueError("路径不能为空")
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径越出工作目录：{relative}")
    return target


def describe_skills(skills: Sequence[SkillInfo]) -> str:
    """技能清单（只描述、不正文），拼进 system prompt 的就是这一段。"""
    return "\n".join(f"- {s.name}: {s.description}" for s in skills)
