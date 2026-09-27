"""Label each cluster with a failure category (MAST / TRAIL condensed).

Jev's `choice` primitive picks one label from the taxonomy given the representatives' evidence.
Without Jev, keyword heuristics over the judges' rationales pick the label. Either way the label
is a category, not a root cause: the card says "suggest looking here first", never "this is why".
"""

from __future__ import annotations

import re
from collections import Counter

from sparkjury.arbiter.jev import JevClient, JevError
from sparkjury.models.cluster import BadCase, Cluster, FailureLabel

_HEURISTICS: list[tuple[FailureLabel, re.Pattern[str]]] = [
    (FailureLabel.LOOP, re.compile(r"(never converged|ran out of steps|max_steps|repeated the same action|loop)", re.I)),
    (FailureLabel.WRONG_TOOL, re.compile(r"(wrong tool|incorrect(ly)? (used|chose|selected)|instead of (modify|updat|cancel|using)|repeated after the user objected|does not match the .*intent|profile address instead)", re.I)),
    (FailureLabel.HALLUCINATED_INFO, re.compile(r"(fabricat|hallucinat|not backed by any tool|no tool result supports|claimed .*(delivered|shipped)|without (any )?(evidence|verification from|checking)|invented|made up)", re.I)),
    (FailureLabel.MISSING_LOOKUP, re.compile(r"(without reading|without first reading|required lookup skipped|status asserted)", re.I)),
    (FailureLabel.UNAUTHENTICATED_ACTION, re.compile(r"(before identity verification|not authenticated|without (identity )?verif(ying|ication)|unauthenticated|without authenticat|skipp?ed authentication)", re.I)),
    (FailureLabel.MISSING_CONFIRMATION, re.compile(r"(without (an )?(explicit |obtaining |receiving |asking for |the user's )?(user )?confirmation|no confirmation|did not confirm|never confirmed|before (the user )?confirm)", re.I)),
    (FailureLabel.WRONG_ARGS, re.compile(r"(wrong argument|bad input|rejected for bad input|invalid argument|missing argument)", re.I)),
    (FailureLabel.PREMATURE_STOP, re.compile(r"(stopped before|gave up|premature|ended the conversation early)", re.I)),
    (FailureLabel.POLICY_VIOLATION, re.compile(r"(policy|not allowed|violat)", re.I)),
]

SUGGESTIONS: dict[FailureLabel, str] = {
    FailureLabel.WRONG_TOOL: "Clarify tool descriptions so order-level vs profile-level operations are unambiguous; add a routing example to the system prompt.",
    FailureLabel.WRONG_ARGS: "Tighten argument schemas and add one worked example per tool in its description.",
    FailureLabel.MISSING_LOOKUP: "Require a read of the record before any status statement or write; add 'always fetch before you answer' to the prompt.",
    FailureLabel.MISSING_CONFIRMATION: "Make the confirmation step explicit in the prompt: restate the action, wait for a literal 'yes', then act.",
    FailureLabel.UNAUTHENTICATED_ACTION: "Gate every write tool behind a verified user id; reject calls without one at the tool layer.",
    FailureLabel.HALLUCINATED_INFO: "Instruct the agent to only state facts present in tool results; consider a post-hoc citation check.",
    FailureLabel.LOOP: "Add a max-retries rule and an explicit 'ask the user' fallback when the same action fails or is rejected twice.",
    FailureLabel.PREMATURE_STOP: "Add a completion checklist the agent must satisfy before ending the conversation.",
    FailureLabel.POLICY_VIOLATION: "Surface the violated policy rule in the prompt with a concrete example.",
    FailureLabel.OTHER: "Review the representative traces manually; no dominant pattern detected.",
}


def label_heuristic(cluster: Cluster, badcases: dict[str, BadCase]) -> tuple[FailureLabel, float]:
    votes: Counter[FailureLabel] = Counter()
    for tid in cluster.member_trace_ids:
        b = badcases.get(tid)
        if not b:
            continue
        text = b.feature_text
        hits = [label for label, pat in _HEURISTICS if pat.search(text)]
        # specific categories beat the generic ones: LOOP / OTHER only count when nothing specific matched
        specific = [h for h in hits if h not in (FailureLabel.LOOP, FailureLabel.POLICY_VIOLATION)]
        if specific:
            for h in specific:
                votes[h] += 1
        elif hits:
            votes[hits[0]] += 1
        else:
            votes[FailureLabel.OTHER] += 1
    if not votes:
        return FailureLabel.OTHER, 0.0
    label, n = votes.most_common(1)[0]
    return label, n / max(1, cluster.size)


def label_with_jev(cluster: Cluster, jev: JevClient) -> tuple[FailureLabel, float | None]:
    state_lines = [f"A cluster of {cluster.size} failing customer-service agent conversations. Representative evidence:"]
    for r in cluster.representatives:
        state_lines.append(f"--- {r.trace_id} ---\n{r.excerpt}")
    # 逃生选项（接口④）：外部实测——把正确选项从列表拿掉后，Jev 仍以 80%+ 置信度
    # 押与事实矛盾的选项。没有出口的单选题会逼一个决策模型硬选；给出口，选了就
    # 如实降级走启发式，不硬贴标签。
    criteria = dict(FailureLabel.descriptions())
    criteria["none_of_the_above"] = "None of these categories fits the evidence."
    answers = jev.ask("\n".join(state_lines), {
        "label": JevClient.q_choice("Which failure category best describes this cluster?", criteria)
    })
    choice, conf = JevClient.parse_choice(answers["label"])
    if choice == "none_of_the_above":
        raise JevError("jev took the escape option: none of the categories fits")
    try:
        return FailureLabel(choice), conf
    except ValueError as e:
        raise JevError(f"unknown label from Jev: {choice}") from e


def label_clusters(clusters: list[Cluster], badcases: list[BadCase], jev: JevClient | None = None) -> dict[str, int]:
    """给每个簇打标签，并如实回报这次走的是哪条路径。

    返回值是给调用方记账用的：`jev` 传了但没配 key、或者 Jev 调用失败时，标签会退回启发式。
    退回本身没问题（离线要能跑），但不该被瞒下来——AGENTS.md 的诚实降级规则要求每次降级都
    写进 manifest 的 degradations。这里只数清「本来想用 Jev、实际没算成」的簇有几个，怎么记
    由调用方决定，所以这一层不依赖 harness。
    """
    by_id = {b.trace_id: b for b in badcases}
    counts = {"jev": 0, "heuristic": 0, "n_jev_failed": 0}
    for c in clusters:
        if c.cluster_id == -1:
            c.label, c.label_source, c.label_confidence = FailureLabel.OTHER, "none", None
            c.summary = f"{c.size} unclustered badcase(s); review individually."
            c.suggestion = SUGGESTIONS[FailureLabel.OTHER]
            continue
        label: FailureLabel | None = None
        if jev is not None and jev.configured:
            try:
                label, conf = label_with_jev(c, jev)
                c.label, c.label_source, c.label_confidence = label, "jev", conf
                counts["jev"] += 1
            except JevError:
                label = None
                counts["n_jev_failed"] += 1
        if label is None:
            c.label, c.label_confidence = label_heuristic(c, by_id)
            c.label_source = "heuristic"
            counts["heuristic"] += 1
        dims = ", ".join(f"{k}x{v}" for k, v in c.failed_dimension_counts.items())
        c.summary = f"{c.size} trace(s), {c.share:.0%} of badcases, failed dimensions: {dims}."
        c.suggestion = SUGGESTIONS[c.label]
    return counts
