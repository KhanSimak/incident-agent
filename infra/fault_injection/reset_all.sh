#!/bin/bash
# infra/fault_injection/reset_all.sh — back to a clean, healthy state.
set -e
cd "$(dirname "$0")/.."

docker compose start inventory-db
PAYMENT_FAULT_MODE=none docker compose up -d --force-recreate payment-service
CHECKOUT_FAULT_MODE=none docker compose up -d --force-recreate checkout-service
rm -f logs/deploys.log

echo "All faults reset. All services healthy."
