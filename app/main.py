import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware 
import json as _json
from fastapi.responses import StreamingResponse
from app.graph import run_pipeline, stream_pipeline

from app.fixtures import ALL_SCENARIOS
from app.config import get_settings
from app.verification.verify import verify_incident   # NEW
from app.remediation.executor import execute_remediation
from app.fictures.scenario_export import export_scenario
from app.testing.fault_injector import inject_fault, generate_traffic, VALID_FAULT_MODES


settings = get_settings()
_incident_store: dict[str, dict] = {} 

app = FastAPI(
    title="Incident Investigation Agent — teaching scaffold"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",     # VS Code Live Server default
        "http://127.0.0.1:5500",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "null",
        "https://incident-agent-henna.vercel.app",                     # frontend opened directly as a file:// URL
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

class IncidentRequest(BaseModel):
    description: str
    scenario_id: str | None = None
    


@app.get("/scenarios")
async def list_scenarios():
    return {
        sid: {
            "suggested_description": s.description,
            "primary_service": s.primary_service
        }
        for sid, s in ALL_SCENARIOS.items()
    }


@app.get("/incidents/stream")
async def stream_incident(description: str, scenario_id: str | None = None):
    incident_id = str(uuid.uuid4())[:8]

    async def event_generator():
        async for event in stream_pipeline(incident_id, description, scenario_id):
            if event["type"] == "trace":
                yield f"event: trace\ndata: {_json.dumps({'message': event['message']})}\n\n"
            else:
                s = event["state"]
                _incident_store[incident_id] = s
                payload = {
                    "incident_id": incident_id,
                    "triage": {"severity": s["severity"], "category": s["category"], "escalated": s["escalate"]},
                    "root_cause_hypothesis": s["root_cause_hypothesis"],
                    "confidence": s["confidence"],
                    "cited_evidence": [s["evidence"][i] for i in s["cited_evidence_indices"]],
                    "recommended_action": s["recommended_action"],
                    "risk_category": s["risk_category"],
                    "approval_status": s["approval_status"],
                    "suggested_alert_rules": s["suggested_alert_rules"],
                    "full_evidence_gathered": s["evidence"],
                    "reasoning_trace": s["reasoning_trace"],
                    "iterations": s["iteration"],
                }
                yield f"event: done\ndata: {_json.dumps(payload)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/incidents")
async def create_incident(req: IncidentRequest):

    if settings.data_source != "live":
        if req.scenario_id not in ALL_SCENARIOS:
            raise HTTPException(
                404,
                f"Unknown scenario_id. Available: {list(ALL_SCENARIOS.keys())}"
            )

    incident_id = str(uuid.uuid4())[:8]

    final_state = await run_pipeline(
        incident_id,
        req.description,
        req.scenario_id
    )
    # module-level, near the other globals
      # NEW — incident_id -> final_state
    _incident_store[incident_id] = final_state   # NEW
    return {
        "incident_id": incident_id,
        "triage": {
            "severity": final_state["severity"],
            "category": final_state["category"],
            "escalated": final_state["escalate"],
        },
        "root_cause_hypothesis": final_state["root_cause_hypothesis"],
        "confidence": final_state["confidence"],
        "cited_evidence": [
            final_state["evidence"][i]
            for i in final_state["cited_evidence_indices"]
        ],
        "recommended_action": final_state["recommended_action"],
        "risk_category": final_state["risk_category"],
        "suggested_alert_rules": final_state["suggested_alert_rules"],
        "full_evidence_gathered": final_state["evidence"],
        "reasoning_trace": final_state["reasoning_trace"],
        "iterations": final_state["iteration"],
    }


class ApprovalDecision(BaseModel):
    decision: str  # "approved" | "rejected"


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    state = _incident_store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Unknown incident_id")
    return {
        "incident_id": incident_id,
        "root_cause_hypothesis": state["root_cause_hypothesis"],
        "confidence": state["confidence"],
        "recommended_action": state["recommended_action"],
        "risk_category": state["risk_category"],
        "approval_status": state["approval_status"],
    }


@app.post("/incidents/{incident_id}/approval")
async def decide_approval(incident_id: str, decision: ApprovalDecision):
    state = _incident_store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Unknown incident_id")

    if state["approval_status"] != "pending":
        raise HTTPException(
            400,
            f"Incident is not awaiting approval (current status: "
            f"{state['approval_status']!r})"
        )

    if decision.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")

    # do_not_apply can be approved for visibility/manual action, but a
    # future executor must independently refuse to auto-execute it even
    # when approval_status == "approved" — that check belongs in the
    # executor, not here. This endpoint only records human intent.
    state["approval_status"] = decision.decision
    _incident_store[incident_id] = state

    return {
        "incident_id": incident_id,
        "approval_status": state["approval_status"],
        "risk_category": state["risk_category"],

    }


@app.post("/incidents/{incident_id}/verify")
async def verify(incident_id: str):
    state = _incident_store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Unknown incident_id")

    if state["approval_status"] != "approved":
        raise HTTPException(
            400,
            f"Incident must be approved before verification "
            f"(current approval_status: {state['approval_status']!r})"
        )

    result = await verify_incident(state)
    state["verification_result"] = result
    _incident_store[incident_id] = state

    return {
        "incident_id": incident_id,
        "verification_result": result,
    }


@app.post("/incidents/{incident_id}/execute")
async def execute(incident_id: str):
    state = _incident_store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Unknown incident_id")

    result = execute_remediation(state)
    state.update(result)
    _incident_store[incident_id] = state

    return {"incident_id": incident_id, **result}

class ExportScenarioRequest(BaseModel):
    scenario_id: str


@app.post("/incidents/{incident_id}/export_scenario")
async def export(incident_id: str, req: ExportScenarioRequest):
    state = _incident_store.get(incident_id)
    if state is None:
        raise HTTPException(404, "Unknown incident_id")

    if settings.data_source != "live":
        raise HTTPException(400, "Scenario export only makes sense from a live-mode incident.")

    exported = await export_scenario(state, req.scenario_id)
    return {"incident_id": incident_id, "exported_to": f"app/fixtures/exported/{req.scenario_id}.json", "scenario": exported}

class FaultInjectRequest(BaseModel):
    service: str
    fault_mode: str


class TrafficRequest(BaseModel):
    service: str
    count: int = 50
    delay_ms: int = 150


@app.post("/admin/inject_fault")
async def admin_inject_fault(req: FaultInjectRequest):
    if settings.data_source != "live":
        raise HTTPException(400, "Fault injection only applies in live mode.")
    result = inject_fault(req.service, req.fault_mode)
    if not result["ok"]:
        raise HTTPException(400, result["detail"])
    return result


@app.post("/admin/generate_traffic")
async def admin_generate_traffic(req: TrafficRequest):
    if settings.data_source != "live":
        raise HTTPException(400, "Traffic generation only applies in live mode.")
    return await generate_traffic(req.service, req.count, req.delay_ms)
