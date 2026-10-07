"""Prometheus metrics for ShopLite: request counts, errors (5xx status) and response times."""

import time

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from apps.shoplite.logging_setup import TAG_FIELDS

# Response-time buckets in seconds (5 ms up to 2.5 s).
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


def instrument(app: FastAPI, tags: dict[str, str]) -> CollectorRegistry:
    """Count and time every request, and expose the results at GET /metrics."""
    # One registry per app, so tests that build several apps don't share counters.
    registry = CollectorRegistry()
    requests_total = Counter(
        "shoplite_http_requests_total",
        "HTTP requests handled, by route and status code. Errors are status 5xx.",
        [*TAG_FIELDS, "method", "route", "status"],
        registry=registry,
    )
    duration = Histogram(
        "shoplite_http_request_duration_seconds",
        "Time spent handling HTTP requests.",
        [*TAG_FIELDS, "method", "route"],
        buckets=LATENCY_BUCKETS,
        registry=registry,
    )

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        status = 500  # if the handler crashes, count it as a server error
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # Use the route pattern (e.g. "/checkout"), not the raw URL, to keep labels few.
            route = request.scope.get("route")
            path = route.path if route else "unmatched"
            if path != "/metrics":  # don't count Prometheus's own scrapes
                requests_total.labels(
                    **tags, method=request.method, route=path, status=str(status)
                ).inc()
                duration.labels(**tags, method=request.method, route=path).observe(
                    time.perf_counter() - start
                )

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return registry
