#!/usr/bin/env python3
"""_contracts.py — clarify 的契约常量、异常与共享小工具（clarify.py 的拆分模块）。

为什么拆：clarify.py 原为 625 行，超出 .claude/rules/coding.md 的 600 行硬上限。
模块划分（与 skills/evalset/scripts/_contracts.py 同一分工）：

  _contracts.py   本文件：契约常量 + 异常 + 值强制转换（pack 读取层与提案编译层共用）
  pack_scan.py     scenario-pack 只读扫描：rubrics/taxonomy/thresholds → 事实表
  proposal.py     五问构造 + 答案编译成 pack_diff / checker 契约 / pending_items
  _selftest.py    内嵌模拟问答 → 提案 fixture 自检
  clarify.py      CLI 入口 + 上述模块的 re-export（对外 API 不变）

这里的常量全是**契约**而非 pack 阈值：它们不参与任何评分，只在"该不该追问"
"算不算有足够证据"这类边界上起作用。改任何一个都必须同步 SKILL.md 与调用方。
"""
from __future__ import annotations

# 五问硬上限，超过即过度设计
QUESTION_BUDGET = 5
# 引用黄金池统计信号所需的最小决策条数
MIN_SUPPORT = 3

# 兜底维度表（pack 扫描不到权重时才用）。维度名以 pack rubrics 为唯一真相源——
# v0.2 已改名 tool_use/safety，写死名单曾让权重求和漏项、把合法输入误判为"和≠1"。
DIMENSIONS = ("outcome", "tool_use", "efficiency", "safety")

# pack 在 sparkjury 仓库内的相对路径前缀（eval-agent 原态在根 scenario-pack/）。
# maps_to / pack_diff.target_file 一律由它构造 —— 提案指向的路径必须在本仓库里真实存在，
# 否则 govern/clarify 拿着 diff 找不到要改的文件。
PACK_PREFIX = "standards/scenario-pack"

# pack 文件 -> 消费 skill（依据 standards/scenario-pack/README.md 的声明 + thresholds.yaml 小节结构）
IMPACT = {
    ("rubrics.yaml", "weight"): (["score", "arbitrate", "regress"], "all"),
    ("rubrics.yaml", "method"): (["score", "arbitrate", "regress"], "outcome"),
    ("rubrics.yaml", "hard_rules"): (["score", "arbitrate", "prioritize", "report"], "risk"),
    ("taxonomy.yaml", "categories"): (["cluster", "prioritize", "report"], "all"),
    ("thresholds.yaml", "regress"): (["regress"], "all"),
    ("thresholds.yaml", "prioritize"): (["prioritize", "report"], "all"),
    ("judges.yaml", "judges"): (["score", "arbitrate", "calibrate"], "all"),
    ("prompts", "prompt"): (["score", "arbitrate"], "all"),
    ("checkers", "checker"): (["score", "regress"], "outcome"),
}

# v0.1 pack 内建的一票否决清单（rubrics.yaml risk checklist），Q4 的对照基线
DEFAULT_VETO_HINTS = ("越权", "PII", "不可逆")
DEFAULT_PASS_K = 3
DEFAULT_PRIMARY_METRIC = "task_pass_rate"


class PackReadError(RuntimeError):
    """pack 不可读/不可解析。显式类型而非裸 RuntimeError：CLI 要把它转成 failed 事件。"""


def _to_int(raw):
    # 把 pack 里扫到的标量拧成 int；拧不动给 None（调用方据此记缺口，不猜）
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None
