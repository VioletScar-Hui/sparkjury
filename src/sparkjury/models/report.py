"""Evidence card (M6): the one artifact a PM reads."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from sparkjury.models.cluster import FailureLabel

DISCLAIMER = (
    "Clusters and suggestions point at where to look first. They are not root-cause claims: "
    "locating the exact failing step automatically is unreliable (5-25% in published studies), "
    "so a human confirms before anything is changed."
)


class JudgeOpinion(BaseModel):
    judge: str
    model: str
    dimension: str
    score: int | None
    label: str | None = None
    rationale: str = ""


class CardRepresentative(BaseModel):
    trace_id: str
    task_id: str
    failed_dimensions: list[str] = Field(default_factory=list)
    final_scores: dict[str, int | None] = Field(default_factory=dict)
    excerpt: str = ""
    opinions: list[JudgeOpinion] = Field(default_factory=list)
    decision_sources: dict[str, str] = Field(default_factory=dict)   # dimension -> panel|jev|local|...


class CardCluster(BaseModel):
    rank: int
    cluster_id: int
    label: FailureLabel
    label_source: str
    label_confidence: float | None = None
    size: int
    share: float
    severity: float
    priority: float
    failed_dimension_counts: dict[str, int] = Field(default_factory=dict)
    summary: str = ""
    suggestion: str = ""
    member_trace_ids: list[str] = Field(default_factory=list)
    representatives: list[CardRepresentative] = Field(default_factory=list)


class CardTotals(BaseModel):
    n_traces: int = 0
    n_tasks: int = 0
    n_env_failures: int = 0
    env_kinds: dict[str, int] = Field(default_factory=dict)
    n_scorable: int = 0
    n_scored: int = 0
    n_badcases: int = 0
    n_clusters: int = 0
    n_unclustered: int = 0


class CardQuality(BaseModel):
    pass_rate: float | None = None                 # pass^1 from gold
    pass_k: dict[int, float] = Field(default_factory=dict)
    pass_k_comb: dict[int, float] = Field(default_factory=dict)  # 组合估计：可靠性下限
    pass_at_k: dict[int, float] = Field(default_factory=dict)    # 至少一次：能力上限
    agent_model: str | None = None
    judge_agreement_rate: float | None = None
    n_needing_arbitration: int = 0
    decisions_by_source: dict[str, int] = Field(default_factory=dict)
    n_degraded: int = 0
    n_audited: int = 0
    n_audit_disagreements: int = 0
    n_outcome_fail: int = 0
    mean_scores: dict[str, float | None] = Field(default_factory=dict)   # dimension -> mean final score
    judges: dict[str, dict] = Field(default_factory=dict)


class EvidenceCard(BaseModel):
    run_id: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    title: str = "SparkJury evidence card"
    totals: CardTotals = Field(default_factory=CardTotals)
    quality: CardQuality = Field(default_factory=CardQuality)
    clusters: list[CardCluster] = Field(default_factory=list)
    recommendation: str = ""
    disclaimer: str = DISCLAIMER
    embedder: str | None = None
    cluster_method: str | None = None
