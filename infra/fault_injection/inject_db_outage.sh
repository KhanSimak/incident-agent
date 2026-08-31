#!/bin/bash
# infra/fault_injection/inject_db_outage.sh
#
# Genuinely stops the inventory-db container — inventory-service's next
# query gets a REAL psycopg.OperationalError, which cascades into a
# REAL 503 from cart-service. Nothing simulated here at all.
set -e
cd "$(dirname "$0")/.."

docker compose stop inventory-db
echo "Injected: inventory-db is now stopped."
echo "Try:  curl -X POST 'http://localhost:8004/cart/add?item_id=sku-001'"
echo "  (cart-service -> inventory-service -> [inventory-db unreachable] -> cascading 503)"
