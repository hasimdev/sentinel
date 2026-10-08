"""MetricSource backed by Prometheus. Uses only the read-only query API (HTTP GET)."""

import math

import httpx2

from orchestrator.adapters.base import AdapterError


class PrometheusMetricSource:
    def __init__(self, base_url: str, timeout: float = 5.0, transport=None) -> None:
        self._client = httpx2.Client(base_url=base_url, timeout=timeout, transport=transport)

    def instant(self, query: str) -> float | None:
        try:
            resp = self._client.get("/api/v1/query", params={"query": query})
            resp.raise_for_status()
            body = resp.json()
        except (httpx2.HTTPError, ValueError) as exc:
            raise AdapterError(f"Prometheus query failed: {exc}") from exc

        if body.get("status") != "success":
            raise AdapterError(f"Prometheus returned an error: {body.get('error', body)}")
        result = body.get("data", {}).get("result", [])
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value
