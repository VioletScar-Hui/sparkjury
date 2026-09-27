#!/usr/bin/env python3
"""_lifecycle.py — govern 的 pack 生命周期动作层。

从 govern.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块只管改 pack 状态的动作：bump / freeze / thaw / verify / history。
冻结校验一律调用 tools/pack_freeze.py，**不复制其 hash 计算**：
两份算法一旦漂移，"pack 没被改过"这个断言失效。

两条不变量：
  1. 路径守卫 —— 这些动作只作用于 pack_freeze.py 实际管辖的 <repo>/standards/scenario-pack/。
     在副本上"冻结"会得到一份与真实 pack 无关的 hash，静默错位比报错危险。
  2. 追加型日志 —— governance_log.json / pack_history.json 均按 action_key 幂等，
     既有条目不改写（真相源是 events.jsonl + pack.manifest.json）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from _golden_pool import PACK_PREFIX  # noqa: F401  — pack 在仓库内的相对路径前缀
from _ledger import (SKILL, append_event, next_event_id, now_iso,
                     read_json, read_ledger, write_json)

# sparkjury 适配（2026-09-27 移植）：
#   * pack 从根 scenario-pack/ 迁到 standards/scenario-pack/（与 tools/pack_freeze.py 的
#     "先存在者优先" 解析一致，两可时取 standards/）；
#   * 根工具从 scripts/ 迁到 tools/。
# 路径守卫的语义不变：仍然只认 pack_freeze.py 实际管辖的那一份 pack，副本一律拒绝。
PACK_DIR = Path(__file__).resolve().parents[3] / "standards" / "scenario-pack"
MANIFEST = PACK_DIR / "pack.manifest.json"
PACK_FREEZE = Path(__file__).resolve().parents[3] / "tools" / "pack_freeze.py"


# --------------------------------------------------------------------------- pack_freeze 调用
def run_pack_freeze(*args) -> tuple:
    proc = subprocess.run([sys.executable, str(PACK_FREEZE), *args],
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def log_governance(out_dir: Path, entry: dict) -> None:
    """governance_log.json：追加型数组，按 action_key 幂等；既有条目不改写。"""
    path = out_dir / "governance_log.json"
    log = read_json(path) if path.exists() else []
    if not isinstance(log, list):
        log = []
    if not any(e.get("action_key") == entry.get("action_key") for e in log):
        log.append(entry)
        write_json(path, log)


def record_history(out_dir: Path, entry: dict) -> None:
    """pack_history.json：追加型 entries，按 action_key 幂等；既有条目不改写。

    形状与 cmd_history 的重建结果一致（`{pack_id, current_version, current_state,
    frozen_hash, entries[]}`，即 schemas/io.schema.json 的 output.pack_history）。
    曾写成裸数组：既违反自己的输出契约，也让 `history` 重建过一次之后的文件再也无法
    追加——读取时按 dict 的 key 迭代，直接 AttributeError，freeze/thaw 会在 pack 状态
    已改变之后才崩，留下没有 history/log 的悬空动作。
    """
    path = out_dir / "pack_history.json"
    history = read_json(path) if path.exists() else {}
    if isinstance(history, list):        # 旧格式（裸数组）就地升级，历史不丢
        history = {"entries": history}
    if not isinstance(history, dict):
        history = {}
    entries = history.get("entries")
    if not isinstance(entries, list):
        entries = []
        history["entries"] = entries
    if any(e.get("action_key") == entry.get("action_key") for e in entries):
        return
    if MANIFEST.exists():
        manifest = read_json(MANIFEST)
        history.setdefault("pack_id", manifest.get("pack_id"))
        history.setdefault("current_version", manifest.get("version"))
        history.setdefault("current_state", manifest.get("state"))
        history.setdefault("frozen_hash", manifest.get("frozen_hash"))
    entries.append(entry)
    write_json(path, history)


def action_key(prefix: str, digest: str, suffix: str) -> str:
    return f"{prefix}:{digest[:16]}:{suffix}"


def require_repo_pack(args) -> bool:
    """生命周期类命令（bump/freeze/thaw/verify/history）只作用于 tools/pack_freeze.py
    实际管辖的 <repo>/standards/scenario-pack/。

    pack_freeze.py 的 PACK_DIR 由它自身位置决定，govern 不复制也不改写 hash 逻辑，
    因此传一个副本路径进来只会让 hash 与真实 pack 脱钩——这种静默错位比报错危险得多。
    只读统计类命令（proposals/ledger）不受此限。
    """
    target = Path(args.pack_dir).resolve()
    if target != PACK_DIR.resolve():
        print(json.dumps({"event_type": "failed", "skill": SKILL, "reason": (
            f"--pack-dir={args.pack_dir} 不是 tools/pack_freeze.py 管辖的 pack "
            f"（{PACK_DIR}）。冻结类动作只能作用于仓库内 standards/scenario-pack/，"
            "govern 不复制 hash 逻辑，也不在副本上假装冻结。")}, ensure_ascii=False))
        return False
    return True


# --------------------------------------------------------------------------- subcommands
def cmd_bump(args) -> int:
    if not require_repo_pack(args):
        return 2
    manifest = read_json(MANIFEST)
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(manifest.get("version", "0.0.0")))
    if not m:
        print(json.dumps({"event_type": "failed", "skill": SKILL,
                          "reason": f"version 不是 semver: {manifest.get('version')!r}"},
                         ensure_ascii=False))
        return 2
    major, minor, patch = (int(x) for x in m.groups())
    if args.level == "minor":
        version = f"{major}.{minor + 1}.0"
    else:
        version = f"{major}.{minor}.{patch + 1}"
    history = manifest.setdefault("history", [])
    entry = {"version": version, "bumped_at": now_iso(),
             "change_summary": args.summary, "decision_event_id": args.decision_event,
             "actor": args.actor, "previous_version": manifest.get("version")}
    history.append(entry)
    manifest["version"] = version
    write_json(MANIFEST, manifest)
    ev = {"event_id": next_event_id(read_ledger(Path(args.ledger))[0]) if args.ledger else None,
          "ts": now_iso(), "run_id": args.run_id, "skill": SKILL, "event_type": "progress",
          "payload": {"action": "bump", "version": version, "change_summary": args.summary,
                      "decision_event_id": args.decision_event}}
    if args.ledger:
        append_event(Path(args.ledger), ev)
    print(f"bump {manifest.get('pack_id')} -> v{version}（{args.summary}）")
    return 0


def cmd_freeze(args) -> int:
    if not require_repo_pack(args):
        return 2
    manifest = read_json(MANIFEST)
    state = manifest.get("state")
    ledger = Path(args.ledger)

    if state == "FROZEN":
        rc, out = run_pack_freeze("--verify")
        if rc == 0:
            print(f"已是 FROZEN 且内容一致，无操作。{out}")
            return 0
        print("FROZEN 但内容与 frozen_hash 不一致：先 verify 定位漂移或 thaw 后重新冻结。\n" + out)
        return 1

    if state != "DRAFT":
        print(json.dumps({"event_type": "failed", "skill": SKILL,
                          "reason": f"state={state} 非法，只能从 DRAFT 冻结"}, ensure_ascii=False))
        return 2

    rc, out = run_pack_freeze()
    if rc != 0:
        print(json.dumps({"event_type": "failed", "skill": SKILL, "reason": out},
                         ensure_ascii=False))
        return rc

    frozen = read_json(MANIFEST)
    digest = frozen.get("frozen_hash")
    ev = {"event_id": next_event_id(read_ledger(ledger)[0]), "ts": now_iso(),
          "run_id": args.run_id, "skill": SKILL, "event_type": "checkpoint",
          "payload": {"artifact": f"{PACK_PREFIX}/{frozen.get('pack_id')}",
                      "hash": digest, "resumable_from": False,
                      "version": frozen.get("version"),
                      "change_summary": args.summary,
                      "decision_event_id": args.decision_event}}
    append_event(ledger, ev)
    key = action_key("freeze", str(digest), str(frozen.get("version")))
    record_history(Path(args.out_dir), {"action_key": key, "action": "freeze",
                                        "ts": ev["ts"], "version": frozen.get("version"),
                                        "state": "FROZEN", "frozen_hash": digest,
                                        "change_summary": args.summary,
                                        "decision_event_id": args.decision_event,
                                        "actor": args.actor})
    log_governance(Path(args.out_dir), {"action_key": key, "ts": ev["ts"], "action": "freeze",
                                        "event_id": ev["event_id"], "detail": out})
    print(f"FROZEN pack={frozen.get('pack_id')} v{frozen.get('version')} hash={digest}")
    print(f"checkpoint 事件 {ev['event_id']} 已入 ledger；回归对比以此为基准。")
    return 0


def cmd_thaw(args) -> int:
    if not require_repo_pack(args):
        return 2
    if not (args.reason or "").strip():
        print(json.dumps({"event_type": "failed", "skill": SKILL,
                          "reason": "无据解冻禁止：thaw 必须附 --reason 并落 decision.thaw_pack"},
                         ensure_ascii=False))
        return 2
    manifest = read_json(MANIFEST)
    if manifest.get("state") != "FROZEN":
        print("当前已是 DRAFT，无需解冻。")
        return 0

    ledger = Path(args.ledger)
    events, _ = read_ledger(ledger)
    ev = {"event_id": next_event_id(events), "ts": now_iso(), "run_id": args.run_id,
          "skill": SKILL, "event_type": "decision",
          "payload": {"decision_kind": "thaw_pack", "actor": args.actor,
                      "target": f"pack:{manifest.get('pack_id')}", "rationale": args.reason,
                      "from_version": manifest.get("version")}}
    if append_event(ledger, ev) == "invalid":
        return 2

    rc, out = run_pack_freeze("--draft")
    key = action_key("thaw", str(ev["event_id"]), "draft")
    record_history(Path(args.out_dir), {"action_key": key, "action": "thaw", "ts": ev["ts"],
                                        "version": manifest.get("version"), "state": "DRAFT",
                                        "frozen_hash": None, "change_summary": args.reason,
                                        "decision_event_id": ev["event_id"], "actor": args.actor})
    log_governance(Path(args.out_dir), {"action_key": key, "ts": ev["ts"], "action": "thaw",
                                        "event_id": ev["event_id"], "detail": out})
    print(f"decision.thaw_pack {ev['event_id']} 已入 ledger：{args.reason}")
    print(out)
    return rc


def cmd_verify(args) -> int:
    if not require_repo_pack(args):
        return 2
    rc, out = run_pack_freeze("--verify")
    manifest = read_json(MANIFEST)
    # action_key 必须带校验结论：只按 hash+分钟去重时，同一分钟内「先 OK 后不一致」
    # 的第二次 verify 会被判为重复而丢弃，治理日志会留着一个已经过期的 ok=true。
    outcome = "ok" if rc == 0 else "hash_mismatch"
    entry = {"action_key": action_key("verify", str(manifest.get("frozen_hash")),
                                      f"{outcome}@{now_iso()[:16]}"),
             "ts": now_iso(), "action": "verify", "ok": rc == 0, "detail": out,
             "pack_id": manifest.get("pack_id"), "version": manifest.get("version"),
             "state": manifest.get("state"), "frozen_hash": manifest.get("frozen_hash")}
    log_governance(Path(args.out_dir), entry)
    if rc != 0:
        flags = ["pack_hash_mismatch"]
        print(json.dumps({"event_type": "degraded", "skill": SKILL,
                          "degraded_flags": flags, "detail": out}, ensure_ascii=False))
        return rc
    print(f"verify OK：{out}")
    return 0


def cmd_history(args) -> int:
    """重建 pack_history.json —— 投影，不是真相源；可随时重跑覆盖。"""
    if not require_repo_pack(args):
        return 2
    manifest = read_json(MANIFEST)
    events, _ = read_ledger(Path(args.ledger))
    entries = []
    for h in manifest.get("history", []):
        entries.append({"action": "bump", "ts": h.get("bumped_at"), "version": h.get("version"),
                        "change_summary": h.get("change_summary"),
                        "decision_event_id": h.get("decision_event_id"), "actor": h.get("actor")})
    for ev in events:
        p = ev.get("payload") or {}
        if ev.get("event_type") == "checkpoint" and PACK_PREFIX in str(p.get("artifact", "")):
            entries.append({"action": "freeze", "ts": ev.get("ts"), "version": p.get("version"),
                            "change_summary": p.get("change_summary"),
                            "decision_event_id": p.get("decision_event_id"),
                            "frozen_hash": p.get("hash"), "event_id": ev.get("event_id")})
        elif ev.get("event_type") == "decision" and p.get("decision_kind") == "thaw_pack":
            entries.append({"action": "thaw", "ts": ev.get("ts"), "rationale": p.get("rationale"),
                            "actor": p.get("actor"), "event_id": ev.get("event_id")})
    entries.sort(key=lambda e: e.get("ts") or "")
    out = Path(args.out) if args.out else Path(args.out_dir) / "pack_history.json"
    write_json(out, {"pack_id": manifest.get("pack_id"),
                     "current_version": manifest.get("version"),
                     "current_state": manifest.get("state"),
                     "frozen_hash": manifest.get("frozen_hash"),
                     "entries": entries})
    print(f"{len(entries)} 条版本链记录 -> {out}")
    return 0
