"""决策账本（append-only JSONL）：卡片上的拍板落成事件，治理层才有东西可消费。

契约真源是 PR#10 那一套：`contracts/event-ledger.schema.json` +
`skills-governance/sparkjury-govern/scripts/_ledger.py` + `references/decision-ledger-spec.md`（#10 尚未合入
main，这里按消费端的约束实现生产端）。几条不能想当然的地方：

- 事件必填 `event_id / ts / run_id / skill / event_type`；`event_id` 形如 `evt-0002`，扫文件里已有的最大号 +1，
  消费端按它做幂等。
- `decision` 的 payload 必填 `decision_kind / actor / target`，并且消费端的 `validate_event` 要求 `target` 里
  带 `:`（报「target 缺少命名空间前缀」）。所以这里统一写带命名空间的形式：卡片决策 `card:<cluster_id>`、
  覆盖排序 `priority:<F0X>`。万凌需求文案里的 `"target":"cluster_id"` 是旧形状，按 #10 自家校验读不了。
- `reject_proposal` / `thaw_pack` 必须带 `rationale`。
- `override_priority` 要真生效必须带 `taxonomy_id`（`F\\d\\d`，匹配键）和 `to_rank`：只写 `target` 的旧形状
  在 prioritize 侧会被判 unmatched、在 govern 侧会进 `override_audit.unusable`。生产端在这里直接拒掉，
  比事后对账便宜。
- `system_top` / `human_top` 是 fixability 校准投影的输入，不是匹配键；缺了排序照样生效，但投影算不出来，
  所以有就拿上。

只追加：同 `event_id` 已存在就不重复写（`append` 返回 `{"duplicate": True}`）。进程内用一把锁串行化写，
多进程并发追加不在本模块的保证范围内（只有 API 进程写这个文件）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sparkjury.pack import repo_root

LEDGER_ENV = "SPARKJURY_LEDGER"

EVENT_TYPES = ("plan", "started", "progress", "checkpoint", "completed", "failed", "degraded", "decision")
DECISION_KINDS = ("accept_card", "override_priority", "rename_cluster", "reject_proposal", "approve_pack_diff", "thaw_pack")
#: 拍板动作之外的那几个 kind 需要写清理由（spec：没有理由的否决/解冻不许进账本）
RATIONALE_REQUIRED = ("thaw_pack", "reject_proposal")

EVENT_ID_RE = re.compile(r"^evt-(\d{4,})$")
TAXONOMY_RE = re.compile(r"^F\d{2}$")

_LOCK = threading.Lock()


class LedgerError(ValueError):
    """账本事件不合法，或者文件本身读不出来。"""


def default_ledger_path() -> Path:
    """默认 `governance/events.jsonl`（仓库根），`SPARKJURY_LEDGER` 可覆盖。"""
    env = os.environ.get(LEDGER_ENV)
    return Path(env) if env else repo_root() / "governance" / "events.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def card_target(cluster_id: int | str) -> str:
    """卡片类决策的 target：`card:<cluster_id>`。"""
    return f"card:{cluster_id}"


def priority_target(taxonomy_id: str) -> str:
    """覆盖排序的 target：`priority:<F0X>`。"""
    return f"priority:{taxonomy_id}"


def read_lines(path: str | Path) -> tuple[list[dict[str, Any]], list[int]]:
    """读账本，返回 (事件列表, 坏行行号)。空行跳过；坏行不打断读取，但会被数出来。"""
    p = Path(path)
    if not p.is_file():
        return [], []
    events: list[dict[str, Any]] = []
    bad: list[int] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            bad.append(i)
            continue
        if isinstance(ev, dict):
            events.append(ev)
        else:
            bad.append(i)
    return events, bad


def next_event_id(events: Iterable[dict[str, Any]]) -> str:
    """下一个 `evt-NNNN`：取已有最大号 +1（消费端按 event_id 幂等，号必须单调不重复）。"""
    top = 0
    for ev in events:
        m = EVENT_ID_RE.match(str(ev.get("event_id", "")))
        if m:
            top = max(top, int(m.group(1)))
    return f"evt-{top + 1:04d}"


def _check_taxonomy(value: Any, field: str) -> None:
    if value is None:
        return
    if not TAXONOMY_RE.match(str(value)):
        raise LedgerError(f"{field} 必须是 F\\d\\d（pack 类目 id），收到 {value!r}")


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    """按消费端的规矩校验一条事件。不合法抛 `LedgerError`（消息就是给人看的原因）。"""
    if not isinstance(event, dict):
        raise LedgerError("event 必须是 JSON 对象")
    for key in ("event_id", "ts", "run_id", "skill", "event_type"):
        if not event.get(key):
            raise LedgerError(f"event 缺必填字段 {key}")
    if not EVENT_ID_RE.match(str(event["event_id"])):
        raise LedgerError(f"event_id 必须是 evt-NNNN 形式，收到 {event['event_id']!r}")
    try:
        datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
    except ValueError as e:
        raise LedgerError(f"ts 不是合法 ISO-8601：{event['ts']!r}") from e
    if event["event_type"] not in EVENT_TYPES:
        raise LedgerError(f"event_type 必须是 {EVENT_TYPES} 之一，收到 {event['event_type']!r}")

    if event["event_type"] != "decision":
        return event

    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise LedgerError("decision 事件必须有 payload 对象")
    kind = payload.get("decision_kind")
    if kind not in DECISION_KINDS:
        raise LedgerError(f"decision_kind 必须是 {DECISION_KINDS} 之一，收到 {kind!r}")
    if not payload.get("actor"):
        raise LedgerError("decision 必须有 actor（人的标识符；治理层的否决/审批不许由 agent 代写）")
    target = str(payload.get("target") or "")
    if not target:
        raise LedgerError("decision 必须有 target")
    if ":" not in target:
        raise LedgerError(f"target 缺少命名空间前缀（消费端会判非法）：{target!r}，应为 card:<id> / priority:<F0X>")
    if kind in RATIONALE_REQUIRED and not str(payload.get("rationale") or "").strip():
        raise LedgerError(f"{kind} 必须带 rationale（spec 规定：没有理由的否决不许进账本）")

    if kind == "override_priority":
        if not payload.get("taxonomy_id"):
            raise LedgerError("override_priority 必须带 taxonomy_id（pack 类目 id，prioritize 的匹配键）；"
                              "只写 target 的旧形状会被判 unmatched、在 govern 侧进 unusable")
        _check_taxonomy(payload.get("taxonomy_id"), "taxonomy_id")
        _check_taxonomy(payload.get("system_top"), "system_top")
        _check_taxonomy(payload.get("human_top"), "human_top")
        to_rank = payload.get("to_rank")
        if to_rank is not None and (not isinstance(to_rank, int) or isinstance(to_rank, bool) or to_rank < 1):
            raise LedgerError(f"to_rank 必须是 >=1 的整数，收到 {to_rank!r}")
        if payload.get("system_top") and payload.get("human_top") and payload["system_top"] == payload["human_top"]:
            raise LedgerError(f"system_top 与 human_top 都是 {payload['human_top']}：没有换类目，不该记 override_priority")
    return event


def decision_event(*, run_id: str, decision_kind: str, actor: str, target: str, event_id: str | None = None,
                   skill: str = "report", ts: str | None = None, taxonomy_id: str | None = None,
                   to_rank: int | None = None, system_top: str | None = None, human_top: str | None = None,
                   rationale: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """造一条 `decision` 事件（不写盘）。

    `event_id` 不给就**不写这个键**：编号是 `DecisionLedger.append` 在锁里按账本现状分配的，这里先填一个
    占位号会让 append 以为「已经有号了」，两条并发事件就会撞成同一个 id。
    """
    payload: dict[str, Any] = {"decision_kind": decision_kind, "actor": actor, "target": target}
    if taxonomy_id:
        payload["taxonomy_id"] = taxonomy_id
    if to_rank is not None:
        payload["to_rank"] = to_rank
    if system_top:
        payload["system_top"] = system_top
    if human_top:
        payload["human_top"] = human_top
    if rationale:
        payload["rationale"] = rationale
    if extra:
        payload.update(extra)
    ev: dict[str, Any] = {"ts": ts or now_iso(), "run_id": run_id, "skill": skill, "event_type": "decision",
                          "payload": payload}
    if event_id:
        ev["event_id"] = event_id
    return ev


class DecisionLedger:
    """一个 JSONL 账本的写入端。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_ledger_path()

    def events(self) -> list[dict[str, Any]]:
        return read_lines(self.path)[0]

    def corrupt_lines(self) -> list[int]:
        return read_lines(self.path)[1]

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """校验 + 追加。同 event_id 已存在时直接返回 `{"duplicate": True, ...}`，不重复写。"""
        with _LOCK:
            events, bad = read_lines(self.path)
            if bad:
                raise LedgerError(f"{self.path} 有 {len(bad)} 行不是合法 JSON（行号 {bad[:5]}）：先修账本再追加")
            ev = dict(event)
            if not ev.get("event_id"):
                ev = {"event_id": next_event_id(events), **ev}   # 编号放最前，人肉看账本时好找
            validate_event(ev)
            if any(e.get("event_id") == ev["event_id"] for e in events):
                return {"event": ev, "duplicate": True, "path": str(self.path)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(ev, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return {"event": ev, "duplicate": False, "path": str(self.path)}
