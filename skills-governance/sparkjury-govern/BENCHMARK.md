# BENCHMARK.md — sparkjury-govern 的基准与通过标准

sparkjury-govern 不产出分数，它的「基准」= 结构门 + 路由门 + 治理不变量门。
所有可量化阈值来自 pack（`thresholds.yaml` / `rubrics.yaml`）或本 skill 明示的契约常量，
本文件不引入自造的经验数字。

## 命令

```bash
# 1. 结构门（必须）
python3 tools/lint_skills.py --skills-dir skills-governance/sparkjury-govern

# 2. 骨架自检（必须）：6 条 decision fixture → 三类投影 + 红线自查
python3 skills-governance/sparkjury-govern/scripts/run.py --selftest        # sparkjury 规范薄包装
python3 skills-governance/sparkjury-govern/scripts/govern.py --selftest   # 直接跑主脚本亦可

# 3. 治理演练（注意：冻结类动作只作用于仓库内 standards/scenario-pack/）
# 3a. 先验证路径守卫：指向副本必须被拒绝（不应有输出写盘）
mkdir -p /tmp/gp && cp -r standards/scenario-pack /tmp/gp/scenario-pack
python3 skills-governance/sparkjury-govern/scripts/run.py --pack-dir /tmp/gp/scenario-pack verify
#    期望：退出码 2 + failed 事件（pack_freeze.py 的 PACK_DIR 由其自身位置决定）
# 3b. verify 只读，可对真实 pack 直接跑（结果写 governance_log.json）
python3 skills-governance/sparkjury-govern/scripts/run.py verify
# 3c. 完整 freeze/thaw 演练会真实改变 pack 状态，属人工治理动作，不放进自动基准；
#     确需演练时由人明确授权，并在演练后 verify + history 复核版本链。
# 3d. ledger / proposals 可用临时路径安全演练（不触碰 pack）
python3 skills-governance/sparkjury-govern/scripts/run.py --ledger /tmp/gp/events.jsonl \
  ledger append --kind accept_card --actor 万凌 --target card:F02
python3 skills-governance/sparkjury-govern/scripts/run.py --ledger /tmp/gp/events.jsonl \
  --out-dir /tmp/gp/governance proposals

# 4. 路由/行为/红线门：evals/evals.json 三条用例
skillevaluator tier1 ./skills-governance/sparkjury-govern
skillevaluator tier3 ./skills-governance/sparkjury-govern      # 本地裁判即可充当
```

## 指标与通过标准

| 指标 | 通过标准 | 来源 |
|---|---|---|
| 结构 lint | `skills-governance/sparkjury-govern` PASS，无 FAIL 项 | `tools/lint_skills.py` |
| `--selftest` | 退出码 0；6 条 fixture 全部落账、幂等 append、三类投影各 ≥1 条、无理由 thaw 被拒、副本 pack 被守卫拒绝 | 本 skill `scripts/govern.py` |
| 路径守卫 | `bump/freeze/thaw/verify/history` 传入非仓库 `--pack-dir` → 退出码 2 且零写盘 | `scripts/govern.py:require_repo_pack`（认 `standards/scenario-pack/`） |
| hash 唯一实现 | freeze/thaw/verify 全部转调 `tools/pack_freeze.py`，自身无 hash 计算 | `tools/pack_freeze.py` |
| 证据先于动作 | `thaw` 内 `decision.thaw_pack` 写入成功后才会调 `--draft` | `references/state-machine.md` §2 |
| 无据解冻 | `thaw` 缺 `--reason` → 退出码 2，pack 保持 FROZEN | `evals/evals.json` / selftest |
| 提案不落地 | 每条 `proposals[].status == pending_human_approval`，`proposed` 恒为 null | `references/decision-ledger-spec.md` §3 |
| 提案可追溯 | 每条提案 `evidence.event_ids` 非空，且能在 ledger 中命中 | 同上 §3 |
| 提案可解释 | 每条提案给出 pack 现值 `current`（新增类时允许 null）与 `direction` | `schemas/io.schema.json` |
| ledger 校验 | 写入前校验 `decision_kind` 枚举、`thaw_pack`/`reject_proposal` 的 rationale 必填、`target` 命名空间前缀 | `contracts/event-ledger.schema.json` |
| ledger 不可变 | 只追加；同 `event_id` 幂等跳过；坏行跳过并计数而非修写 | 同上 |
| 回归铁律 | verify 不一致时返回非零并发 `degraded` + `pack_hash_mismatch` | `standards/scenario-pack/README.md` 回归铁律 |
| 版本链 | 每次 freeze 记录 version + `frozen_hash` + `change_summary` + `decision_event_id` | `references/state-machine.md` §3 |
| 最小支持数 | `MIN_SUPPORT=2`，**契约常量**（单人 PM 场景，非 pack 阈值） | `references/decision-ledger-spec.md` §6 |

## 记录格式

每次跑 benchmark 的结果追加到本文件底部（追加，不改历史条目）：

```
## 2026-09-27 — sparkjury-govern v0.1.0（sparkjury 移植）
pack: retail-default v0.1.0 FROZEN hash=3d736734…
lint PASS / selftest PASS (22 assertions)
治理演练: /tmp/gp 副本 verify 被守卫拒绝（rc=2，零写盘）
备注: frozen_at 为 pack_freeze.py 的人工确认日期常量，权威时间戳以 ledger ts 为准
```

（**尚无记录** —— 上面只是格式样例，不是已跑结果。当前只跑到命令 1-2 与 3a/3d：
结构门 + selftest + 路径守卫拒绝 + 临时路径的 ledger/proposals 演练在本机通过且可复现；
命令 3b 对真实 pack 的 verify、3c 的 freeze/thaw 演练**未跑**（会真实改变 pack 状态，
属人工治理动作），evals tier1/tier3 未跑。）
