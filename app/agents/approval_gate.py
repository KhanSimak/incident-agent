"""
agents/approval_gate.py — the human-approval checkpoint. Not an LLM call:
risk_category was already decided by response.py (mapper or LLM path),
this node's only job is turning that into an approval_status the pipeline
halts on. There is no executor yet, so "halts" currently means the graph
run ends with approval_status="pending" and nothing further happens
automatically — a human (or a future executor) must act on it out of band.
"""
from app.state import IncidentState


async def gate_on_approval(state: IncidentState) -> dict:
    risk = state["risk_category"]

    if risk == "auto_apply_safe":
        status = "not_required"
    else:
        # covers "needs_approval" AND "do_not_apply" — both require a
        # human before anything happens; do_not_apply additionally can
        # never be auto-executed even once approved (that's enforced at
        # the executor, not here — this node only records intent).
        status = "pending"

    return {"approval_status": status}