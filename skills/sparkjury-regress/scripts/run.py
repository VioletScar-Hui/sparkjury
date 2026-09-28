#!/usr/bin/env python3
"""Thin wrapper: runs the SparkJury CLI for this skill. Extra arguments are passed through.

Usage: python scripts/run.py [CLI options...]
Requires the `sparkjury` package (uv sync in the SparkJury repo, or pip install -e <repo>).
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

MODE = "regress"


def toml_str(s: str) -> str:
    """把字符串写成合法的 TOML 基本字符串。

    Windows 路径里全是反斜杠，直接拼引号会写出非法的转义序列，TOML 解析当场失败。
    借 json.dumps 来转义：它产出的转义写法是 TOML 基本字符串接受的子集，而且这样
    源码里不需要出现反斜杠字面量——本文件是生成封装脚本的模板，反斜杠要在这里过两层
    转义，能不写就不写。

    这个封装在 Windows 上从来没跑通过，原因就是它从没被真的执行过
    （tests/test_m9_skills_nat.py 对多命令封装只做编译检查，编译通过不等于跑得通）。
    """
    return json.dumps(s, ensure_ascii=False)


def cli(*args: str) -> int:
    exe = shutil.which("sparkjury")
    cmd = [exe, *args] if exe else [sys.executable, "-m", "sparkjury.cli", *args]
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd)


def main(argv: list[str]) -> int:
    if MODE == "ingest+precheck":
        db = argv[argv.index("--db") + 1] if "--db" in argv else "runs/sparkjury.db"
        rc = cli("ingest", *argv)
        return rc or cli("precheck", "--db", db)
    if MODE == "score+arbitrate":
        rc = cli("score", *argv)
        if rc:
            return rc
        # arbitrate 只认 score 打完后的分歧，score 专属选项不能透传：
        # 带值的跳两格，布尔开关跳一格（按两格跳会吞掉后面的参数）。
        keep, skip2, skip1 = [], {"--dims", "--limit", "--workers", "--evalset"}, {"--allow-self-judge"}
        i = 0
        while i < len(argv):
            if argv[i] in skip2:
                i += 2
                continue
            if argv[i] in skip1:
                i += 1
                continue
            keep.append(argv[i])
            i += 1
        return cli("arbitrate", *keep)
    if MODE == "evalset":
        args = dict(zip(argv[::2], argv[1::2]))
        db = args.get("--db", "runs/sparkjury.db")
        run_id = args.get("--run-id", "evalset")
        lines = [f"run_id = {toml_str(run_id)}", f"db = {toml_str(db)}", 'stages = ["EVALSET"]', "[evalset]"]
        if "--limit" in args:
            lines.append(f"limit = {int(args['--limit'])}")
        if "--task-ids" in args:
            lines.append("task_ids = [" + ", ".join(toml_str(t) for t in args["--task-ids"].split(",")) + "]")
        p = pathlib.Path(tempfile.mkdtemp()) / "evalset.toml"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cli("run", "--config", str(p))
    return cli(MODE, *argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
