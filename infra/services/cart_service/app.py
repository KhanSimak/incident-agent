"""
infra/services/cart_service/app.py — calls inventory-service over real
HTTP. When inventory-service fails (because inventory-db is down), this
propagates as a genuine 503 from a real network call — the cascade in
fixtures.py's Scenario 3 is now something that actually happens across
two real services, not two hardcoded log entries.
"""
import json
import logging
import os
import sys
import time

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, make_asgi_app

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("/var/log/app/cart-service.log"),
])
logger = logging.getLogger("cart-service")


def log_event(level: str, message: str):
    logger.info(json.dumps({
        "timestamp": time.time(), "service": "cart-service", "level": level, "message": message,
    }))


INVENTORY_URL = os.environ.get("INVENTORY_SERVICE_URL", "http://inventory-service:8000")

app = FastAPI()
app.mount("/metrics", make_asgi_app())
ERROR_COUNT = Counter("cart_errors_total", "Total cart errors")

log_event("INFO", "cart-service started")


@app.post("/cart/add")
async def add_to_cart(item_id: str):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{INVENTORY_URL}/inventory/{item_id}")
        if resp.status_code != 200:
            ERROR_COUNT.inc()
            log_event("ERROR", f"UpstreamError: inventory-service returned {resp.status_code}")
            raise HTTPException(503, "could not verify inventory")
        return {"item_id": item_id, "status": "added_to_cart"}
    except httpx.RequestError as e:
        ERROR_COUNT.inc()
        log_event("ERROR", f"UpstreamError: inventory-service unreachable ({e})")
        raise HTTPException(503, "inventory-service unreachable")


@app.get("/health")
async def health():
    return {"status": "ok"}
