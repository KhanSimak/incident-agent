#!/bin/bash
# infra/fault_injection/inject_bad_deploy.sh
#
# Simulates a real deploy: recreates payment-service with FAULT_MODE=bad_deploy
# via compose's own env-var substitution (see PAYMENT_FAULT_MODE in
# docker-compose.yml) — no manual port/volume duplication, compose
# handles that the same way it does for a normal `up`.
#
# Also appends a real deploy-event record to logs/deploys.log, which
# tools/deploys_live.py reads from — a real, timestamped fact on disk,
# not something hardcoded into fixtures.py.
set -e
cd "$(dirname "$0")/.."
mkdir -p logs

echo "$(date -u +%s)|payment-service|fault-$(date +%s)|Refactor process_payment signature to accept currency parameter" >> logs/deploys.log

PAYMENT_FAULT_MODE=bad_deploy docker compose up -d --force-recreate payment-service

echo "Injected: bad_deploy on payment-service."
echo "Try:  curl -X POST 'http://localhost:8001/charge?amount=10'"
