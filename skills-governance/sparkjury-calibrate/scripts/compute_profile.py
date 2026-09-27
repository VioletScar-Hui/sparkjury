#!/usr/bin/env python3
"""compute_profile.py — calibrate skill 的骨架实现（CLI 入口）。

职责：读入三裁判在 calibration 集上的试打分与金标，逐 (裁判 × 维度) cell 算可靠性指标，
按 gate-policy.md 的顺序规则判 PASS/DEGRADE/FAIL/UNKNOWN，排出 lead judge，
产出只提案不生效的 judges.yaml override 提案。

设计约束（与 SKILL.md 一致）：
  * 纯函数：(votes, golds, pack) -> payload。同输入两次运行 payload hash 必须一致（--selftest 断言）。
  * 不调用任何模型。三裁判跑在本地 vLLM 端点，本脚本只消费已产出的投票 JSON。
  * 不自行改 rubric / prompt / judges.yaml，不删 panel 成员 —— 一律走 proposals[]。
  * 不自造阈值：门禁阈值只从 pack thresholds.calibrate.* 取，缺失即 UNKNOWN + 告警 + 提案。

用法：
  python3 compute_profile.py --votes votes.json --gold gold.json --output judge_profile.json \
      [--calibration-set set.json] [--thresholds pack_thresholds.json] \
      [--judged-agent agent.json] [--ledger ledger.jsonl]
  python3 compute_profile.py --selftest

退出码：0 成功；1 画像自检失败；2 参数/输入文件问题；3 前置校验失败（同族红线 / schema 不符）。

拆分结构（单文件 600 行上限，.claude/rules/coding.md）：
  _contracts.py   — 输入契约层：异常分级 / 序列化 / YAML 与阈值读取 / event ledger 事件
  _metrics.py     — 指标层：六个可靠性指标 + 主票筛选 + 四维量表定义
  _gates.py       — 门禁层：四态判定 + 画像渲染 + lead judge 排序 + 提案
  _profile.py     — 画像构造层：输入校验 + 同族红线 + build_profile 纯函数入口
  _selftest.py    — `--selftest` 的 6 票对 4 金标 fixture 与断言
  本文件          — CLI + pack 元信息读取 + envelope 组装。
                    以上模块的公开符号全部 re-export，`import compute_profile` 的既有调用点不变。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _contracts import (  # noqa: E402
    ProfileError,
    PreflightError,
    UsageError,
    append_ledger,
    canonical_hash,
    ledger_events,
    load_thresholds,
    load_yaml_file,
    mean,
    percentile,
    read_json,
    utc_now,
    variance,
)
from _gates import (DEFAULT_MIN_N_PER_CELL, _gate_verdict, _proposals,  # noqa: E402,F401
                    _reason, render_profile)
from _metrics import (DIMENSIONS, _primary_votes, _round_or_none,  # noqa: E402,F401
                      agreement_with_gold, agreement_within_1,
                      order_swap_conflict_rate, repeat_agreement)
from _profile import _validate, build_profile, check_same_family  # noqa: E402,F401
import _selftest  # noqa: E402

SKILL = "calibrate"
SKILL_VERSION = "0.1.0"
TRACE_VERSION = "trace.v1"

SOP_STEPS = [
    "validate_inputs", "same_family_red_line", "match_votes_to_gold",
    "cell_metrics", "gate_verdicts", "lead_judge_ranking",
    "proposals", "write_output",
]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _pack_meta(pack_path: Path | None) -> tuple:
    """读 pack.manifest.json 的 (pack_id, version, pack_hash)。

    读不到返回 (None, None, None)（不猜）：manifest 的 pack 段宁可得 null，
    也不能写一个硬编码的 pack_id/version 冒充"绑过 pack"。
    """
    if not pack_path or not pack_path.is_dir():
        return None, None, None
    f = pack_path / "pack.manifest.json"
    if not f.is_file():
        return None, None, None
    try:
        manifest = read_json(f)
    except UsageError:
        return None, None, None
    return manifest.get("pack_id"), manifest.get("version"), manifest.get("frozen_hash")


def _pack_forbidden_families(pack_path: Path | None) -> list:
    """从 pack judges.yaml 读 judged_agent_forbidden_families。读不到返回空（不猜）。

    2026-09-26：原来这里直接 `import yaml` 且 ImportError 时**静默跳过同族红线**——
    "红线未生效"只打一条 WARN 就继续跑，等于没 PyYAML 的环境里这道门自动失效。
    改走权威实现 tools/_yaml_lite.py 后读法与其他 skill 一致，也不再依赖 PyYAML。
    """
    if not pack_path or not pack_path.is_dir():
        return []
    f = pack_path / "judges.yaml"
    if not f.is_file():
        return []
    data = load_yaml_file(f)
    return list(data.get("judged_agent_forbidden_families") or [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="calibrate — 裁判可靠性画像与上岗门禁")
    ap.add_argument("--votes", type=Path, help="三裁判试打分 JSON")
    ap.add_argument("--gold", type=Path, help="金标 JSON")
    ap.add_argument("--output", type=Path, help="输出 judge_profile.json")
    ap.add_argument("--calibration-set", type=Path, help="evalset 的 sets.calibration")
    ap.add_argument("--thresholds", type=Path, help="pack 阈值（JSON，或需 PyYAML 的 YAML）")
    ap.add_argument("--judged-agent", type=Path, help="被评 agent 身份 JSON（同族红线用）")
    ap.add_argument("--judges-panel", type=Path, help="pack judges.yaml 的 judges[]")
    ap.add_argument("--pack", type=Path, default=None,
                    help="scenario pack 目录（可选，绑 pack_hash 与同族清单；"
                         "sparkjury 仓库内即 standards/scenario-pack/）")
    ap.add_argument("--ledger", type=Path, help="event ledger JSONL 追加路径")
    ap.add_argument("--selftest", action="store_true", help="运行内嵌自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest.run()

    missing = [n for n in ("votes", "gold", "output") if getattr(args, n) is None]
    if missing:
        print(f"FATAL: 缺参数 {missing}（或用 --selftest）")
        return 2

    try:
        votes_payload = read_json(args.votes)
        votes = (votes_payload.get("votes")
                 if isinstance(votes_payload, dict) and isinstance(votes_payload.get("votes"), list)
                 else votes_payload)
        swap_runs = (votes_payload.get("swap_runs")
                     if isinstance(votes_payload, dict) and isinstance(votes_payload.get("swap_runs"), list)
                     else [])
        golds = read_json(args.gold, ("golds", "gold"))
        calibration_set = (read_json(args.calibration_set, ("calibration", "entries"))
                           if args.calibration_set else [])
        thresholds = load_thresholds(args.thresholds)
        judged_agent = read_json(args.judged_agent) if args.judged_agent else None
        judges_panel = read_json(args.judges_panel, ("judges",)) if args.judges_panel else []
        check_same_family(judged_agent, judges_panel, _pack_forbidden_families(args.pack))
        pack_id, pack_version, pack_hash = _pack_meta(args.pack)
        profile = build_profile({"votes": votes, "golds": golds, "calibration_set": calibration_set,
                                "swap_runs": swap_runs, "thresholds": thresholds,
                                "judges_panel": judges_panel, "pack_hash": pack_hash})
        digest = canonical_hash(profile)
        # 未传 --pack 时画像未绑 pack：manifest 记 null + degraded flag，不硬编码冒充
        envelope_flags = list(profile["degraded_flags"])
        if not pack_hash and "calibrate_pack_unbound" not in envelope_flags:
            envelope_flags.append("calibrate_pack_unbound")
        envelope = {
            "manifest": {
                "run_id": "calibrate-local",
                "started_at": utc_now(),
                "ended_at": None,
                "trace_version": TRACE_VERSION,
                "pack": {"pack_id": pack_id, "version": pack_version,
                         "pack_hash": pack_hash,
                         "state_at_run": "FROZEN" if pack_hash else None},
                "skill_versions": {SKILL: SKILL_VERSION},
                "model_versions": {str(j.get("id")): str(j.get("model"))
                                   for j in judges_panel if j.get("id")},
                "outputs": [{"skill": SKILL, "artifact": "judge_profile.json", "hash": digest,
                             "records": len(profile["gates"])}],
                "degraded_flags": envelope_flags,
                "ledger_path": str(args.ledger) if args.ledger else None,
            },
            "skill": {"name": SKILL, "version": SKILL_VERSION, "pack_hash": pack_hash},
            "payload": profile,
            "degraded_flags": envelope_flags,
        }
    except (UsageError, PreflightError) as exc:
        print(f"FATAL: {exc}")
        return 3 if isinstance(exc, PreflightError) else 2
    except ProfileError as exc:
        print(f"FAIL: {exc}")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.ledger:
        append_ledger(args.ledger, ledger_events(
            "calibrate-local", SKILL, SOP_STEPS, "judge_profile.json", digest,
            {"pack_hash": pack_hash, "judges": [j.get("id") for j in judges_panel]}))

    tally: dict = {}
    for g in profile["gates"]:
        tally[g["verdict"]] = tally.get(g["verdict"], 0) + 1
    print(f"OK {args.output}")
    print(f"   gates={tally} cells={profile['coverage']['cells_total']} "
          f"matched={profile['coverage']['votes_matched_to_gold']}/{profile['coverage']['votes_total']}")
    print(f"   degraded_flags={profile['degraded_flags']}")
    print(f"   payload_hash={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
