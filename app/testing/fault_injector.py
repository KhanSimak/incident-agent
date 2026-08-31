"""
testing/fault_injector.py — dev/demo-only fault injection and traffic
generation, driven from the UI instead of a manual PowerShell/docker
compose session. This is NOT part of the remediation pipeline — the
executor (app/remediation/executor.py) only ever acts on what the
mapper decided for a real incident; this module exists purely so a
reviewer can trigger and observe a fault without a terminal.

Reuses the same docker compose subprocess mechanism as executor.py
rather than duplicating it — same INFRA_DIR, same service->env-var
mapping, same one-command-per-call shape.
"""
import logging

import httpx

from app.remediation.executor import _run_compose, SERVICE_FAULT_ENV_VAR

logger = logging.getLogger(__name__)

# Local port mapping from infra/docker-compose.yml — only the two
# services that currently support fault injection.
SERVICE_PORT = {
    "checkout-service": 8002,
    "payment-service": 8001,
}

VALID_FAULT_MODES = {
    "none",
    "pool_leak",
    "memory_leak",
    "fd_leak",
    "cpu_saturation",
    "bad_deploy",
}


def inject_fault(service: str, fault_mode: str) -> dict:
    """
    Sets FAULT_MODE on the target service and recreates the container —
    the same mechanism as the executor's reset_fault_mode action, just
    generalized to any mode instead of hardcoding "none".
    """
    if service not in SERVICE_FAULT_ENV_VAR:
        return {"ok": False, "detail": f"Unsupported service: {service!r}"}
    if fault_mode not in VALID_FAULT_MODES:
        return {"ok": False, "detail": f"Unsupported fault_mode: {fault_mode!r}"}

    import os
    env = os.environ.copy()
    env[SERVICE_FAULT_ENV_VAR[service]] = fault_mode

    ok, detail = _run_compose(["up", "-d", "--force-recreate", service], env=env)
    return {"ok": ok, "detail": detail, "service": service, "fault_mode": fault_mode}


async def generate_traffic(service: str, count: int = 50, delay_ms: int = 150) -> dict:
    """
    Fires `count` real POST /checkout requests at the service so an
    injected fault actually manifests (per app.py, the leak only grows
    on real request traffic — injection alone changes nothing until
    requests happen). Runs server-side so the browser never needs to
    call the service directly (avoids a second CORS setup on the
    service itself).
    """
    if service not in SERVICE_PORT:
        return {"ok": False, "detail": f"Unsupported service: {service!r}"}

    port = SERVICE_PORT[service]
    url = f"http://localhost:{port}/checkout"
    succeeded = 0
    failed = 0

    async with httpx.AsyncClient(timeout=5.0) as client:
        for i in range(count):
            try:
                await client.post(url, params={"order_id": f"demo-{i}"})
                succeeded += 1
            except httpx.RequestError as e:
                failed += 1
                logger.warning(f"traffic generation request {i} failed: {e}")
            if delay_ms:
                import asyncio
                await asyncio.sleep(delay_ms / 1000)

    return {"ok": True, "requested": count, "succeeded": succeeded, "failed": failed}