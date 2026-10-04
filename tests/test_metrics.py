import pytest

from app.tools.metrics import infer_metric_direction, detect_anomaly


@pytest.mark.parametrize(
    "name, values, expected_direction, expected_anomalous",
    [
        (
            "memory leak",
            [200, 210, 225, 240, 260, 290, 320, 350, 390, 430],
            "increase",
            True,
        ),
        (
            "cpu saturation",
            [20, 25, 31, 38, 47, 58, 68, 77, 86, 94],
            "increase",
            True,
        ),
        (
            "fd leak",
            [100, 115, 130, 150, 175, 205, 240, 280, 325, 370],
            "increase",
            True,
        ),
        (
            "db connection leak",
            [5, 7, 9, 12, 15, 19, 24, 29, 35, 42],
            "increase",
            True,
        ),
        (
            "queue buildup",
            [10, 14, 19, 25, 32, 41, 51, 63, 77, 92],
            "increase",
            True,
        ),
        (
            "latency increase",
            [100, 110, 125, 140, 160, 185, 210, 240, 275, 310],
            "increase",
            True,
        ),
        (
            "error rate increase",
            [1, 1, 2, 3, 4, 6, 8, 11, 15, 20],
            "increase",
            True,
        ),
        (
            "disk usage increase",
            [40, 43, 46, 50, 55, 60, 66, 72, 79, 87],
            "increase",
            True,
        ),
        (
            "decreasing",
            [900, 850, 800, 750, 700, 650, 600, 550, 500, 450],
            "decrease",
            True,
        ),
        (
            "cpu recovery",
            [20, 25, 30, 35, 40, 80, 70, 55, 40, 30],
            "decrease",
            False,
        ),
        (
            "memory leak then restart",
            [100, 150, 200, 250, 300, 350, 400, 450, 120, 120],
            "recovering",
            False,
        ),
        (
            "sudden spike",
            [100, 100, 100, 100, 500, 100, 100, 100, 100, 100],
            "recovering",
            False,
        ),
        (
            "sudden drop",
            [500, 500, 500, 500, 100, 500, 500, 500, 500, 500],
            "recovering",
            False,
        ),
        (
            "flat",
            [100, 101, 99, 100, 101, 100, 99, 100, 101, 100],
            "recovering",
            False,
        ),
        (
            "noisy",
            [100, 130, 90, 140, 110, 125, 95, 135, 105, 120],
            "both",
            False,
        ),
    ],
)
def test_metric_detection(
    name,
    values,
    expected_direction,
    expected_anomalous,
):
    direction = infer_metric_direction(values)

    result = detect_anomaly(
        values,
        direction=direction,
    )

    assert direction == expected_direction, (
        f"{name}: expected direction "
        f"{expected_direction}, got {direction}"
    )

    assert result["anomalous"] == expected_anomalous, (
        f"{name}: expected anomalous="
        f"{expected_anomalous}, got {result['anomalous']}"
    )