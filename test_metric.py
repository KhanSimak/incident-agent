from app.tools.metrics import infer_metric_direction, detect_anomaly


tests = {
    # 1. Memory leak — steadily increasing
    "memory leak": [
        200, 210, 225, 240, 260,
        290, 320, 350, 390, 430
    ],

    # 2. CPU saturation — steadily increasing
    "cpu saturation": [
        20, 25, 31, 38, 47,
        58, 68, 77, 86, 94
    ],

    # 3. File descriptor leak — steadily increasing
    "fd leak": [
        100, 115, 130, 150, 175,
        205, 240, 280, 325, 370
    ],

    # 4. DB connection leak — steadily increasing
    "db connection leak": [
        5, 7, 9, 12, 15,
        19, 24, 29, 35, 42
    ],

    # 5. Queue buildup — increasing
    "queue buildup": [
        10, 14, 19, 25, 32,
        41, 51, 63, 77, 92
    ],

    # 6. Latency degradation — increasing
    "latency increase": [
        100, 110, 125, 140, 160,
        185, 210, 240, 275, 310
    ],

    # 7. Error rate increasing
    "error rate increase": [
        1, 1, 2, 3, 4,
        6, 8, 11, 15, 20
    ],

    # 8. Disk usage increasing
    "disk usage increase": [
        40, 43, 46, 50, 55,
        60, 66, 72, 79, 87
    ],

    # 9. Pure decreasing signal
    "decreasing": [
        900, 850, 800, 750, 700,
        650, 600, 550, 500, 450
    ],

    # 10. CPU dropping after load
    "cpu recovery": [
        20, 25, 30, 35, 40,
        80, 70, 55, 40, 30
    ],

    # 11. Memory leak → restart → recovery
    "memory leak then restart": [
        100, 150, 200, 250, 300,
        350, 400, 450, 120, 120
    ],

    # 12. Sudden spike
    "sudden spike": [
        100, 100, 100, 100, 500,
        100, 100, 100, 100, 100
    ],

    # 13. Sudden drop
    "sudden drop": [
        500, 500, 500, 500, 100,
        500, 500, 500, 500, 500
    ],

    # 14. Flat / healthy
    "flat": [
        100, 101, 99, 100, 101,
        100, 99, 100, 101, 100
    ],

    # 15. Noisy / mixed behavior
    "noisy": [
        100, 130, 90, 140, 110,
        125, 95, 135, 105, 120
    ],
}


for name, values in tests.items():
    direction = infer_metric_direction(values)
    result = detect_anomaly(values, direction=direction)

    print(f"\n{name}")
    print(f"  direction : {direction}")
    print(f"  anomalous : {result['anomalous']}")
    print(f"  change    : {result.get('change')}")