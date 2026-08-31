import json
import logging
import re
import httpx
from groq import AsyncGroq
from openai import AsyncOpenAI   # NEW — OpenRouter is OpenAI-API-compatible

from app.state import IncidentState, EvidenceEntry
from app.config import get_settings
from app.fixtures import ALL_SCENARIOS
from app.tools.metrics import run_metric_query
from app.tools.logs import run_log_query
from app.tools.deploys import get_recent_deploys
from app.tools.topology import get_service_dependents
from app.tools.incidents_rag import search_similar_incidents
from app.tools.metrics_live import run_metric_query_live, discover_metrics
from app.tools.logs_live import run_log_query_live
from app.tools.deploys_live import get_recent_deploys_live

logger = logging.getLogger(__name__)
settings = get_settings()
_llm = AsyncGroq(api_key=settings.groq_api_key)

# OpenRouter fallback client — only constructed if a key is configured, so
# this stays completely inert for anyone not using the fallback (no crash,
# no behavior change). Isolated to this module: nothing outside
# investigation.py needs to know a second provider exists.
_openrouter_configured = bool(settings.openrouter_api_key)
_fallback_llm = (
    AsyncOpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url)
    if settings.openrouter_api_key
    else None
)


class _OpenRouterResponse:
    """
    Minimal adapter so httpx's raw JSON response can be read the same
    way downstream code already reads a Groq/OpenAI SDK response object
    (resp.choices[0].message.content, resp.choices[0].finish_reason).
    Nothing below this class needs to know the transport changed.
    """
    class _Choice:
        class _Message:
            def __init__(self, content: str):
                self.content = content

        def __init__(self, message_content: str, finish_reason: str | None):
            self.message = self._Message(message_content)
            self.finish_reason = finish_reason

    def __init__(self, data: dict):
        choice = (data.get("choices") or [{}])[0]
        message_content = (choice.get("message") or {}).get("content", "")
        finish_reason = choice.get("finish_reason")
        self.choices = [self._Choice(message_content, finish_reason)]


async def _call_openrouter_chat(
    *,
    messages: list[dict],
    max_tokens: int,
    response_format: dict | None = None,
) -> _OpenRouterResponse:
    """
    Direct httpx POST to OpenRouter's chat/completions endpoint, bypassing
    the openai SDK client entirely — the SDK was sending requests that
    OpenRouter rejected with 401 "Missing Authentication header" even
    though the same key works via plain curl, so the Authorization header
    is set explicitly here instead of relying on SDK header handling.
    """
    if not _openrouter_configured:
        raise RuntimeError("OpenRouter is not configured (OPENROUTER_API_KEY unset)")

    payload = {
        "model": settings.openrouter_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenRouter request failed: {resp.status_code} {resp.text[:300]}"
        )

    return _OpenRouterResponse(resp.json())





async def _call_llm_with_fallback(groq_call, openrouter_call, label: str):
    """
    Shared provider-failover wrapper for all three LLM call sites in this
    file (reasoning, fallback synthesis, groundedness check). Tries Groq
    first; on ANY exception (quota, timeout, connection error, API error),
    retries the SAME request through OpenRouter if a fallback client is
    configured. This function only handles WHICH PROVIDER answers — the
    prompt, schema, token limits, and all downstream parsing/validation
    for each call site are unchanged and stay local to that call site.

    groq_call / openrouter_call: zero-arg async callables that each
    return the raw chat-completion response object for their provider.
    Returns (response, provider_label).
    """
    try:
        resp = await groq_call()
        return resp, "groq"
    except Exception as e:
        logger.warning(f"{label}: Groq call failed ({e!r}), attempting OpenRouter fallback")

        if not _openrouter_configured:
            logger.error(
                f"{label}: no OpenRouter fallback configured "
                f"(OPENROUTER_API_KEY unset) — re-raising Groq error"
            )
            raise

        resp = await openrouter_call()
        return resp, "openrouter (fallback after Groq failure)"

MAX_ITERATIONS = 8

REASONING_PROMPT = """You are investigating a production incident.

Incident: {description}
Category (from triage): {category}
Service named in description: {service}
IMPORTANT:
The triage category is only a hypothesis, not ground truth. The named
service (if any) is only what the description happens to mention, not
a confirmed diagnosis — the fault could be in a dependency instead.
Do not assume the category or named service is the root cause.
Use the incident description and gathered evidence to determine
what actually happened.

The investigation must be able to discover that the triage category
is wrong.

Evidence gathered so far ({num_evidence}):
{evidence_summary}

{sufficiency_note}

{iteration_note}

Reasoning trace so far:
{trace}

Choose exactly ONE next action.

The "thought" field must be ONE short sentence, maximum 20 words.
Do not explain your reasoning in detail.

IMPORTANT:
Do NOT invent metric names.

If you need metrics and you do not yet know the real metric names,
first use "discover_metrics".

Only after real metric names have been discovered should you use
"query_metrics" and compose PromQL.

Available actions:

- "discover_metrics": action_input is just the service name.
  Use this when you need to know which Prometheus metrics actually exist
  for the service.

- "query_metrics": action_input is ONE valid PromQL query using metric
  names that were discovered from evidence.

  NEVER invent metric names — only use names that actually appeared in
  discover_metrics evidence for THIS incident.

  For example, if discovery shows:
    <metric_name_1>
    <metric_name_2>

  then valid queries include:
    rate(<metric_name_1>[1m])
    rate(<metric_name_2>[1m])

  Substitute the ACTUAL discovered metric names — the names above are
  placeholders, not real metrics to query.
Metric-type rule:

Use the discovered metric type when composing PromQL.

- counter: rate(), irate(), increase(), or another counter-appropriate query
- gauge: query the current value directly or use an appropriate gauge operation
- histogram: use an appropriate histogram query
- unknown type: do not assume counter semantics

Do not apply rate() to a gauge metric merely because a time trend is desired.
  IMPORTANT:
  Send ONLY ONE PromQL expression per query_metrics action.
  Do NOT combine multiple PromQL expressions with commas.

- "query_logs": action_input must use this exact format:
  {{service="service-name"}} |= "pattern"

  Search for patterns that are relevant to the current incident.

  Examples of possible patterns include:
  timeout
  failed
  error
  exception
  refused
  unavailable
  exhausted
  OOM
  crash
  missing
  invalid
  denied
  but these are examples only.

  Do not blindly search every example.
  Choose the next pattern based on the incident description and
  evidence already gathered.
IMPORTANT FOR LOG INVESTIGATION:
- If a query returns 0 matching lines, do NOT repeat the same query.

- Do not keep guessing arbitrary log keywords after repeated
  unsuccessful searches.

- If two consecutive log queries return 0 matching lines,
  stop guessing log patterns and investigate a different
  evidence source or investigation dimension.

- Possible next evidence sources include:
  query_metrics
  get_recent_deploys
  get_service_dependents
  search_similar_incidents

- Choose the next evidence source based on the incident and
  evidence already gathered.
  itself and determine what failure mode is actually supported by
  the evidence.

- Do not assume the dependency failure is a database failure.
  It could involve network connectivity, timeouts, resource exhaustion,
  application errors, configuration, deployment changes, or another
  service-level failure.

- Choose log patterns based on the incident description and evidence
  already gathered.
- Prefer concrete application exceptions over generic words such as
  "unavailable".
- If a discovered metric directly measures the reported failure or provides
  useful correlation, query it when it can resolve the current uncertainty.
- Do not repeatedly query the same metric without a new investigative purpose.
If evidence identifies a failure mechanism, determine whether the underlying
cause has been established before choosing "answer".
- Do not treat every exception as root-cause evidence.
  The evidence must explain the incident.

- For example, an unrelated TypeError elsewhere in the service is not
  sufficient evidence for a database, network, deployment, or resource
  incident.

  Do not assume that "500" appears literally in the log message.

- "get_recent_deploys": action_input is just the service name.
  Use this to check whether a recent deploy correlates with the incident.

- get_service_dependents is for identifying the relevant dependency
  relationship, not for blindly traversing the dependency graph.
- After using get_service_dependents, investigate the dependency only
  when the incident description or existing evidence provides a reason
  to suspect that dependency.
- Do not recursively traverse unrelated services.

- "search_similar_incidents": action_input is a short description of
  the pattern currently being observed.

- "answer": use this ONLY when the evidence supports a root cause.
  The root cause should be supported by multiple independent pieces of
  evidence when possible.

CRITICAL EVIDENCE RULE:

A negative investigation result means only that the specific query
did not find evidence.

It does NOT prove that the underlying condition is absent.

Examples:

- 0 log matches does not prove there are no errors.
- 0 timeout matches does not prove there are no timeouts.
- 0 matches for "500" does not prove there are no 500 responses.
- A normal metric does not prove the incident is false.
- No recent deployment does not prove that deployment was unrelated.

Negative evidence may help eliminate a hypothesis, but it cannot by
itself establish a root cause or prove that an incident is a false alarm.

Before choosing "answer", the evidence must contain at least one
POSITIVE diagnostic finding that directly supports the hypothesis.

If no positive diagnostic evidence exists, do NOT answer with a root cause.
Continue investigating or answer that the available evidence is
insufficient.

Never convert "nothing found" into "nothing happened."

CRITICAL PROGRESS RULES:

- NEVER choose an action that has already been executed with the SAME
  action_input.
- The evidence list is authoritative.
- Before selecting an action, compare the proposed tool and
  action_input against every previously executed action.
- If the exact same tool + action_input already exists in evidence,
  NEVER select it again.
- When an action has already been executed, choose a different
  investigation action.
- If discover_metrics has already returned actual metric names,
  NEVER call discover_metrics again for that service.

- If get_recent_deploys has already returned deploy information for
  the service, NEVER call get_recent_deploys again for that same service
  unless investigating a different service.

- If a metric query returned useful data, reason from that evidence.
  Do NOT repeat the same metric query.

- If a metric query shows no anomaly but the incident description
  directly states a specific failure symptom, do NOT conclude that the
  incident is resolved. Investigate logs.

- If logs contain a concrete exception that directly explains the
  incident, prefer "answer" immediately unless a specific missing
  correlation is required to establish the root cause.

- If a recent deploy changed the code related to the exception found
  in logs, those pieces of evidence together can support a root cause.

- Do NOT keep gathering evidence merely because more evidence is
  possible.

- When multiple independent pieces of evidence point to the same cause,
  choose "answer".

- Before choosing an action, inspect the evidence gathered so far and
  choose the NEXT useful action. Do not blindly follow a fixed sequence.


ACTION DIVERSITY AND HYPOTHESIS CONTROL:

Do not choose an investigation dimension merely because it is a plausible
failure category.

When no evidence points to a specific failure mechanism yet, prefer a
broad diagnostic measurement or observation that can discriminate between
multiple hypotheses.

After one failure mechanism is observed, do not automatically deepen that
same mechanism. First determine whether another independent signal could
explain it.

Prefer evidence that can distinguish competing explanations over evidence
that merely confirms the current hypothesis.

Do not select the same failure domain repeatedly unless new evidence
requires deeper investigation in that domain.

DECISION RULES:

A concrete exception, metric anomaly, log pattern, deployment change,
or resource anomaly is evidence of a failure mechanism, but it is not
automatically the underlying root cause.

Before declaring a root cause, distinguish:

1. OBSERVED SYMPTOM
   What directly failed or became abnormal?

2. FAILURE MECHANISM
   What immediate mechanism explains that symptom?

3. UNDERLYING CAUSE
   What condition caused that mechanism?

When evidence identifies a downstream symptom, ask:
"What caused this symptom?"

A positive observation does not automatically identify the root cause.

Before choosing "answer", test whether the proposed root cause itself is
supported by the evidence.

For a resource or capacity symptom, determine whether the resource is:
- currently saturated,
- persistently saturated,
- temporarily saturated,
- or merely involved as a downstream effect.

A single timeout, exception, metric anomaly, or resource spike is
insufficient to establish that component as the underlying cause.

Prefer the hypothesis that explains the largest number of independent
incident-specific observations with the fewest unsupported assumptions.

Do not answer at the first plausible mechanism when another independent
diagnostic dimension could establish the underlying cause.

Examples of causal reasoning across different domains:

- Resource:
  resource saturation -> slower processing -> queueing/timeouts.

- Dependency:
  dependency latency -> waiting requests -> application timeouts.

- Networking:
  packet loss or connection failure -> retries -> latency/errors.

- Deployment:
  recent code/config change -> new application failure -> incident symptoms.

- Storage:
  storage exhaustion -> write failures -> application errors.

- Memory:
  memory pressure -> GC/OOM behavior -> request failures.

- Capacity/load:
  traffic increase -> resource saturation -> latency/errors.

These are examples of reasoning patterns, not an incident taxonomy.
Do not assume that an incident belongs to any of these categories.
Use the evidence gathered for the current incident to determine the
actual causal chain.

CONTRADICTORY EVIDENCE:

When a proposed root cause is contradicted by another diagnostic result,
do not continue defending the original hypothesis.

Treat contradiction as an unresolved question that must be explained.

Examples of contradictions include:
- logs indicate resource contention, but the corresponding resource
  metric is normal;
- an error appears, but the metric measuring that failure mechanism is
  normal;
- a deployment exists, but the relevant failure predates the deployment;
- a dependency appears in the topology, but no incident-specific evidence
  implicates it.

When evidence contradicts the current hypothesis:
1. explicitly identify the contradiction,
2. stop treating the current hypothesis as established,
3. investigate another diagnostic dimension that could explain both
   the positive and negative evidence,
4. answer only after the contradiction is resolved or the evidence is
   explicitly insufficient.

   

ROOT-CAUSE CONSISTENCY:

The final hypothesis must be consistent with ALL cited evidence.

If a cited diagnostic result directly contradicts the hypothesis,
do not choose "answer" with that hypothesis.

Either:
1. investigate the contradiction with a new diagnostic action, or
2. answer that the evidence is insufficient.

Never cite a negative or normal metric as support for a hypothesis that
requires that metric to be abnormal.
EVIDENCE AND STOPPING

Once evidence identifies a specific failure mechanism, stop exploring
unrelated failure classes.

If two independent pieces of incident-specific evidence support the same
failure mechanism, choose "answer".

MAX_ITERATIONS is a safety limit, NOT a target.

Do not continue investigating merely because iterations remain.

Every action must have a clear purpose:

"What specific uncertainty about THIS incident will this action resolve?"

If there is no specific unresolved question, choose "answer".

DEPLOYMENT RULE

Never claim that a deployment caused the incident unless deployment
evidence actually supports that conclusion.

Do not investigate deployments merely because they are available as a tool.

DEPENDENCY RULE

Do not investigate service dependencies merely because a dependency tool
exists.

Only investigate a dependency when existing evidence indicates that the
dependency is involved.

METRIC RULE

Never invent metric names.

If a metric is required but its name is unknown, use discover_metrics on
the PRIMARY SERVICE first.

LOG RULE

query_logs accepts exactly:

{{service="SERVICE"}} |= "PATTERN"

Use exactly one service selector and one pattern.

If multiple patterns are necessary, perform separate queries.

Do not use OR, AND, multiple service selectors, or other LogQL syntax.

ACTION SELECTION

Before selecting an action, mentally complete:

Primary service:
Specific failure:
Current evidence:
Unresolved question:
Why this action resolves it:

If the action does not directly address the unresolved question,
do not select it.

OUTPUT CONTRACT — ABSOLUTE REQUIREMENT

Your response MUST be exactly ONE valid JSON object.

NEVER respond with:
- reasoning in plain text
- explanations outside JSON
- markdown
- code fences
- "I should..."
- "We need..."
- "Let's..."
- analysis followed by JSON

The FIRST CHARACTER of your response MUST be {{.
The LAST CHARACTER of your response MUST be }}.

The JSON object MUST contain exactly these fields:

{{
  "thought": "<brief reason for the selected action>",
  "action": "<one allowed action>",
  "action_input": "<action-specific input>",
  "hypothesis": "<empty string unless action is answer>",
  "cited_evidence_indices": []
}}

Allowed actions:

discover_metrics
query_metrics
query_logs
get_recent_deploys
get_service_dependents
search_similar_incidents
answer

If action is NOT "answer":

- hypothesis MUST be ""
- cited_evidence_indices MUST be []
- action_input MUST contain the required input for that action

If action IS "answer":

- hypothesis MUST contain the supported root-cause explanation
- cited_evidence_indices MUST be a JSON array of integer evidence indices
- action_input MUST be ""

IMPORTANT:

Do NOT write the action as prose.

BAD:

We need log query for database connection error.

BAD:

I should inspect the inventory logs.

BAD:

Let's query the database errors.

GOOD:

{{"thought":"The incident concerns a specific failure mechanism, so I need direct evidence from the primary service.","action":"query_logs","action_input":"{{service=\"SERVICE\"}} |= \"PATTERN\"","hypothesis":"","cited_evidence_indices":[]}}
Return ONLY the JSON object.

All string values MUST use valid JSON escaping.

For example, a log query MUST be represented like this:

{{"thought":"The available evidence does not explain the incident, so I need to inspect the primary service logs for a concrete exception.","action":"query_logs","action_input":"{{service=\"SERVICE\"}} |= \"PATTERN\"","hypothesis":"","cited_evidence_indices":[]}}
Another valid example:


The JSON schema is:

{{"thought":"<brief reasoning>","action":"discover_metrics|query_metrics|query_logs|get_recent_deploys|get_service_dependents|search_similar_incidents|answer","action_input":"<service name, ONE PromQL query, valid log query, or short description>","hypothesis":"<only when action is answer, otherwise empty string>","cited_evidence_indices":[<only when action is answer>]}}
DIAGNOSTIC COVERAGE:

When the current evidence does not establish a root cause, prefer an
unexplored diagnostic dimension that could distinguish competing
hypotheses.

Do not repeatedly investigate the first plausible failure mechanism
just because logs contain evidence related to it.

After an initial symptom is found, consider whether other major evidence
dimensions remain unchecked, including:
- resource behavior
- dependency behavior
- application behavior
- deployment/configuration changes
- traffic/capacity behavior
- persistence over time

Choose the dimension that best resolves the current uncertainty.

Do not treat a discovered but unqueried diagnostic dimension as irrelevant
without evidence.

"""

def _summarize_evidence(evidence: list[EvidenceEntry]) -> str:
    if not evidence:
        return "(nothing gathered yet)"

    lines = []

    for i, e in enumerate(evidence):
        status = "INFORMATIVE" if e.get("informative") else "NO NEW SIGNAL"

        lines.append(
            f"[{i}] {e['tool']}(\"{e['query']}\") "
            f"[{status}] -> {e['result_summary']}"
        )

    return "\n".join(lines)

def _valid_log_query(query: str) -> bool:
    pattern = r'^\{service="[^"]+"\} \|= "[^"]+"$'
    return bool(re.fullmatch(pattern, query.strip()))

def _already_gathered(state: IncidentState, tool: str, query: str) -> bool:
    return any(
        e["tool"] == tool and e["query"] == query
        for e in state["evidence"]
    )


# DIAGNOSTIC_TOOLS excludes discover_metrics (a lookup step, not evidence
# toward a root cause itself), get_service_dependents and
# search_similar_incidents (context, not primary diagnostic signal) —
# these are the four tool types that can actually SUPPORT a root-cause
# claim. This set is what both sufficiency-checking functions below key
# off of, so "what counts as diagnostic" only needs to be defined once.
DIAGNOSTIC_TOOLS = {"query_metrics", "query_logs", "get_recent_deploys"}


def _extract_primary_service(description: str) -> str | None:
    """
    Best-effort extraction of the affected service name from the
    incident's own free-text description — e.g. "payment-service is
    returning 500s" -> "payment-service".

    Deliberately NOT a lookup against scenario/fixture ground truth
    (ALL_SCENARIOS[...].primary_service): this agent is meant to
    investigate any incident, not just the 3 predefined fixture
    scenarios, and a real incident report doesn't come pre-labeled with
    which service is actually at fault — that's what investigation is
    for. This only reads the "<n>-service" naming convention already
    used throughout this codebase's own tool schemas and fixtures, from
    whatever text the incident description happens to contain. If no
    such token is present, returns None rather than guessing — callers
    must treat that as "unknown," not as license to fall back to a
    ground-truth lookup.
    """
    match = re.search(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*-service)\b", description, re.IGNORECASE)
    return match.group(1).lower() if match else None


def validate_action(state: IncidentState, action: str, action_input: str) -> tuple[bool, str]:
    """
    Validate whether the proposed investigation action is useful
    given the evidence already gathered.
    """

    # 1. Never repeat the exact same action + input.
    if _already_gathered(state, action, action_input):
        return False, (
            f"Blocked repeated investigation: "
            f"{action}({action_input})"
        )

    # 1b. If the incident description names a service (e.g.
    # "payment-service is returning 500s"), nudge query_logs toward it —
    # derived from the incident text itself, not from fixture ground
    # truth, so this applies to any incident, not just the 3 predefined
    # scenarios. When no service can be identified from the description,
    # we do NOT block: better to let the model investigate freely than
    # to guess and wrongly constrain it.
    if action == "query_logs":
        expected_service = _extract_primary_service(state["description"])
        match = re.fullmatch(
            r'\{service="([^"]+)"\} \|= "([^"]+)"',
            action_input.strip(),
        )
        if expected_service and match and match.group(1) != expected_service:
            return False, (
                f"Blocked: query_logs targeted service "
                f"'{match.group(1)}', but the incident description "
                f"names '{expected_service}'. Investigate the named "
                f"service first unless evidence explicitly implicates "
                f"a dependency."
            )

    evidence = state["evidence"]

    # 2. Never query a metric that has not been discovered.
    if action == "query_metrics":
        discovered = []

        for e in evidence:
            if e["tool"] == "discover_metrics":
                # result_summary looks like:
                # "discovered 2 metric(s): checkout_errors_created, checkout_errors_total"
                match = re.search(
                    r"discovered \d+ metric\(s\):\s*(.*)",
                    e["result_summary"],
                )

                if match:
                    discovered.extend(
                        m.strip()
                        for m in match.group(1).split(",")
                        if m.strip()
                    )

        
        metric_match = re.match(
            r"^\s*(?:rate|irate|increase|sum|avg|max|min)?\s*\(\s*"
            r"([a-zA-Z_:][a-zA-Z0-9_:]*)"
            r"(?:\{.*\})?"
            r"(?:\[[^\]]+\])?"
            r"\s*\)\s*$",
            action_input,
        )

        if metric_match:
            metric_names = [metric_match.group(1)]
        else:
    # Also allow a simple metric selector such as:
    # db_pool_active_connections{service="checkout-service"}
            simple_match = re.match(
                r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)"
                r"(?:\{.*\})?"
                r"(?:\[[^\]]+\])?\s*$",
                action_input,
            )

            metric_names = (
                [simple_match.group(1)]
                if simple_match
                else []
            )



        unknown = [
            metric
            for metric in metric_names
            if metric not in discovered
        ]

        if unknown:
            return False, (
                f"Blocked PromQL: metric(s) {unknown} "
                f"were not discovered. Use discover_metrics first "
                f"and query only discovered metric names."
            )
    # Note: we deliberately do NOT hard-block further diagnostic actions
    # based on log message content (e.g. matching "connection pool" or
    # "timeout" substrings). That would hardcode a specific fault
    # taxonomy into the validator — it only fires for whichever error
    # wording happens to be on the list, silently does nothing for any
    # other scenario, and blocks the very corroborating query needed to
    # reach _evidence_sufficiency's 2-independent-source bar. "Stop
    # investigating, you likely have enough" is already communicated
    # generically via sufficiency_note in the prompt (diagnostic-type
    # diversity + diminishing returns, no error-string matching) — that
    # stays a nudge the model weighs, not a hard block here.

    return True, ""


def _discovered_metrics(state: IncidentState) -> set[str]:
    """Return metric names that were actually discovered in this investigation."""
    metrics = set()

    for evidence in state["evidence"]:
        if evidence["tool"] != "discover_metrics":
            continue

        match = re.search(r"discovered \d+ metric\(s\): (.+)", evidence["result_summary"])
        if not match:
            continue

        for metric in match.group(1).split(","):
            metrics.add(metric.strip())

    return metrics



def _metric_query_uses_discovered_metric(
    state: IncidentState,
    query: str,
) -> bool:
    discovered = _discovered_metrics(state)

    if not discovered:
        return False

    return any(
        re.search(rf"\b{re.escape(metric)}\b", query)
        for metric in discovered
    )


def _is_informative(tool: str, result: dict) -> bool:
    """
    Tool-agnostic 'did this call actually find something, or come back
    empty' check — deliberately generic, no incident-type logic. Each
    tool's OWN result shape already tells you hit-vs-miss; this just
    reads that shape consistently so downstream code doesn't have to.
    """
    if tool == "discover_metrics":
        return bool(result.get("metrics"))
    if tool == "query_metrics":
        return bool(result.get("anomaly", {}).get("anomalous"))
    if tool == "query_logs":
        return result.get("match_count", 0) > 0
    if tool == "get_recent_deploys":
        return bool(result.get("deploys"))
    if tool == "get_service_dependents":
        return bool(result.get("depends_on") or result.get("depended_on_by"))
    if tool == "search_similar_incidents":
        top = (result.get("results") or [{}])[0]
        return bool(top.get("similarity", 0) > 0.2)
    return False



def _evidence_signal(tool: str, result: dict) -> str:
    """Classify the result as positive, negative, or neutral evidence."""

    if tool == "discover_metrics":
        return "positive" if result.get("metrics") else "negative"

    if tool == "query_metrics":
        anomaly = result.get("anomaly", {})
        return "positive" if anomaly.get("anomalous") else "negative"

    if tool == "query_logs":
        return "positive" if result.get("match_count", 0) > 0 else "negative"

    if tool == "get_recent_deploys":
        return "positive" if result.get("deploys") else "negative"

    return "neutral"


def _evidence_sufficiency(evidence: list[EvidenceEntry]) -> dict:
    """
    General investigation sufficiency check.

    We do NOT hardcode incident types or specific error strings.
    The LLM remains responsible for determining whether the evidence
    explains the incident. This function only measures whether the
    investigation has gathered enough independent diagnostic evidence
    and whether recent investigation attempts are still producing value.
    """

    informative_diagnostic_types = {
        e["tool"]
        for e in evidence
        if e.get("informative") and e["tool"] in DIAGNOSTIC_TOOLS
    }

    recent = evidence[-2:]

    diminishing_returns = (
        len(evidence) >= 2
        and not any(e.get("informative") for e in recent)
    )

    return {
        "diagnostic_source_count": len(informative_diagnostic_types),
        "diagnostic_sources": sorted(informative_diagnostic_types),
        "diminishing_returns": diminishing_returns,
        "sufficient": (
            len(informative_diagnostic_types) >= 2
            and diminishing_returns
        ),
    }

INVESTIGATION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string"
        },
        "action": {
            "type": "string",
            "enum": [
                "discover_metrics",
                "query_metrics",
                "query_logs",
                "get_recent_deploys",
                "get_service_dependents",
                "search_similar_incidents",
                "answer"
            ]
        },
        "action_input": {
            "type": "string"
        },
        "hypothesis": {
            "type": "string"
        },
        "cited_evidence_indices": {
            "type": "array",
            "items": {
                "type": "integer"
            }
        }
    },
    "required": [
        "thought",
        "action",
        "action_input",
        "hypothesis",
        "cited_evidence_indices"
    ],
    "additionalProperties": False
}

# Actions the model is allowed to select. Defined once at module level so
# _parse_decision and the prompt-facing docs stay in sync.
VALID_ACTIONS = {
    "query_metrics",
    "discover_metrics",
    "query_logs",
    "get_recent_deploys",
    "get_service_dependents",
    "search_similar_incidents",
    "answer",
}

# Normalizes common action-name mistakes (camelCase instead of the
# documented snake_case) instead of treating them as a hard parse failure.
ACTION_ALIASES = {
    "getRecentDeploys": "get_recent_deploys",
    "discoverMetrics": "discover_metrics",
    "queryMetrics": "query_metrics",
    "queryLogs": "query_logs",
    "getServiceDependents": "get_service_dependents",
    "searchSimilarIncidents": "search_similar_incidents",
}


async def _call_reasoning_llm(prompt: str) -> str:
    """
    Single call to the reasoning model. Tries Groq first; on any Groq
    failure (quota, timeout, API error), retries the SAME prompt/schema
    through OpenRouter if configured (see _call_llm_with_fallback). This
    is provider failover only — INVESTIGATION_SCHEMA, the prompt text,
    and every downstream parsing/validation step are unchanged and
    identical regardless of which provider actually answered.

    Logs finish_reason alongside content length — a "length" finish_reason
    with reasoning_effort set means max_completion_tokens was hit before
    the JSON object closed, which is a likely cause of "invalid JSON" that
    strict json_schema mode can't prevent (the response never finished).
    If that shows up in the logs, raise max_completion_tokens rather than
    adding retries.
    """
    async def groq_call():
        return await _llm.chat.completions.create(
            model=settings.groq_model,
            max_completion_tokens=500,
            reasoning_effort="none",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "investigation_decision",
                    "schema": INVESTIGATION_SCHEMA,
                },
            },
            messages=[{"role": "user", "content": prompt}],
        )

    async def openrouter_call():
        return await _call_openrouter_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "investigation_decision",
                    "schema": INVESTIGATION_SCHEMA,
                },
            },
        )

    resp, provider = await _call_llm_with_fallback(groq_call, openrouter_call, "reasoning_node")
    choice = resp.choices[0]
    raw = (choice.message.content or "").strip()

    logger.info(
        f"reasoning_node: provider={provider} finish_reason={choice.finish_reason} "
        f"content_len={len(raw)}"
    )
    if choice.finish_reason == "length":
        logger.warning(
            "reasoning_node: response truncated by the token limit "
            "before the JSON object likely closed — this is the usual "
            "cause of downstream JSON parse failures, not malformed "
            "formatting."
        )

    print("\n===== RAW LLM RESPONSE =====")
    print(raw)
    print("===== END RAW RESPONSE =====\n")

    return raw


def _parse_decision(raw: str, state: IncidentState) -> dict:
    """
    Parse and validate one reasoning-model response. Raises
    json.JSONDecodeError / ValueError on any failure — the caller decides
    whether to retry or fall back; this function only ever produces a
    fully-valid decision dict or an exception, never a partial one.
    """
    cleaned = raw.strip()

    # Remove markdown code fences if present.
    cleaned = re.sub(r"```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("No valid JSON object found in LLM response")

    cleaned = cleaned[start:end + 1]
    parsed = json.loads(cleaned)

    if parsed.get("action") in ACTION_ALIASES:
        parsed["action"] = ACTION_ALIASES[parsed["action"]]

    if parsed.get("action") not in VALID_ACTIONS:
        raise ValueError(f"invalid action: {parsed.get('action')}")

    if parsed["action"] == "query_logs":
        if not _valid_log_query(parsed.get("action_input", "")):
            raise ValueError(
                f"invalid query_logs format: {parsed.get('action_input', '')}"
            )

    if (
        parsed["action"] == "query_metrics"
        and state["evidence"]
        and "not discovered" in state["evidence"][-1]["result_summary"].lower()
    ):
        # Redirect to discover_metrics only when we can identify which
        # service to discover from the incident description itself —
        # not from scenario/fixture ground truth. If the description
        # doesn't name a service, don't guess: leave the action as-is
        # and let validate_action's generic "unknown metric" block (which
        # doesn't require knowing a service name) give the model
        # corrective feedback instead.
        inferred_service = _extract_primary_service(state["description"])
        if inferred_service:
            parsed["action"] = "discover_metrics"
            parsed["action_input"] = inferred_service
            parsed["thought"] = (
                "The requested metric was not discovered, so I must "
                "discover the actual metrics before querying PromQL."
            )

    # The json_schema "required" list already guarantees these exist when
    # strict mode worked; this is a defensive backstop for the rare
    # hand-repaired response (e.g. after brace-trimming above) that could
    # otherwise KeyError downstream.
    parsed.setdefault("thought", "No reasoning provided")
    parsed.setdefault("action_input", "")
    parsed.setdefault("hypothesis", "")
    parsed.setdefault("cited_evidence_indices", [])

    return parsed


async def _get_reasoning_decision(prompt: str, state: IncidentState) -> dict:
    """
    Get one validated decision, retrying once with the specific parse/
    validation error fed back to the model before giving up. This turns
    a fixable slip (wrong action name, malformed query_logs syntax) into
    a corrected second attempt instead of immediately forcing a
    low-confidence "answer" on the very first hiccup.
    """
    raw = await _call_reasoning_llm(prompt)
    try:
        return _parse_decision(raw, state)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            f"Investigation reasoning failed to parse ({e}); "
            f"retrying once with the error fed back"
        )
        retry_prompt = (
            prompt
            + "\n\nYour previous response was rejected.\n"
            + f"Previous response:\n{raw}\n\n"
            + f"Validation error: {e}\n\n"
            + "Return ONLY a corrected JSON object matching the schema above. "
            + "Fix only what caused the error."
        )
        raw2 = await _call_reasoning_llm(retry_prompt)
        try:
            return _parse_decision(raw2, state)
        except (json.JSONDecodeError, ValueError) as e2:
            logger.warning(
                f"Investigation reasoning failed to parse after retry "
                f"({e2}), forcing answer with low confidence"
            )
            return {
                "thought": f"Reasoning parse failed twice ({e2})",
                "action": "answer",
                "action_input": "",
                "hypothesis": (
                    "Unable to determine root cause — reasoning step "
                    "failed to produce a valid decision after retry."
                ),
                "cited_evidence_indices": [],
            }


async def reasoning_node(state: IncidentState) -> dict:
    sufficiency = _evidence_sufficiency(state["evidence"])
    sufficiency_note = (
        f"EVIDENCE SUFFICIENCY (computed, not your judgment call): "
        f"{sufficiency['diagnostic_source_count']} independent diagnostic source(s) "
        f"support a finding ({', '.join(sufficiency['diagnostic_sources']) or 'none yet'}). "
        f"Diminishing returns: {sufficiency['diminishing_returns']} "
        f"(the last 1-2 actions found nothing new)."
        + (
            "\n>>> This evidence is SUFFICIENT to answer. Prefer \"answer\" now "
            "over further investigation unless you have a SPECIFIC, stated reason "
            "the current evidence could be misleading."
            if sufficiency["sufficient"] else ""
        )
    )

    iterations_remaining = max(MAX_ITERATIONS - state["iteration"], 0)
    iteration_note = (
        f"ITERATION BUDGET: {state['iteration']}/{MAX_ITERATIONS} used "
        f"({iterations_remaining} remaining). This is a safety cap, not "
        f"a target — running out forces a low-quality answer, so do not "
        f"spend remaining iterations without a specific unresolved question."
    )

    prompt = REASONING_PROMPT.format(
        description=state["description"],
        category=state["category"],
        service=_extract_primary_service(state["description"]) or "not named in the description",
        num_evidence=len(state["evidence"]),
        evidence_summary=_summarize_evidence(state["evidence"]),
        sufficiency_note=sufficiency_note,
        iteration_note=iteration_note,
        trace="\n".join(state["reasoning_trace"]) or "(none yet)",
    )

    parsed = await _get_reasoning_decision(prompt, state)
    thought = parsed["thought"]

    result = {
        "next_action": parsed["action"],
        "action_input": parsed.get("action_input", ""),
        "reasoning_trace": state["reasoning_trace"] + [
            f"Thought: {thought} -> Action: "
            f"{parsed['action']}({parsed.get('action_input', '')})"
        ],
    }
    # When the model decides to answer, write the hypothesis straight
    # into the declared state field (root_cause_hypothesis is already
    # part of IncidentState, nullable) rather than a temp/ad-hoc key —
    # LangGraph state should be fully declared in state.py, not carried
    # through undeclared dict keys. pending_citations IS declared
    # specifically as the reasoning_node -> answer_node handoff channel
    # for the citation list (see state.py's comment on that field).
    if parsed["action"] == "answer":
        result["root_cause_hypothesis"] = parsed.get("hypothesis", "")
        result["pending_citations"] = parsed.get("cited_evidence_indices", [])

    return result



async def execute_tool_node(state: IncidentState) -> dict:
    action = state["next_action"]
    action_input = state["action_input"]

    valid, reason = validate_action(
        state,
        action,
        action_input,
    )

    if not valid:
        logger.warning(reason)

        return {
            "evidence": state["evidence"] + [{
                "tool": "validator",
                "query": f"{action}({action_input})",
                "result_summary": reason,
                "informative": False,
            }],
            "iteration": state["iteration"] + 1,
            "reasoning_trace": state["reasoning_trace"] + [
                f"Validator: {reason}"
            ],
        }

    entry = await _run_tool(state)
    

    return {
        "evidence": state["evidence"] + [entry],
        "iteration": state["iteration"] + 1,
        "reasoning_trace": state["reasoning_trace"] + [
            f"Observation: {entry['result_summary']}"
        ],
    }
    summary = f"{result.get('match_count', 0)} matching lines"
    
    if result.get("matched_lines"):
        summary += ": " + "; ".join(
            l["message"]
            for l in result["matched_lines"][:3]
        )
        print("\n===== TOOL OBSERVATION =====")
        print(entry["result_summary"])
        print("===== END TOOL OBSERVATION =====\n")
    



async def _run_tool(state: IncidentState) -> EvidenceEntry:
    live = settings.data_source == "live"

    scenario = None
    if not live:
        scenario = ALL_SCENARIOS[state["scenario_id"]]
    tool, query = state["next_action"], state["action_input"]
    live = settings.data_source == "live"

    if tool == "discover_metrics":
        if live:
            result = await discover_metrics(
                settings.prometheus_url,
                query,
           )
        else:
            # BUG FIX: this used to hardcode metrics=[] regardless of
            # scenario — meaning fixture-mode discovery could NEVER
            # return a real metric name, forcing the model to either
            # violate "don't invent metric names" or get stuck. Return
            # the scenario's actual known metrics instead, same as the
            # comment here always claimed it did.
            result = {
                "service": query,
                "metrics": list(scenario.metrics.keys()) if query == scenario.primary_service else [],
            }

        if "error" in result:
            summary = result["error"]
        else:
            metrics = result["metrics"]
            summary = (
                f"discovered {len(metrics)} metric(s): "
                + ", ".join(metrics)
                if metrics
                else f"no metrics found for service '{query}'"
            )

        return {
            "tool": tool,
            "query": query,
            "result_summary": summary,
            "informative": _is_informative(tool, result),
        }

    elif tool == "query_metrics":
        if not _metric_query_uses_discovered_metric(state, query):
            return {
                "tool": tool,
                "query": query,
                "result_summary": (
                    "Blocked: PromQL references a metric that was not discovered."
                ),
                "informative": False,
            }

        result = (await run_metric_query_live(settings.prometheus_url, query) if live
                  else run_metric_query(scenario, query))
        summary = f"anomaly={result.get('anomaly')}" if "anomaly" in result else str(result.get("error", result))
    elif tool == "query_logs":
        if not _valid_log_query(query):
            return {
                "tool": tool,
                "query": query,
                "result_summary": (
                    "invalid query format; expected "
                    '{service="x"} |= "pattern"'
                ),
                "informative": False,   # a malformed query never counts as a diagnostic finding
            }
        # Service-targeting is now enforced earlier in validate_action,
        # before execute_tool_node ever calls this function — kept out
        # of this branch so that check lives in exactly one place.
        result = (run_log_query_live(query, settings.log_dir) if live
                  else run_log_query(scenario, query))
        summary = f"{result.get('match_count', 0)} matching lines" if "match_count" in result else str(result.get("error", result))
        if result.get("matched_lines"):
            summary += ": " + "; ".join(l["message"] for l in result["matched_lines"][:3])
    elif tool == "get_recent_deploys":
        result = (get_recent_deploys_live(query, settings.deploys_log_path) if live
                  else get_recent_deploys(scenario, query))
        summary = f"{len(result['deploys'])} recent deploy(s)" + (f": {result['deploys'][0]['description']}" if result["deploys"] else "")
    elif tool == "get_service_dependents":
        if live:
            return {
                "tool": tool,
                "query": query,
                "result_summary": (
                    "Live service topology is not currently configured."
                ),
                "informative": False,
                "signal": "no_signal",
            }

        result = get_service_dependents(scenario, query)
        summary = (
            f"depends_on={result['depends_on']}, "
            f"depended_on_by={result['depended_on_by']}"
        )
    else:  # search_similar_incidents
        # Same in both modes too — past-incident text doesn't become
        # "live" data just because the CURRENT incident is real; the
        # postmortem corpus is static either way.
        result = search_similar_incidents(query)
        top = result["results"][0] if result["results"] else None
        summary = f"best match: {top['id']} (similarity={top['similarity']})" if top else "no matches"

    return {"tool": tool, "query": query, "result_summary": summary, "informative": _is_informative(tool, result,),"signal": _evidence_signal(tool, result),}
    print(
        f"TOOL={tool} | "
        f"SIGNAL={_evidence_signal(tool, result)} | "
        f"SUMMARY={summary}" 
    )





async def _synthesize_fallback_answer(state: IncidentState) -> tuple[str, list[int]]:
    """
    Only called when the loop ends (sufficiency-triggered or
    MAX_ITERATIONS-triggered) WITHOUT the model ever having chosen
    action="answer" itself — which is exactly the gap that used to make
    a forced stop default to the unhelpful "Unable to determine root
    cause" even when the evidence gathered was actually strong. One more
    LLM call, but only on this rare path, not every iteration.

    Provider failover applies here too, same pattern as
    _call_reasoning_llm — Groq first, OpenRouter on any Groq failure.
    """

    FALLBACK_SYNTHESIS_PROMPT = """
You are the final synthesis step of an incident investigation.

Incident:
{description}

Evidence gathered:
{evidence_summary}

Based ONLY on the evidence above:

1. State the strongest supported root-cause hypothesis.
2. Do not invent facts.
3. Do not introduce a new possible cause.
4. Do not recommend a code fix unless the evidence explicitly identifies it.
5. Cite only evidence that directly supports the hypothesis.
6. If the evidence is insufficient, say so rather than guessing.

Return ONLY valid JSON matching this structure:

{{
  "hypothesis": "concise root-cause explanation",
  "cited_evidence_indices": [0]
}}
"""
    prompt = FALLBACK_SYNTHESIS_PROMPT.format(
        description=state["description"],
        evidence_summary=_summarize_evidence(state["evidence"]),
    )

    FALLBACK_SCHEMA = {
        "type": "object",
        "properties": {
            "hypothesis": {
                "type": "string"
            },
            "cited_evidence_indices": {
                "type": "array",
                "items": {
                    "type": "integer"
                }
            }
        },
        "required": [
            "hypothesis",
            "cited_evidence_indices"
        ],
        "additionalProperties": False
    }

    async def groq_call():
        return await _llm.chat.completions.create(
            model=settings.groq_model,
            max_completion_tokens=200,
            reasoning_effort="none",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "fallback_synthesis",
                    "schema": FALLBACK_SCHEMA,
                },
            },
            messages=[{"role": "user", "content": prompt}],
        )

    async def openrouter_call():
        return await _call_openrouter_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "fallback_synthesis",
                    "schema": FALLBACK_SCHEMA,
                },
            },
        )

    try:
        resp, provider = await _call_llm_with_fallback(
            groq_call, openrouter_call, "_synthesize_fallback_answer"
        )
    except Exception as e:
        logger.warning(f"Fallback synthesis: both providers failed ({e})")
        return "Unable to determine root cause with available evidence.", []

    logger.info(f"_synthesize_fallback_answer: provider={provider}")

    try:
        parsed = json.loads((resp.choices[0].message.content or "").strip())

        hypothesis = parsed.get("hypothesis", "")
        cited_indices = parsed.get("cited_evidence_indices", [])

        if not isinstance(hypothesis, str):
            raise ValueError("hypothesis must be a string")

        if not isinstance(cited_indices, list) or not all(
            isinstance(i, int) for i in cited_indices
        ):
            raise ValueError("cited_evidence_indices must be a list of integers")

        return hypothesis, cited_indices

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Fallback synthesis failed to parse ({e})")
        return "Unable to determine root cause with available evidence.", []


GROUNDEDNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["supported", "reason"],
    "additionalProperties": False,
}

GROUNDEDNESS_PROMPT = """You are independently checking a root-cause hypothesis
against the specific evidence cited for it. You did not investigate this
incident yourself — judge only whether the evidence below actually
establishes the claim, not whether the claim sounds plausible in general.

Incident:
{description}

Hypothesis:
{hypothesis}

Evidence cited in support of this hypothesis:
{cited_evidence}

Does the cited evidence actually support this specific hypothesis?
Answer supported=false if the evidence is merely consistent with the
hypothesis without establishing it, if it's unrelated, or if the
hypothesis claims more than the evidence shows (for example, blaming a
deployment when no deployment evidence was cited).

Return ONLY valid JSON:
{{
  "supported": true or false,
  "reason": "<one short sentence>"
}}
"""


async def _verify_groundedness(
    description: str,
    hypothesis: str,
    cited_evidence: list[EvidenceEntry],
) -> tuple[bool, str]:
    """
    Second, independent LLM call that checks whether the cited evidence
    actually supports the hypothesis — a semantic check the deterministic
    confidence scoring below can't do, since that scoring only counts
    citation diversity, not whether the causal reasoning holds (a model
    can cite two real, valid pieces of evidence and still draw a non
    sequitur from them, and the citation count alone would score that
    just as high as a sound conclusion).

    Runs once, after the loop has already ended, on the final hypothesis
    only. Deliberately NOT a fix for JSON-validity or tool-selection
    drift during the investigation loop — those are handled earlier
    (_get_reasoning_decision's retry, and validate_action's code-level
    constraints, respectively). This is narrowly scoped to "was real,
    cited evidence actually enough to support this specific claim."

    Provider failover applies here too, same pattern as the other two
    LLM call sites — Groq first, OpenRouter on any Groq failure. If BOTH
    providers fail, this fails open (same as the original single-provider
    behavior): a broken verifier call is a bug in the verifier, not
    evidence the hypothesis is wrong.
    """
    if not cited_evidence:
        return False, "no evidence was cited"

    evidence_block = "\n".join(
        f"- {e['tool']}(\"{e['query']}\") -> {e['result_summary']}"
        for e in cited_evidence
    )

    prompt = GROUNDEDNESS_PROMPT.format(
        description=description,
        hypothesis=hypothesis,
        cited_evidence=evidence_block,
    )

    async def groq_call():
        return await _llm.chat.completions.create(
            model=settings.groq_model,
            max_completion_tokens=150,
            reasoning_effort="none",
            response_format={
                "type": "json_object",
                
            },
            messages=[{"role": "user", "content": prompt}],
        )

    async def openrouter_call():
        return await _call_openrouter_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            response_format={"type": "json_object"},
        )

    try:
        resp, provider = await _call_llm_with_fallback(
            groq_call, openrouter_call, "_verify_groundedness"
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        logger.info(f"_verify_groundedness: provider={provider}")
        return bool(parsed.get("supported")), str(parsed.get("reason", ""))
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning(
            f"Groundedness check failed to parse ({e}); "
            f"leaving the structural confidence score unadjusted"
        )
        # Fail open: a broken verifier call is a bug in the verifier, not
        # evidence the hypothesis is wrong — don't let it silently tank
        # a confidence score that was already computed deterministically.
        return True, f"groundedness check unavailable ({e})"
    except Exception as e:
        # Both providers failed outright (not a parse error — a
        # connection/API failure on both). Same fail-open reasoning as
        # above: don't let infra failure look like evidence against the
        # hypothesis.
        logger.warning(
            f"Groundedness check: both providers failed ({e}); "
            f"leaving the structural confidence score unadjusted"
        )
        return True, f"groundedness check unavailable ({e})"


async def answer_node(state: IncidentState) -> dict:
    hypothesis = state.get("root_cause_hypothesis")
    citations = state.get("pending_citations", [])

    if not hypothesis:
        # The loop ended without the model itself choosing "answer" —
        # synthesize one from evidence instead of defaulting to
        # "unable to determine", which used to happen on EVERY forced
        # stop (sufficiency-triggered or MAX_ITERATIONS), not just rare
        # true-failure cases.
        hypothesis, citations = await _synthesize_fallback_answer(state)
        hypothesis = hypothesis or "Unable to determine root cause with available evidence."

    valid_citations = [
        i
        for i in citations
        if isinstance(i, int)
        and 0 <= i < len(state["evidence"])
    ]

    if not valid_citations:
        confidence = 0.2
    else:
        # Reuses _evidence_sufficiency's diagnostic-type counting instead
        # of separately recomputing has_log/has_metric/has_deploy here —
        # "what counts as corroborating evidence" now lives in exactly
        # one function, not two that could silently drift apart.
        cited_evidence = [state["evidence"][i] for i in valid_citations]

# Only evidence that is both diagnostic and positively informative
# contributes to confidence. Merely citing a tool (especially a
# negative/no-signal result) should not make the diagnosis look stronger.
        positive_cited_evidence = [
            e
            for e in cited_evidence
            if (
                e["tool"] in DIAGNOSTIC_TOOLS
                and e.get("informative") is True
                and e.get("signal") == "positive"
            )
        ]

        if len(positive_cited_evidence) >= 2:
            confidence = 0.8
        elif len(positive_cited_evidence) == 1:
            confidence = 0.6
        else:
            confidence = 0.3

        supported, reason = await _verify_groundedness(
            state["description"], hypothesis, cited_evidence
        )
        logger.info(f"groundedness check: supported={supported} reason={reason}")
        if not supported:
            # The independent check disagrees with the hypothesis-evidence
            # link even though citation *counting* looked fine — trust
            # this over the structural score, since counting citations
            # can't catch a non sequitur.
            confidence = min(confidence, 0.3)

    return {
        "root_cause_hypothesis": hypothesis,
        "confidence": confidence,
        "cited_evidence_indices": valid_citations,
    }


def check_stop_condition(state: IncidentState) -> str:
    if state["next_action"] == "answer":
        return "answer"
    if state["iteration"] >= MAX_ITERATIONS:
        return "answer"   # force an answer rather than loop forever — same cap pattern as your real agent
    return "execute_tool"