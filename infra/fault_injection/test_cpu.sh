#!/bin/bash

end=$((SECONDS + 90))

while [ $SECONDS -lt $end ]; do
    curl -s --max-time 10 \
        -X POST \
        "http://localhost:8002/checkout?order_id=cpu-test" \
        -o /dev/null
done