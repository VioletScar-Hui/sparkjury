#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_fixtures.py — prioritize skill selftest 的内嵌 fixture（数据，非逻辑）。

3 个簇刻意选成"频率、severity、可修复性三者互相错开"的局面，用来验证公式不是被
单一因子主导：只看频率会选 F05、只看 severity 会选 F04，三者相乘才是 F02。
另附 F99 簇 —— pack 里没有它的 fixability_boost，必须被拒排而不是猜一个。
"""


SELFTEST_CLUSTERS = [
    {"cluster_id": "CL-F02", "label": "F02", "category_name": "工具参数错误",
     "size": 7, "severity_max": 2, "trace_ids": ["t1", "t2", "t3"],
     "representatives": [{"badcase_id": "b1"}], "share": 0.35},
    {"cluster_id": "CL-F04", "label": "F04", "category_name": "遗漏必要动作",
     "size": 3, "severity_max": 3, "trace_ids": ["t4", "t5"],
     "representatives": [{"badcase_id": "b2"}], "share": 0.15},
    {"cluster_id": "CL-F05", "label": "F05", "category_name": "多余动作",
     "size": 10, "severity_max": 1, "trace_ids": ["t6"],
     "representatives": [{"badcase_id": "b3"}], "share": 0.50},
]

SELFTEST_THRESHOLDS = """prioritize:
  formula: "priority = frequency_norm * severity_weight * fixability_boost"
  severity_weights: { "3": 1.0, "2": 0.6, "1": 0.3 }
  fixability_boost:
    F01: 1.0
    F02: 1.2
    F03: 1.0
    F04: 0.8
    F05: 0.6
    F06: 1.2
    F07: 1.2
    F08: 0.5
    F09: 0.8
    F10: 0.8
    F11: 0.9
    F12: 0.7
  top_recommendation_count: 1
"""

# cluster 侧同样会产出 F99 簇；F99 不在 fixability_boost 里，必须被拒排而不是猜一个系数
SELFTEST_F99 = {"cluster_id": "CL-F99", "label": "F99", "category_name": "未分类",
                "size": 2, "severity_max": 1, "trace_ids": ["t9"], "share": 0.10}
