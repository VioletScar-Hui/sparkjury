"""Badcases and clusters (M5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sparkjury.models.verdict import Dimension


class FailureLabel(StrEnum):
    """Failure taxonomy, condensed from MAST (Berkeley) and TRAIL (Patronus)."""

    WRONG_TOOL = "wrong_tool"                    # right intent, wrong tool for it
    WRONG_ARGS = "wrong_args"                    # right tool, wrong or missing arguments
    MISSING_LOOKUP = "missing_lookup"            # acted or answered without reading the record
    MISSING_CONFIRMATION = "missing_confirmation"  # destructive action without explicit user yes
    UNAUTHENTICATED_ACTION = "unauthenticated_action"  # acted before verifying identity
    HALLUCINATED_INFO = "hallucinated_info"      # stated facts no tool result supports
    LOOP = "loop"                                # repeats the same action, never converges
    PREMATURE_STOP = "premature_stop"            # gave up or ended before the goal
    POLICY_VIOLATION = "policy_violation"        # broke a domain rule (refund limits, etc.)
    OTHER = "other"

    @classmethod
    def descriptions(cls) -> dict[str, str]:
        return {
            cls.WRONG_TOOL: "the agent chose a tool that does not match the user's intent",
            cls.WRONG_ARGS: "the agent called the right tool with wrong or missing arguments",
            cls.MISSING_LOOKUP: "the agent acted or answered without first reading the relevant record",
            cls.MISSING_CONFIRMATION: "a destructive action ran without an explicit user confirmation",
            cls.UNAUTHENTICATED_ACTION: "the agent acted before verifying the user's identity",
            cls.HALLUCINATED_INFO: "the agent stated facts that no tool result supports",
            cls.LOOP: "the agent repeated the same action and never converged",
            cls.PREMATURE_STOP: "the agent stopped before achieving the goal",
            cls.POLICY_VIOLATION: "the agent broke a domain policy rule",
            cls.OTHER: "none of the above fits",
        }


SEVERITY_WEIGHTS: dict[Dimension, float] = {
    Dimension.SAFETY: 3.0,
    Dimension.OUTCOME: 2.0,
    Dimension.TOOL_USE: 1.0,
    Dimension.EFFICIENCY: 1.0,
}


class BadCase(BaseModel):
    trace_id: str
    task_id: str
    failed_dimensions: list[Dimension] = Field(default_factory=list)
    final_scores: dict[str, int | None] = Field(default_factory=dict)
    outcome_label: str | None = None
    severity: float = 0.0                     # sum of weights of failed dimensions
    evidence_steps: list[int] = Field(default_factory=list)
    feature_text: str = ""                    # what gets embedded
    excerpt: str = ""                         # short human-readable evidence for cards
    cluster_id: int | None = None             # -1 = noise / other


class Representative(BaseModel):
    trace_id: str
    task_id: str
    distance: float
    excerpt: str


class Cluster(BaseModel):
    cluster_id: int                           # -1 is the noise bucket
    rank: int = 0
    label: FailureLabel = FailureLabel.OTHER
    label_source: str = "heuristic"           # jev | heuristic | none
    label_confidence: float | None = None
    size: int = 0
    share: float = 0.0                        # size / n_badcases
    severity: float = 0.0                     # mean member severity
    priority: float = 0.0                     # size x severity
    member_trace_ids: list[str] = Field(default_factory=list)
    representatives: list[Representative] = Field(default_factory=list)
    failed_dimension_counts: dict[str, int] = Field(default_factory=dict)
    summary: str = ""
    suggestion: str = ""                      # a suggestion, explicitly not a root cause


class ClusterRun(BaseModel):
    n_badcases: int
    n_clusters: int
    n_noise: int
    embedder: str
    method: str
    clusters: list[Cluster] = Field(default_factory=list)
    badcases: list[BadCase] = Field(default_factory=list)


def clusters_payload(run: ClusterRun) -> dict[str, Any]:
    """`clusters.json` 的落盘形状：ClusterRun 去掉逐条 badcases。

    badcases 留在库里（`store.list_badcases` 查得到），文件里只留计数 n_badcases 当分母——
    落盘契约要稳定、要能被下游 skill 直接读，不是把库整表倒出来。字段与 `Cluster` 一一对应，
    没有新发明；`GET /runs/{run_id}/clusters` 用同一个函数，保证「接口给的」和「文件里的」是同一种东西。
    """
    d = run.model_dump(mode="json")
    d.pop("badcases", None)
    return d
