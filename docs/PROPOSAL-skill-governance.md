# 提案：评测治理层五个 Skill、三个工具件与标准冻结机制（供团队核验）

| | |
|---|---|
| 提案人 | 卢万凌（B 组 · Skill 与路由，验收人：陈人瑜） |
| 日期 | 2026-09-27 |
| 状态 | **待团队核验**。核验通过后我按 AGENTS.md 走 worktree + 分支 + PR |
| 一句话 | 我在 9/27 的本地整合里造了一套**评测治理层**：五 个治理 Skill + 三个工具件 + 一份可版本冻结的评测标准。全部在 `sparkjury` CLI **之外**，不动 `src/`，与现有六个 Skill 是同一流水线的治理层，不替代、不重叠 |

---

## 0. 为什么先出文档而不是直接 PR

按 AGENTS.md，每个 PR 必须节点部署 + 冒烟 + 结果贴 PR 描述。我手上**没有** `deploy/dgx/node.env`（节点密码）和 OMS 签名证书，所以现在开 PR，那条最硬的规则我只能原样写"卡在节点部署验证"。隐瞒才算失败，所以我选择：**先用本文档请团队核验内容本身**，核验通过、凭据到位后再走完整 PR 流程。本文档所有数字都在我本地跑过，命令可复现（§3）。

---

## 1. 为什么需要这套东西（对应 TEAM.md / MODULES.md 已诊断的问题）

| 文档里已诊断的事实 | 这套资产对应什么 |
|---|---|
| 「11 条模块验收全是『待确认』= 一条证书都没有」（第 1 轮二） | `lint_skills.py` + 各 Skill 的 selftest 把通过条件变成**可执行断言**：拒绝路径、不猜、不静默，全部有 rc |
| 「跑数发现的问题没有回到流水线，停在聊天记录里」（第 1 轮七） | `sparkjury-govern` 的决策账本 + 黄金池三类投影：PM 每次拍板变成可统计的事件，投影出 pack 调整**方向**（只给方向，数值那一跳留给人） |
| 「回归铁律没有进 CLI，pack hash 只能带外人工卡」（第 5 轮遗留） | `tools/pack_freeze.py`（内容寻址）+ `verify.sh` 第 5 步先把铁律**卡住**；正式进 CLI 的接口需求写成 §6-D4 给滨辉 |
| 「`sparkjury-evalset` 从来没被执行过，靠人记」（MODULES.md 第二件） | 门禁脚本化：`verify.sh` 一条命令跑五步，顺序固化（为什么这样排序见 §2 工具件） |
| B 组自己的话：「校验通过加测试全绿，这条今天就是绿的，当证书等于自欺」 | 这批资产的 selftest **全部断言红线**，不是 happy path：govern 拒绝对仓外 pack 冻结、prioritize 缺系数拒排不夹逼、calibrate 无阈值时诚实判 UNKNOWN、override 匹配不上进 unmatched 不静默。**它们是 fail-first 形态的实物样例**，可直接给 B 组六个 e2e 用例当写法参考 |

---

## 2. 提议清单（5 Skill + 3 工具件 + 1 标准）

### 2.1 五个治理 Skill

| Skill | 是什么 | 解决什么 | 关键红线（已实证） |
|---|---|---|---|
| `sparkjury-calibrate` | 裁判可靠性画像 + 上岗门禁：六个指标（agreement 双口径/over-under/方差/顺序交换敏感度…）、四态门禁、零方差退化裁判识别、最小样本量 Wald 推导 | **先验裁判，再让裁判上场**。现在 mock/降级裁判的输出和金标的一致率没有任何门在管——演示说"本地小评委够用"却没有测量 | 阈值缺失时判 UNKNOWN 而不是假判过；同族裁判直接拒；缺字段显式报错 |
| `sparkjury-prioritize` | 优先级公式显式化（`frequency_norm × severity_weight × fixability_boost`）+ **黄金池 override hook** | 卡片上"PM: fix this first"今天**没有消费者**：点完没有回路，人的排序决策不进任何系统 | 缺 severity 系数拒排不夹逼；旧形状 override 事件（只有 `target=cluster_id`）进 `unmatched` **不静默失效** |
| `sparkjury-clarify` | 澄清期唯一的 pack 写入口：五问预算（被评对象/维度权重/有无金标/风险容忍/回归口径）→ pack diff 提案 | 评测标准今天改起来没有流程、没有留痕 | 只提案不生效；**不自动生成 checker**；追问超五个 = 过度设计，停下走默认 |
| `sparkjury-govern` | pack 生命周期（冻结/解冻/bump/verify）+ 决策账本 + 黄金池三类投影 | pack 变更无流程、解冻无据、人的决策无沉淀 | 非仓 pack 拒绝冻结且**零写盘**；thaw 必须 ledger 先行；投影缺字段时不用 None 当键 |
| `sparkjury-arbitrate` | 仲裁协议外露为 Skill（薄文档层，包 CLI 已有的 `sparkjury arbitrate`） | 仲裁规则（3:0/spread≤1/Jev 三形态/降级）只存在于代码里 | 2:1 无 gold 必须升级不许省 Jev 调用；降级必须落 manifest |

### 2.2 三个工具件（`tools/`）

| 工具 | 是什么 | 为什么值得进仓库 |
|---|---|---|
| `pack_freeze.py` | 评测标准内容寻址：DRAFT→FROZEN 状态机，`frozen_hash` 覆盖 pack 全部文件 | 回归对比的前提是"标准没变过"。当前 hash `3d736734…`，`--verify` 一条命令证明标准未被触碰 |
| `lint_skills.py` | Skill 结构门：NVIDIA 准入六件套 + pyc 门禁 | **pyc 门禁是血泪教训**：pytest 自身写的 `.pyc` 会被 SkillSpector 判成二进制可执行文件，把 skill 直接打成 CRITICAL（我这边踩中三次）。`.gitignore` 只防 git 不防扫描 |
| `verify.sh` | 五步门禁一条命令：清 pyc → pytest → 再清 → lint → validate → pack verify | 顺序固化是为了消灭"先跑啥后跑啥"的人为失误；两个环境坑（pytest 写 pyc、本机代理大小写四变量）写死在脚本里 |

### 2.3 一份可冻结的评测标准（`standards/scenario-pack/`）

把散落在 README/ARCHITECTURE/代码里的判据（rubric 量表与维度、失败分类、阈值权重、裁判配置、prompts、checker 契约）收敛成**一个可版本冻结的目录**：DRAFT→FROZEN、内容寻址、变更走 govern。它不是新标准——是给现有事实一个唯一住所和版本号。

---

## 3. 今天怎么核验（全部可复现）

仓库：`claude/coding/projects/sparkjury/`（我本地的整合副本，= 团队 zip + 上述资产；`.venv` 重建后可跑）。

```bash
# 门禁一：五步全绿（今天实测 ALL GATES PASSED）
bash tools/verify.sh
#   pytest 104 passed, 3 skipped ｜ validate_skills 11/11 ｜ lint_skills 11/11 ｜ pack FROZEN @3d736734…

# 门禁二：治理 skill 的红线断言（不是 happy path）
python3 skills/sparkjury-govern/scripts/run.py --selftest        # 拒无据解冻/幂等/投影
python3 skills/sparkjury-prioritize/scripts/run.py --selftest    # 拒排/override 不静默
python3 skills/sparkjury-clarify/scripts/run.py --selftest       # 不写 pack/五问预算
python3 skills/sparkjury-calibrate/scripts/run.py --selftest     # 无阈值 UNKNOWN/零方差识别
```

**在你们自己的 demo 产物上验黄金池 hook**（今天用 `runs/demo-1` 实测，`card.json` 的 clusters 直接喂会被**拒排**——这是 fail-safe；加一层显式标签映射后通）：

```bash
uv run sparkjury run --demo                    # 产出 runs/<id>/card/card.json
# ① 原样喂：rejected_candidates 列出"severity_weight 未在 pack 定义"，ranked=0（不猜）
# ② 映射 FailureLabel→F 编号后：公式排序 F08(unauthenticated_action)>F01(wrong_tool)
# ③ 喂一条 PM override 事件（payload 带 taxonomy_id=F01, to_rank=1）：
#    排序改写为 [F01, F08]，applied 记 from_rank:2，top_recommendation 变为 F01
# ④ 旧形状事件（只有 target=cluster_id）→ meta.override_unmatched，不静默、不生效
```

第 ④ 步是这套东西和"聊天记录里改排序"的本质区别：**人的每个决策要么生效要么被记账，没有第三条路**。

---

## 4. 诚实的边界（先说清不主张的）

1. **它们是 CLI 之外的桥接层，不是 CLI 内部实现。** MODULES.md 口径第一件写"只通过 sparkjury CLI 调用，不在 Skill 里自己实现评测逻辑"——calibrate/prioritize/clarify/govern 现在是标准库桥接脚本，读 CLI 产物。正确终态是等滨辉的 CLI 提供接口后迁入；**迁入前必须标注为 v0.1 桥接**，不能冒充 CLI 透传。这条是提案的前提，不是瑕疵。
2. **12 项 pack 与运行时的语义差异已登记未裁决**（我的 `docs/INTEGRATION.md` §4 有逐条表：judges.yaml 的 Gemma vs 部署实测的 Nemotron+StepFun API、0-3 vs 0-4 量表、process vs tool_use 维度名、F01-F99 vs FailureLabel、`degraded_flags` vs `degradations[]`、k 自适应 vs pass³ 固定、回归门禁 CLI 未实现…）。**本提案不是"以我的为准"**，是把差异显式化请团队裁决；默认建议已写在 `docs/PACK_V0.2_PROPOSAL.md`。
3. **治理层没跑过真实裁判 trace**（我的 `standards/` 与 skill 接受任意 trace 输入，但真裁判产物在滨辉那边，等回收）。demo-1 是 mock 裁判产物。
4. **我的 11-skill 本地版不是要把团队仓扩成 11 个 skill**。本提案只提议"治理层 + 工具件 + 标准"这部分；团队口径的六个 Skill 与三件事（MODULES.md）不变，我的 B 组动作（六个 fail-first e2e、路由表）照原计划推进，这套资产是那两件事的**弹药**，不是替代。

---

## 5. 需要团队核验/拍板的（编号决策点）

| # | 决策 | 我的建议 |
|---|---|---|
| D1 | 是否 adopt 5+3+1；先 adopt 哪件 | **先 adopt 工具件**（`pack_freeze.py` + `lint_skills.py` + `verify.sh`）：零风险、当天可用、直接服务全组的提交纪律；治理 Skill 随后 |
| D2 | pack 冻结机制是否成为仓库规范 | 是。`verify.sh` 第 5 步已带外卡住；正式进 CLI 见 D4 |
| D3 | 12 项差异哪些统一、以谁为准 | 按 `PACK_V0.2_PROPOSAL.md` 的默认建议（裁判面板/量表/维度名跟运行时，taxonomy 二选一），逐条过 |
| D4 | 给滨辉的接口需求 | ① 回归铁律进 CLI（`--pack-hash` 校验 + delta 阈值 + 新严重类阻断）；② cluster 产物落 `clusters.json`（现在只在 db/card 里）；③ report 卡片的 decision 事件生产（黄金池的唯一现实入口） |
| D5 | OMS 签名证书 | `scripts/sign_skills.sh` 需要团队证书，我手上没有——谁有时告诉我，我补 |
| D6 | 节点凭据 | 有 `node.env` 我就能补齐 PR 的「节点部署验证」栏，把 D1 的 adopt 走完整流程 |

---

## 6. 证据索引（本地）

| 内容 | 位置 |
|---|---|
| 整合全过程 + 12 项差异登记册 | `docs/INTEGRATION.md` |
| pack v0.2 变更提案（diff + 批准路径） | `docs/PACK_V0.2_PROPOSAL.md` |
| 五个治理 Skill（六件套：SKILL.md/skill-card/schemas/evals/references/BENCHMARK） | `skills/sparkjury-{calibrate,prioritize,clarify,govern,arbitrate}/` |
| 工具件 | `tools/` |
| 冻结中的评测标准 | `standards/scenario-pack/`（FROZEN @3d736734…） |
| 契约与接缝真相源 | `contracts/`（含 `cross-skill-interfaces.md` 事故表） |
| 分支/提交纪律 | `docs/GIT_WORKFLOW.md` |

**核验方式建议**：人瑜按 §3 的命令跑一遍（约 5 分钟），重点看三处——`verify.sh` 五绿、治理 selftest 的拒绝路径、demo 产物上第 ③④ 步的 override 记账。有异议直接改这份文档或 §6-D1~D6 逐条回复，我按结论开工。
