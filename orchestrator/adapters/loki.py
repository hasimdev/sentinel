"""LogSource backed by Loki. Uses only the read-only query API (HTTP GET)."""

from datetime import UTC, datetime, timedelta

import httpx2

from orchestrator.adapters.base import AdapterError
from orchestrator.models import LogLine


class LokiLogSource:
    def __init__(self, base_url: str, timeout: float = 5.0, transport=None) -> None:
        self._client = httpx2.Client(base_url=base_url, timeout=timeout, transport=transport)

    def search(self, query: str, since: timedelta, limit: int) -> list[LogLine]:
        params = {
            "query": query,
            "since": f"{int(since.total_seconds())}s",
            "limit": limit,
            "direction": "backward",  # newest first
        }
        try:
            resp = self._client.get("/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx2.HTTPError, ValueError) as exc:
            raise AdapterError(f"Loki query failed: {exc}") from exc

        if body.get("status") != "success":
            raise AdapterError(f"Loki returned an error: {body}")
        lines = [
            LogLine(
                ts=datetime.fromtimestamp(int(ts_ns) / 1e9, UTC),
                line=line,
                labels=stream.get("stream", {}),
            )
            for stream in body.get("data", {}).get("result", [])
            for ts_ns, line in stream.get("values", [])
        ]
        lines.sort(key=lambda log_line: log_line.ts, reverse=True)
        return lines[:limit]
