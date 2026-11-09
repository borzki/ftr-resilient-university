"""Prometheus metrics shared by all services."""
from prometheus_client import Counter, Gauge, Histogram

from . import config

LABELS = {"service": config.SERVICE_NAME, "instance_id": config.INSTANCE_ID}

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0),
)
IN_FLIGHT = Gauge("http_requests_in_flight", "Requests currently being processed", ["service"])
DEPENDENCY_CALLS = Counter(
    "dependency_calls_total",
    "Outbound dependency calls by outcome",
    ["service", "dependency", "outcome"],
)
RETRIES = Counter("dependency_retries_total", "Retry attempts", ["service", "dependency"])
CIRCUIT_STATE = Gauge(
    "circuit_breaker_state", "0=closed 1=half-open 2=open", ["service", "dependency"]
)
DEPENDENCY_UP = Gauge("dependency_up", "Dependency reachable (1) or not (0)", ["service", "dependency"])
DEGRADED = Counter("degraded_responses_total", "Responses served in degraded mode", ["service", "feature"])
DB_FAILOVER_READS = Counter("db_failover_reads_total", "Reads served by the replica", ["service"])
AUTH_FAILURES = Counter("auth_failures_total", "Authentication/authorization failures", ["service", "reason"])
AUDIT_EVENTS = Counter("audit_events_total", "Audit events", ["service", "action", "outcome"])
PAYMENTS = Counter("payments_total", "Payment requests by outcome", ["service", "outcome"])
VALIDATION_ERRORS = Counter("validation_errors_total", "Rejected invalid inputs", ["service", "route"])
