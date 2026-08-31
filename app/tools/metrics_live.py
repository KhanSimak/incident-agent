"""
tools/metrics_live.py — same composed-query interface as tools/metrics.py,
real Prometheus underneath. This is the actual proof of the promise made
in metrics.py's docstring: "swap what's INSIDE run_metric_query, the
agent doesn't need to change." detect_anomaly() is imported UNCHANGED
from metrics.py — the statistics are the same regardless of where the
numbers came from, only the data source differs.
"""

import time
import httpx

from app.tools.metrics import detect_anomaly, infer_metric_direction



async def discover_metrics(prometheus_url: str, service: str) -> dict:
    """
    Discover metric names that actually belong to the requested service.

    We cannot determine relevance from the metric NAME alone because
    generic metrics such as process_cpu_seconds_total don't contain
    the service name. Instead, query Prometheus for series carrying
    service="checkout-service" and extract their __name__ labels.
    """

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{prometheus_url}/api/v1/series",
                params={
                    "match[]": f'{{service="{service}"}}'
                },
            )
        except httpx.RequestError as e:
            return {
                "error": f"Could not reach Prometheus at "
                          f"{prometheus_url}: {e}"
            }

    data = resp.json()

    if data.get("status") != "success":
        return {
            "error": (
                "Prometheus metric discovery failed: "
                f"{data.get('error', 'unknown error')}"
            )
        }

    series = data.get("data", [])

    metrics = sorted({
        item["__name__"]
        for item in series
        if "__name__" in item
    })

    return {
        "service": service,
        "metrics": metrics,
    }

"""
tools/metrics_live.py — same composed-query interface as tools/metrics.py,
real Prometheus underneath. This is the actual proof of the promise made
in metrics.py's docstring: "swap what's INSIDE run_metric_query, the
agent doesn't need to change." detect_anomaly() AND infer_metric_direction()
are both imported UNCHANGED from metrics.py — the statistics and the
direction inference are the same regardless of where the numbers came
from, only the data source differs. This file previously hardcoded
direction="both" and never called infer_metric_direction() at all, which
meant live-mode anomaly detection silently skipped the auto-inference
that fixture mode always had — restored to match here.
"""


async def run_metric_query_live(
    prometheus_url: str,
    query: str,
    lookback_seconds: int = 300,
    step: str = "5s",
    direction: str | None = None,
) -> dict:
    """
    Run a PromQL range query against Prometheus and analyze the returned
    time series.

    direction:
        None -> auto-infer from the fetched data via
                infer_metric_direction() (the default — mirrors
                run_metric_query()'s fixture-mode default exactly)
        "both" / "increase" / "decrease" / "recovering" -> forced
                explicitly by the caller

    Both fixture and live paths now call the SAME infer_metric_direction()
    from metrics.py when direction is left at its default — one shared,
    generic classification function, not two separately-defaulted ones.
    """
    end = time.time()
    start = end - lookback_seconds

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{prometheus_url}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step,
                },
            )
        except httpx.RequestError as e:
            return {
                "error": (
                    f"Could not reach Prometheus at {prometheus_url}: {e}"
                )
            }

    data = resp.json()

    if data.get("status") != "success":
        return {
            "error": (
                f"Prometheus query failed: "
                f"{data.get('error', 'unknown error')}"
            )
        }

    results = data["data"]["result"]

    if not results:
        return {
            "error": (
                f"No data returned for query '{query}' — "
                "check the metric name and service label are correct"
            )
        }

    values = [(float(ts), float(v)) for ts, v in results[0]["values"]]
    raw_values = [v for _, v in values]

    resolved_direction = (
        infer_metric_direction(raw_values) if direction is None else direction
    )

    anomaly = detect_anomaly(
        raw_values,
        direction=resolved_direction,
    )

    return {
        "metric": query,
        "data_points": values,
        "anomaly": anomaly,
    }