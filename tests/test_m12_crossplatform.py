"""仓库约定守卫：跨平台、跨工具，以及那些会悄悄过期的东西。"""

import os
import py_compile
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXT = {".py", ".sh", ".md", ".toml", ".yml", ".yaml", ".json", ".html", ".txt"}
SKIP = {".venv", "runs", "__pycache__", ".pytest_cache", "logs", "data", ".git", ".worktrees"}


def _text_files():
    for p in ROOT.rglob("*"):
        # .venv* covers the node's extra environments (.venv-tau2, .venv-vllm): third-party files inside them
        # are not ours to lint, and litellm ships a couple of CRLF files that used to fail this test on the node.
        if p.is_file() and p.suffix in TEXT_EXT and not any(part in SKIP or part.startswith(".venv") for part in p.parts):
            yield p


def test_no_crlf_in_tracked_text_files():
    bad = [str(p.relative_to(ROOT)) for p in _text_files() if b"\r\n" in p.read_bytes()]
    assert bad == [], f"CRLF line endings would break bash on the node: {bad}"


def test_editorconfig_and_gitattributes_force_lf():
    assert "end_of_line = lf" in (ROOT / ".editorconfig").read_text(encoding="utf-8")
    assert "eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


def test_docs_have_no_windows_only_commands():
    docs = [ROOT / "README.md", ROOT / "docs" / "MODULES.md", ROOT / "docs" / "ARCHITECTURE.md", ROOT / "deploy" / "README.md",
            ROOT / "skills" / "README.md", ROOT / "nat" / "README.md", ROOT / "docs" / "CROSS_PLATFORM.md"]
    offenders = []
    for d in docs:
        for i, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("cd D:\\") or stripped.startswith("cd C:\\"):
                offenders.append(f"{d.name}:{i} absolute Windows path")
            if re.match(r"^set [A-Z_]+=", stripped):
                offenders.append(f"{d.name}:{i} cmd.exe 'set' without a POSIX alternative")
            if re.match(r"^(start|type) ", stripped) and "macOS" not in line and "Windows" not in line:
                offenders.append(f"{d.name}:{i} Windows-only command without the macOS form")
            if stripped.startswith("```powershell"):
                offenders.append(f"{d.name}:{i} PowerShell-only code block")
    assert offenders == [], offenders


def test_helper_scripts_compile_and_use_no_shell_specific_calls():
    for name in ("node.py", "screenshot.py", "validate_skills.py", "gen_skills.py", "make_samples.py", "certificate.py"):
        p = ROOT / "scripts" / name
        py_compile.compile(str(p), doraise=True)
        src = p.read_text(encoding="utf-8")
        assert "shell=True" not in src and "os.system(" not in src, name


def test_ops_dependency_group_declared():
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "ops = [" in py and "paramiko" in py and "playwright" in py


def _shell_files():
    """shell 脚本与 git 钩子。钩子没有 .sh 后缀，_text_files 扫不到，单独走一遍。"""
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(part in SKIP or part.startswith(".venv") for part in p.parts):
            continue
        if p.suffix == ".sh" or p.parent.name == ".githooks":
            yield p


def test_shell_vars_are_braced_before_non_ascii():
    """变量后面紧跟中文标点时必须写成 ${var}。

    macOS 自带的 bash 3.2 在没有 UTF-8 locale 时按字节解析，会把「，」「）」这类标点的首字节
    吃进变量名：`log "分支 $branch，路径 $path"` 报的是 `branch\\xef: unbound variable`，
    看起来完全不像语法问题。踩过一次，这里立个守卫。
    """
    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")
    offenders = []
    for p in _shell_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            m = pattern.search(line)
            if m:
                offenders.append(f"{p.relative_to(ROOT)}:{i} ${m.group(1)}")
    assert offenders == [], f"变量后紧跟非 ASCII 字符，bash 3.2 下会变成 unbound variable，请写成 ${{var}}：{offenders}"


def test_git_hooks_are_present_and_executable():
    """提交门禁要能真的跑起来：钩子缺失或没有执行位，等于门禁不存在。"""
    for name in ("pre-commit", "commit-msg"):
        hook = ROOT / ".githooks" / name
        assert hook.is_file(), f"缺少 .githooks/{name}"
        assert os.access(hook, os.X_OK), f".githooks/{name} 没有执行位（chmod +x）"
    assert (ROOT / "scripts" / "check_staged.py").is_file(), "pre-commit 依赖 scripts/check_staged.py"


MIRROR_NOTICE = "> 这一份和"


def _without_notice(text):
    """去掉镜像首行那句说明，连带它后面那行空行。"""
    out, skip_blank = [], False
    for line in text.splitlines():
        if line.startswith(MIRROR_NOTICE):
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        out.append(line)
    return out


def test_skill_mirrors_stay_in_sync():
    """同一条技能放两份：`.agents/skills` 是正文，`.claude/skills` 是给 Claude Code 的镜像。

    两边唯一的允许差异是镜像那句「两处一起改」的说明。改的时候只改一边、另一边的 Agent
    读到旧规矩，是最容易发生也最难发现的事，所以让测试盯着。
    """
    body_root = ROOT / ".agents" / "skills"
    mirror_root = ROOT / ".claude" / "skills"
    assert body_root.is_dir() and mirror_root.is_dir(), "技能目录缺失：.agents/skills 或 .claude/skills"

    names = sorted(p.name for p in body_root.iterdir() if p.is_dir())
    assert names, ".agents/skills 下没有技能"
    assert names == sorted(p.name for p in mirror_root.iterdir() if p.is_dir()), "两处技能目录名不一致"

    for name in names:
        for f in sorted((body_root / name).rglob("*")):
            if not f.is_file():
                continue
            mirror = mirror_root / name / f.relative_to(body_root / name)
            assert mirror.is_file(), f".claude 缺镜像文件：{mirror.relative_to(ROOT)}"
            body = _without_notice(f.read_text(encoding="utf-8"))
            copy = _without_notice(mirror.read_text(encoding="utf-8"))
            assert body == copy, f"镜像与正文不一致，两边要一起改：{f.relative_to(ROOT)}"


# AGENTS.md 的「红线」是真源，技能里那份是给只加载技能的 Agent 看的副本。
# 这两份真的分叉过：技能里压成了一句摘要，漏掉 scp 的 1GB 上限、8888/9000 必须鉴权、
# 节点活动结束会清盘三条——只读技能、没读 AGENTS.md 正文的 Agent 就真的不知道。
REDLINE_TOKENS = [
    "reboot",
    "shutdown",
    "poweroff",
    "192.168.110.0/24",
    "scp",
    "tmux",
    "8888",
    "9000",
]


def test_skill_restates_every_red_line():
    """AGENTS.md 的红线必须在技能里逐条写全，不能只留一句摘要。

    判断依据取自 AGENTS.md 正文而不是这里另抄一份，所以往 AGENTS.md 加一条红线、
    忘了同步到技能时，这个测试会直接点名缺的是哪几个词。
    """
    contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## 红线" in contract, "AGENTS.md 里找不到「## 红线」一节"
    source = contract.split("## 红线", 1)[1].split("\n## ", 1)[0]

    for rel in (
        ".agents/skills/sj-push-and-deploy/SKILL.md",
        ".claude/skills/sj-push-and-deploy/SKILL.md",
    ):
        skill = (ROOT / rel).read_text(encoding="utf-8")
        assert "## 红线" in skill, f"{rel} 里没有「## 红线」一节"
        copy = skill.split("## 红线", 1)[1]
        missing = [t for t in REDLINE_TOKENS if t in source and t not in copy]
        assert missing == [], (
            f"{rel} 的红线漏了 AGENTS.md 里写着的：{missing}。"
            "技能常常是 Agent 唯一的规矩来源，不能比 AGENTS.md 少。"
        )


# 上手提示词里点名的入口，改名或搬走之后这里会先红，而不是等新人撞墙
ONBOARDING_PATHS = [
    "AGENTS.md",
    "scripts/worktree.sh",
    "scripts/node.py",
    "deploy/dgx/node.env.example",
    ".githooks",
    ".github/pull_request_template.md",
    ".agents/skills/sj-worktree/SKILL.md",
    ".agents/skills/sj-push-and-deploy/SKILL.md",
    "docs/ARCHITECTURE.md",
    "docs/MODULES.md",
    "docs/ABLATION.md",
    "deploy/README.md",
]


def test_onboarding_paths_are_not_stale():
    """上手提示词和它指向的契约文件里点名的入口必须真实存在。

    提示词只有一句话，新人第一次打开它时也正是最不能出错的时候——第一步就撞墙是最坏的首印象。
    """
    onboarding = (ROOT / "docs" / "ONBOARDING.md").read_text(encoding="utf-8")
    contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in onboarding, "上手提示词没有指向 AGENTS.md"
    assert "github.com/VioletScar-Hui/sparkjury" in onboarding, "上手提示词没给仓库地址"
    # 提示词把细节都交给 AGENTS.md，所以那两个入口得由契约文件点名
    for required in ("scripts/worktree.sh", "scripts/node.py"):
        assert required in contract, f"AGENTS.md 漏了 {required}"
    named = [p for p in ONBOARDING_PATHS if p in onboarding or p in contract]
    missing = sorted({p for p in named if not (ROOT / p).exists()})
    assert missing == [], f"点到了不存在的路径：{missing}"

def test_certificate_is_wired_into_the_gates():
    """证书必须真的被门禁调用，否则它只是一份没人跑、会过期的报告。

    一条规矩写进文档只是「请求」，挂到钩子和 CI 上才是「强制」。这条测试盯着两处接线：
    pre-commit 跑快档（不执行测试，几秒回来），CI 跑全档（含端到端流水线与六个技能封装）。
    """
    pre = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "certificate.py" in pre, "pre-commit 没有跑 scripts/certificate.py"
    assert "certificate.py" in ci, "CI 没有跑 scripts/certificate.py"
    assert "--fast" in pre, "pre-commit 该跑证书快档，全档交给 CI"


def test_json_assertions_read_stdout_not_the_mixed_output():
    """`--json` 的断言必须读 r.stdout，不能读 r.output。

    typer 的 CliRunner 会把 stderr 混进 output（typer/testing.py 里 BytesIOCopy(copy_to=...)，
    它的类文档第一句就是 "Mixes stdout and stderr streams"）。于是只要命令往 stderr 写一句降级
    提示，拿 r.output 去 json.loads 就会以 JSONDecodeError 崩掉，而且报错看不出真正原因。本轮就
    踩过一次：cluster 加了「Jev 没配 key」的提示，六处解析 JSON 的断言里立刻红了一个。stdout 才是
    「机器可读」这份契约。

    这里用 ast 找真实调用而不是按文本 grep：这段说明本身就写着要禁的那串字符，按文本找会先把自己
    算成违规。
    """
    import ast

    offenders = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            fn, arg = node.func, node.args[0]
            if not (isinstance(fn, ast.Attribute) and fn.attr == "loads"
                    and isinstance(fn.value, ast.Name) and fn.value.id == "json"):
                continue
            if isinstance(arg, ast.Attribute) and arg.attr == "output":
                offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == [], f"这些地方连 stderr 一起当 JSON 解析，应改用 r.stdout：{offenders}"


def _load_script(name):
    """按路径加载 scripts/ 下的脚本，不要求它是包的一部分。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"sj_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_node_py_rejects_extra_args_instead_of_acting():
    """`node.py sync --help` 不能真的 sync。

    `node.py` 用 main(argv) 手工分发，sync 分支以前不看多余参数——于是「看看用法」这个动作会
    把代码推过去，覆盖团队共享的 `~/sparkjury`，而上面常驻着 loop、tau2full 这类无人值守任务。
    本仓库自己就踩过一次（主树当时正跑着两个长任务）。这里不碰网络：只验证分发层在参数多余时
    返回非零，并且压根不去调 sync()/check()。
    """
    mod = _load_script("node")
    called = []
    mod.sync = lambda: (called.append("sync"), 0)[1]
    mod.check = lambda: (called.append("check"), 0)[1]

    assert mod.main(["sync", "--help"]) == 2, "sync 收到多余参数应当直接失败"
    assert mod.main(["check", "--dry-run"]) == 2, "check 收到多余参数应当直接失败"
    assert called == [], f"多余的参数不该触发动作，却调用了 {called}"
    # 不带参数时照常工作，别把正常路径一起堵死
    assert mod.main(["sync"]) == 0 and called == ["sync"]


def test_gen_skills_refuses_args_instead_of_regenerating():
    """`gen_skills.py --help` 不能顺手把六个技能重新生成。

    它的 main() 以前完全不看 sys.argv，所以想看用法的人会得到一次真实的重新生成，
    把手改过的 `skills/*/SKILL.md`、`scripts/run.py`、`skill-card.md` 全部覆盖。
    """
    mod = _load_script("gen_skills")

    def snapshot():
        # 比字节而不是解码后的文本：skills/ 下有 __pycache__ 这类二进制产物。
        # __pycache__ 本身要跳过——它是跑技能时生成的字节码，不是生成物，且每次都变。
        return {p: p.read_bytes() for p in sorted((ROOT / "skills").rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts}

    before = snapshot()
    assert mod.main(["--help"]) == 0, "--help 应当打印用法并以 0 退出"
    assert mod.main(["--dry-run"]) == 2, "不认识的参数应当返回非零"
    assert snapshot() == before, "带参数调用不该重新生成技能"



SKILL_COMMANDS = ("ingest", "precheck", "score", "arbitrate", "cluster", "report", "regress", "run")


def _non_ascii(text: str) -> str:
    return "".join(sorted({c for c in text if ord(c) > 127}))


def test_cli_help_text_of_skill_commands_is_ascii_for_windows_consoles():
    """Windows CI 的控制台是 cp1252：--help 也走同一个控制台，帮助文本里有非 ASCII 就 UnicodeEncodeError。

    六技能封装把 CLI 当子进程调，`sparkjury cluster --help` 就是这么红的：新加的两个选项写了中文 help。
    （rich 的边框字符不用管：真 Windows 控制台上它自己会换成 ASCII 边框，出问题的是帮助文本本身。）
    这里把六个技能用到的子命令一次钉住——再加中文 help / 中文 docstring 就在本地红，不用等 Windows。
    """
    import click
    import typer.main

    from sparkjury.cli import app as cli_app

    group = typer.main.get_command(cli_app)
    assert group.commands.keys() >= set(SKILL_COMMANDS)
    bad = []
    for name in SKILL_COMMANDS:
        cmd = group.commands[name]
        texts = [("docstring", cmd.help or "")]
        ctx = click.Context(cmd)
        for prm in cmd.get_params(ctx):
            texts.append((f"option {prm.name}", getattr(prm, "help", None) or ""))
        for where, text in texts:
            chars = _non_ascii(text)
            if chars:
                bad.append(f"{name} 的 {where} 里有非 ASCII: {chars!r}")
    assert bad == [], "帮助文本要能用 cp1252 编码（Windows 控制台）：" + "; ".join(bad)
