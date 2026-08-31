"""
agents/response.py — the simplest agent alongside triage.py: one LLM
call, no loop. Its ONLY job is turning a root cause into a recommended
next step with a risk category. It never executes anything — that
boundary is deliberate (see the chat history: "recommends only, never
executes, even for safe-looking actions" was a stated design decision,
not a placeholder for a future auto-execute feature).
"""
import json
import logging
import re

from groq import AsyncGroq

from app.state import IncidentState
from app.config import get_settings
from app.remediation.mapper import map_root_cause_to_remediation   
from app.prevention.prevention_rules import generate_prevention_rules

logger = logging.getLogger(__name__)
settings = get_settings()
_llm = AsyncGroq(api_key=settings.groq_api_key)

RESPONSE_PROMPT = """An investigation has concluded. Propose ONE
concrete next step — do not propose to execute anything, only recommend.

Root cause: {hypothesis}
Confidence: {confidence}

Respond ONLY with JSON, no other text:
{{
  "recommended_action": "<one concrete, specific action>",
  "risk_category": "auto_apply_safe|needs_approval|do_not_auto_apply",
  "reasoning": "<one sentence why this risk category>"
}}

risk_category guide:
- auto_apply_safe: fully reversible, no user-facing impact if wrong (e.g. adding a log statement, a read-only diagnostic)
- needs_approval: reversible but has real impact if wrong (e.g. a rollback, a config change, a restart)
- do_not_auto_apply: irreversible, high-impact, or confidence is too low to act on (e.g. confidence < 0.5, or a schema/data migration)
"""


async def respond_to_incident(state: IncidentState) -> dict:

    mapped = map_root_cause_to_remediation(state)   # NEW

    if mapped is not None:                          # NEW branch
        parsed = mapped
        logger.info(f"Remediation mapper matched a rule: {parsed['reasoning']}")
    else:
        prompt = RESPONSE_PROMPT.format(
            hypothesis=state["root_cause_hypothesis"],
            confidence=state["confidence"],
        )
        RESPONSE_SCHEMA = {
            "type": "object",
            "properties": {
                "recommended_action": {
                    "type": "string"
                },
                "risk_category": {
                    "type": "string",
                    "enum": [
                        "auto_apply_safe",
                        "needs_approval",
                        "do_not_auto_apply"
                    ]
                },
                "reasoning": {
                    "type": "string"
                }
            },
            "required": [
                "recommended_action",
                "risk_category",
                "reasoning"
            ],
            "additionalProperties": False
        }

        resp = await _llm.chat.completions.create(
            model=settings.groq_model,
            max_completion_tokens=300,
            reasoning_effort="none",
            response_format={
                "type": "json_object",
            
        },
        messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.choices[0].message.content.strip()

        try:
            parsed = json.loads(raw)
            parsed["action_type"] = None  
        except json.JSONDecodeError as e:
            logger.warning(f"Response step failed to parse ({e}), defaulting to safest option")
            parsed = {
                "recommended_action": "Manual review required — automated recommendation failed to generate.",
                "risk_category": "do_not_auto_apply",
                "reasoning": f"Parse failed ({e}), defaulting to the safest category.",
                "action_type": None,   # NEW
            }

    # HARD OVERRIDE, not a prompt suggestion: low confidence NEVER gets a
    # low-risk category, regardless of what the model proposed. Same
    # "don't trust self-reported judgment on the one thing that isn't
    # actually a judgment call" pattern as the redundant-retrieve guard
    # in your real Codebase Q&A agent.
    if state["confidence"] is not None and state["confidence"] < 0.5 and parsed["risk_category"] != "do_not_auto_apply":
        logger.info(f"Overriding risk_category to do_not_auto_apply — confidence {state['confidence']} is below 0.5")
        parsed["risk_category"] = "do_not_auto_apply"
        parsed["reasoning"] = " (risk category overridden to do_not_auto_apply — confidence too low to act on regardless of the action itself)"
    alert_rules = generate_prevention_rules(state) 
    return {
        "recommended_action": parsed["recommended_action"],
        "risk_category": parsed["risk_category"],
         "remediation_action_type": parsed.get("action_type"),
        "reasoning_trace": state["reasoning_trace"] + [f"Response: {parsed['reasoning']}"],
        "suggested_alert_rules": alert_rules,
    }
