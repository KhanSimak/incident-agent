#!/bin/bash

set -e

cd "$(dirname "$0")/.."

CHECKOUT_FAULT_MODE=cpu_saturation \
docker compose up -d --force-recreate checkout-service

echo "Injected CPU saturation fault on checkout-service."
echo "Waiting for checkout-service..."
sleep 3

echo "Generating controlled checkout traffic for 60 seconds..."

end=$((SECONDS + 60))
counter=1

while [ $SECONDS -lt $end ]; do
    curl -s \
        -X POST \
        "http://localhost:8002/checkout?order_id=cpu-$counter" \
        -o /dev/null &

    counter=$((counter + 1))

    # Keep concurrency controlled
    sleep 0.1
done

wait

echo
echo "Fault injection complete."