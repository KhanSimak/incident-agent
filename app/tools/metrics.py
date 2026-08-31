"""
tools/metrics.py — the two mechanisms we specifically chose to make this
project different from your Codebase Q&A agent's tools (see the chat
history for why): the agent COMPOSES the actual query text instead of
calling a fixed function with fixed parameters, and anomaly detection
uses real statistics, not embedding similarity.

DELIBERATE SIMPLIFICATION, stated honestly rather than hidden: real
PromQL supports functions, aggregations, and complex label matching.
Parsing all of that is a project on its own. This implements ONE
pattern — `metric_name{service="x"}` — which is enough to demonstrate
the actual mechanism (agent writes real query syntax, not a Python
function call) without building a full PromQL parser. If you extend
this toward real Prometheus later, `prometheus-api-client` handles the
real query language for you — you'd swap what's INSIDE run_metric_query,
not how the agent calls it.
"""
import re
import numpy as np

from app.fixtures import Scenario


def run_metric_query(
    scenario: Scenario,
    query: str,
    direction: str | None = None,
) -> dict:
    """
    Runs a composed query like: metric_name{service="checkout-service"}

    direction:
        None       -> auto-infer from the fetched data via
                       infer_metric_direction() (the default — this is
                       what callers should normally use)
        "both"     -> increases/decreases may be anomalous
        "increase" -> only increases are anomalous
        "decrease" -> only decreases are anomalous

    Callers can still force a direction explicitly, but nothing in this
    module decides direction from the metric's NAME anymore — see
    infer_metric_direction(), which works off the data itself.
    """
    match = re.match(
        r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\{?\s*'
        r'(?:service="([^"]+)")?\s*\}?\s*$',
        query,
    )

    if not match:
        return {
            "error": (
                f"Could not parse query '{query}'. "
                'Expected format: metric_name{service="x"}'
            )
        }

    metric_name, service_filter = match.groups()

    values = scenario.metrics.get(metric_name)

    if values is None:
        return {
            "error": (
                f"No metric named '{metric_name}' in this scenario. "
                f"Available: {list(scenario.metrics.keys())}"
            )
        }

    if service_filter and service_filter != scenario.primary_service:
        return {
            "error": (
                f"No data for metric '{metric_name}' "
                f"filtered to service='{service_filter}'"
            )
        }

    raw_values = [v for _, v in values]

    resolved_direction = (
        infer_metric_direction(raw_values) if direction is None else direction
    )

    anomaly = detect_anomaly(
        raw_values,
        direction=resolved_direction,
    )

    return {
        "metric": metric_name,
        "data_points": values,
        "anomaly": anomaly,
    }


def _segment_persistence_direction(
    segment: list[float],
    threshold: float = 0.75,
) -> str:
    """
    Shared building block: does this segment's point-to-point movement
    persistently agree on a direction? Used both to scan for a sustained
    episode ANYWHERE in the series and to classify the recent tail on its
    own — one implementation, so the two checks can't quietly diverge.

    Returns "increase", "decrease", or "both" (flat / no clear majority).
    """
    diffs = [segment[i + 1] - segment[i] for i in range(len(segment) - 1)]
    nonzero = [d for d in diffs if d != 0]

    if not nonzero:
        return "both"

    increasing_steps = sum(1 for d in nonzero if d > 0)
    decreasing_steps = sum(1 for d in nonzero if d < 0)
    total_steps = len(nonzero)

    if increasing_steps / total_steps >= threshold:
        return "increase"
    if decreasing_steps / total_steps >= threshold:
        return "decrease"
    return "both"


def infer_metric_direction(
    values: list[float],
    window: int = 5,
) -> str:
    """
    Generically infers which direction of change (if any) represents a
    real, persistent trend for THIS data — with no knowledge of what the
    metric is called. No metric-name matching anywhere in this function.

    Distinguishes two shapes purely from the data:

    1. A SUSTAINED DIRECTIONAL EPISODE anywhere in the series BEFORE the
       recent window — found by scanning every window-sized segment for
       persistent same-direction movement, but only accepting a segment
       as a qualifying "prior episode" if its extreme point falls
       strictly before the recent window starts. This is what rules out
       ongoing growth: in a still-climbing series, the strongest episode
       always peaks at the very last point — which is inside the recent
       window, not before it — so it is correctly never treated as a
       completed episode to recover from.

    2. RECOVERY from that prior episode — requires BOTH:
         a) the recent window's OWN persistent direction (computed by
            the same persistence check, not just "is the mean below the
            peak") differs from the episode's direction — i.e. the tail
            has actually stopped moving the same way, whether that shows
            up as a flat/mixed tail or an outright reversal, and
         b) the recent window has moved meaningfully closer to the
            pre-episode baseline than the episode's extreme was.
       Both are required so that a series which merely plateaued at a
       high, still-abnormal level (tail direction differs, but hasn't
       actually moved back toward baseline) is NOT misclassified as
       recovering.

    If no qualifying prior episode is found, or the recovery conditions
    aren't both met, this returns the recent window's own persistence
    direction unchanged — same fallback behavior as before.

    Used both for the initial query during investigation and for the
    post-remediation replay (verify.py), and shared identically between
    fixture and live data (see metrics_live.py) — one function, so all
    three call sites agree on what a given shape means.

    Returns "increase", "decrease", "both", or "recovering".
    """
    if len(values) < window * 2:
        return "both"

    baseline_window = values[:window]
    baseline_mean = float(np.mean(baseline_window))

    start_of_tail = len(values) - window

    # Scan every window-sized segment for a persistent episode, but only
    # accept one whose extreme lies strictly BEFORE the recent window —
    # this is what excludes "the peak IS the current tail" cases like
    # ongoing monotonic growth.
    best_episode = None  # (direction, extreme_value, distance_from_baseline)

    for start in range(0, len(values) - window + 1):
        segment = values[start:start + window]
        direction = _segment_persistence_direction(segment)

        if direction == "both":
            continue

        local_extreme = max(segment) if direction == "increase" else min(segment)
        extreme_idx = start + segment.index(local_extreme)

        if extreme_idx >= start_of_tail:
            continue  # extreme is inside (or is) the recent tail — not a completed prior episode

        distance = abs(local_extreme - baseline_mean)

        if best_episode is None or distance > best_episode[2]:
            best_episode = (direction, local_extreme, distance)

    recent = values[-window:]
    recent_mean = float(np.mean(recent))
    tail_direction = _segment_persistence_direction(recent)

    if best_episode is not None:
        episode_direction, episode_extreme, episode_distance = best_episode

        dist_recent_to_baseline = abs(recent_mean - baseline_mean)
        moving_back_toward_baseline = dist_recent_to_baseline < episode_distance
        tail_no_longer_matches_episode = tail_direction != episode_direction

        if moving_back_toward_baseline and tail_no_longer_matches_episode:
            return "recovering"

    return tail_direction

def detect_anomaly(
    values: list[float],
    window: int = 5,
    direction: str = "both",
) -> dict:
    """
    Compare an early clean baseline against the latest window.

    direction:
        "both"       -> increases and decreases can be anomalous
        "increase"   -> only an increase is considered anomalous
        "decrease"   -> only a decrease is considered anomalous
        "recovering" -> NEVER anomalous — a large swing back toward
                         baseline after an earlier sustained episode
                         (e.g. a restart) is the episode resolving, not
                         a fresh problem in the opposite direction. This
                         is what previously got misclassified as "both"
                         and flagged regardless of being a recovery.
    """
    if len(values) < window * 2:
        return {
            "anomalous": False,
            "reason": (
                f"not enough data points ({len(values)}) for two clean "
                f"{window}-point windows"
            ),
        }

    baseline = values[:window]
    recent = values[-window:]

    baseline_mean = float(np.mean(baseline))
    baseline_std = float(np.std(baseline))
    recent_mean = float(np.mean(recent))

    change = recent_mean - baseline_mean

    def matches_direction() -> bool:
        if direction == "recovering":
            return False
        if direction == "increase":
            return change > 0
        if direction == "decrease":
            return change < 0
        return abs(change) > 0

    # Normal z-score path.
    if baseline_std > 0:
        z_score = change / baseline_std
        anomalous = abs(z_score) > 3 and matches_direction()

        result = {
            "anomalous": anomalous,
            "z_score": round(z_score, 2),
            "baseline_avg": baseline_mean,
            "recent_avg": recent_mean,
            "change": round(change, 2),
            "direction": direction,
        }
        if direction == "recovering":
            result["reason"] = (
                "recent window is recovering toward baseline after an "
                "earlier sustained episode; not flagged as a new anomaly"
            )
        return result

    # Flat baseline: use an absolute change threshold.
    absolute_change = abs(change)
    MIN_ABSOLUTE_CHANGE = 1.0

    anomalous = (
        absolute_change >= MIN_ABSOLUTE_CHANGE
        and matches_direction()
    )

    result = {
        "anomalous": anomalous,
        "z_score": None,
        "baseline_avg": baseline_mean,
        "recent_avg": recent_mean,
        "absolute_change": round(absolute_change, 2),
        "change": round(change, 2),
        "direction": direction,
        "reason": (
            "recent window is recovering toward baseline after an "
            "earlier sustained episode; not flagged as a new anomaly"
            if direction == "recovering"
            else "flat baseline; anomaly determined by absolute change threshold"
        ),
    }
    return result

def detect_anomaly(
    values: list[float],
    window: int = 5,
    direction: str = "both",
) -> dict:
    """
    Compare an early clean baseline against the latest window.

    direction:
        "both"     -> increases and decreases can be anomalous
        "increase" -> only an increase is considered anomalous
        "decrease" -> only a decrease is considered anomalous

    This is important for resource metrics such as memory, where a large
    decrease after a restart is unusual but is NOT evidence of a memory leak.
    """
    if len(values) < window * 2:
        return {
            "anomalous": False,
            "reason": (
                f"not enough data points ({len(values)}) for two clean "
                f"{window}-point windows"
            ),
        }

    baseline = values[:window]
    recent = values[-window:]

    baseline_mean = float(np.mean(baseline))
    baseline_std = float(np.std(baseline))
    recent_mean = float(np.mean(recent))

    change = recent_mean - baseline_mean

    def matches_direction() -> bool:
        if direction == "increase":
            return change > 0
        if direction == "decrease":
            return change < 0
        return abs(change) > 0

    # Normal z-score path.
    if baseline_std > 0:
        z_score = change / baseline_std

        anomalous = abs(z_score) > 3 and matches_direction()

        return {
            "anomalous": anomalous,
            "z_score": round(z_score, 2),
            "baseline_avg": baseline_mean,
            "recent_avg": recent_mean,
            "change": round(change, 2),
            "direction": direction,
        }

    # Flat baseline: use an absolute change threshold.
    absolute_change = abs(change)

    MIN_ABSOLUTE_CHANGE = 1.0

    anomalous = (
        absolute_change >= MIN_ABSOLUTE_CHANGE
        and matches_direction()
    )

    return {
        "anomalous": anomalous,
        "z_score": None,
        "baseline_avg": baseline_mean,
        "recent_avg": recent_mean,
        "absolute_change": round(absolute_change, 2),
        "change": round(change, 2),
        "direction": direction,
        "reason": (
            "flat baseline; anomaly determined by absolute change threshold"
        ),
    }