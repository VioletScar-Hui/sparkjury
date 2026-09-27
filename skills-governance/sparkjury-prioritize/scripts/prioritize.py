#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prioritize.py — prioritize skill 的标准库骨架实现（CLI 入口）。

职责：把 cluster 产出的类排成"先修哪一类"的修复顺序，且只推一个 top_recommendation。

公式（pack thresholds.prioritize，禁止本文件自造权重）：
    priority = frequency_norm × severity_weight × fixability_boost
    frequency_norm   = 类内 badcase 数 / 总 badcase 数
    severity_weight  = severity_weights[str(severity_max)]
    fixability_boost = fixability_boost[category_id]

黄金池 hook：PM 在 ledger 里 decision_kind=override_priority 的人工修正，
下一轮排序必须消费（把该类提升到人工曾给出的名次），并在 rationale 注明来源。

override payload 的权威匹配键（v0.2 联调补充，见 contracts/cross-skill-interfaces.md）：
  `taxonomy_id` = category_id（形如 F04）—— apply_overrides 唯一认的键；
  `target`      = cluster_id（形如 C03，PM 语义， report 侧规定，优先级的兼容路径）；
  `to_rank` / `system_top` / `human_top` = 人工名次与改序前后的 top 类。
两条历史路径都接：只有 target 且形如 F\\d\\d 时按 target 匹配（v0.1 遗物）；
匹配不上一律记进 `override_unmatched` 并在 stderr 打 debug 级说明，**不许静默丢弃**。

用法：
  python3 prioritize.py --clusters clusters.json --thresholds thresholds.yaml \
      --out-dir out/ [--overrides overrides.jsonl] [--total-badcases N] \
      [--run-id R] [--pack-hash H] [--pack DIR] [--ledger L]
  python3 prioritize.py --selftest

------------------------------------------------------------------------------
拆分结构（原单文件 623 行，超出 .claude/rules/coding.md 600 行硬上限后按职责拆出）：

  pack_binding.py  pack 身份绑定 + FROZEN frozen_hash 校验（不复写 hash 算法）
  ranking.py       优先级公式 compute_priority + 排序 rank（含 top_recommendation）
  overrides.py     黄金池 hook：override 事件解析 / 权威匹配键 / apply_overrides
  pipeline.py      run() 编排 + IO 助手（hash / 落盘 / ledger 事件）
  prioritize.py    本文件：CLI 入口 + 上述模块的 re-export（对外 API 不变）

_selftest.py / _fixtures.py 里的 `import prioritize as impl` 与 `impl.xxx(...)`
全部仍然可用 —— 所有公开函数与常量都从原位置 re-export。
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证 `python3 /任意/路径/prioritize.py` 都能 import 同目录 helper（NVIDIA 约定用绝对路径调脚本）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# YAML 读取统一走项目唯一权威实现 tools/_yaml_lite.py（2026-09-26 收敛，删除了本 skill
# 私有的 _miniyaml.py 副本）。本模块内只出现 YamlLiteError 这一种写法，不再设别名 ——
# 别名会让"这里抛的到底是哪个解析器的错"在一次 grep 里看不出来。
# sparkjury 适配（2026-09-27 移植）：eval-agent 的根工具在 scripts/，sparkjury 在 tools/。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from _yaml_lite import YamlLiteError, load_yaml_file  # noqa: E402

from overrides import (  # noqa: E402
    CATEGORY_ID_RE,
    apply_overrides,
    load_overrides,
    override_match_key,
)
from pack_binding import (  # noqa: E402
    FLAG_PACK_HASH_MISSING,
    FLAG_PACK_NOT_FROZEN,
    FLAG_PACK_UNBOUND,
    PACK_MANIFEST_NAME,
    STATE_UNKNOWN,
    load_pack_freeze,
    resolve_pack_binding,
)
from pipeline import (  # noqa: E402
    SKILL_NAME,
    SKILL_VERSION,
    emit,
    now_iso,
    resolve_total_badcases,
    run,
    sha256_obj,
    write_json,
)
from ranking import FIXABILITY_SOURCE, compute_priority, load_thresholds, rank  # noqa: E402

__all__ = [
    # pack 身份绑定
    "PACK_MANIFEST_NAME", "STATE_UNKNOWN", "FLAG_PACK_UNBOUND", "FLAG_PACK_NOT_FROZEN",
    "FLAG_PACK_HASH_MISSING", "load_pack_freeze", "resolve_pack_binding",
    # 公式 / 排序
    "FIXABILITY_SOURCE", "load_thresholds", "compute_priority", "rank",
    # 黄金池 hook
    "CATEGORY_ID_RE", "override_match_key", "load_overrides", "apply_overrides",
    # 编排与 IO
    "sha256_obj", "now_iso", "write_json", "emit", "resolve_total_badcases", "run",
    # 解析器异常（main 捕获它转退出码 3）
    "YamlLiteError", "load_yaml_file",
    # 身份与 CLI
    "SKILL_NAME", "SKILL_VERSION", "main",
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="prioritize skill — 修复顺序排序（只推一个先修）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--clusters", type=Path)
    ap.add_argument("--thresholds", type=Path)
    ap.add_argument("--overrides", type=Path, help="ledger JSONL，供 load_overrides 筛 override_priority")
    ap.add_argument("--total-badcases", type=int, default=None,
                    help="frequency_norm 分母；缺省自动读同目录 cluster_report.json 的 total_badcases")
    ap.add_argument("--out-dir", type=Path, default=Path("out"))
    ap.add_argument("--run-id")
    ap.add_argument("--pack-hash", help="pack 内容 sha256；--pack 已给时缺省自动算")
    ap.add_argument("--pack", type=Path, default=None,
                    help="pack 目录（含 pack.manifest.json）；给了就从 manifest 读 "
                         "pack_id/version/state，并校验 FROZEN 的 frozen_hash")
    ap.add_argument("--pack-id", default=None, help="无 --pack 时的显式 pack_id（否则记 null）")
    ap.add_argument("--pack-version", default=None, help="无 --pack 时的显式 pack version（否则记 null）")
    ap.add_argument("--ledger")
    args = ap.parse_args(argv)

    if args.selftest:
        # 自检逻辑独立在 _selftest.py；这里只做转发，避免实现与验证互相耦合
        from _selftest import selftest
        return selftest()
    if args.pack and args.thresholds is None:
        args.thresholds = args.pack / "thresholds.yaml"
    missing = [n for n, v in (("--clusters", args.clusters), ("--thresholds", args.thresholds))
               if v is None]
    if missing:
        print("FATAL: 缺参数 " + ", ".join(missing) + "（或用 --selftest）", file=sys.stderr)
        return 2
    try:
        return run(args.clusters, args.thresholds, args.out_dir, args.run_id,
                   args.pack_hash, args.ledger, args.overrides, args.total_badcases,
                   args.pack, args.pack_id, args.pack_version)
    except (YamlLiteError, ValueError, json.JSONDecodeError) as e:
        print(f"FATAL: 输入/pack 不可用，未做任何修补：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
