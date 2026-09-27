# 提交清单（截止 2026-09-29）

来源：wiki《作品提交和评分规则》。以链接形式填组委会表单。

## 1. 开源仓库

- [ ] 推到 GitHub 或码云，公开
- [x] README ≥ 500 字，含作品特点、核心亮点、技术实现、架构设计、优化方案（`README.md`）
- [x] 部署说明：本地算力如何部署智能体、如何优化大模型、如何设计 Agent Skills（`README.md` Quick Start + `deploy/README.md` + `skills/README.md`）
- [x] 技术栈说明：NVIDIA SDK（DGX Spark、vLLM、NeMo Agent Toolkit）与 StepFun 模型（step-3.7-flash）（`README.md` Models）
- [x] Skill markdown 文件（`skills/*/SKILL.md`）
- [ ] 删除 `runs/`、`.env`、任何密钥；`git log` 干净
- [ ] 补 Screenshots 段的三张图

## 2. 演示视频

- [ ] 按 `docs/VIDEO_SCRIPT.md` 录制，3 到 4 分钟
- [ ] 上传 B 站，公开，链接填表单

## 3. 十日谈征文

- [ ] `docs/ESSAY_十日谈.md` 补 27 到 29 日实际内容
- [ ] 发 CSDN 或知乎，链接填表单

## 4. 团队资料

- [ ] 团队合影

## 评分对照自查

| 维度 | 权重 | 我们的证据 |
|---|---|---|
| 实用性、落地价值、创新 | 25% | Problem 段的调研数据；本地 + 开箱即用 + 优先级；千富的真实场景故事 |
| 智能体与模型优化深度 | 25% | 三家族裁判团、Jev 仲裁、5% 审计、三层降级、端云成本模型 |
| 完整性 | 20% | 280 passed、3 skipped（2026-09-27 实测）、一条命令跑通、看板、文档 17 节 |
| 平台适配 | 15% | DGX 显存分配与 MoE 选型、vLLM、NeMo Agent Toolkit 评估器、StepFun 裁判 |
| 演示效果 | 10% | Cockpit 同屏 Agent 与 DGX；断网备选 |
| 征文 | 5% | 十日谈 |
