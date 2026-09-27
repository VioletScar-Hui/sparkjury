#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reliability.py — 可靠性曲线与 ECE（期望校准误差）计算器。

背景（外部情报 2026-09-27）：Jev 官方从未公布 ECE/可靠性图，且其文档自认「置信度=
选项间相对偏好≠正确率」；第三方实测拿掉正确选项后仍以 80%+ 置信度押错。我们有
别人没有的真值来源——黄金决策池里 PM 的拍板。本工具把 (置信度, 对/错) 对喂进来，
输出 10-bin 可靠性表 + ECE + ASCII 曲线，回答「裁判/仲裁说 80% 把握时到底多少次是对的」。

输入（--pairs，JSON 数组或 JSONL 每行一条）：
    {"confidence": 0.85, "correct": true, "source": "judge_a|jev|...(可选)"}
真值口径：correct = 该判断与 PM 最终拍板一致（黄金池），或与确定性 checker 一致（校准集）。

诚实红线：
  - 样本量 < MIN_PAIRS 时照算但强制打 low_sample 标注——20 个点画出来的曲线是噪声不是校准。
  - 空 bin 不参与 ECE，也不在表里伪造 0。
只用标准库。用法：
    python3 reliability.py --pairs pairs.json [--bins 10] [--out report.json]
    python3 reliability.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MIN_PAIRS = 30  # 工程参考线：低于此值曲线只能当趋势看，不能当校准结论引用


def load_pairs(path: Path) -> list:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    out = []
    for i, r in enumerate(rows):
        c, ok = r.get("confidence"), r.get("correct")
        if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0) or not isinstance(ok, bool):
            raise ValueError(f"pairs[{i}] 非法：confidence 须在 [0,1] 且 correct 为布尔，得到 {r!r}")
        out.append({"confidence": float(c), "correct": ok, "source": r.get("source")})
    return out


def compute(pairs: list, bins: int = 10) -> dict:
    """可靠性表 + ECE。ECE = Σ (bin 样本占比 × |bin 平均置信 − bin 实际正确率|)。"""
    n = len(pairs)
    table = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        # 最后一个 bin 闭区间收 1.0
        grp = [p for p in pairs if (lo <= p["confidence"] < hi) or (b == bins - 1 and p["confidence"] == 1.0)]
        if not grp:
            continue  # 空 bin 不伪造 0
        conf = sum(p["confidence"] for p in grp) / len(grp)
        acc = sum(1 for p in grp if p["correct"]) / len(grp)
        gap = conf - acc
        ece += (len(grp) / n) * abs(gap)
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(grp),
                      "avg_confidence": round(conf, 4), "accuracy": round(acc, 4),
                      "gap": round(gap, 4)})
    verdict = ("overconfident" if any(r["gap"] > 0.1 and r["n"] >= 5 for r in table)
               else "underconfident" if any(r["gap"] < -0.1 and r["n"] >= 5 for r in table)
               else "roughly_calibrated")
    return {
        "n_pairs": n, "bins": bins, "ece": round(ece, 4), "verdict": verdict,
        "low_sample": n < MIN_PAIRS,
        "note": (f"样本量 {n} < {MIN_PAIRS}，曲线只能当趋势看，不得作为校准结论引用"
                 if n < MIN_PAIRS else
                 "correct 的真值口径：与 PM 黄金池拍板一致或与确定性 checker 一致"),
        "table": table,
    }


def render(report: dict) -> str:
    lines = [f"n={report['n_pairs']}  ECE={report['ece']}  判定={report['verdict']}"
             + ("  ⚠️ low_sample" if report["low_sample"] else "")]
    lines.append("bin        n    置信    正确率   gap   （█=正确率，·=置信落点）")
    for r in report["table"]:
        bar = "█" * round(r["accuracy"] * 20)
        dot = min(round(r["avg_confidence"] * 20), 20)
        bar = (bar + " " * 21)[:21]
        bar = bar[:dot] + "·" + bar[dot + 1:]
        lines.append(f"{r['bin']}  {r['n']:4d}  {r['avg_confidence']:.2f}   {r['accuracy']:.2f}   "
                     f"{r['gap']:+.2f}  {bar}")
    return "\n".join(lines)


def selftest() -> int:
    fails = []
    # 良校准：置信≈正确率 → ECE 低
    good = [{"confidence": c, "correct": (i % 10) < round(c * 10)}
            for c in (0.15, 0.35, 0.55, 0.75, 0.95) for i in range(10)]
    r = compute(good)
    if r["ece"] > 0.12 or r["verdict"] == "overconfident":
        fails.append(f"良校准样本 ECE 应低，得到 {r['ece']} / {r['verdict']}")
    # 过度自信（Jev 财报实测形态：高置信押错）→ ECE 高且判 overconfident
    over = [{"confidence": 0.9, "correct": i < 3} for i in range(10)] * 4
    r = compute(over)
    if r["ece"] < 0.4 or r["verdict"] != "overconfident":
        fails.append(f"过度自信样本应判 overconfident 且 ECE 高，得到 {r['ece']} / {r['verdict']}")
    # 小样本必须打 low_sample
    r = compute(over[:8])
    if not r["low_sample"]:
        fails.append("n<30 必须标 low_sample")
    # 空 bin 不伪造
    r = compute([{"confidence": 0.95, "correct": True}] * 40)
    if len(r["table"]) != 1:
        fails.append(f"只有一个 bin 有数据时表应只有 1 行，得到 {len(r['table'])}")
    # 非法输入显式报错
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('[{"confidence": 1.5, "correct": true}]')
        load_pairs(Path(f.name))
        fails.append("confidence>1 必须报错")
    except ValueError:
        pass
    if fails:
        print("[reliability --selftest] FAIL")
        for x in fails:
            print("  -", x)
        return 1
    print("[reliability --selftest] PASS — 良校准低 ECE / 过度自信高 ECE(Jev 形态) / "
          "小样本强制标注 / 空 bin 不伪造 / 非法输入显式拒绝")
    return 0


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    ap = argparse.ArgumentParser(description="可靠性曲线与 ECE（真值=PM 黄金池拍板或确定性 checker）")
    ap.add_argument("--pairs", type=Path)
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.pairs:
        ap.print_help()
        return 2
    try:
        report = compute(load_pairs(args.pairs), args.bins)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"FATAL: 输入不可用：{e}", file=sys.stderr)
        return 3
    print(render(report))
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
