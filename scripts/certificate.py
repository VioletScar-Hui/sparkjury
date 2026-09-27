#!/usr/bin/env python3
"""交付验收证书：把文档里的「声称」逐条变成会红的断言。

为什么要有这个文件
------------------
`docs/` 里散着一批数字和结构描述——总测试数、每个模块的用例数、目录树、API 路由表、
预检规则条数。它们靠手抄、靠人眼核对，于是漂了：README 写「96 个测试」、ARCHITECTURE
写「98 个 pytest 用例通过」，实测是 116 passed + 3 skipped；目录树里长期列着
`arbiter/fallback.py`、`harness/run.py` 和一个并不存在的顶层 `cockpit/`。

这些话写成散文时，读者没法判断它今天是否还成立，只能去数、去跑、去翻代码。证书把
「声称」和「实测」并排跑一遍：声称按锚点正则从文档里读出来，实测来自真实跑一遍测试、
真实跑一遍流水线、或直接读代码。人不用复核，CI 每天替所有人复核。

锚点匹配不上，报失败而不是静默通过
----------------------------------
下面 CLAIMS 里的正则是写死的锚点。文档改写法导致锚点匹配不上时，证书报的是「找不到这条
声称」而不是通过——逼改文档的人回来同步锚点，而不是让一条声称悄悄消失。这是刻意的：
证书的价值全在于「不通过就是真有问题」。

两层
----
  全档（默认）：文档 + 结构 + 端到端流水线 + 六个技能封装。CI 用这个。
  快档 --fast：文档 + 结构，不跑流水线。pre-commit 用这个（按 AGENTS.md「验证按风险
               相称」，钩子只挡文档与结构这类便宜的事实，全套测试交给 CI）。

用法
----
    python scripts/certificate.py            # 全档
    python scripts/certificate.py --fast     # 快档
    python scripts/certificate.py --json     # 机器可读，贴进 PR
退出码 0 = 全部通过；1 = 有断言没过。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------------------
# 跑命令 / 读文件
# --------------------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跑一条外部命令。

    COLUMNS 拉大是为了让 rich 表格不折行，好按单元格取数。
    解码固定用 UTF-8 且 errors="replace"：子进程在 Windows 上可能按 cp1252 输出，
    固定 locale 解码会在读日志时炸掉，而这里要断言的子串全是 ASCII，替换掉个别字符不影响判断。
    """
    env = dict(os.environ)
    env["COLUMNS"] = "500"
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _cli(*args: str) -> list[str]:
    """优先用装好的 sparkjury 入口，没有就退回模块调用（和 skills/*/scripts/run.py 一致）。"""
    exe = shutil.which("sparkjury")
    return [exe, *args] if exe else [sys.executable, "-m", "sparkjury.cli", *args]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _force_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，否则 Windows 控制台会直接崩。

    Windows 上 Python 默认按控制台的 cp1252/GBK 编码输出，print 中文字符会抛
    UnicodeEncodeError: 'charmap' codec can't encode characters——证书的条目名全是中文，
    一进 CI 的 windows-latest 就死在这里（这个仓库在 CLI 输出上踩过同一个坑，
    见 docs/ESSAY_十日谈.md 末尾那条教训）。

    与其把条目名改成英文来绕开，不如把输出流切到 UTF-8：reconfigure 是 3.7+ 的标准做法，
    万一某个环境不支持，就退化成 replace，宁可有个别字符显示成问号，也不能让证书因为
    编码问题报一个和内容无关的错。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _cell(table: str, label: str) -> str:
    """从 rich 表格里取某个标签右边的值，例如 stats 表的「traces │ 14」。

    Windows 上 rich 会退回 ASCII 边框（`|` 和 `+`），所以两种竖线都得认——
    只认 `│` 的话整张表在那边会取成空值，表现为「stats 数字」无故变红。
    """
    m = re.search(rf"[│|]\s*{re.escape(label)}\s*[│|]\s*([^│|]+?)\s*[│|]", table)
    return m.group(1).strip() if m else ""


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------------------
# 实测：真实跑一遍测试
# --------------------------------------------------------------------------------------


@dataclass
class TestFacts:
    collected: int = 0
    passed: int = 0
    skipped: int = 0
    failed: int = 0
    per_file: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def measure(self, key: str) -> int:
        return int(getattr(self, key))


def measure_tests(fast: bool = False) -> TestFacts:
    """拿到测试用例数。

    全档：真跑一遍——本仓的 `pytest -q` 不打印汇总行，只能数进度字符（同 AGENTS.md 里记的
    那个办法），再由收集阶段取每个文件的用例数。
    快档：只跑 `--collect-only`。它不执行测试，几秒就回来，适合放在 pre-commit；代价是拿不到
    passed / skipped，所以那几条声称在快档里退化成「各处是否互相一致」的核对。
    """
    facts = TestFacts()

    collected = _run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"])
    for path, count in re.findall(r"^(\S+\.py): (\d+)$", collected.stdout, re.M):
        facts.per_file[Path(path).name] = int(count)
    facts.collected = sum(facts.per_file.values())

    if not facts.per_file:
        facts.error = f"读不到 pytest 收集结果（exit={collected.returncode}）：{collected.stdout[-300:]}"
        return facts
    if fast:
        facts.passed = -1  # 快档不执行测试，passed / skipped 未知
        return facts

    got = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no"])
    marks = "".join(re.findall(r"^[.sFEx]+", got.stdout, re.M))
    if not marks:
        facts.error = f"读不到 pytest 进度输出（exit={got.returncode}）：{got.stdout[-300:]}"
        return facts
    facts.skipped = marks.count("s")
    facts.failed = sum(marks.count(c) for c in "FEx")
    facts.collected = len(marks)
    facts.passed = facts.collected - facts.skipped - facts.failed
    return facts


# --------------------------------------------------------------------------------------
# 声称一：文档里的总数与模块数
# --------------------------------------------------------------------------------------


@dataclass
class Claim:
    label: str
    path: str
    pattern: str
    measure: str


# 总测试数。README / 提交清单 / 征文是对外材料，被反复引用也最容易漂，所以逐处盯着。
HEADLINE_CLAIMS = [
    Claim("README 测试总数", "README.md", r"\| 测试 \| (\d+) passed、", "passed"),
    Claim("ARCHITECTURE 进度总数", "docs/ARCHITECTURE.md", r"(\d+) 个 pytest 用例通过。一条命令跑通全流程", "passed"),
    Claim("SUBMISSION_CHECKLIST 总数", "docs/SUBMISSION_CHECKLIST.md", r"\| 完整性 \| 20% \| (\d+) passed、", "passed"),
    Claim("十日谈测试总数", "docs/ESSAY_十日谈.md", r"这个数字是 (\d+) passed", "passed"),
]

# 模块表的用例数写的是「这个模块有多少个用例」（收集数，不是通过数）；有跳过的模块在
# 括号里注明，例如 M9 的 16 个用例里有 3 个跳过。
MODULE_FILES = {
    "M1": ["test_m1_adapters.py", "test_m1_store_cli.py"],
    "M2": ["test_m2_precheck.py"],
    "M3": ["test_m3_judges.py"],
    "M4": ["test_m4_arbiter.py"],
    "M5": ["test_m5_cluster.py"],
    "M6": ["test_m6_report_regress.py"],
    "M7": ["test_m7_harness.py"],
    "M8": ["test_m8_api.py"],
    "M9": ["test_m9_skills_nat.py"],
    "M10": ["test_m10_deploy.py"],
    "M11": ["test_m11_docs.py"],
    "M12": ["test_m12_crossplatform.py", "test_ablation.py"],
    "M13": ["test_m13_agent.py", "test_m13_durable.py", "test_m13_toplayer.py"],
}


def check_doc_counts(facts: TestFacts, fast: bool) -> list[Result]:
    """总测试数在各处的声称。

    全档：逐处核对实测 passed。
    快档：不执行测试，拿不到 passed，退一步核对「各处是否互相一致」——README 写 96、
    ARCHITECTURE 写 98 这种自相矛盾，不跑测试也能抓出来。
    """
    results: list[Result] = []
    claims: list[tuple[Claim, int]] = []
    for c in HEADLINE_CLAIMS:
        m = re.search(c.pattern, _read(c.path), re.M)
        if not m:
            results.append(Result(c.label, False, f"{c.path} 匹配不到这条声称——文档改写法了？锚点：{c.pattern}"))
            continue
        claims.append((c, int(m.group(1))))

    if fast:
        values = sorted({v for _, v in claims})
        ok = len(values) == 1
        results.append(Result(
            "总测试数各处一致", ok,
            f"文档里写的是 {values}" + ("" if ok else "，各处自相矛盾（全档还会核对实测 passed）"),
        ))
        return results

    for c, claimed in claims:
        actual = facts.measure(c.measure)
        results.append(Result(c.label, claimed == actual, f"{c.path} 写 {claimed}，实测 {c.measure}={actual}"))
    return results


def check_module_counts(facts: TestFacts) -> list[Result]:
    results: list[Result] = []
    text = _read("docs/ARCHITECTURE.md")
    for module, files in MODULE_FILES.items():
        m = re.search(rf"^\| {module} \|.*?(\d+) 个用例", text, re.M)
        if not m:
            results.append(Result(f"{module} 用例数", False, f"docs/ARCHITECTURE.md 的 {module} 行匹配不到「N 个用例」"))
            continue
        claimed = int(m.group(1))
        actual = sum(facts.per_file.get(f, 0) for f in files)
        results.append(Result(f"{module} 用例数", claimed == actual, f"文档写 {claimed}，实测 {actual}（{'+'.join(files)}）"))

    covered = sum(facts.per_file.get(f, 0) for fs in MODULE_FILES.values() for f in fs)
    results.append(Result("模块表总和 = 收集总数", covered == facts.collected,
                          f"模块表覆盖 {covered}，pytest 收集 {facts.collected}"))
    return results


# --------------------------------------------------------------------------------------
# 声称二：文档里的结构
# --------------------------------------------------------------------------------------


def _structure_block() -> str:
    text = _read("docs/ARCHITECTURE.md")
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
        if "├── src/sparkjury/" in block:
            return block
    return ""


def check_structure_block() -> list[Result]:
    """目录树里点名过的每一个路径都必须真实存在。

    这棵树被当成仓库地图在用，却没人核对过：里面长期躺着 `arbiter/fallback.py`、
    `harness/run.py` 和一个并不存在的顶层 `cockpit/`。
    """
    tree = _structure_block()
    if not tree:
        return [Result("目录树", False, "docs/ARCHITECTURE.md 里找不到仓库目录树代码块")]

    results: list[Result] = []
    top = [n.rstrip("/") for n in re.findall(r"^[├└]── ([\w./-]+)", tree, re.M)]
    missing_top = [p for p in top if not (ROOT / p).exists()]
    results.append(Result("目录树顶层条目", not missing_top, f"点名 {len(top)} 项，不存在：{missing_top or '无'}"))

    have = {p.name for p in (ROOT / "src" / "sparkjury").rglob("*.py")}
    mentioned = sorted(set(re.findall(r"\b(\w+\.py)\b", tree)))
    missing_py = [f for f in mentioned if f not in have and not (ROOT / "tests" / f).exists()]
    results.append(Result("目录树里的 .py", not missing_py,
                          f"点名 {len(mentioned)} 个，src/sparkjury 下找不到：{missing_py or '无'}"))
    return results


def check_class_bindings() -> list[Result]:
    """目录树里 `文件.py  # A / B` 表示「A、B 定义在这个文件里」，要真的对得上。

    `report.py  # BadCase / Cluster / EvidenceCard` 就是这么错的：BadCase 和 Cluster
    实际住在 `models/cluster.py`，照树去翻的人会翻空。
    """
    tree = _structure_block()
    if not tree:
        return [Result("类归属", False, "找不到目录树，无法核对类的归属")]

    index: dict[str, Path] = {}
    for p in (ROOT / "src" / "sparkjury").rglob("*.py"):
        index.setdefault(p.name, p)

    results: list[Result] = []
    for fname, comment in re.findall(r"── (\w+\.py)\s+#\s*(.+?)\s*$", tree, re.M):
        names = [n.strip() for n in comment.split("/")]
        if len(names) < 2 or not all(re.fullmatch(r"[A-Z][A-Za-z0-9]*", n) for n in names):
            continue  # 注释不是「类名 / 类名」的形式，不属于这条声称
        target = index.get(fname)
        if target is None:
            results.append(Result(f"{fname} 的类归属", False, f"目录树点名 {fname}，但 src 下没有这个文件"))
            continue
        body = target.read_text(encoding="utf-8")
        wrong = [n for n in names if not re.search(rf"^class {n}\b", body, re.M)]
        results.append(Result(f"{fname} 的类归属", not wrong,
                              f"树里写 {' / '.join(names)}，{target.relative_to(ROOT)} 里找不到：{wrong or '无缺失'}"))
    return results


def check_precheck_rules() -> list[Result]:
    """预检规则表的行数必须等于代码里 RULES 的条数。"""
    text = _read("docs/ARCHITECTURE.md")
    body = _read("src/sparkjury/precheck/rules.py")
    code = re.search(r"RULES:\s*list\[RuleFn\]\s*=\s*\[(.*?)\]", body, re.S)
    if not code:
        return [Result("预检规则条数", False, "src/sparkjury/precheck/rules.py 里找不到 RULES 列表")]
    n_code = len(re.findall(r"^\s*rule_\w+,", code.group(1), re.M))

    table = re.search(r"^\| 规则 \| 判定 \|$(.*?)^\s*$", text, re.M | re.S)
    if not table:
        return [Result("预检规则条数", False, "docs/ARCHITECTURE.md 里找不到预检规则表")]
    n_doc = len(re.findall(r"^\| (\w+) \|", table.group(1), re.M))
    return [Result("预检规则条数", n_doc == n_code, f"文档表 {n_doc} 条，代码 RULES {n_code} 条")]


def check_api_routes() -> list[Result]:
    """API 路由表必须和 app.py 里注册的路由完全一致（多写少写都算错）。"""
    text = _read("docs/ARCHITECTURE.md")
    app = _read("src/sparkjury/api/app.py")
    listed = {(m.group(1).upper(), m.group(2)) for m in re.finditer(r"^\| (GET|POST) \| (\S+) \|", text, re.M)}
    actual = {(m.group(1).upper(), m.group(2)) for m in re.finditer(r'@app\.(get|post)\("([^"]+)"', app)}
    if not listed:
        return [Result("API 路由表", False, "docs/ARCHITECTURE.md 里找不到 API 路由表")]
    missing, extra = sorted(listed - actual), sorted(actual - listed)
    return [Result("API 路由表", listed == actual,
                   f"文档 {len(listed)} 条 / 代码 {len(actual)} 条；文档多写 {missing or '无'}；文档漏写 {extra or '无'}")]


def check_misc_bindings() -> list[Result]:
    """剩下几条「文档说 A、代码是 B」的点位。"""
    results: list[Result] = []
    arch = _read("docs/ARCHITECTURE.md")

    # NAT 配置文件名（文档曾写成 nat/eval_config.yml，实际在 nat/configs/ 下）
    nat = "nat/configs/sparkjury_eval.yml"
    results.append(Result("NAT 配置文件名", nat in arch and (ROOT / nat).exists(),
                          f"文档提到 {nat}={nat in arch}；文件存在={(ROOT / nat).exists()}"))

    # vLLM 绑定地址：文档每一处说法都要和启动脚本一致
    sh = _read("deploy/dgx/common.sh")
    m = re.search(r"--host\s+(\S+)", sh)
    host = m.group(1) if m else "?"
    said = re.findall(r"vLLM[^\n]{0,40}?(?:绑|监听)\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", arch)
    results.append(Result("vLLM 绑定地址", bool(said) and all(s == host for s in said),
                          f"common.sh 是 --host {host}；文档说 {said or '没有一处提到绑定地址'}"))

    # badcase 的 safety 阈值（safety 比其它维度严，文档必须写出来）
    bc = _read("src/sparkjury/cluster/badcase.py")
    safety = re.search(r"SAFETY_LOW_SCORE\s*=\s*(\d+)", bc)
    val = safety.group(1) if safety else "?"
    results.append(Result("badcase safety 阈值", f"≤ {val} 分" in arch,
                          f"代码 SAFETY_LOW_SCORE={val}，文档里「≤ {val} 分」{'有' if f'≤ {val} 分' in arch else '没有'}"))
    return results


# --------------------------------------------------------------------------------------
# 声称三：端到端流水线（全档）
# --------------------------------------------------------------------------------------

# 流水线各阶段的验收数字。这里是这些数字唯一的真源：文档不再手抄，只指向本文件。
PIPELINE = {
    "traces": "14",
    "tasks": "6",
    "pass1": "64.3%",
    "pass3": "25.0%",
    "env_failures": 1,
    "to_judges": 13,
    "scored_traces": 13,
    "verdicts": 156,
    "arbitrated_dims": 52,
    "badcases": 5,
    "clusters": 2,
    "failed_traces": 5,
}


def check_pipeline() -> list[Result]:
    """按 docs/ARCHITECTURE.md 的分步命令真跑一遍，逐条断言文档承诺的数字。"""
    tmp = Path(tempfile.mkdtemp(prefix="sparkjury-cert-"))
    db = tmp / "sparkjury.db"
    results: list[Result] = []

    def has(name: str, needle: str, haystack: str) -> None:
        results.append(Result(name, needle in haystack, f"期望输出含 {needle!r}"))

    try:
        rc1 = _run(_cli("ingest", "--path", str(ROOT / "data/samples/tau2_retail_sample.json"),
                        "--source", "tau2", "--db", str(db))).returncode
        rc2 = _run(_cli("ingest", "--path", str(ROOT / "data/samples/otel_sample.json"),
                        "--source", "otel", "--db", str(db))).returncode
        results.append(Result("ingest 两个样本", rc1 == 0 and rc2 == 0, f"tau2 rc={rc1}，otel rc={rc2}"))

        stats = _run(_cli("stats", "--db", str(db))).stdout
        cells = {k: _cell(stats, label) for k, label in
                 (("traces", "traces"), ("tasks", "tasks"),
                  ("pass1", "pass rate (pass^1)"), ("pass3", "pass^3"))}
        want = {k: PIPELINE[k] for k in ("traces", "tasks", "pass1", "pass3")}
        detail = f"实测 {cells}，期望 {want}"
        if cells != want:
            # 表格取数失败时把原始输出带上：跨平台渲染差异（比如 Windows 的 ASCII 边框）
            # 只有看到原样输出才能一眼定位，免得下次又在 CI 里猜。
            detail += f"；stats 原始输出片段：{stats[:240]!r}"
        results.append(Result("stats 数字", cells == want, detail))

        con = sqlite3.connect(db)
        try:
            n_failed = con.execute("select count(*) from traces where success=0").fetchone()[0]
        finally:
            con.close()
        results.append(Result("失败 trace 条数", n_failed == PIPELINE["failed_traces"],
                              f"实测 {n_failed} 条，期望 {PIPELINE['failed_traces']} 条"))

        pre = _run(_cli("precheck", "--db", str(db))).stdout
        has("precheck 环境失败/进裁判",
            f"{PIPELINE['env_failures']} environment failures, {PIPELINE['to_judges']} go to judges", pre)

        score = _run(_cli("score", "--db", str(db))).stdout
        has("score 裁决数",
            f"scored {PIPELINE['scored_traces']} traces, {PIPELINE['verdicts']} verdicts", score)

        arb = _run(_cli("arbitrate", "--db", str(db))).stdout
        has("arbitrate 维度数",
            f"decided {PIPELINE['scored_traces']} traces x {PIPELINE['arbitrated_dims']} dimensions", arb)

        clu = _run(_cli("cluster", "--min-cluster-size", "2", "--db", str(db))).stdout
        has("cluster 聚类数",
            f"clustered {PIPELINE['badcases']} badcases into {PIPELINE['clusters']} cluster(s)", clu)

        rep = _run(_cli("report", "--db", str(db))).stdout
        cards = [ROOT / "runs" / "card" / f"card.{ext}" for ext in ("json", "md", "html")]
        results.append(Result("report 写出三个文件", all(p.exists() for p in cards) and "wrote" in rep,
                              f"runs/card/ 下 {[p.name for p in cards if p.exists()]}"))

        reg = _run(_cli("regress", "--before", str(db), "--after", str(db))).stdout
        has("regress 同库对比 unchanged", "unchanged", reg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    demo = _run(_cli("run", "--demo", "--run-id", "cert-demo")).stdout
    results.append(Result("run --demo 七阶段 ok",
                          demo.count("stage_end") >= 7 and "status: ok" in demo,
                          f"stage_end×{demo.count('stage_end')}，status: ok={'status: ok' in demo}"))
    return results


def check_wrappers() -> list[Result]:
    """六个技能封装必须真的能跑，而不是只通过「能编译」。

    tests/test_m9_skills_nat.py:58 对三个多命令封装直接 skip，理由是「由下面的 import
    与语法检查覆盖」，而下面那个测试只做 py_compile——这三个封装从来没被执行过，其中
    sparkjury-evalset 还要现写临时 TOML，是最容易坏的一个。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sparkjury-cert-skill-"))
    db = tmp / "sparkjury.db"
    results: list[Result] = []

    def wrapper(name: str, *args: str) -> int:
        return _run([sys.executable, str(ROOT / "skills" / name / "scripts" / "run.py"), *args]).returncode

    try:
        sample = str(ROOT / "data/samples/tau2_retail_sample.json")
        calls = [
            ("sparkjury-clean", ("--path", sample, "--source", "tau2", "--db", str(db)), "ingest + precheck"),
            ("sparkjury-evalset", ("--db", str(db), "--run-id", "cert-evalset", "--limit", "3"), "现写临时 TOML 再 run --config"),
            ("sparkjury-score", ("--db", str(db)), "score + arbitrate"),
            ("sparkjury-cluster", ("--db", str(db), "--min-cluster-size", "2"), "cluster"),
            ("sparkjury-report", ("--db", str(db)), "report"),
            ("sparkjury-regress", ("--before", str(db), "--after", str(db)), "regress"),
        ]
        for name, args, what in calls:
            rc = wrapper(name, *args)
            results.append(Result(f"{name} 真跑", rc == 0, f"rc={rc}（{what}）"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="交付验收证书：文档声称 vs 实测")
    ap.add_argument("--fast", action="store_true", help="只查文档与结构，不跑端到端流水线")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = ap.parse_args(argv)

    facts = measure_tests(fast=args.fast)
    results: list[Result] = []
    if facts.error:
        results.append(Result("pytest 实测", False, facts.error))
    else:
        baseline = (f"collected={facts.collected}（快档不执行测试）" if args.fast else
                    f"collected={facts.collected} passed={facts.passed} "
                    f"skipped={facts.skipped} failed={facts.failed}")
        results.append(Result("测试基线", True, baseline))

    results += check_doc_counts(facts, args.fast)
    results += check_module_counts(facts)
    results += check_structure_block()
    results += check_class_bindings()
    results += check_precheck_rules()
    results += check_api_routes()
    results += check_misc_bindings()
    if not args.fast:
        results += check_pipeline()
        results += check_wrappers()

    bad = [r for r in results if not r.ok]
    if args.json:
        print(json.dumps(
            {"passed": len(results) - len(bad), "failed": len(bad),
             "results": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results]},
            ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"{'ok  ' if r.ok else 'FAIL'}  {r.name:<24} {r.detail}")
        print()
        print(f"{len(results) - len(bad)}/{len(results)} 条声称与实测一致"
              + (f"，{len(bad)} 条不符" if bad else "，证书通过"))
        if args.fast:
            print("（快档：跳过了端到端流水线与技能封装，CI 跑全档）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
