"""
state.py — the data that flows through the whole pipeline.

Same idea as AgentState in your real app/agent/state.py: a TypedDict
that gets passed between every step, accumulating information as it
goes. Start reading here — this file tells you WHAT the system tracks,
before you look at HOW any of it gets filled in.
"""
from typing import Literal, TypedDict


class EvidenceEntry(TypedDict):
    """One piece of evidence the Investigation agent gathered — every
    tool call produces one of these. This is what the final root-cause
    hypothesis has to CITE from, so an answer can be checked against
    what was actually found instead of trusted at face value."""
    tool: str            # "query_metrics" | "query_logs" | "get_recent_deploys" | "get_service_dependents"
    query: str            # the actual composed query/args the agent used
    result_summary: str
    informative: bool   # a short, human-readable summary of what came back
    signal: str          # "positive" | "negative" | "neutral" | "no_signal" — see investigation.py's _evidence_signal; mapper.py and answer_node's confidence scoring both key off this


class IncidentState(TypedDict):
    # Input
    incident_id: str
    description: str          # what a human reported, e.g. "payment-service returning 500s"
    scenario_id: str | None          # which fixture dataset this incident is running against

    # Set by the Triage agent
    severity: str | None                  # "low" | "medium" | "high" | "critical"
    category: str | None                  # "deploy_regression" | "resource_exhaustion" | "downstream_dependency" | "unknown"
    escalate: bool | None                 # False = triage alone is the final answer, no investigation needed


    # Accumulated by the Investigation agent's loop
    evidence: list[EvidenceEntry]
    reasoning_trace: list[str]
    iteration: int
    next_action: Literal["discover_metrics","query_metrics", "query_logs", "get_recent_deploys", "get_service_dependents", "search_similar_incidents", "answer"] | None
    action_input: str | None              # the composed query/args for whichever tool is next
    pending_citations: list[int]          # carries cited_evidence_indices from reasoning_node to answer_node — LangGraph nodes only communicate through declared state fields, this is that channel

    # Set once the Investigation loop finishes
    root_cause_hypothesis: str | None
    confidence: float | None              # 0-1, see agents/investigation.py's groundedness check for how this is computed
    cited_evidence_indices: list[int]      # which entries in `evidence` the hypothesis actually references

    # Set by the Response agent
    recommended_action: str | None
    risk_category: Literal["auto_apply_safe", "needs_approval", "do_not_apply"] | None
    approval_status: Literal["not_required", "pending", "approved", "rejected"] | None
    verification_result: dict | None
    suggested_alert_rules: list[dict] | None 
    remediation_action_type: Literal["restart_service", "reset_fault_mode"] | None  # NEW — "restart_service" | "reset_fault_mode" | None
    execution_status: str | None          # NEW — "not_executed" | "succeeded" | "failed"
    execution_detail: str | None
    execution_started_at: float | None