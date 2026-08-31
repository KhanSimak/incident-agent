"""
prevention/prevention_rules.py — template a candidate Prometheus alert
rule from evidence that was already cited for the root-cause hypothesis.

Pure templating, no LLM call. Reuses the exact PromQL the investigation
agent already composed and validated (see investigation.py's
"Do NOT invent metric names" rule) and the anomaly stats detect_anomaly()
already computed for it — this only reformats data that already exists,
it never queries anything new.

result_summary for query_metrics is built in investigation.py as
f"anomaly={result.get('anomaly')}" — a Python str() of the dict returned
by detect_anomaly() (see tools/metrics.py), NOT JSON. ast.literal_eval is
safe to use on it because that dict only ever contains bool/float/None/str
values, never arbitrary expressions.
"""
import ast
import re

from app.state import IncidentState, EvidenceEntry


def _parse_anomaly(result_summary: str) -> dict | None:
    match = re.match(r"^anomaly=(\{.*\})$", result_summary.strip())
    if not match:
        return None
    try:
        return ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return None


def _extract_metric_name(query: str) -> str | None:
    match = re.match(
        r"^\s*(?:rate|irate|increase|sum|avg|max|min)?\s*\(?\s*"
        r"([a-zA-Z_:][a-zA-Z0-9_:]*)",
        query,
    )
    return match.group(1) if match else None


def suggest_alert_rule(evidence: EvidenceEntry) -> dict | None:
    """
    Returns None when the evidence isn't a query_metrics entry, doesn't
    parse, or wasn't actually anomalous — a normal reading gives nothing
    to propose a threshold from.
    """
    if evidence["tool"] != "query_metrics":
        return None

    anomaly = _parse_anomaly(evidence["result_summary"])
    if not anomaly or not anomaly.get("anomalous"):
        return None

    metric_name = _extract_metric_name(evidence["query"])
    if not metric_name:
        return None

    baseline = anomaly.get("baseline_avg")
    if baseline is None:
        return None

    if anomaly.get("z_score") is not None:
        # Normal-variance path. Threshold set halfway between baseline
        # and this incident's actual anomalous reading, so an alert
        # fires on early drift next time rather than only at full
        # incident severity.
        recent = anomaly["recent_avg"]
        threshold = baseline + 0.5 * (recent - baseline)
        rationale = (
            f"observed z_score={anomaly['z_score']} against "
            f"baseline_avg={baseline}; threshold set halfway to the "
            f"anomalous reading of {recent} seen in this incident"
        )
    else:
        # Flat-baseline / absolute-change path.
        change = anomaly.get("absolute_change", 0)
        threshold = baseline + 0.5 * change
        rationale = (
            f"flat baseline_avg={baseline}; observed absolute_change="
            f"{change}; threshold set halfway to the anomalous change "
            f"seen in this incident"
        )

    return {
        "metric": metric_name,
        "expr": f"{metric_name} > {round(threshold, 3)}",
        "for": "5m",
        "rationale": rationale,
        "source_query": evidence["query"],
    }


def generate_prevention_rules(state: IncidentState) -> list[dict]:
    evidence = state.get("evidence") or []
    cited = state.get("cited_evidence_indices") or []
    cited_entries = [
        evidence[i] for i in cited
        if isinstance(i, int) and 0 <= i < len(evidence)
    ]

    rules = [suggest_alert_rule(e) for e in cited_entries]
    return [r for r in rules if r is not None]