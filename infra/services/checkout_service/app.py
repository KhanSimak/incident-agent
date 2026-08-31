"""
infra/services/checkout_service/app.py — reproduces Scenario 2
(connection_pool_exhaustion) for real: a genuine bounded pool of
connection objects that, in fault mode, doesn't release a connection
back to the pool on a request that errors — a real leak, not a
described one, so the pool really does fill up over real requests.
"""
import json
import logging
import os
import sys
import time
import asyncio

from fastapi import FastAPI, HTTPException
from prometheus_client import Gauge, Counter, make_asgi_app

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("/var/log/app/checkout-service.log"),
])
logger = logging.getLogger("checkout-service")


def log_event(level: str, message: str):
    logger.info(json.dumps({
        "timestamp": time.time(), "service": "checkout-service", "level": level, "message": message,
    }))
def burn_cpu(duration: float = 2.0):
    end = time.time() + duration

    while time.time() < end:
        _ = sum(i * i for i in range(10000))

FAULT_MODE = os.environ.get("FAULT_MODE", "none")
POOL_MAX_SIZE = 20

app = FastAPI()
app.mount("/metrics", make_asgi_app())

POOL_ACTIVE = Gauge("db_pool_active_connections", "Active connections in the pool")
ERROR_COUNT = Counter("checkout_errors_total", "Total checkout errors")

MEMORY_LEAK = []
FD_LEAK = []

if FAULT_MODE == "fd_leak":
    log_event("WARN", "File descriptor leak fault injection enabled")

if FAULT_MODE == "memory_leak":
    log_event("WARN", "Memory leak fault injection enabled")
if FAULT_MODE == "bad_deploy":
    log_event("WARN", "Payment processing path is returning an application error")
class ConnectionPool:
    """A real bounded pool — not a mock. acquire()/release() actually
    change the same active-connection count the anomaly detector queries
    via db_pool_active_connections{service="checkout-service"} — same
    metric name the agent already knows how to query from fixtures.py."""
    def __init__(self, max_size: int):
        self.max_size = max_size
        self.active = 0
        self._lock = asyncio.Lock()

    async def acquire(self, timeout=5.0):
        start = time.time()
        while self.active >= self.max_size:
            if time.time() - start > timeout:
                raise TimeoutError(f"could not acquire connection from pool within {int(timeout*1000)}ms")
            await asyncio.sleep(0.05)
        async with self._lock:
            self.active += 1
        POOL_ACTIVE.set(self.active)

    async def release(self):
        async with self._lock:
            self.active = max(0, self.active - 1)
        POOL_ACTIVE.set(self.active)


pool = ConnectionPool(POOL_MAX_SIZE)
log_event("INFO", f"checkout-service started, FAULT_MODE={FAULT_MODE}, pool max_size={POOL_MAX_SIZE}")


@app.post("/checkout")
async def checkout(order_id: str):


    if FAULT_MODE == "cpu_saturation":
        await asyncio.to_thread(burn_cpu, 2.0)

    if FAULT_MODE == "bad_deploy":
        ERROR_COUNT.inc()
        log_event("ERROR", "TypeError: process_payment() missing required argument")
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
    )
    try:
        await pool.acquire()
        if FAULT_MODE == "fd_leak":
            leaked_fd = open("/tmp/checkout-leak.txt", "a")
            FD_LEAK.append(leaked_fd)
        if FAULT_MODE == "memory_leak":
            MEMORY_LEAK.append(bytearray(1024 * 1024))
 
    except TimeoutError as e:
        ERROR_COUNT.inc()
        log_event("ERROR", f"TimeoutError: {e}")
        raise HTTPException(504, str(e))

    try:
        await asyncio.sleep(0.05)   # simulated work
        log_event("INFO", f"Order {order_id} checked out. Pool active: {pool.active}/{POOL_MAX_SIZE}")
        if pool.active >= POOL_MAX_SIZE * 0.85:
            log_event("WARN", f"Connection pool at {int(pool.active/POOL_MAX_SIZE*100)}% capacity ({pool.active}/{POOL_MAX_SIZE})")
        return {"order_id": order_id, "status": "checked_out"}
    finally:
        if FAULT_MODE != "pool_leak":
            await pool.release()
        # FAULT_MODE == "pool_leak": deliberately DON'T release — this
        # IS the bug. Every request under this mode permanently consumes
        # one connection, exactly like fixtures.py's ground_truth: "a
        # connection leak in a code path that doesn't close on error."


@app.get("/health")
async def health():
    return {"status": "ok", "fault_mode": FAULT_MODE, "pool_active": pool.active, "pool_max": POOL_MAX_SIZE}
