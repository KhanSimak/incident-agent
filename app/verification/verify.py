"""
verification/verify.py — replay the exact evidence that was cited for
the root-cause hypothesis and check whether the signal is still present.

Deliberately reuses the SAME queries the investigation already composed
and validated (not new queries) — the point isn't to re-investigate,
it's to check whether the specific signal that justified the hypothesis
has changed since. Works against fixture or live data depending on
settings.data_source, mirroring how investigation.py itself switches.

Metrics: "resolved" means the replayed query no longer trips the same
anomaly detector. Logs: there's no anomaly score for logs, so "resolved"
means the same pattern now returns zero matches in the lookback window —
a strong-but-different signal from the metric case (presence vs absence),
not a z-score.
"""
from app.state import IncidentState, EvidenceEntry
from app.config import get_settings
from app.fixtures import ALL_SCENARIOS
from app.tools.metrics import run_metric_query
from app.tools.metrics_live import run_metric_query_live
from app.tools.logs import run_log_query
from app.tools.logs_live import run_log_query_live
import ast

settings = get_settings()

async def _replay_metric(state: IncidentState, evidence: EvidenceEntry) -> dict:
    query = evidence["query"]

    # Recover the ORIGINAL anomaly dict, using the same safe
    # ast.literal_eval-on-"anomaly={...}" convention already established
    # in prevention_rules.py — that dict only ever contains
    # bool/float/None/str values, so this is safe.
    #
    # BUG FIX: this used to try to regex out "recent_avg=<number>" from
    # the summary text, but the actual format (Python's str(dict)) is
    # "'recent_avg': <number>" — a colon-separated, quoted-key dict
    # repr, not "key=value". That regex could never match, so
    # original_recent was always None, silently and permanently
    # collapsing onto the fallback path below on every single call.
    original_summary = evidence.get("result_summary", "")
    original_anomaly: dict = {}
    if original_summary.startswith("anomaly="):
        try:
            original_anomaly = ast.literal_eval(original_summary[len("anomaly="):])
        except (ValueError, SyntaxError):
            original_anomaly = {}

    # BUG FIX: this used to call run_metric_query[_live] with no
    # `direction` at all, silently defaulting to "both". For a metric
    # whose concerning direction is an increase (e.g. a leak), "both"
    # means a big DROP right after a successful restart trips
    # detect_anomaly's anomalous=True just as readily as the original
    # leak did — so a genuinely fixed incident could still come back
    # "still_anomalous" on replay.
    #
    # The fix is NOT to re-infer a direction from the replay's own data:
    # that data reflects the post-fix state, which can look completely
    # different in shape from what justified the original hypothesis
    # (flat/stable now vs. trending then), so a fresh inference here
    # could legitimately land on a different direction than investigation
    # used, silently changing what "resolved" means mid-check. Instead,
    # reuse the exact direction investigation.py already settled on —
    # detect_anomaly() stores it as "direction" inside the anomaly dict
    # it returns, which is already sitting right here in the evidence's
    # own result_summary. Falls back to "both" (detect_anomaly's own
    # default) if it's missing, e.g. the original result was the
    # "not enough data points" branch, which never had a direction to
    # begin with.
    direction = original_anomaly.get("direction") or "both"

    if settings.data_source == "live":
        result = await run_metric_query_live(
            settings.prometheus_url, query, direction=direction,
        )
    else:
        scenario = ALL_SCENARIOS[state["scenario_id"]]
        result = run_metric_query(scenario, query, direction=direction)

    if "error" in result:
        return {
            "tool": "query_metrics",
            "query": query,
            "status": "inconclusive",
            "detail": result["error"],
        }

    current_anomaly = result.get("anomaly", {})

    original_recent = original_anomaly.get("recent_avg")
    current_recent = current_anomaly.get("recent_avg")

    # The decisive signal is the current, direction-aware anomaly flag —
    # it already encodes "did the concerning direction of change
    # persist," which a bare recent-value comparison can't (a metric
    # whose concerning direction is a DECREASE would need
    # current_recent > original_recent to count as resolved, not "<").
    # before_value/current_value are carried for display only.
    resolved = not current_anomaly.get("anomalous", False)

    return {
        "tool": "query_metrics",
        "query": query,
        "status": "resolved" if resolved else "still_anomalous",
        "anomaly": current_anomaly,
        "before_value": original_recent,
        "current_value": current_recent,
    }

def _replay_log(state: IncidentState, evidence: EvidenceEntry) -> dict:
    query = evidence["query"]

    if settings.data_source == "live":
        result = run_log_query_live(query, settings.log_dir)
    else:
        scenario = ALL_SCENARIOS[state["scenario_id"]]
        result = run_log_query(scenario, query)

    if "error" in result:
        return {
            "tool": "query_logs",
            "query": query,
            "status": "inconclusive",
            "detail": result["error"],
        }

    matched_lines = result.get("matched_lines", [])

    execution_started_at = state.get("execution_started_at")

    if settings.data_source == "live" and execution_started_at is not None:
        new_matches = []

        for line in matched_lines:
            timestamp = line.get("timestamp") if isinstance(line, dict) else None

            # Defensive: don't let an unexpected timestamp shape (a
            # string, None, or a malformed log line) crash verification —
            # a line we can't confidently place after execution_started_at
            # is treated as old/unremarkable rather than aborting the
            # whole check.
            try:
                is_new = timestamp is not None and float(timestamp) > execution_started_at
            except (TypeError, ValueError):
                is_new = False

            if is_new:
                new_matches.append(line)

        matched_lines = new_matches

    match_count = len(matched_lines)

    return {
        "tool": "query_logs",
        "query": query,
        "status": "resolved" if match_count == 0 else "still_anomalous",
        "match_count": match_count,
    }

async def verify_incident(state: IncidentState) -> dict:
    """
    Returns:
    {
      "overall_status": "resolved" | "still_anomalous" | "inconclusive" | "no_replayable_evidence",
      "checks": [ {tool, query, status, ...}, ... ]
    }

    overall_status is "resolved" only if EVERY replayed check (metric AND
    log) came back resolved — one still-anomalous or still-matching
    result is enough to withhold "resolved", same conservative bias as
    the groundedness check.
    """
    evidence = state.get("evidence") or []
    cited = state.get("cited_evidence_indices") or []
    cited_entries = [
        evidence[i] for i in cited
        if isinstance(i, int) and 0 <= i < len(evidence)
        and evidence[i]["tool"] in ("query_metrics", "query_logs")
    ]

    if not cited_entries:
        return {"overall_status": "no_replayable_evidence", "checks": []}

    checks = []
    for e in cited_entries:
        if e["tool"] == "query_metrics":
            checks.append(await _replay_metric(state, e))
        else:
            checks.append(_replay_log(state, e))

    statuses = {c["status"] for c in checks}
    if statuses == {"resolved"}:
        overall = "resolved"
    elif "inconclusive" in statuses and "still_anomalous" not in statuses:
        overall = "inconclusive"
    else:
        overall = "still_anomalous"

    return {"overall_status": overall, "checks": checks}
    3