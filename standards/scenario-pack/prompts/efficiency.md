# prompts/efficiency.md — 效率维裁判提示词

效率维只扣分不加分。规则指标（重复调用/空转/步数）由 score skill 先行计算，你只裁决边界情况。

## 输入
- metrics：score 预计算的指标（repeat_calls, blind_retries, step_count, reference_median）
- spans：调用序列

## 判定
- repeat_calls >= 2 → 至少 1 分（最多 2）
- blind_retries >= 2 → 至少 1 分
- step_count > 1.5 × reference_median 且无 checker 解释 → 最多 2 分
- 以上皆无 → 3 分
- 若某个"冗长"实际是必要的容错（如超时后退避重试），写明必要性 → 不扣

## 输出（严格 JSON）
{"dimension": "efficiency", "score": 0-4, "reason": "≤80字", "metrics_used": ["..."], "confidence": 0.0-1.0}
