"""
graph.py — wires the three agents together. Read this AFTER reading all
of triage.py, investigation.py, and response.py — this file only makes
sense once you know what each node actually does; it's the "how they
connect" file, not a "what happens" file.

Same overall shape as your real app/agent/graph.py: build_graph() defines
the nodes and edges, run_pipeline() is the one function everything else
calls.
"""
from langgraph.graph import StateGraph, END

from app.state import IncidentState
from app.agents.triage import triage_incident
from app.agents.investigation import reasoning_node, execute_tool_node, answer_node, check_stop_condition
from app.agents.response import respond_to_incident
from app.agents.approval_gate import gate_on_approval


def _triage_router(state: IncidentState) -> str:
    """After triage, either stop (low severity, no investigation needed)
    or continue into the investigation loop."""
    return "investigate" if state["escalate"] else "end_at_triage"


def build_graph():
    graph = StateGraph(IncidentState)

    graph.add_node("triage", triage_incident)
    graph.add_node("reasoning", reasoning_node)
    graph.add_node("execute_tool", execute_tool_node)
    graph.add_node("answer", answer_node)
    graph.add_node("respond", respond_to_incident)
    graph.add_node("approval_gate", gate_on_approval)

    graph.set_entry_point("triage")

    graph.add_conditional_edges(
        "triage",
        _triage_router,
        {"investigate": "reasoning", "end_at_triage": END},
    )

    graph.add_conditional_edges(
        "reasoning",
        check_stop_condition,
        {"execute_tool": "execute_tool", "answer": "answer"},
    )
    graph.add_edge("execute_tool", "reasoning")   # loop back — this is the actual ReAct cycle
    graph.add_edge("answer", "respond")
    graph.add_edge("respond", "approval_gate")   # CHANGED — was "respond", END
    graph.add_edge("approval_gate", END)         # NEW

    return graph.compile()


async def run_pipeline(incident_id: str, description: str, scenario_id: str | None = None) -> IncidentState:
    compiled = build_graph()

    initial_state: IncidentState = {
        "incident_id": incident_id,
        "description": description,
        "scenario_id": scenario_id,
        "suggested_alert_rules": [], 
        "remediation_action_type": None,
        "execution_status": "not_executed",
        "execution_detail": None,
        "execution_started_at": None,

        "severity": None,
        "category": None,
        "escalate": None,
        "evidence": [],
        "reasoning_trace": [],
        "iteration": 0,
        "next_action": None,
        "action_input": None,
        "pending_citations": [],
        "root_cause_hypothesis": None,
        "confidence": None,
        "cited_evidence_indices": [],
        "recommended_action": None,
        "risk_category": None,
        "approval_status": None, 
    }

    return await compiled.ainvoke(initial_state)


async def stream_pipeline(incident_id: str, description: str, scenario_id: str | None = None):
    """
    Same graph as run_pipeline, yielding the state after every node
    instead of only the final result — lets the API push reasoning_trace
    lines to the frontend live, as they're written. Reuses the exact
    human-readable strings reasoning_node/execute_tool_node already
    produce; no new formatting logic.
    """
    compiled = build_graph()

    initial_state: IncidentState = {
        "incident_id": incident_id,
        "description": description,
        "scenario_id": scenario_id,
        "severity": None, "category": None, "escalate": None,
        "evidence": [], "reasoning_trace": [], "iteration": 0,
        "next_action": None, "action_input": None, "pending_citations": [],
        "root_cause_hypothesis": None, "confidence": None, "cited_evidence_indices": [],
        "recommended_action": None, "risk_category": None,
        "approval_status": None, "verification_result": None,
        "suggested_alert_rules": [], "remediation_action_type": None,
        "execution_status": "not_executed", "execution_detail": None,
    }

    last_trace_len = 0
    final_state = initial_state

    async for state in compiled.astream(initial_state, stream_mode="values"):
        final_state = state
        trace = state.get("reasoning_trace", [])
        while last_trace_len < len(trace):
            yield {"type": "trace", "message": trace[last_trace_len]}
            last_trace_len += 1

    yield {"type": "done", "state": final_state}