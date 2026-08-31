"""
tools/logs.py — same composed-query philosophy as metrics.py, applied to
logs. Real Loki uses LogQL (`{service="x"} |= "pattern"`); this
implements that exact syntax on a small scale, since matching the real
query language (rather than inventing a simplified one) is what makes
this a genuine skill transfer if you ever wire up real Loki later.
"""
import re

from app.fixtures import Scenario


def run_log_query(scenario: Scenario, query: str) -> dict:
    """
    Composed query like: {service="checkout-service"} |= "timeout"

    The agent chooses the service filter AND the search pattern itself —
    e.g. after seeing an anomalous metric, it might compose a query
    specifically searching for "timeout" or "connection" to find the
    log lines that explain WHY the metric moved.
    """
    match = re.match(r'^\s*\{service="([^"]+)"\}\s*\|=\s*"([^"]*)"\s*$', query)
    if not match:
        return {"error": f"Could not parse query '{query}'. Expected format: {{service=\"x\"}} |= \"pattern\""}

    service_filter, pattern = match.groups()

    matches = [
        {"timestamp": log.timestamp, "level": log.level, "message": log.message}
        for log in scenario.logs
        if log.service == service_filter and (pattern == "" or pattern.lower() in log.message.lower())
    ]

    return {
        "service": service_filter,
        "pattern": pattern,
        "matched_lines": matches,
        "match_count": len(matches),
    }
