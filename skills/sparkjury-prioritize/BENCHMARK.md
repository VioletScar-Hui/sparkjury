# BENCHMARK.md — sparkjury-prioritize skill 的验收基准

本文件的阈值**全部引用 `standards/scenario-pack/thresholds.yaml`**，不在此处自造数字。
pack 是 FROZEN 态；要改阈值走 `govern` 的 DRAFT→FROZEN 状态机，不改本文件。

## 命令

```bash
# 1. 结构与脚本门（必须）
python3 -m json.tool skills/sparkjury-prioritize/schemas/io.schema.json > /dev/null
python3 -m json.tool skills/sparkjury-prioritize/evals/evals.json > /dev/null
python3 -m py_compile skills/sparkjury-prioritize/scripts/*.py && find skills/sparkjury-prioritize -name __pycache__ -exec rm -rf {} +  # py_compile 无视 PYTHONDONTWRITEBYTECODE，必须显式清，否则下一条 lint 的 pyc 门禁必红
python3 skills/sparkjury-prioritize/scripts/run.py --selftest       # sparkjury 规范薄包装
python3 skills/sparkjury-prioritize/scripts/prioritize.py --selftest

# 2. 结构门（官方 linter）
python3 tools/lint_skills.py --skills-dir skills      # 取 [PASS] sparkjury-prioritize 那一行

# 3. 路由门：evals/evals.json 三条用例过 SkillEvaluator tier1/tier3
skillevaluator tier1 ./skills/sparkjury-prioritize
skillevaluator tier3 ./skills/sparkjury-prioritize    # 本 skill 无模型调用，tier3 主要验路由与红线

# 4. 行为基准：确定性排序端到端（先跑 cluster 产出输入）
PACK_HASH=$(python3 -c "import json;print(json.load(open('standards/scenario-pack/pack.manifest.json'))['frozen_hash'])")
python3 skills/sparkjury-prioritize/scripts/prioritize.py \
  --clusters /tmp/cluster-bench/clusters.json \
  --thresholds standards/scenario-pack/thresholds.yaml \
  --out-dir /tmp/cluster-bench --run-id bench-1 \
  --pack-hash "$PACK_HASH" --ledger /tmp/cluster-bench/ledger.jsonl \
  --overrides /tmp/cluster-bench/ledger.jsonl
```

## 指标与通过标准

| 指标 | 通过标准 | 来源 |
|---|---|---|
| `--selftest` 七组断言 | 全 PASS，退出码 0 | 本 skill `scripts/_selftest.py` |
| lint 结构门 | `[PASS] sparkjury-prioritize`，无 FAIL 项 | `tools/lint_skills.py` |
| evals 三条用例 | `expected_behavior` 全命中 | `evals/evals.json` |
| JSON 合法性 | `json.tool` 对 schemas/evals 零错误 | `python3 -m json.tool` |
| 公式正确性 | `priority == frequency_norm × severity_weight × fixability_boost`，逐位相符 | pack `prioritize.formula` |
| 系数来源 | 所有 `severity_weight` / `fixability` 值均可回溯到 pack；0 个本 skill 自造值 | pack `prioritize.severity_weights` / `fixability_boost` |
| 缺系数处理 | 缺系数的类 100% 进 `rejected_candidates`，0 个被补默认值 | 本 skill 红线 |
| 分母口径 | `total_badcases` 与 `cluster_report.total_badcases` 一致；来源记入 `total_badcases_source` | 本 skill SOP |
| top 唯一性 | `top_recommendation` 恰 1 条 | pack `prioritize.top_recommendation_count = 1` |
| 逐条可解释 | 每条 `rationale` 可还原为三因子乘法 | 本 skill 红线 |
| 先验标注 | 每条 ranked 带 `fixability_source=human_prior_pack_v0.1_uncalibrated`；`meta.disclaimer` 非空 | pack `fixability_boost` 注释 |
| override 幂等 | 同一 override 事件重复应用，输出 sha256 一致 | 本 skill `_selftest` 第 6 组 |
| override 可溯源 | 被 override 的条目含 `event_id` + `from_rank→to_rank` + `actor` | `contracts/event-ledger.schema.json` |
| 只读纪律 | 运行前后 `standards/scenario-pack/` 内容 hash 不变 | `tools/pack_freeze.py --verify` |
| 可复算性 | 无模型调用；同输入 byte 级可复现 | 本 skill 设计约束 |

### 待 `regress` 有数据后的补充指标（当前未验证）

| 指标 | 通过标准 | 状态 |
|---|---|---|
| 排序有效性 | top_recommendation 修复后 `task_pass_rate` 提升 ≥ `regress.gates.delta_min`（0.02） | 未验证（无多轮数据） |
| override 收敛 | PM override 次数随轮次下降 | 未验证（无历史数据） |
| 人工先验校准 | `fixability_boost` 与实测修复成本的相关性 | 未验证（无成本数据） |

## 记录格式

每次跑 benchmark 的结果追加到本文件底部：

```
---
日期: YYYY-MM-DD
pack_hash: <frozen_hash 前 16 位>
skill 版本: 0.1.0
读数: selftest=PASS/FAIL；lint=PASS/FAIL；ranked=N；rejected=N；top=Fxx；override 应用=k
结论: <一句话>
```

（尚无记录 —— 本 skill 首版，首次 benchmark 待跑。）
