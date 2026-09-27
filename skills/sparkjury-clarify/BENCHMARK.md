# BENCHMARK.md — sparkjury-clarify 的基准与通过标准

sparkjury-clarify 是治理层 skill，不产出分数，因此它的「基准」= 结构门 + 路由门 + 提案质量门。
所有可量化阈值只能来自 pack（`thresholds.yaml` / `rubrics.yaml`）或本 skill 明示的契约常量，
本文件不引入任何自造的经验数字。

## 命令

```bash
# 1. 结构门（必须）
python3 tools/lint_skills.py --skills-dir skills/sparkjury-clarify

# 2. 骨架自检（必须）：内嵌模拟问答 → 提案 fixture，含红线自查
python3 skills/sparkjury-clarify/scripts/run.py --selftest        # sparkjury 规范薄包装
python3 skills/sparkjury-clarify/scripts/clarify.py --selftest   # 直接跑主脚本亦可

# 3. 五问与默认值预览（人工检查问题是否只围绕标准定义）
python3 skills/sparkjury-clarify/scripts/run.py --questions

# 4. 路由/行为/红线门：evals/evals.json 三条用例
skillevaluator tier1 ./skills/sparkjury-clarify
skillevaluator tier3 ./skills/sparkjury-clarify      # 本地裁判即可充当

# 5. 端到端 fixture（人机协作部分需人工）
python3 skills/sparkjury-clarify/scripts/clarify.py \
  --answers <fixture>.json --source-text "risk 比效率重要，提到 0.25" --out /tmp/session.json
```

## 指标与通过标准

| 指标 | 通过标准 | 来源 |
|---|---|---|
| 结构 lint | `skills/sparkjury-clarify` PASS，无 FAIL 项 | `tools/lint_skills.py` |
| `--selftest` | 退出码 0，全部断言 PASS（含「不写 pack 内容文件」红线自查） | 本 skill `scripts/clarify.py` |
| evals 三条用例 | `expected_behavior` 全命中 | `evals/evals.json` |
| 五问预算 | `questions_asked.length ≤ 5`；`QUESTION_BUDGET=5` 为**契约常量**（非 pack 阈值） | `references/interview-protocol.md` §4 |
| current 可信度 | 每条 `pack_diff.current` 非 null（由 `tools/_yaml_lite.py` 解析后投影，禁止凭记忆填写） | `references/pack-diff-format.md` §2 |
| 权重合法性 | Q2 权重和 `= 1.0 ± 1e-6`；`rubrics.yaml` 声明 scale 0-3 | `standards/scenario-pack/rubrics.yaml:meta` |
| 回归口径 | `pass_k` 取值为正整数（tau2-bench 口径）；默认值与 pack `regress.pass_k` 一致 | `standards/scenario-pack/thresholds.yaml:regress` |
| 回归铁律 | 提案涉及 `thresholds.yaml:regress.*` 时必须提示「同 `pack_hash` 才可比」 | `standards/scenario-pack/README.md` 回归铁律 |
| 红线命中率 | 三条红线用例（自动生效 / 写 checker 实现 / 手改 pack）全部拒绝 | `evals/evals.json` case 3 |
| 决策可追溯 | 每条提案能被一条 `decision` 事件（`approve_pack_diff`/`reject_proposal`）引用 | `contracts/event-ledger.schema.json` |

## 记录格式

每次跑 benchmark 的结果追加到本文件底部（追加，不改历史条目）：

```
## 2026-09-27 — sparkjury-clarify v0.1.0（sparkjury 移植）
pack: retail-default v0.1.0 FROZEN hash=3d736734…
lint PASS / selftest PASS (16 assertions)
evals: tier1 pending / tier3 pending（赛事后补）
备注: -
```

（**尚无记录** —— 上面只是格式样例，不是已跑结果。当前只跑到命令 1-3：结构门 + selftest +
五问预览在本机通过且可复现；evals tier1/tier3 未跑，真实访谈行为未验证。）
