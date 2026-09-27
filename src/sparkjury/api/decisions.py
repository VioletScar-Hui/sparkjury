"""PM decisions -> the governance decision ledger (interface #3, the golden-pool inlet).

看板上的"先修这一类 / 换一类 / 本轮不修"此前只写 confirm.json（死胡同）。本模块把
每次拍板追加成 `governance/events.jsonl` 的 decision 事件——治理层（prioritize 的
override hook、govern 的三类投影、calibrate 的可靠性真值）吃的就是这份账本。

契约要点（contracts/event-ledger.schema.json + card-spec §8.1）：
- append-only：只追加不改写；本进程内加锁，跨进程约定单写者（API 是唯一自动写入方）。
- override_priority 必须带 taxonomy_id / system_top / human_top / to_rank，缺字段的
  事件会被消费方计入 unmatched/unusable——宁可显式没用，不可静默丢失。
- taxonomy_id 写运行时标签（如 "loop"）：src 不读 pack；pack v0.2 的 runtime_label
  映射由消费方（sparkjury-prioritize）翻译成规范 F-id。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

LEDGER = Path("governance") / "events.jsonl"
_LOCK = threading.Lock()


def append_decision(run_id: str, kind: str, actor: str, target: str,
                    payload_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "event_id": f"evt-api-{int(time.time() * 1000)}",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_id": run_id,
        "skill": "report",
        "event_type": "decision",
        "payload": {"decision_kind": kind, "actor": actor, "target": target,
                    **(payload_extra or {})},
    }
    with _LOCK:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def list_decisions(run_id: str) -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # govern 不修历史，这里同样只跳过坏行
        if ev.get("event_type") == "decision" and ev.get("run_id") == run_id:
            out.append(ev)
    return out
