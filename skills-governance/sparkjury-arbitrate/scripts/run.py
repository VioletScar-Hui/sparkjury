#!/usr/bin/env python3
"""Thin wrapper: runs the SparkJury CLI for this skill. Extra arguments are passed through.

Usage: python scripts/run.py [CLI options...]
Requires the `sparkjury` package (uv sync in the SparkJury repo, or pip install -e <repo>).
"""
import pathlib
import shutil
import subprocess
import sys

# Windows 控制台默认编码非 UTF-8（cp1252/GBK），本 skill 的中文输出会抛
# UnicodeEncodeError。统一强制 UTF-8（errors=replace 保底），对 Unix 无副作用。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import tempfile

MODE = "arbitrate"


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
        keep, skip = [], {"--dims", "--limit", "--workers"}
        i = 0
        while i < len(argv):
            if argv[i] in skip:
                i += 2
                continue
            keep.append(argv[i])
            i += 1
        return cli("arbitrate", *keep)
    if MODE == "evalset":
        args = dict(zip(argv[::2], argv[1::2]))
        db = args.get("--db", "runs/sparkjury.db")
        run_id = args.get("--run-id", "evalset")
        lines = [f'run_id = "{run_id}"', f'db = "{db}"', 'stages = ["EVALSET"]', "[evalset]"]
        if "--limit" in args:
            lines.append(f"limit = {int(args['--limit'])}")
        if "--task-ids" in args:
            lines.append("task_ids = [" + ", ".join(f'"{t}"' for t in args["--task-ids"].split(",")) + "]")
        p = pathlib.Path(tempfile.mkdtemp()) / "evalset.toml"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cli("run", "--config", str(p))
    return cli(MODE, *argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
