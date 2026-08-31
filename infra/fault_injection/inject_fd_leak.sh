#!/bin/bash

set -e

cd "$(dirname "$0")/.."

CHECKOUT_FAULT_MODE=fd_leak \
docker compose up -d --force-recreate checkout-service

echo "Injected file descriptor leak on checkout-service."
echo "Waiting for checkout-service..."
sleep 3

echo "Generating controlled traffic for 60 seconds..."

end=$((SECONDS + 60))
counter=1

while [ $SECONDS -lt $end ]; do
    curl -s \
        --max-time 10 \
        -X POST \
        "http://localhost:8002/checkout?order_id=fd-$counter" \
        -o /dev/null || true

    counter=$((counter + 1))
done

echo
echo "File descriptor leak injection complete."