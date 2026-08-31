"""
infra/services/payment_service/app.py — a real, runnable FastAPI service
that reproduces Scenario 1 from fixtures.py (bad_deploy_regression) for
real, instead of describing it in a dataclass.

FAULT_MODE env var controls behavior:
  - unset / "none": process_payment() works normally
  - "bad_deploy": process_payment() is missing the 'currency' parameter
    a caller still passes — the EXACT bug from fixtures.py's
    BAD_DEPLOY scenario, but now it's real code raising a real
    TypeError, not a hardcoded log line describing one.

Structured JSON logging (not print()) — this is what tools/logs_live.py
greps through later, same LogQL-style query the agent already composes.
"""
import json
import logging
import os
import sys
import time

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, make_asgi_app

# ── structured logging setup ────────────────────────────────────────────
# ── structured logging setup ────────────────────────────────────────────
# Writes to BOTH stdout (so `docker-compose logs` shows it live) AND a
# file under /var/log/app/ (the bind-mounted ./logs/ directory) — the
# file is what tools/logs_live.py actually greps through, same LogQL-
# style query the agent already composes against fixture data.
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("/var/log/app/payment-service.log"),
])
logger = logging.getLogger("payment-service")


def log_event(level: str, message: str):
    """One JSON object per line — real log files get read+filtered by
    tools/logs_live.py's LogQL-style query, same as the fixture version's
    LogLine dataclass, just written to disk instead of hardcoded."""
    logger.info(json.dumps({
        "timestamp": time.time(),
        "service": "payment-service",
        "level": level,
        "message": message,
    }))


FAULT_MODE = os.environ.get("FAULT_MODE", "none")

app = FastAPI()
app.mount("/metrics", make_asgi_app())   # Prometheus scrapes this path

REQUEST_COUNT = Counter("payment_requests_total", "Total payment requests")
ERROR_COUNT = Counter("payment_errors_total", "Total payment errors")

log_event("INFO", f"payment-service started, FAULT_MODE={FAULT_MODE}")


def process_payment(amount: float, currency: str = None):
    """
    THE BUG, when FAULT_MODE=bad_deploy: currency has no default and
    callers below don't pass one — exactly mirrors fixtures.py's
    ground_truth: "Deploy changed process_payment()'s signature to
    require a 'currency' argument, but a caller wasn't updated."
    """
    if FAULT_MODE == "bad_deploy":
        def process_payment_broken(amount, currency):   # no default — this is the deploy bug
            return {"amount": amount, "currency": currency, "status": "charged"}
        return process_payment_broken(amount)   # caller doesn't pass currency — raises TypeError, unmodified real Python behavior

    return {"amount": amount, "currency": currency or "USD", "status": "charged"}


@app.post("/charge")
async def charge(amount: float):
    REQUEST_COUNT.inc()
    try:
        result = process_payment(amount)
        log_event("INFO", f"Payment processed: amount={amount}")
        return result
    except TypeError as e:
        ERROR_COUNT.inc()
        log_event("ERROR", f"TypeError: {e}")
        raise HTTPException(500, str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "fault_mode": FAULT_MODE}
