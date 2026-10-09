"""MetricSource backed by Prometheus. Uses only the read-only query API (HTTP GET)."""

import math

import httpx2

from orchestrator.adapters.base import AdapterError, MetricSeries, explain_http_error


class PrometheusMetricSource:
    def __init__(self, base_url: str, timeout: float = 5.0, transport=None) -> None:
        self._client = httpx2.Client(base_url=base_url, timeout=timeout, transport=transport)

    def query(self, query: str) -> list[MetricSeries]:
        """Evaluate a query now; one entry per labelled series (NaN values dropped)."""
        try:
            resp = self._client.get("/api/v1/query", params={"query": query})
            if resp.is_error:
                raise AdapterError(explain_http_error("Prometheus", resp))
            body = resp.json()
        except (httpx2.HTTPError, ValueError) as exc:
            raise AdapterError(f"Prometheus query failed: {exc}") from exc

        if body.get("status") != "success":
            raise AdapterError(f"Prometheus returned an error: {body.get('error', body)}")
        data = body.get("data", {})
        result = data.get("result", [])
        if data.get("resultType") == "scalar":  # e.g. query "1 + 1"
            result = [{"metric": {}, "value": result}]
        series = []
        for item in result:
            value = float(item["value"][1])
            if not math.isnan(value):
                series.append(MetricSeries(labels=item.get("metric", {}), value=value))
        return series

    def instant(self, query: str) -> float | None:
        series = self.query(query)
        return series[0].value if series else None
