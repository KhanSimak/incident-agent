"""
agents/triage.py — deliberately the simplest agent in this project: ONE
LLM call, no loop, no tools. Read this file first, before investigation.py
— it's the plainest example of "ask an LLM to make a structured decision"
before you look at the more complex iterative version.
"""
import json
import logging
import re

from groq import AsyncGroq

from app.state import IncidentState
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_llm = AsyncGroq(api_key=settings.groq_api_key)

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

    return {
        "severity": parsed.get("severity", "medium"),
        "category": parsed.get("category", "unknown"),
        "escalate": parsed.get("escalate", True),
        "reasoning_trace": [
            f"Triage: {parsed.get('reasoning', '')} "
            f"(severity={parsed.get('severity')}, "
            f"escalate={parsed.get('escalate')})"
        ],
    }