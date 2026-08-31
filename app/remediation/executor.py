"""
remediation/executor.py — the only file in this project that mutates
real infrastructure. Runs docker compose as a subprocess on the host
(see infra/docker-compose.yml — this agent runs outside Docker, with
CLI access to that compose project, no socket mount).

Deliberately narrow: supports exactly two action types, both of which
map directly onto the fault-injection mechanism already used for
testing. Anything else is refused rather than guessed at.

Guardrails are enforced HERE, not just upstream, because this is the
one place a mistake has a real side effect:
  - only "restart_service" and "reset_fault_mode" are supported
  - only runs if approval_status == "approved"
  - never runs if risk_category == "do_not_apply", even if approved
  - never runs twice for the same incident (idempotency via execution_status)
"""
import logging
import subprocess
import time
from app.state import IncidentState

logger = logging.getLogger(__name__)

INFRA_DIR = r"C:\incident-agent\infra"

# service name (as used throughout state/evidence) -> compose env var
# that carries its FAULT_MODE. Only services with a FAULT_MODE var in
# docker-compose.yml are listed — others (inventory-service, cart-service,
# inventory-db) don't take fault injection and are not executor targets.
SERVICE_FAULT_ENV_VAR = {
    "checkout-service": "CHECKOUT_FAULT_MODE",
    "payment-service": "PAYMENT_FAULT_MODE",
}

SUPPORTED_ACTION_TYPES = {"restart_service", "reset_fault_mode"}


def _target_service(state: IncidentState) -> str | None:
    """
    Which service to act on. Uses the same primary-service extraction
    investigation.py already relies on (the incident description's own
    "<name>-service" token) rather than trusting any fixture/ground-truth
    lookup — see investigation.py's _extract_primary_service for why.
    """
    from app.agents.investigation import _extract_primary_service
    return _extract_primary_service(state["description"])


def _run_compose(args: list[str], env: dict | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=INFRA_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "docker compose call timed out after 60s"
    except FileNotFoundError:
        return False, "docker CLI not found on host PATH"

    if result.returncode != 0:
        return False, f"docker compose exited {result.returncode}: {result.stderr.strip()}"
    return True, result.stdout.strip()


def execute_remediation(state: IncidentState) -> dict:
    """
    Returns {"execution_status": "succeeded"|"failed", "execution_detail": str}
    Never raises — infra failures are reported in the return value, not
    as exceptions, so main.py's endpoint can always respond cleanly.
    """
    action_type = state.get("remediation_action_type")
    risk = state.get("risk_category")
    approval = state.get("approval_status")

    if state.get("execution_status") == "succeeded":
        return {
            "execution_status": "succeeded",
            "execution_detail": "Already executed for this incident — refusing to re-run.",
        }

    if approval != "approved":
        return {
            "execution_status": "failed",
            "execution_detail": f"Refusing: approval_status is {approval!r}, not 'approved'.",
        }

    if risk == "do_not_apply":
        return {
            "execution_status": "failed",
            "execution_detail": "Refusing: risk_category is do_not_apply — this can never be auto-executed regardless of approval.",
        }
    

    if action_type not in SUPPORTED_ACTION_TYPES:
        return {
            "execution_status": "failed",
            "execution_detail": f"No automated action available for action_type={action_type!r}. Manual remediation required.",
        }
    

    service = _target_service(state)
    if service is None or service not in SERVICE_FAULT_ENV_VAR:
        return {
            "execution_status": "failed",
            "execution_detail": f"Could not resolve a supported target service from the incident description (got {service!r}).",
        }
    execution_started_at = time.time()

    if action_type == "restart_service":
        ok, detail = _run_compose(["restart", service])
        return {
            "execution_status": "succeeded" if ok else "failed",
            "execution_detail": detail,
            "execution_started_at": execution_started_at,
        }

    # reset_fault_mode
    import os
    env = os.environ.copy()
    env[SERVICE_FAULT_ENV_VAR[service]] = "none"
    ok, detail = _run_compose(["up", "-d", "--force-recreate", service], env=env)
    return {
        "execution_status": "succeeded" if ok else "failed",
        "execution_detail": detail,
        "execution_started_at": execution_started_at,
    }