"""
fixtures.py — synthetic fault scenarios, each with a KNOWN ground truth.

WHY THIS FILE EXISTS AND WHY IT'S FIRST: an agent that investigates
incidents is only as testable as the data you feed it. Real production
logs/metrics tied to a confirmed root cause essentially don't exist
publicly — companies don't publish that. So instead of scraping fake
"AI-generated" log lines (which don't have the noise real systems
produce, and which fall apart the moment someone asks "where did this
eval data come from"), each scenario here represents a fault YOU know
the cause of, because in the real version of this project you'd inject
it yourself into a real running docker-compose system. This file is the
standalone version of that idea — the fault is hardcoded instead of
actually injected, so you can study the agent logic without first
standing up real infrastructure.

Every scenario has the SAME shape: logs, a metric time series, deploy
events, and a `ground_truth` field. The agent NEVER sees ground_truth —
that's only used afterward, by you, to check whether the agent's
conclusion actually matches what really happened.
"""
from dataclasses import dataclass, field


@dataclass
class LogLine:
    timestamp: int          # seconds since incident start (0 = incident begins)
    service: str
    level: str               # "INFO" | "WARN" | "ERROR"
    message: str


@dataclass
class DeployEvent:
    timestamp: int
    service: str
    commit: str
    description: str


@dataclass
class Scenario:
    id: str
    description: str                    # what a human would type when reporting this incident
    primary_service: str
    logs: list[LogLine]
    metrics: dict[str, list[tuple[int, float]]]   # metric_name -> [(timestamp, value), ...]
    deploys: list[DeployEvent]
    service_graph: list[tuple[str, str]]           # (caller, callee) edges — caller depends on callee
    ground_truth: str                              # NEVER shown to the agent — used only for your own eval afterward


# ─────────────────────────────────────────────────────────────────────────
# Scenario 1 — bad deploy, immediate regression. The "easy" case: a
# deploy happens, errors start seconds later, the stack trace points at
# exactly what changed. This is the case CodeAct-style query composition
# and deploy-correlation should nail almost every time.
# ─────────────────────────────────────────────────────────────────────────
BAD_DEPLOY = Scenario(
    id="bad_deploy_regression",
    description="payment-service is returning 500s to a large fraction of requests",
    primary_service="payment-service",
    logs=[
        LogLine(-5, "payment-service", "INFO", "Deploy started: commit a1b2c3d"),
        LogLine(0, "payment-service", "INFO", "Deploy completed: commit a1b2c3d"),
        LogLine(12, "payment-service", "ERROR", "TypeError: process_payment() missing 1 required positional argument: 'currency'"),
        LogLine(18, "payment-service", "ERROR", "TypeError: process_payment() missing 1 required positional argument: 'currency'"),
        LogLine(25, "payment-service", "ERROR", "TypeError: process_payment() missing 1 required positional argument: 'currency'"),
        LogLine(40, "payment-service", "WARN", "Retry queue depth increasing: 47 pending"),
        LogLine(55, "payment-service", "ERROR", "TypeError: process_payment() missing 1 required positional argument: 'currency'"),
    ],
    metrics={
        "error_rate": [(-60, 0.3), (-45, 0.4), (-30, 0.5), (-15, 0.4), (-10, 0.5), (-5, 0.4), (0, 0.6), (10, 45.0), (20, 62.0), (30, 71.0), (40, 68.0), (55, 74.0)],
        "request_rate": [(-60, 119), (-45, 121), (-30, 118), (-15, 120), (-10, 120), (-5, 118), (0, 121), (10, 119), (20, 122), (30, 120), (40, 117), (55, 119)],
    },
    deploys=[
        DeployEvent(-5, "payment-service", "a1b2c3d", "Refactor process_payment signature to accept currency parameter"),
    ],
    service_graph=[("checkout-service", "payment-service"), ("mobile-app", "payment-service")],
    ground_truth="Deploy a1b2c3d changed process_payment()'s signature to require a 'currency' argument, but a caller wasn't updated to pass it — every call raises TypeError. Fix: either patch the caller or add a default value.",
)

# ─────────────────────────────────────────────────────────────────────────
# Scenario 2 — gradual resource exhaustion, NOT deploy-correlated. The
# "harder" case on purpose: nothing deployed recently, so deploy
# correlation finds nothing. This is exactly what the statistical
# anomaly detector (not just "is there a recent deploy") needs to catch —
# a slow trend, not a step change.
# ─────────────────────────────────────────────────────────────────────────
POOL_EXHAUSTION = Scenario(
    id="connection_pool_exhaustion",
    description="checkout-service requests are timing out intermittently",
    primary_service="checkout-service",
    logs=[
        LogLine(-600, "checkout-service", "INFO", "Connection pool initialized: max_size=20"),
        LogLine(-30, "checkout-service", "WARN", "Connection pool at 85% capacity (17/20)"),
        LogLine(-10, "checkout-service", "WARN", "Connection pool at 95% capacity (19/20)"),
        LogLine(0, "checkout-service", "ERROR", "TimeoutError: could not acquire connection from pool within 5000ms"),
        LogLine(15, "checkout-service", "ERROR", "TimeoutError: could not acquire connection from pool within 5000ms"),
        LogLine(30, "checkout-service", "ERROR", "TimeoutError: could not acquire connection from pool within 5000ms"),
        LogLine(45, "checkout-service", "WARN", "Connection pool at 100% capacity (20/20)"),
    ],
    metrics={
        # a slow climb over 10 minutes leading up to the incident — no
        # single moment looks alarming in isolation, the TREND is the signal
        "db_pool_active_connections": [
            (-600, 3), (-500, 4), (-400, 6), (-300, 9), (-200, 13),
            (-100, 16), (-30, 17), (-10, 19), (0, 20), (15, 20), (30, 20),
        ],
        "error_rate": [(-600, 0.1), (-100, 0.2), (-30, 1.5), (-10, 4.0), (0, 22.0), (15, 31.0), (30, 35.0)],
    },
    deploys=[],   # deliberately empty — nothing deployed recently, this is a genuine trend, not a regression
    service_graph=[("checkout-service", "inventory-service"), ("checkout-service", "payment-service")],
    ground_truth="checkout-service's DB connection pool (max 20) filled gradually over ~10 minutes with connections that weren't being released — likely a connection leak in a code path that doesn't close on error — and once full, new requests time out waiting for a free connection.",
)

# ─────────────────────────────────────────────────────────────────────────
# Scenario 3 — cascading downstream failure. Tests the service-graph
# tool specifically: the SYMPTOM is on cart-service, but the CAUSE is on
# a service it depends on.
# ─────────────────────────────────────────────────────────────────────────
CASCADING_FAILURE = Scenario(
    id="downstream_dependency_failure",
    description="cart-service is failing to add items for many users",
    primary_service="cart-service",
    logs=[
        LogLine(-2, "inventory-service", "ERROR", "Database connection refused: inventory-db:5432"),
        LogLine(0, "cart-service", "ERROR", "UpstreamError: inventory-service returned 503"),
        LogLine(5, "cart-service", "ERROR", "UpstreamError: inventory-service returned 503"),
        LogLine(8, "inventory-service", "ERROR", "Database connection refused: inventory-db:5432"),
        LogLine(12, "cart-service", "ERROR", "UpstreamError: inventory-service returned 503"),
        LogLine(20, "inventory-service", "ERROR", "Database connection refused: inventory-db:5432"),
    ],
    metrics={
        "error_rate": [(-60, 0.2), (-45, 0.3), (-30, 0.4), (-15, 0.3), (-10, 0.3), (-2, 0.5), (0, 38.0), (5, 41.0), (12, 44.0), (20, 46.0)],
    },
    deploys=[],
    service_graph=[("cart-service", "inventory-service"), ("checkout-service", "cart-service"), ("inventory-service", "inventory-db")],
    ground_truth="inventory-db became unreachable (connection refused), which made inventory-service start returning 503s, which cascaded into cart-service failures — the reported symptom (cart-service) is a downstream effect, not the actual failure point.",
)

ALL_SCENARIOS: dict[str, Scenario] = {
    s.id: s for s in [BAD_DEPLOY, POOL_EXHAUSTION, CASCADING_FAILURE]
}
