#!/bin/bash

set -e

cd "$(dirname "$0")/.."

CHECKOUT_FAULT_MODE=memory_leak \
docker compose up -d --force-recreate checkout-service

echo "Injected memory leak fault on checkout-service."
echo "Waiting for checkout-service..."
sleep 3

echo "Generating controlled traffic for 60 seconds..."

end=$((SECONDS + 90))
counter=1

while [ $SECONDS -lt $end ]; do
    curl -s \
        --max-time 10 \
        -X POST \
        "http://127.0.0.1:8002/checkout?order_id=mem-$counter" \
        -o /dev/null || true

    counter=$((counter + 1))
done

echo
echo "Memory leak injection complete."