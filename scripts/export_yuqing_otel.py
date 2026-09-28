"""把舆情评论打标的真实运行记录导出成脱敏后的 OTel trace（data/real/）。

输入全在本地，不进仓库：
  --v38-results   V3.8 评测那一轮的 results.json（每条含笔记、配置、评论串、模型回答、业务金标）
  --relabel-xlsx  生产环境人工改判导出（sm_comment_review 里 模型标签 != 人工标签 的行）
  --causes        PM 手工归因表 {"causes": {"<行号>": "<成因>"}}
  --alias-map     品牌 / 产品 / 联名方 / 竞品 / 代言人 / 活动口号的原名到代号，按组写，如 {"brands": {...}, "products": {...}}
  --salt          ID 哈希用的盐（或环境变量 YUQING_SALT）

输出两份文件，同一条评论在两边的 task_id 相同，可以直接喂给 `sparkjury regress`：
  yuqing_v31_prod.json   生产 V3.1 被人工改判的评论（全部判错）
  yuqing_v38_eval.json   V3.8 评测：与上面重叠的评论 + 业务金标里判错的全部 + 判对的抽样

脱敏规则写在 data/real/README.md。输出前做泄露自检，命中任何一条就非零退出、不落文件。

读 xlsx 需要 openpyxl，它不在项目依赖里，用法：
  uv run --with openpyxl python scripts/export_yuqing_otel.py --v38-results ... --relabel-xlsx ... \
      --causes ... --alias-map ... --salt-file ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DOMAIN = "marketing_yuqing"
AGENT = "yuqing_tagging_agent"
LABEL = {"NEGATIVE": "负向", "NEUTRAL": "中性", "POSITIVE": "正向"}

RE_URL = re.compile(r"https?://\S+", re.I)
RE_MENTION = re.compile(r"@(?!用户)[^\s@:：,，。!！?？\[\]#]+")
RE_LONG_DIGITS = re.compile(r"\d{7,}")
RE_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
NO_DIGITS = str.maketrans("0123456789", "ghijklmnop")


class Scrubber:
    def __init__(self, alias_map: dict[str, dict[str, str]], salt: str) -> None:
        pairs: dict[str, str] = {}
        for group, table in alias_map.items():
            if not group.startswith("_") and isinstance(table, dict):
                pairs.update(table)
        # 长词先换，免得「霸王茶姬」被「霸王」拆坏
        self.pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        self.originals = [k for k, _ in self.pairs]
        self.salt = salt

    def text(self, s: str | None) -> str | None:
        if s is None:
            return None
        for src, dst in self.pairs:
            s = re.sub(re.escape(src), dst, s, flags=re.I)
        s = RE_URL.sub("[链接已删除]", s)
        s = RE_MENTION.sub("@用户", s)
        s = RE_HEX_ID.sub("[ID已删除]", s)
        s = RE_LONG_DIGITS.sub("[数字已删除]", s)
        return s

    def h(self, kind: str, raw: str, n: int = 12) -> str:
        """脱敏 ID：加盐 sha256，数字位换成字母，免得和泄露自检里的「长数字」撞上。"""
        return hashlib.sha256(f"{self.salt}|{kind}|{raw}".encode()).hexdigest()[:n].translate(NO_DIGITS)

    def leaks(self, s: str) -> list[str]:
        found = [o for o in self.originals if o.lower() in s.lower()]
        for name, rx in (("url", RE_URL), ("mention", RE_MENTION), ("digits", RE_LONG_DIGITS), ("hex_id", RE_HEX_ID)):
            m = rx.search(s)
            if m:
                found.append(f"{name}:{m.group(0)[:20]}")
        return found


def round_floats(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: round_floats(v) for k, v in x.items()}
    if isinstance(x, list):
        return [round_floats(v) for v in x]
    return x


def norm_text(s: str | None) -> str:
    return (s or "").strip()


def attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def ns(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1e9))


def make_trace(
    sc: Scrubber,
    *,
    file_tag: str,
    text_key: str,
    model: str,
    prompt_version: str,
    user_payload: dict[str, Any],
    answer: dict[str, Any],
    model_label: str,
    human_label: str,
    gold_source: str,
    start: datetime,
    seconds: float,
    tokens_in: int | None,
    tokens_out: int | None,
    extra: dict[str, Any],
) -> list[dict[str, Any]]:
    trace_id = sc.h(f"trace:{file_tag}", text_key, 32)
    root_id = sc.h(f"root:{file_tag}", text_key, 16)
    chat_id = sc.h(f"chat:{file_tag}", text_key, 16)
    end = start + timedelta(seconds=seconds)
    task_id = "yq-" + sc.h("task", text_key, 10)
    root_attrs = [
        attr("gen_ai.operation.name", "invoke_agent"),
        attr("gen_ai.agent.name", AGENT),
        attr("gen_ai.request.model", model),
        attr("sparkjury.task_id", task_id),
        attr("sparkjury.trial", 0),
        attr("sparkjury.success", model_label == human_label),
        attr("sparkjury.domain", DOMAIN),
        attr("yuqing.prompt_version", prompt_version),
        attr("yuqing.model_label", model_label),
        attr("yuqing.human_label", human_label),
        attr("yuqing.gold_source", gold_source),
    ] + [attr(k, v) for k, v in extra.items() if v is not None]
    system = f"舆情评论打标 Agent，System Prompt {prompt_version}。任务：判断目标评论对目标品牌的情感（正向/中性/负向），负向时给出负面归因大类。Prompt 全文属业务方资产，不随数据公开。"
    input_messages = [
        {"role": "system", "parts": [{"type": "text", "content": system}]},
        {"role": "user", "parts": [{"type": "text", "content": json.dumps(user_payload, ensure_ascii=False)}]},
    ]
    output_messages = [
        {"role": "assistant", "parts": [{"type": "text", "content": json.dumps(answer, ensure_ascii=False)}], "finish_reason": "stop"}
    ]
    chat_attrs = [
        attr("gen_ai.operation.name", "chat"),
        attr("gen_ai.request.model", model),
        attr("gen_ai.input.messages", json.dumps(input_messages, ensure_ascii=False)),
        attr("gen_ai.output.messages", json.dumps(output_messages, ensure_ascii=False)),
    ]
    if tokens_in is not None:
        chat_attrs.append(attr("gen_ai.usage.input_tokens", int(tokens_in)))
    if tokens_out is not None:
        chat_attrs.append(attr("gen_ai.usage.output_tokens", int(tokens_out)))
    return [
        {
            "traceId": trace_id,
            "spanId": root_id,
            "parentSpanId": "",
            "name": f"invoke_agent {AGENT}",
            "kind": 1,
            "startTimeUnixNano": ns(start),
            "endTimeUnixNano": ns(end),
            "attributes": root_attrs,
            "status": {"code": 1},
        },
        {
            "traceId": trace_id,
            "spanId": chat_id,
            "parentSpanId": root_id,
            "name": f"chat {model}",
            "kind": 1,
            "startTimeUnixNano": ns(start),
            "endTimeUnixNano": ns(end),
            "attributes": chat_attrs,
            "status": {"code": 1},
        },
    ]


def v38_payload(sc: Scrubber, inp: dict[str, Any]) -> dict[str, Any]:
    cfg = inp.get("config", {})
    note = inp.get("note", {})
    return {
        "note": {"note_id": sc.h("note", str(note.get("note_id"))), "title": sc.text(note.get("title")), "body": sc.text(note.get("body"))},
        "config": {
            "target_brand": sc.text(cfg.get("target_brand")),
            "target_products": [sc.text(x) for x in cfg.get("target_products", [])],
            "competitors": [sc.text(x) for x in cfg.get("competitors", [])],
            "good_monitor_keywords": [sc.text(x) for x in cfg.get("good_monitor_keywords", [])],
            "bad_monitor_keywords": [sc.text(x) for x in cfg.get("bad_monitor_keywords", [])],
        },
        "target_comment_ids": [sc.h("comment", str(x)) for x in inp.get("target_comment_ids", [])],
        "comments": [
            {
                "id": sc.h("comment", str(c.get("id"))),
                "reply_to": sc.h("comment", str(c["reply_to"])) if c.get("reply_to") else None,
                "text": sc.text(c.get("text")),
            }
            for c in inp.get("comments", [])
        ],
    }


def load_relabels(path: Path) -> list[dict[str, Any]]:
    import openpyxl  # 只在导出时需要，见模块说明

    ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(x) for x in rows[0]]
    return [dict(zip(head, r)) | {"_row": i} for i, r in enumerate(rows[1:], 1)]


def spans_doc(spans: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [attr("service.name", AGENT)]},
                "scopeSpans": [{"scope": {"name": scope}, "spans": spans}],
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--v38-results", type=Path, required=True)
    ap.add_argument("--relabel-xlsx", type=Path, required=True)
    ap.add_argument("--causes", type=Path, required=True)
    ap.add_argument("--alias-map", type=Path, required=True)
    ap.add_argument("--salt-file", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("data/real"))
    ap.add_argument("--v31-extra", type=int, default=5, help="V3.1 里不与 V3.8 重叠、另取的条数")
    ap.add_argument("--v38-pass", type=int, default=16, help="V3.8 业务金标里判对的抽样条数")
    ap.add_argument("--seed", type=int, default=20260928)
    args = ap.parse_args(argv)

    salt = args.salt_file.read_text().strip() if args.salt_file else os.environ.get("YUQING_SALT", "")
    if not salt:
        ap.error("需要 --salt-file 或环境变量 YUQING_SALT")
    sc = Scrubber(json.loads(args.alias_map.read_text(encoding="utf-8")), salt)
    causes: dict[str, str] = json.loads(args.causes.read_text(encoding="utf-8"))["causes"]
    rng = random.Random(args.seed)

    v38 = json.loads(args.v38_results.read_text(encoding="utf-8"))
    v38_by_text = {norm_text(r["text"]): r for r in v38}
    v38_created = datetime(2026, 9, 22, 9, 14, 20, tzinfo=timezone.utc)
    # 同一句话可能被不同用户各发一次（「出吗」），打标任务是同一个，按文本去重
    relabels, seen_text = [], set()
    for r in load_relabels(args.relabel_xlsx):
        if norm_text(r["comment_content"]) not in seen_text:
            seen_text.add(norm_text(r["comment_content"]))
            relabels.append(r)

    # ---- V3.1 生产改判：与 V3.8 重叠的全取，另按成因补几条没覆盖到的 ----
    overlap = [r for r in relabels if norm_text(r["comment_content"]) in v38_by_text]
    rest = [r for r in relabels if norm_text(r["comment_content"]) not in v38_by_text]
    # 先让每个还没覆盖到的成因各出一条，再按行号补满
    seen = {causes[str(r["_row"])] for r in overlap}
    firsts, others = [], []
    for r in rest:
        c = causes[str(r["_row"])]
        (others if c in seen else firsts).append(r)
        seen.add(c)
    extra = (firsts + others)[: args.v31_extra]
    v31_rows = overlap + extra

    v31_spans: list[dict[str, Any]] = []
    for r in v31_rows:
        key = norm_text(r["comment_content"])
        reviewed = datetime.fromisoformat(str(r["reviewedAt"]).replace(" +00:00", "+00:00"))
        model_label, human_label = LABEL[r["model_sentiment"]], LABEL[r["manual_sentiment"]]
        v31_spans += make_trace(
            sc,
            file_tag="v31",
            text_key=key,
            model="production-v3.1（模型名未随导出留存）",
            prompt_version="V3.1",
            user_payload={
                "说明": "生产导出只保留了评论本身，笔记正文与评论串未留存",
                "comments": [{"id": sc.h("comment", str(r["comment_id"])), "text": sc.text(r["comment_content"])}],
            },
            answer={"sentiment": model_label, "说明": "生产环境只落了最终标签，原始响应未留存"},
            model_label=model_label,
            human_label=human_label,
            gold_source="生产人工改判",
            start=reviewed,
            seconds=0.0,
            tokens_in=None,
            tokens_out=None,
            extra={
                "yuqing.relabel_cause": causes[str(r["_row"])],
                "yuqing.review_reason": sc.text(r.get("review_reason")),
                "yuqing.timestamp_note": "时间戳是人工复核时间，不是模型调用时间",
            },
        )

    # ---- V3.8 评测：重叠的 + 金标判错的全部 + 金标判对的抽样 ----
    overlap_v38 = [(v38_by_text[norm_text(r["comment_content"])], LABEL[r["manual_sentiment"]], "生产人工改判") for r in overlap]
    overlap_text = {norm_text(r["comment_content"]) for r in overlap}
    gold = [r for r in v38 if r.get("gold") and norm_text(r["text"]) not in overlap_text]
    gold = list({norm_text(r["text"]): r for r in gold}.values())
    gold_fail = [r for r in gold if r["gold"] != r["sentiment"]]
    gold_pass = [r for r in gold if r["gold"] == r["sentiment"]]
    taken = {id(x[0]) for x in overlap_v38} | {id(r) for r in gold_fail}
    by_point: dict[str, list[dict[str, Any]]] = {}
    for r in gold_pass:
        if id(r) not in taken:
            by_point.setdefault(str(r.get("negative_point")), []).append(r)
    for rows in by_point.values():
        rng.shuffle(rows)
    picked: list[dict[str, Any]] = []
    while len(picked) < args.v38_pass and any(by_point.values()):
        for k in sorted(by_point):
            if by_point[k] and len(picked) < args.v38_pass:
                picked.append(by_point[k].pop())
    v38_rows = overlap_v38 + [(r, r["gold"], r["gold_source"]) for r in gold_fail + picked]

    v38_spans: list[dict[str, Any]] = []
    for r, human_label, gsrc in v38_rows:
        usage = r["response"].get("usage", {})
        v38_spans += make_trace(
            sc,
            file_tag="v38",
            text_key=norm_text(r["text"]),
            model=r["response"].get("model", "unknown"),
            prompt_version="V3.8",
            user_payload=v38_payload(sc, r["input"]),
            answer=round_floats(r["response"]["answers"]),
            model_label=r["sentiment"],
            human_label=human_label,
            gold_source="业务复核金标" if gsrc and "金标" in gsrc else gsrc,
            start=v38_created + timedelta(seconds=int(r["row"])),
            seconds=float(r.get("elapsed_seconds") or 0.0),
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            extra={
                "yuqing.negative_point": r.get("negative_point"),
                "yuqing.timestamp_note": "开始时间按该轮评测创建时间加行号近似，时长是真实调用耗时",
            },
        )

    outputs = {
        "yuqing_v31_prod.json": spans_doc(v31_spans, "sparkjury.real.yuqing.v31"),
        "yuqing_v38_eval.json": spans_doc(v38_spans, "sparkjury.real.yuqing.v38"),
    }

    # ---- 泄露自检：扫的是除 traceId/spanId 以外的全部内容 ----
    problems: list[str] = []
    for name, doc in outputs.items():
        for sp in doc["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            blob = json.dumps(sp["attributes"], ensure_ascii=False) + sp["name"]
            for leak in sc.leaks(blob):
                problems.append(f"{name} span {sp['spanId']}: {leak}")
    if problems:
        print(f"泄露自检未通过，{len(problems)} 处，不写文件：", file=sys.stderr)
        for p in problems[:30]:
            print("  " + p, file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, doc in outputs.items():
        (args.out_dir / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    n31, n38 = len(v31_spans) // 2, len(v38_spans) // 2
    print(f"v31: {n31} traces ({len(overlap)} 条与 V3.8 重叠，另取 {len(extra)} 条), 全部判错")
    print(f"v38: {n38} traces (重叠 {len(overlap_v38)}，金标判错 {len(gold_fail)}，金标判对抽样 {len(picked)})")
    print(f"泄露自检通过，写入 {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
