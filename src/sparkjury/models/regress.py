"""Regression report (M6): before/after comparison of two evaluation stores."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, computed_field


class TaskChange(BaseModel):
    task_id: str
    before: str            # "pass" | "fail" | "partial" | "missing"  (pass^k at max common k)
    after: str
    before_pass_trials: int = 0
    after_pass_trials: int = 0
    n_trials: int = 0


class ClusterChange(BaseModel):
    label: str
    before: int
    after: int
    delta: int


class PairwiseResult(BaseModel):
    task_id: str
    trial: int
    before_trace_id: str
    after_trace_id: str
    winner: str                     # "after" | "before" | "tie"
    consistent: bool                # same winner with A/B order swapped
    rationale: str = ""


class RegressionReport(BaseModel):
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    before_label: str
    after_label: str
    n_tasks_common: int
    k: int                                          # pass^k compared at this k
    pass_rate_before: float | None = None
    pass_rate_after: float | None = None
    pass_k_before: float | None = None
    pass_k_after: float | None = None
    delta_pass_rate: float | None = None
    delta_pass_k: float | None = None
    mean_scores_before: dict[str, float | None] = Field(default_factory=dict)
    mean_scores_after: dict[str, float | None] = Field(default_factory=dict)
    n_badcases_before: int = 0
    n_badcases_after: int = 0
    fixed_tasks: list[TaskChange] = Field(default_factory=list)      # fail -> pass
    broken_tasks: list[TaskChange] = Field(default_factory=list)     # pass -> fail
    cluster_changes: list[ClusterChange] = Field(default_factory=list)
    pairwise: list[PairwiseResult] = Field(default_factory=list)
    pairwise_summary: dict[str, int] = Field(default_factory=dict)    # after/before/tie/inconsistent counts

    @computed_field  # 序列化进 model_dump_json()：以前 --json 输出里没有它
    @property
    def verdict(self) -> str:
        if self.delta_pass_k is None:
            return "no gold outcomes to compare"
        if self.delta_pass_k > 0 and not self.broken_tasks:
            return "improved"
        if self.delta_pass_k > 0:
            return "improved with regressions"
        if self.delta_pass_k == 0 and not self.broken_tasks and not self.fixed_tasks:
            return "unchanged"
        return "regressed"
