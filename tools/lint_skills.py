#!/usr/bin/env python3
"""lint_skills.py — skill 库结构 linter（对齐 NVIDIA/skills 官方准入结构）。

检查每个 skills/<name>/：
  SKILL.md       frontmatter 含 name/description/license/metadata/allowed-tools；description ≥ 80 字符（官方风格：穷举触发场景）
  skill-card.md  含官方模板必需小节
  schemas/       ≥1 个可解析 JSON 且含 $schema
  evals/evals.json  可解析数组；每条含 id/question/expected_skill/ground_truth/expected_behavior；≥3 条
  references/    非空
  BENCHMARK.md   存在且含通过标准段落
  scripts/       可选（标记 optional）
用法：python3 scripts/lint_skills.py [--skills-dir skills]
退出码：0 全通过；1 有 FAIL。
"""
import json
import subprocess
import sys
from pathlib import Path

REQUIRED_FRONTMATTER = ["name", "description", "license", "metadata", "allowed-tools"]
REQUIRED_CARD_SECTIONS = [
    "Description", "Owner", "License", "Use Case", "Requirements",
    "Known Risks", "Reference(s)", "Skill Output", "Evaluation Agents Used",
]
REQUIRED_EVAL_KEYS = ["id", "question", "expected_skill", "ground_truth", "expected_behavior"]


def parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm, closed = {}, False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    fm["_closed"] = closed
    return fm


def lint_skill(skill_dir: Path) -> list:
    errs = []
    name = skill_dir.name

    # 编译产物门禁：__pycache__/*.pyc 是二进制文件，SkillSpector 会按 SC8/SC9 判
    # 二进制可执行文件直接把 skill 打成 CRITICAL（2026-09-26 两次踩中）。.gitignore
    # 只防 git 提交，不防本地扫描；在此处硬拦。
    pyc = [p for p in skill_dir.rglob("*.pyc")] + [p for p in skill_dir.rglob("__pycache__") if p.is_dir()]
    if pyc:
        errs.append(f"发现编译产物 {len(pyc)} 处（__pycache__/*.pyc）——先删再提交/扫描：find {skill_dir} -name __pycache__ -exec rm -rf {{}} +")

    sk = skill_dir / "SKILL.md"
    if not sk.exists():
        errs.append("缺 SKILL.md")
    else:
        fm = parse_frontmatter(sk.read_text(encoding="utf-8"))
        if not fm.get("_closed"):
            errs.append("SKILL.md frontmatter 未闭合")
        for k in REQUIRED_FRONTMATTER:
            if k not in fm:
                errs.append(f"SKILL.md frontmatter 缺字段 {k}")
        desc = fm.get("description", "")
        if len(desc) < 80:
            errs.append(f"description 仅 {len(desc)} 字符（<80，触发场景写得不够）")
        if fm.get("name") and fm["name"] != name:
            errs.append(f"frontmatter name={fm['name']} 与目录名 {name} 不一致")

    card = skill_dir / "skill-card.md"
    if not card.exists():
        errs.append("缺 skill-card.md")
    else:
        ctext = card.read_text(encoding="utf-8")
        for sec in REQUIRED_CARD_SECTIONS:
            if sec not in ctext:
                errs.append(f"skill-card.md 缺小节 {sec}")

    schemas = list((skill_dir / "schemas").glob("*.json")) if (skill_dir / "schemas").is_dir() else []
    if not schemas:
        errs.append("schemas/ 无 JSON")
    else:
        for s in schemas:
            try:
                payload = json.loads(s.read_text(encoding="utf-8"))
                if "$schema" not in payload:
                    errs.append(f"{s.name} 缺 $schema 字段")
            except json.JSONDecodeError as e:
                errs.append(f"{s.name} 不是合法 JSON: {e}")

    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.exists():
        errs.append("缺 evals/evals.json")
    else:
        try:
            evals = json.loads(evals_path.read_text(encoding="utf-8"))
            if not isinstance(evals, list) or len(evals) < 3:
                errs.append("evals.json 少于 3 条用例")
            else:
                for i, e in enumerate(evals):
                    for k in REQUIRED_EVAL_KEYS:
                        if k not in e:
                            errs.append(f"evals[{i}] 缺字段 {k}")
                    if e.get("expected_skill") not in (name, None):
                        errs.append(f"evals[{i}] expected_skill={e.get('expected_skill')} 应为 {name} 或 null")
        except json.JSONDecodeError as e:
            errs.append(f"evals.json 不是合法 JSON: {e}")

    refs = list((skill_dir / "references").glob("*")) if (skill_dir / "references").is_dir() else []
    if not refs:
        errs.append("references/ 为空")

    bench = skill_dir / "BENCHMARK.md"
    if not bench.exists():
        errs.append("缺 BENCHMARK.md")
    elif "通过标准" not in bench.read_text(encoding="utf-8") and "Pass" not in bench.read_text(encoding="utf-8"):
        errs.append("BENCHMARK.md 未写通过标准")

    return errs



def repo_checks(root: Path) -> list:
    """仓库级门禁（--repo-only）：与 skill 文件结构无关，任何状态下可跑。

    1. 编译产物：Python 运行再生的 .pyc 会被外部安全扫描器判为二进制可执行文件，
       直接把整个 skill 目录打成 CRITICAL。.gitignore 只防 git 提交，不防扫描。
    2. 禁入路径：.env / node.env / runs/ / *.db / logs/ / .worktrees/ 被 git 跟踪。
    """
    errs: list = []
    banned = (".env", "node.env", "runs/", "logs/")
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except Exception as e:  # 非 git 环境不硬失败
        return [f"repo: git ls-files 不可用（{e}），跳过禁入路径检查"]
    for f in tracked:
        if f.endswith((".pyc", ".db")) or "__pycache__/" in f:
            errs.append(f"禁入路径被跟踪: {f}")
        elif f.endswith("node.env") or f.endswith("/.env") or f == ".env":
            errs.append(f"禁入路径被跟踪: {f}")
        elif f.startswith(banned):
            errs.append(f"禁入路径被跟踪: {f}")
        elif f.startswith(".worktrees/"):
            errs.append(f"禁入路径被跟踪: {f}")
    for d in ("skills", "tools", "scripts", "src", "tests"):
        base = root / d
        if not base.is_dir():
            continue
        n = sum(1 for _ in base.rglob("*.pyc")) + sum(1 for _ in base.rglob("__pycache__"))
        if n:
            errs.append(f"{d}/ 下有 {n} 处编译产物（__pycache__/*.pyc）：find {d} -name __pycache__ -exec rm -rf {{}} +")
    return errs


def main() -> int:
    if "--repo-only" in sys.argv:
        # 仓库级门禁：pyc / 禁入路径。不检查 skill 文件结构，任何状态下可跑。
        errs = repo_checks(Path(".").resolve())
        for e in errs:
            print(f"    - {e}")
        print(f"repo checks: {'PASS' if not errs else str(len(errs)) + ' FAIL'}")
        return 1 if errs else 0
    base = Path(sys.argv[sys.argv.index("--skills-dir") + 1]) if "--skills-dir" in sys.argv else Path("skills")
    only = None
    if "--only" in sys.argv:
        only = {s.strip() for s in sys.argv[sys.argv.index("--only") + 1].split(",") if s.strip()}
    if not base.is_dir():
        print(f"FATAL: {base} 不存在"); return 1
    # 兼容两种调用：指向单个 skill 目录（含 SKILL.md）或 skill 的父目录
    if (base / "SKILL.md").exists():
        skills = [base]
    else:
        skills = sorted(d for d in base.iterdir()
                        if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").exists()
                        and (only is None or d.name in only))
    total_fail = 0
    if only is not None:
        missing = only - {d.name for d in skills}
        if missing:
            print(f"FATAL: --only 指定了不存在的 skill: {sorted(missing)}")
            return 1
    for d in skills:
        errs = lint_skill(d)
        status = "PASS" if not errs else "FAIL"
        if errs:
            total_fail += 1
        print(f"[{status}] {d.name}")
        for e in errs:
            print(f"    - {e}")
    print(f"\n{len(skills)} 个 skill，{total_fail} 个 FAIL")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
