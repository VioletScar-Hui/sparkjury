#!/usr/bin/env python3
"""govern.py — pack 生命周期治理 + 决策账本 + 黄金决策池投影（Python 标准库，无第三方依赖）。

职责边界（对应 SKILL.md）：
  * freeze/thaw/verify 一律调用 scripts/pack_freeze.py，不复制其 hash 计算；
    pack 内容文件只读，govern 只写 pack.manifest.json 的治理字段（version/state/history），
    该文件被 pack_freeze.py 排除在 frozen_hash 之外。
  * ledger 只追加不改写：append 按 event_id 幂等去重；校验严格按
    contracts/event-ledger.schema.json 的 if-then 约束。
  * proposals 只产出提案，绝不 apply 任何标准变更。

subcommands:
  freeze    DRAFT -> FROZEN（需 --summary，附 checkpoint 事件）
  thaw      FROZEN -> DRAFT（必须 --reason，先落 decision.thaw_pack 再动作）
  verify    校验当前内容与 frozen_hash 是否一致，结果写 governance_log.json
  bump      版本号 +1（默认 minor）并写 pack.manifest.json 的 history
  ledger    append | query | export
  proposals ledger -> 三种统计投影 -> proposals.json
  history   重建 pack_history.json（投影）

注意：governance_log.json 没有独立 subcommand，由 freeze/thaw/verify 追加写入。

用法示例：
  python3 scripts/govern.py bump --summary "risk 权重调整" --decision-event evt-0007
  python3 scripts/govern.py freeze --summary "risk 0.15->0.25" --decision-event evt-0007
  python3 scripts/govern.py thaw --reason "黄金池信号要求重定 severity" --actor 万凌
  python3 scripts/govern.py verify
  python3 scripts/govern.py ledger append --kind accept_card --actor 万凌 --target card:F02
  python3 scripts/govern.py proposals
  python3 scripts/govern.py --selftest

拆分结构（单文件 600 行上限，.claude/rules/coding.md）：
  _ledger.py      — 账本层：基础 IO + 事件校验 + 幂等追加
  _golden_pool.py — 黄金决策池投影：pack 事实 + override 审计 + 三类统计投影
  _lifecycle.py   — pack 生命周期：freeze/thaw/bump/verify/history + 追加型治理日志
  本文件          — CLI + ledger/proposals 两个 subcommand + 内嵌 selftest。
                    三个模块的公开符号全部 re-export，`import govern` 的既有调用点不变。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# sparkjury 适配（2026-09-27 移植）：eval-agent 的根工具在 scripts/，sparkjury 在 tools/。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

# 拆模块前这两个名字就在入口的符号面上，继续 re-export（实际解析权在 _golden_pool.py）。
from _yaml_lite import load_yaml_file, load_yaml_text  # noqa: E402,F401

# re-export：govern 的公开 API 仍集中在本文件入口，调用方不需要知道拆了几个模块。
from _golden_pool import thin_candidates  # noqa: E402
from _golden_pool import (MIN_SUPPORT, OVERRIDE_TOP_FIELDS, PACK_PREFIX,  # noqa: E402,F401
                         audit_override_payloads, build_proposals, _group_key,
                         project_golden_pool, read_dimensions, read_facts)
from _ledger import (DECISION_KINDS, EVENT_TYPES, RATIONALE_REQUIRED, SKILL,  # noqa: E402,F401
                     append_event, next_event_id, now_iso, read_json,
                     read_ledger, validate_event, write_json)
from _lifecycle import (MANIFEST, PACK_DIR, PACK_FREEZE, action_key,  # noqa: E402,F401
                       cmd_bump, cmd_freeze, cmd_history, cmd_thaw, cmd_verify,
                       log_governance, record_history, require_repo_pack,
                       run_pack_freeze)

DEFAULT_LEDGER = PROJECT_ROOT / "governance" / "events.jsonl"
DEFAULT_OUT_DIR = PROJECT_ROOT / "governance"


# --------------------------------------------------------------------------- subcommands
def cmd_ledger(args) -> int:
    path = Path(args.ledger)
    if args.action == "append":
        events, _ = read_ledger(path)
        payload = {"decision_kind": args.kind, "actor": args.actor, "target": args.target}
        if args.rationale:
            payload["rationale"] = args.rationale
        try:
            payload.update(json.loads(args.payload) if args.payload else {})
        except json.JSONDecodeError as exc:
            print(json.dumps({"event_type": "failed", "skill": SKILL,
                              "reason": f"--payload 不是合法 JSON: {exc}"}, ensure_ascii=False))
            return 2
        ev = {"event_id": args.event_id or next_event_id(events), "ts": now_iso(),
              "run_id": args.run_id, "skill": args.skill, "event_type": "decision",
              "payload": payload}
        result = append_event(path, ev)
        if result == "appended":
            print(f"append {ev['event_id']} decision:{args.kind} target={args.target}")
            return 0
        if result == "exists":
            print(f"{ev['event_id']} 已存在，幂等跳过（ledger 只追加不改写）")
            return 0
        return 2

    events, corrupt = read_ledger(path)
    if args.action == "query":
        def keep(ev):
            p = ev.get("payload") or {}
            if ev.get("event_type") != "decision":
                return False
            if args.kind and p.get("decision_kind") != args.kind:
                return False
            if args.actor and p.get("actor") != args.actor:
                return False
            if args.target and args.target not in str(p.get("target", "")):
                return False
            return True

        hits = [ev for ev in events if keep(ev)][: args.limit]
        print(json.dumps({"total": len(hits), "events": hits}, ensure_ascii=False, indent=2))
        return 0

    export = {"exported_at": now_iso(), "path": str(path), "events": len(events),
              "corrupt_lines": corrupt,
              "by_decision_kind": dict(Counter(
                  (e.get("payload") or {}).get("decision_kind") for e in events
                  if e.get("event_type") == "decision")),
              "by_actor": dict(Counter(
                  (e.get("payload") or {}).get("actor") for e in events
                  if e.get("event_type") == "decision")),
              "by_target_key": dict(Counter(
                  _group_key(str((e.get("payload") or {}).get("target", ""))) for e in events
                  if e.get("event_type") == "decision")),
              "by_event_type": dict(Counter(e.get("event_type") for e in events))}
    if args.out:
        write_json(Path(args.out), export)
        print(f"导出 {args.out}")
    else:
        print(json.dumps(export, ensure_ascii=False, indent=2))
    return 0


def cmd_proposals(args) -> int:
    path = Path(args.ledger)
    events, corrupt = read_ledger(path)
    payload = build_proposals(events, Path(args.pack_dir), path, corrupt)
    if getattr(args, "runs_dir", None):
        # Add/Thin：run 产物里的消费证据 -> 解冻时的 Thin 候选清单（只给方向，不动手）
        payload["thin_candidates"] = thin_candidates(args.runs_dir, args.pack_dir)
    out = Path(args.out) if args.out else Path(args.out_dir) / "proposals.json"
    write_json(out, payload)
    for p in payload["proposals"]:
        print(f"[{p['proposal_id']}] {p['signal']} -> {p['target_file']}:{p['field']} "
              f"current={p['current']} direction={p['direction']} "
              f"support={p['evidence']['support']}")
    print(f"{len(payload['proposals'])} 条提案（status=pending_human_approval，未 apply）-> {out}")
    if corrupt:
        print(f"注意：ledger 有 {corrupt} 行无法解析，已跳过（govern 不修历史）")
    return 0


# --------------------------------------------------------------------------- selftest
def selftest() -> int:
    failures = []

    def check(label, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + label + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ledger = tmpdir / "events.jsonl"
        pack = tmpdir / "scenario-pack"
        pack.mkdir()
        # 最小 pack fixture：只放被测字段（govern 不写内容文件）
        (pack / "pack.manifest.json").write_text(json.dumps(
            {"pack_id": "retail-default", "version": "0.1.0", "state": "FROZEN",
             "frozen_hash": "0" * 64}, ensure_ascii=False), encoding="utf-8")
        (pack / "taxonomy.yaml").write_text(
            "categories:\n  - id: F02\n    default_severity: 2\n"
            "  - id: F08\n    default_severity: 3\n", encoding="utf-8")
        (pack / "rubrics.yaml").write_text(
            "dimensions:\n  - id: outcome\n    weight: 0.4\n", encoding="utf-8")
        before = {p: p.stat().st_mtime_ns for p in pack.rglob("*")}

        fixtures = [
            ("accept_card", "card:F02", {}),
            ("reject_proposal", "card:F02", {"rationale": "F02 的建议这次不适用"}),
            ("rename_cluster", "cluster:8", {"old_label": "未分类", "new_label": "承诺时效误判"}),
            ("rename_cluster", "cluster:11", {"old_label": "未分类", "new_label": "承诺时效误判"}),
            ("override_priority", "priority:F02", {"system_top": "F02", "human_top": "F08"}),
            ("override_priority", "priority:F02", {"system_top": "F02", "human_top": "F08"}),
        ]
        for kind, target, extra in fixtures:
            payload = {"decision_kind": kind, "actor": "万凌", "target": target}
            payload.update(extra)
            ev = {"event_id": next_event_id(read_ledger(ledger)[0]), "ts": now_iso(),
                  "run_id": "governance", "skill": SKILL, "event_type": "decision",
                  "payload": payload}
            res = append_event(ledger, ev)
            check(f"append {kind} -> {target}", res == "appended", res)

        events, corrupt = read_ledger(ledger)
        check("6 条 decision fixture 全部落账", len(events) == 6 and corrupt == 0,
              f"events={len(events)} corrupt={corrupt}")
        dup = dict(events[0])
        dup["event_id"] = events[0]["event_id"]
        check("同 event_id 重复 append 幂等", append_event(ledger, dup) == "exists")
        check("幂等后仍 6 行", len(read_ledger(ledger)[0]) == 6)

        bad = {"event_id": "evt-bad", "ts": now_iso(), "run_id": "governance", "skill": SKILL,
               "event_type": "decision",
               "payload": {"decision_kind": "accept_all", "actor": "万凌", "target": "card:F02"}}
        errs = validate_event(bad)
        check("非法 decision_kind 被拒绝", any("decision_kind" in e for e in errs), str(errs))
        no_reason = {"event_id": "evt-bad2", "ts": now_iso(), "run_id": "governance",
                     "skill": SKILL, "event_type": "decision",
                     "payload": {"decision_kind": "thaw_pack", "actor": "万凌",
                                 "target": "pack:retail-default"}}
        check("thaw_pack 无 rationale 被拒绝",
              any("rationale" in e for e in validate_event(no_reason)))

        payload = build_proposals(events, pack, ledger, corrupt)
        s = payload["summary"]
        check("投影 1 优先级权重校准触发", s["priority_weight_calibration"] >= 1)
        check("投影 2 taxonomy 缺口触发（同名 rename ×2）", s["taxonomy_gap"] >= 1)
        check("投影 3 fixability 先验校准触发", s["fixability_prior_calibration"] >= 1)
        fx = [p for p in payload["proposals"] if p["signal"] == "fixability_prior_calibration"]
        check("投影 3 的证据带 system_top/human_top 与 taxonomy_id",
              fx and all({"system_top", "human_top", "taxonomy_ids"} <= set(p["evidence"])
                         for p in fx),
              json.dumps([p["evidence"] for p in fx], ensure_ascii=False))

        # v0.2 联调补充：report 侧按 card-spec §8 写全 payload 的事件必须能喂饱投影 3；
        # 缺 system_top/human_top 的旧事件不得被None 当键偷偷计成信号，必须在
        # override_audit 里显式曝光 —— 否则"没有信号"和"没采到字段"输出里长得一样。
        full_ev = {"event_id": "evt-full", "ts": now_iso(), "run_id": "governance",
                   "skill": SKILL, "event_type": "decision",
                   "payload": {"decision_kind": "override_priority", "actor": "万凌",
                               "target": "CL-F02", "taxonomy_id": "F02",
                               "to_rank": 2, "system_top": "F02", "human_top": "F04",
                               "rationale": "F04 阻塞主链路"}}
        check("完整 override payload 可被投影 3 消费",
              audit_override_payloads([full_ev])["usable"] == 1)
        check("完整 override payload 的 taxonomy_id 被记录",
              audit_override_payloads([full_ev])["taxonomy_ids"] == ["F02"])
        check("report 完整事件投影 3 生效",
              any(p["signal"] == "fixability_prior_calibration"
                  and p["evidence"]["system_top"] == "F02"
                  and p["evidence"]["human_top"] == "F04"
                  for p in project_golden_pool(
                      events + [full_ev, dict(full_ev, event_id="evt-full-2")],
                      read_facts(pack))),
              "")

        for bad_payload, reason in (
            ({"decision_kind": "override_priority", "actor": "万凌", "target": "CL-F02",
              "taxonomy_id": "F02", "to_rank": 1, "human_top": "F04"},
             "override_payload_missing_system_top"),
            ({"decision_kind": "override_priority", "actor": "万凌", "target": "C03",
              "to_rank": 1, "system_top": "F02"},
             "override_payload_missing_human_top"),
            ({"decision_kind": "override_priority", "actor": "万凌", "target": "C03",
              "to_rank": 1, "system_top": "F02", "human_top": "C03"},
             "override_payload_human_top_not_category_id"),
        ):
            one = {"event_id": "evt-bad-ov", "ts": now_iso(), "run_id": "governance",
                   "skill": SKILL, "event_type": "decision", "payload": bad_payload}
            a = audit_override_payloads([one])
            check(f"{reason} 被计入 unusable", a["unusable"] == 1 and reason in a["reasons"],
                  json.dumps(a, ensure_ascii=False))
            check(f"{reason} 不产出投影 3 提案",
                  not any(p["signal"] == "fixability_prior_calibration"
                          for p in project_golden_pool([one] * MIN_SUPPORT, read_facts(pack))))

        check("build_proposals 带 override_audit",
              payload["override_audit"]["usable"] >= 1
              and isinstance(payload["override_audit"]["reasons"], dict),
              json.dumps(payload["override_audit"], ensure_ascii=False))
        check("空 ledger 的 override_audit 全 0",
              build_proposals([], pack, ledger, 0)["override_audit"]
              == {"usable": 0, "unusable": 0, "reasons": {}, "taxonomy_ids": []})

        check("每条提案都是 pending_human_approval",
              all(p["status"] == "pending_human_approval" for p in payload["proposals"]))
        check("每条提案 proposed 均为 null（govern 不填数字）",
              all(p["proposed"] is None for p in payload["proposals"]))
        check("每条提案都能追溯到 ledger event_id",
              all(p["evidence"]["event_ids"] for p in payload["proposals"]))
        check("提案 target_file 落在 pack 内",
              all(p["target_file"].startswith(PACK_PREFIX + "/") for p in payload["proposals"]))
        check("taxonomy 提案给出下一个可用 F 类 id",
              any(p["signal"] == "taxonomy_gap" and "F13" in p["field"]
                  for p in payload["proposals"]),
              json.dumps([p["field"] for p in payload["proposals"] if p["signal"] == "taxonomy_gap"],
                                 ensure_ascii=False))

        # 黄金池空转时不产出提案
        empty = build_proposals([], pack, ledger, 0)
        check("ledger 为空 -> 0 条提案", empty["proposals"] == [])

        after = {p: p.stat().st_mtime_ns for p in pack.rglob("*")}
        check("govern 提案过程不写 pack 内容文件", before == after)

        # 无理由 thaw 必须拒绝（pack_dir 指向真实 pack，确保拒绝来自理由校验而非路径守卫）
        rc = cmd_thaw(argparse.Namespace(reason="", actor="万凌", ledger=str(ledger),
                                         run_id="governance", out_dir=str(tmpdir),
                                         pack_dir=str(PACK_DIR)))
        check("thaw 无理由 -> 拒绝（退出码 2）", rc == 2, str(rc))
        check("被拒后 pack 仍 FROZEN",
              read_json(pack / "pack.manifest.json")["state"] == "FROZEN")

        # 生命周期命令拒绝非仓库 pack（防止在副本上假冻结、与真实 pack 脱钩）
        foreign = argparse.Namespace(reason="x", actor="万凌", ledger=str(ledger),
                                     run_id="governance", out_dir=str(tmpdir),
                                     pack_dir=str(pack))
        check("thaw 指向副本 pack -> 拒绝", cmd_thaw(foreign) == 2)
        check("freeze 指向副本 pack -> 拒绝",
              cmd_freeze(argparse.Namespace(summary="x", decision_event=None,
                                            run_id="governance", out_dir=str(tmpdir),
                                            pack_dir=str(pack))) == 2)
        check("verify 指向副本 pack -> 拒绝",
              cmd_verify(argparse.Namespace(run_id="governance", out_dir=str(tmpdir),
                                            pack_dir=str(pack))) == 2)
        check("被拒后 ledger 未新增 thaw_pack",
              sum(1 for e in read_ledger(ledger)[0]
                  if (e.get("payload") or {}).get("decision_kind") == "thaw_pack") == 0)

    print(f"\ngovern selftest: {'PASS' if not failures else 'FAIL ' + str(failures)}")
    return 0 if not failures else 1


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="govern — pack 生命周期治理与黄金决策池")
    ap.add_argument("--pack-dir", default=str(PACK_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--run-id", default="governance")
    ap.add_argument("--actor", default="万凌")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("bump", help="版本号 +1 并写 manifest history")
    p.add_argument("--summary", required=True, help="变更摘要（版本链必填）")
    p.add_argument("--decision-event", default=None, help="批准本次变更的 decision 事件 id")
    p.add_argument("--level", choices=["minor", "patch"], default="minor")
    p.set_defaults(func=cmd_bump)

    p = sub.add_parser("freeze", help="DRAFT -> FROZEN")
    p.add_argument("--summary", required=True, help="变更摘要，进 pack_history")
    p.add_argument("--decision-event", default=None)
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("thaw", help="FROZEN -> DRAFT（必须附理由）")
    p.add_argument("--reason", default="", help="解冻理由，必填，落 decision.thaw_pack")
    p.set_defaults(func=cmd_thaw)

    p = sub.add_parser("verify", help="校验 pack_hash 一致性")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("ledger", help="decision ledger append/query/export")
    p.add_argument("action", choices=["append", "query", "export"])
    p.add_argument("--kind", choices=DECISION_KINDS)
    p.add_argument("--actor")
    p.add_argument("--target")
    p.add_argument("--rationale")
    p.add_argument("--event-id")
    p.add_argument("--skill", default=SKILL)
    p.add_argument("--payload", default=None, help="额外 payload 字段（JSON）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--out")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("proposals", help="黄金池三类统计投影 -> proposals.json")
    p.add_argument("--runs-dir", default=None,
                   help="扫描 run 产物目录出 Add/Thin 候选（从未被 badcase 消费过的类/系数）")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_proposals)

    p = sub.add_parser("history", help="重建 pack_history.json")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_history)

    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    if not PACK_FREEZE.exists():
        print(json.dumps({"event_type": "failed", "skill": SKILL,
                          "reason": f"缺 {PACK_FREEZE}"}, ensure_ascii=False))
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
