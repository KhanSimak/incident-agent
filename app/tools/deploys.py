"""
tools/deploys.py — a plain lookup, not a composed query, and that's a
deliberate choice worth noticing: not every tool needs CodeAct-style
composition. Deploy history has a small, fixed shape (service + time
window) — there's no expressive query language it's worth the agent
"writing" for. Force composition everywhere and you're adding complexity
without adding capability; use it only where genuine expressiveness
(PromQL, LogQL) is the actual point.
"""
from app.fixtures import Scenario


def get_recent_deploys(scenario: Scenario, service: str, lookback_seconds: int = 900) -> dict:
    """Deploys to `service` within `lookback_seconds` of incident start (timestamp 0)."""
    recent = [
        {"timestamp": d.timestamp, "commit": d.commit, "description": d.description}
        for d in scenario.deploys
        if d.service == service and d.timestamp >= -lookback_seconds
    ]
    return {"service": service, "lookback_seconds": lookback_seconds, "deploys": recent}
