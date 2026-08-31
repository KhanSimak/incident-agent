"""
remediation/mapper.py — deterministic root-cause -> remediation lookup.

Design constraint: this must not depend on how the LLM happened to phrase
the hypothesis. Hypothesis text is free-form and can vary run to run even
for the same underlying fault, so keying rules off substrings in it is
brittle. Instead, every rule here keys off STRUCTURED state that the
pipeline already validated before this point:

  - state["category"]                 (set by triage)
  - cited evidence tools               (which evidence the answer actually cites)
  - the cited evidence's own `query`   (the real PromQL/LogQL the agent
                                         composed, which can only reference
                                         metrics that discover_metrics
                                         actually returned — see
                                         investigation.py's "Do NOT invent
                                         metric names" rule)

This runs BEFORE the LLM call in response.py. If a rule matches, we skip
the LLM entirely for that incident. Returns None (defer to the LLM path)
whenever the structured evidence doesn't clearly identify a known pattern
— we do not fall back to guessing from hypothesis wording.
"""
from app.state import IncidentState, EvidenceEntry


def _cited_evidence(state: IncidentState) -> list[EvidenceEntry]:
    evidence = state.get("evidence") or []
    cited = state.get("cited_evidence_indices") or []

    return [
        evidence[i]
        for i in cited
        if isinstance(i, int) and 0 <= i < len(evidence)
    ]


def _cited_tools(cited: list[EvidenceEntry]) -> set[str]:
    return {e["tool"] for e in cited}


def _cited_positive_tools(cited: list[EvidenceEntry]) -> set[str]:
    """
    Which tools were cited AND actually returned a positive, informative
    finding. `_cited_tools` alone only tells you the tool was cited —
    not whether what it found supports the hypothesis. A deploy_regression
    hypothesis citing get_recent_deploys evidence that found ZERO recent
    deploys would still pass `"get_recent_deploys" in _cited_tools(...)`,
    which used to let a rule recommend "roll back the deployment" on the
    strength of evidence that actually found no deployment at all. Every
    rule below that checks tool presence (as opposed to
    `_cited_metric_queries_text`, which already filters this way for
    query_metrics) must use this instead.
    """
    return {
        e["tool"]
        for e in cited
        if e.get("informative") is True and e.get("signal") == "positive"
    }


def _cited_metric_queries_text(cited: list[EvidenceEntry]) -> str:
    """
    Only include cited query_metrics evidence that was actually
    informative and positive.
    """
    return " ".join(
        e["query"].lower()
        for e in cited
        if (
            e["tool"] == "query_metrics"
            and e.get("informative") is True
            and e.get("signal") == "positive"
        )
    )
# Each entry: (predicate, action, risk_category, reasoning)
# predicate receives (category, cited_tools, metric_query_text) and
# returns True/False. First match wins.
#
# Resource-type rules match on substrings that appear in the METRIC NAME
# itself (e.g. "pool", "resident_memory", "open_fds", "cpu") inside the
# cited query, not on hypothesis phrasing. That ties the rule to what was
# actually queried and validated, not to how the answer was worded.
MAPPING_RULES: list[tuple] = [
    (
        lambda cat, tools, mq: "query_metrics" in tools and "pool" in mq,
        "Increase the connection pool size and add a pool-utilization "
        "alert; investigate what is holding connections open.",
        "needs_approval",
        "resource_exhaustion pattern: cited query_metrics evidence queried a pool metric",
        None,
    ),
    (
        lambda cat, tools, mq: "query_metrics" in tools
        and "resident_memory_bytes" in mq,
        "Restart the affected service to reclaim memory, then schedule "
        "a fix for the leak (do not rely on restart as the permanent fix).",
        "needs_approval",
        "resource_exhaustion pattern: cited query_metrics evidence queried resident memory",
        "restart_service",
    ),
    (
        lambda cat, tools, mq: "query_metrics" in tools
        and ("open_fds" in mq or "fds" in mq or "file_descriptor" in mq),
        "Restart the affected service to release file descriptors, then "
        "audit the code path that opens them without closing.",
        "needs_approval",
        "resource_exhaustion pattern: cited query_metrics evidence queried an fd metric",
        "restart_service",
    ),
    (
        lambda cat, tools, mq: "query_metrics" in tools and "cpu" in mq,
        "Scale the service horizontally or raise its CPU limit; add a "
        "sustained high-CPU alert.",
        "needs_approval",
        "resource_exhaustion pattern: cited query_metrics evidence queried a cpu metric",
        None,
    ),
    (
        lambda cat, tools, mq: cat == "deploy_regression"
        and "get_recent_deploys" in tools,
        "Roll back to the previous deployment.",
        "needs_approval",
        "deploy_regression category confirmed by cited get_recent_deploys evidence",
        None,
    ),
    (
        lambda cat, tools, mq: cat == "downstream_dependency"
        and "get_service_dependents" in tools,
        "Investigate and, if confirmed, fail over or isolate the "
        "implicated dependency.",
        "needs_approval",
        "downstream_dependency category confirmed by cited get_service_dependents evidence",
        None,
    ),
]


def map_root_cause_to_remediation(state: IncidentState) -> dict | None:
    confidence = state.get("confidence")
    hypothesis = state.get("root_cause_hypothesis")

    if confidence is None or confidence < 0.5 or not hypothesis:
        return None

    cited = _cited_evidence(state)
    if not cited:
        return None

    cat = state.get("category")
    tools = _cited_positive_tools(cited)
    metric_query_text = _cited_metric_queries_text(cited)

    for predicate, action, risk, reason,action_type in MAPPING_RULES:
        if predicate(cat, tools, metric_query_text):
            return {
                "recommended_action": action,
                "risk_category": risk,
                "reasoning": reason,
                "action_type": action_type,
            }

    return None