#!/usr/bin/env python3
"""_ledger.py — govern 的账本层：基础 IO + 事件校验 + 幂等追加。

从 govern.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块只回答"一条事件能不能进账本、怎么读回账本"，不含任何治理动作。

三件事：
  1. 基础 IO —— read_json / write_json / now_iso，全 skill 唯一的读写底座。
  2. 校验 —— validate_event 严格按 contracts/event-ledger.schema.json 的 if-then 约束，
     返回错误列表而不是抛异常：调用方要能区分"写不进去"和"崩了"。
  3. 幂等追加 —— append_event 按 event_id 去重。ledger 只追加不改写，
     重复 append 必须返回 exists 而不是追加第二行。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# skill 名。定义在最底层模块，让 failed 事件的 skill 字段全 skill 只有一个来源
# （govern.py 与 _lifecycle.py 都从这里取，不各写一遍字面量）。
SKILL = "govern"

# 契约枚举（非 pack 阈值）：与 contracts/event-ledger.schema.json 逐字对齐，
# 改这里等于改契约，必须同步 schemas/ 与 contracts/。
EVENT_TYPES = ("plan", "started", "progress", "checkpoint", "completed", "failed",
               "degraded", "decision")
DECISION_KINDS = ("accept_card", "override_priority", "rename_cluster",
                  "reject_proposal", "approve_pack_diff", "thaw_pack")
RATIONALE_REQUIRED = ("thaw_pack", "reject_proposal")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_ledger(path: Path) -> tuple:
    """返回 (events, corrupt_lines)。坏行跳过并计数，不静默吞掉。"""
    events, corrupt = [], 0
    if not path.exists():
        return events, corrupt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            corrupt += 1
    return events, corrupt


def next_event_id(events: list) -> str:
    """扫已有 event_id 取最大值 +1。同一次 append 前必须重读账本，否则并发写会撞 id。"""
    top = 0
    for ev in events:
        m = re.fullmatch(r"evt-(\d+)", str(ev.get("event_id", "")))
        if m:
            top = max(top, int(m.group(1)))
    return f"evt-{top + 1:04d}"


def validate_event(ev: dict) -> list:
    """按 contracts/event-ledger.schema.json 校验一条事件，返回错误列表（空=合法）。"""
    errs = []
    for key in ("event_id", "ts", "run_id", "skill", "event_type"):
        if not ev.get(key):
            errs.append(f"缺字段 {key}")
    if ev.get("event_type") not in EVENT_TYPES:
        errs.append(f"event_type={ev.get('event_type')} 不在枚举内")
    try:
        datetime.fromisoformat(str(ev.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        errs.append(f"ts 不是合法 ISO-8601: {ev.get('ts')!r}")
    payload = ev.get("payload")
    if ev.get("event_type") == "decision":
        if not isinstance(payload, dict):
            errs.append("decision 事件缺 payload 对象")
            return errs
        for key in ("decision_kind", "actor", "target"):
            if not payload.get(key):
                errs.append(f"decision payload 缺字段 {key}")
        if payload.get("decision_kind") not in DECISION_KINDS:
            errs.append(f"decision_kind={payload.get('decision_kind')} 不在枚举内")
        elif payload["decision_kind"] in RATIONALE_REQUIRED and not payload.get("rationale"):
            errs.append(f"{payload['decision_kind']} 必须附 rationale")
        if payload.get("target") and ":" not in str(payload["target"]):
            errs.append(f"target 缺少命名空间前缀: {payload['target']!r}")
    elif ev.get("event_type") == "checkpoint":
        if not isinstance(payload, dict):
            errs.append("checkpoint 事件缺 payload 对象")
        else:
            for key in ("artifact", "hash", "resumable_from"):
                if key not in payload:
                    errs.append(f"checkpoint payload 缺字段 {key}")
    return errs


def append_event(path: Path, ev: dict) -> str:
    """幂等追加：同 event_id 已存在则不重复写入。返回 appended / exists / invalid。"""
    errs = validate_event(ev)
    if errs:
        print(json.dumps({"event_type": "failed", "skill": SKILL, "errors": errs},
                         ensure_ascii=False))
        return "invalid"
    events, _ = read_ledger(path)
    if any(e.get("event_id") == ev["event_id"] for e in events):
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return "appended"
