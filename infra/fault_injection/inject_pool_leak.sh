#!/bin/bash
# infra/fault_injection/inject_pool_leak.sh
#
# Recreates checkout-service with FAULT_MODE=pool_leak. Unlike
# bad_deploy, this fault needs REPEATED requests to actually manifest —
# a single call doesn't fill a 20-connection pool. This script also
# fires the requests, so `db_pool_active_connections` genuinely climbs
# in Prometheus, matching fixtures.py's gradual-trend scenario for real.
set -e
cd "$(dirname "$0")/.."
mkdir -p logs

CHECKOUT_FAULT_MODE=pool_leak docker compose up -d --force-recreate checkout-service
echo "Injected: pool_leak on checkout-service. Waiting for it to be ready..."
sleep 3

echo "Firing 25 requests to actually leak 25 connections (pool max is 20 — this WILL start timing out near the end, that's the incident happening for real)..."
for i in $(seq 1 25); do
  curl -s -X POST "http://localhost:8002/checkout?order_id=order-$i" -o /dev/null -w "  request $i -> HTTP %{http_code}\n" || true
done

echo "Done. Check current pool state:  curl http://localhost:8002/health"
