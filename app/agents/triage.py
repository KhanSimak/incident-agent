"""
agents/triage.py — deliberately the simplest agent in this project: ONE
LLM call, no loop, no tools. Read this file first, before investigation.py
— it's the plainest example of "ask an LLM to make a structured decision"
before you look at the more complex iterative version.
"""
import asyncio
import json
import logging
import re

from groq import AsyncGroq

from app.state import IncidentState
from app.config import get_settings
from app.fixtures import ALL_SCENARIOS
from app.tools.metrics_live import list_services_live
from app.tools.logs_live import list_log_services

logger = logging.getLogger(__name__)
settings = get_settings()
_llm = AsyncGroq(api_key=settings.groq_api_key)


async def discover_known_services(state: IncidentState) -> list[str]:
    """
    Build the catalog of services that actually exist for this run, from
    the same backend the investigation will query — so the catalog can
    never disagree with what the tools can see.

    Live:    Prometheus `service` label values, unioned with the services
             that have a log file on disk. Either source may be ahead of
             the other (a service can log before it's scraped), so both
             are used.
    Fixture: the scenario's own logs, service graph and primary service.

    Returns [] when nothing is discoverable (Prometheus down or slow, no
    logs yet). Callers treat that as "unknown" and fall back to the
    naming convention rather than blocking — an unreachable OR merely
    slow backend must not make the agent refuse every action, and must
    not stall triage either: list_services_live already carries its own
    short timeout, and the wait_for below is a hard backstop in case
    that timeout is ever bypassed (e.g. DNS resolution hanging before
    httpx's own clock starts).
    """
    services: set[str] = set()

    if settings.data_source == "live":
        try:
            live = await asyncio.wait_for(
                list_services_live(settings.prometheus_url), timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Service discovery against Prometheus did not return "
                "within 5s; proceeding with an empty catalog for this run."
            )
            live = []
        services.update(live)
        services.update(list_log_services(settings.log_dir))
    else:
        scenario = ALL_SCENARIOS.get(state.get("scenario_id") or "")
        if scenario:
            services.add(scenario.primary_service)
            services.update(line.service for line in scenario.logs)
            services.update(d.service for d in scenario.deploys)
            for caller, callee in scenario.service_graph:
                services.update((caller, callee))

    return sorted({s.strip().lower() for s in services if s and s.strip()})

TRIAGE_PROMPT = """An incident has been reported.

Classify the incident. Do not investigate the cause yet. Decide how
urgent it is and whether it needs deeper investigation.

Incident: {description}

Return ONLY a valid JSON object with exactly these fields:

{{
  "severity": "low|medium|high|critical",
  "category": "deploy_regression|resource_exhaustion|downstream_dependency|unknown",
  "escalate": true,
  "reasoning": "one sentence explaining the classification"
}}

severity must be one of:
low, medium, high, critical.

category must be one of:
deploy_regression, resource_exhaustion, downstream_dependency, unknown.

escalate must be a boolean: true or false.

escalate=false ONLY for genuinely low-severity, self-evidently benign
reports. When in doubt, escalate.
"""

async def triage_incident(state: IncidentState) -> dict:
    prompt = TRIAGE_PROMPT.format(
        description=state["description"]
    )

    resp = await _llm.chat.completions.create(
        model=settings.groq_model,
        max_completion_tokens=500,
        reasoning_effort="none",
        response_format={
            "type": "json_object"
        },
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
    )

    raw = (resp.choices[0].message.content or "").strip()

    try:
        cleaned = re.sub(
            r"^```json\s*|\s*```$",
            "",
            raw,
            flags=re.MULTILINE
        ).strip()

        parsed = json.loads(cleaned)

    except json.JSONDecodeError as e:
        logger.warning(
            f"Triage response failed to parse ({e}), "
            f"defaulting to escalate"
        )

        parsed = {
            "severity": "medium",
            "category": "unknown",
            "escalate": True,
            "reasoning": (
                f"Parse failed ({e}), defaulting to escalate."
            ),
        }

    # Discover which services actually exist, then resolve the affected
    # one ONCE, here, and pin both into state so every downstream node
    # agrees on the target instead of re-deriving (and re-losing) it from
    # free text on each loop iteration. Imported locally to avoid a
    # triage <-> investigation import cycle.
    #
    # Only paid for when this incident is actually escalating: a
    # non-escalated incident never enters the investigation loop, so
    # known_services/affected_service are never read for it, and there
    # is no reason to make it wait on a Prometheus round-trip (or
    # anything else) it will never use.
    from app.agents.investigation import resolve_affected_service

    escalate = parsed.get("escalate", True)

    if not escalate:
        return {
            "severity": parsed.get("severity", "medium"),
            "category": parsed.get("category", "unknown"),
            "escalate": False,
            "affected_service": None,
            "known_services": [],
            "reasoning_trace": [
                f"Triage: {parsed.get('reasoning', '')} "
                f"(severity={parsed.get('severity')}, escalate=False)"
            ],
        }

    known_services = await discover_known_services(state)

    affected_service = resolve_affected_service(
        {**state, "known_services": known_services}
    )

    if affected_service:
        service_note = f"Affected service resolved: {affected_service}."
    else:
        service_note = (
            "Affected service could not be resolved from the incident "
            "description — investigation must identify it from evidence "
            "rather than assume one."
        )
        logger.warning(
            "Triage could not resolve an affected service from description: "
            f"{state['description']!r} (known services: {known_services or 'none discovered'})"
        )

    return {
        "severity": parsed.get("severity", "medium"),
        "category": parsed.get("category", "unknown"),
        "escalate": parsed.get("escalate", True),
        "affected_service": affected_service,
        "known_services": known_services,
        "reasoning_trace": [
            f"Triage: {parsed.get('reasoning', '')} "
            f"(severity={parsed.get('severity')}, "
            f"escalate={parsed.get('escalate')})",
            f"Triage: {service_note}",
        ],
    }