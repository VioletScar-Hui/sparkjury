#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clarify.py — 澄清期标准编译骨架（Python 标准库，无第三方依赖）。CLI 入口。

职责边界（对应 SKILL.md）：
  * 只读 scenario-pack/，产出 pack_diff 提案；不写 pack、不冻结、不写 checker 实现。
  * 追问预算 QUESTION_BUDGET=5，超预算强制收敛到默认值 + pending_items。
  * current 值一律由下方行扫描器从 pack 读出，扫不到即失败，禁止凭记忆填写。

用法：
  python3 scripts/clarify.py --questions                 # 打印五问 + 各问默认值
  python3 scripts/clarify.py --answers answers.json --source-text "..." --out session.json
  python3 scripts/clarify.py --selftest                 # 内嵌模拟问答 → 提案 fixture 自检

answers.json 允许两种形状：
  {"Q1": "客服退款 agent", "Q2": {"kind": "explicit", "weights": {...}}}
  [{"id": "Q1", "answer": "..."}]

------------------------------------------------------------------------------
拆分结构（原单文件 625 行，超出 .claude/rules/coding.md 600 行硬上限后按职责拆出）：

  _contracts.py   契约常量（QUESTION_BUDGET / MIN_SUPPORT / IMPACT 影响表）+ 异常 + 值转换
  pack_scan.py     scenario-pack 只读扫描：rubrics / taxonomy / thresholds → 事实表
  proposal.py     五问构造 + 答案编译成 pack_diff / checker 契约 / pending_items
  _selftest.py    内嵌模拟问答 → 提案 fixture 自检
  clarify.py      本文件：CLI 入口 + 上述模块的 re-export（对外 API 不变）

YAML 读取统一走项目唯一权威实现 tools/_yaml_lite.py，收敛过程见 pack_scan.py 顶部说明。
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证 `python3 /任意/路径/clarify.py` 与 `import clarify` 都能找到同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _contracts import (  # noqa: E402
    DEFAULT_PASS_K,
    DEFAULT_PRIMARY_METRIC,
    DEFAULT_VETO_HINTS,
    DIMENSIONS,
    IMPACT,
    MIN_SUPPORT,
    QUESTION_BUDGET,
    PackReadError,
    _to_int,
)
from _selftest import _fixture_pack, selftest  # noqa: E402
from pack_scan import (  # noqa: E402
    _doc,
    project_root,
    read_list_items_by_id,
    read_manifest,
    read_pack,
    read_rubric_dimensions,
    scan_dict_scalars,
)
from proposal import (  # noqa: E402
    _contract_id,
    _diff,
    _kind_of,
    build_questions,
    compile_proposal,
    run_session,
)

SKILL = "clarify"
VERSION = "0.1.0"

__all__ = [
    # 契约常量（_contracts.py 定义，经本入口 re-export，`clarify.MIN_SUPPORT` 仍可用）
    "QUESTION_BUDGET", "MIN_SUPPORT", "DIMENSIONS", "IMPACT",
    "DEFAULT_VETO_HINTS", "DEFAULT_PASS_K", "DEFAULT_PRIMARY_METRIC",
    "PackReadError", "_to_int",
    # pack 读取层
    "project_root", "read_manifest", "_doc", "read_rubric_dimensions",
    "read_list_items_by_id", "scan_dict_scalars", "read_pack",
    # 提案编译层
    "build_questions", "_diff", "_kind_of", "_contract_id",
    "compile_proposal", "run_session",
    # CLI 输入适配
    "normalize_answers", "golden_support_from",
    # 自检与离线 fixture
    "selftest", "_fixture_pack",
    # CLI
    "SKILL", "VERSION", "main",
]


def normalize_answers(payload) -> tuple:
    """返回 (answers dict, prior_answered ids)。"""
    prior = []
    if isinstance(payload, dict) and "answers" in payload:
        payload = payload["answers"]
    if isinstance(payload, list):
        answers = {}
        for item in payload:
            qid = item.get("id")
            ans = item.get("answer", item)
            answers[qid] = ans
            if item.get("already_answered"):
                prior.append(qid)
        return answers, prior
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if k.startswith("Q")}, prior
    raise ValueError("answers 形状不支持：应为 dict 或 list")


def golden_support_from(path) -> int:
    if not path:
        return 0
    p = Path(path)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    items = data.get("proposals", data) if isinstance(data, dict) else data
    return len(items) if isinstance(items, list) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="clarify 澄清期标准编译骨架")
    ap.add_argument("--pack-dir", default=None,
                    help="scenario pack 路径（只读；sparkjury 仓库内默认 standards/scenario-pack/）")
    ap.add_argument("--answers", default=None, help="answers.json 路径")
    ap.add_argument("--source-text", default="", help="用户自然语言原文")
    ap.add_argument("--golden-proposals", default=None, help="govern 的 proposals.json 路径")
    ap.add_argument("--out", default="clarification_session.json", help="输出路径（不写 scenario pack/）")
    ap.add_argument("--prior-answered", default="", help="逗号分隔的、用户已自发答出的问题 id")
    ap.add_argument("--questions", action="store_true", help="只打印五问与默认值")
    ap.add_argument("--selftest", action="store_true", help="内嵌模拟问答 → 提案 fixture 自检")
    args = ap.parse_args(argv)

    # sparkjury 适配：pack 在 standards/scenario-pack/（eval-agent 原态在根 scenario-pack/）。
    pack_dir = Path(args.pack_dir) if args.pack_dir else project_root() / "standards" / "scenario-pack"

    if args.selftest:
        return selftest(pack_dir)

    if args.questions:
        pack = read_pack(pack_dir)
        print(json.dumps(build_questions(pack), ensure_ascii=False, indent=2))
        return 0

    if not args.answers:
        ap.error("需要 --answers PATH（或使用 --questions / --selftest）")

    try:
        pack = read_pack(pack_dir)
    except PackReadError as exc:
        print(json.dumps({"event_type": "failed", "skill": SKILL, "reason": str(exc)},
                         ensure_ascii=False))
        return 2

    answers_payload = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    answers, prior = normalize_answers(answers_payload)
    prior += [q.strip() for q in args.prior_answered.split(",") if q.strip()]
    session = run_session(
        args.source_text, answers, pack,
        prior_answered=prior,
        golden_support=golden_support_from(args.golden_proposals),
    )
    out = Path(args.out)
    out.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    d = session["proposal"]["pack_diff"]
    print(f"{SKILL} v{VERSION}: pack={session['provenance']['pack_id']} "
          f"state={session['provenance']['pack_state']} -> {len(d)} 条 diff, "
          f"{len(session['pending_items'])} 项 pending, status={session['proposal']['status']}")
    print(f"写出 {out}（未改 scenario pack/，未冻结）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
