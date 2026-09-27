#!/usr/bin/env python3
"""Thin wrapper for the prioritize skill: execs the skill's own main script.

用法：python3 scripts/run.py [CLI options...]        # 透传给 prioritize.py
      python3 scripts/run.py --selftest              # 移植正确性自检
      python3 scripts/run.py --help                  # 退出码 0

失败类修复优先级排序（priority formula + 黄金池 override hook）。主脚本即移植实现，本文件只做定位与转交，不重复任何逻辑；
根工具（_yaml_lite / pack_freeze）由主脚本自行按仓库位置解析（tools/）。
"""
import runpy
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent / "prioritize.py"

if __name__ == "__main__":
    if not MAIN.is_file():
        print(f"FATAL: 缺主脚本 {MAIN}", file=sys.stderr)
        sys.exit(2)
    # argv[0] 换成正主脚本，让 --help / 用法串里显示的是它而不是本包装
    sys.argv[0] = str(MAIN)
    runpy.run_path(str(MAIN), run_name="__main__")
