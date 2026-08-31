"""
infra/services/inventory_service/app.py — depends on a REAL Postgres
container (inventory-db in docker-compose.yml), not a simulated failure
flag. When the fault-injection script stops inventory-db, this service
gets a genuine psycopg connection error — reproducing Scenario 3
(downstream_dependency_failure) with a real dependency failure, not a
described one.
"""
import json
import logging
import os
import sys
import time

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, make_asgi_app
import psycopg

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("/var/log/app/inventory-service.log"),
])
logger = logging.getLogger("inventory-service")


def log_event(level: str, message: str):
    logger.info(json.dumps({
        "timestamp": time.time(), "service": "inventory-service", "level": level, "message": message,
    }))


DB_DSN = os.environ.get("INVENTORY_DB_DSN", "postgresql://postgres:postgres@inventory-db:5432/inventory")

app = FastAPI()
app.mount("/metrics", make_asgi_app())
ERROR_COUNT = Counter("inventory_errors_total", "Total inventory errors")

log_event("INFO", "inventory-service started")


@app.get("/inventory/{item_id}")
async def get_inventory(item_id: str):
    try:
        # A REAL connection attempt — psycopg.connect will genuinely
        # raise OperationalError if inventory-db is stopped, exactly
        # matching fixtures.py's log line: "Database connection refused:
        # inventory-db:5432", but now it's the actual exception message
        # psycopg produces, not a hand-typed string.
        async with await psycopg.AsyncConnection.connect(DB_DSN, connect_timeout=2) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT quantity FROM inventory WHERE item_id = %s", (item_id,))
                row = await cur.fetchone()
                return {"item_id": item_id, "quantity": row[0] if row else 0}
    except psycopg.OperationalError as e:
        ERROR_COUNT.inc()
        log_event("ERROR", f"Database connection refused: {e}")
        raise HTTPException(503, "inventory database unreachable")


@app.get("/health")
async def health():
    return {"status": "ok"}
