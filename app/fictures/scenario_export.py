"""
fixtures/scenario_export.py — turn a live, verified investigation into a
permanent fixture-mode regression scenario.

Deliberately NOT automatic. This re-queries live Prometheus/logs for the
CITED evidence (same replay pattern as verification/verify.py) to capture
real value series, not just the text result_summary already stored on
the incident. Exported scenarios are written to disk for manual review —
they are NOT auto-registered into ALL_SCENARIOS. A human should read the
exported ground_truth before trusting it as a regression baseline: the
agent's own hypothesis is a candidate ground truth, not a verified one,
unless verification_result["overall_status"] == "resolved" was already
confirmed for this incident.
"""
import json
import time
from pathlib import Path

from app.state import IncidentState
from app.config import get_settings
from app.tools.metrics_live import run_metric_query_live
from app.tools.logs_live import run_log_query_live
from app.tools.deploys_live import get_recent_deploys_live

settings = get_settings()

EXPORT_DIR = Path("app/fixtures/exported")


async def _capture_metric(query: str) -> list[tuple[float, float]] | None:
    result = await run_metric_query_live(settings.prometheus_url, query)
    if "error" in result:
        return None
    return result["data_points"]


def _capture_log(query: str) -> list[dict] | None:
    result = run_log_query_live(query, settings.log_dir)
    if "error" in result:
        return None
    return result["matched_lines"]


async def export_scenario(state: IncidentState, scenario_id: str) -> dict:
    """
    Returns the exported scenario dict AND writes it to
    app/fixtures/exported/<scenario_id>.json for manual review.
    Does not modify ALL_SCENARIOS.
    """
    evidence = state.get("evidence") or []
    cited = state.get("cited_evidence_indices") or []
    cited_entries = [
        evidence[i] for i in cited
        if isinstance(i, int) and 0 <= i < len(evidence)
    ]

    metrics: dict[str, list[tuple[float, float]]] = {}
    logs: list[dict] = []

    for e in cited_entries:
        if e["tool"] == "query_metrics":
            points = await _capture_metric(e["query"])
            if points is not None:
                # best-effort metric-name extraction, mirrors
                # prevention_rules.py's approach
                metrics[e["query"]] = points
        elif e["tool"] == "query_logs":
            lines = _capture_log(e["query"])
            if lines is not None:
                logs.extend(lines)

    deploys_result = get_recent_deploys_live(
        state.get("description", ""), settings.deploys_log_path
    )

    verification = state.get("verification_result")
    verified_resolved = bool(
        verification and verification.get("overall_status") == "resolved"
    )

    exported = {
        "scenario_id": scenario_id,
        "description": state["description"],
        "primary_service": None,  # fill in manually — see _extract_primary_service in investigation.py
        "metrics": metrics,
        "logs": logs,
        "deploys": deploys_result.get("deploys", []),
        "ground_truth": state["root_cause_hypothesis"],
        "ground_truth_verified": verified_resolved,  # False = human must confirm before trusting this as a fixture baseline
        "exported_from_incident_id": state["incident_id"],
        "exported_at": time.time(),
    }

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXPORT_DIR / f"{scenario_id}.json"
    out_path.write_text(json.dumps(exported, indent=2))

    return exported