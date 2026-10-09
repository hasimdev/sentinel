"""The four READ-ONLY look-up tools, as plain functions.

Shared by the MCP server (for Claude Code) and the orchestrator's triage agent, so there is
exactly one implementation. None of them can change anything (see CLAUDE.md).
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Literal, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from orchestrator.adapters.base import AdapterError, LogSource, MetricSource
from orchestrator.models import Incident

# Limits keep answers small enough for an AI to read and stop runaway queries.
MAX_INCIDENTS = 50
MAX_SERIES = 50
MAX_LOG_LINES = 100
MAX_LOG_MINUTES = 24 * 60


class IncidentSource(Protocol):
    def list(self) -> list[Incident]: ...
    def get(self, incident_id: int) -> Incident | None: ...


def _summary(incident: Incident) -> dict:
    return incident.model_dump(
        mode="json",
        include={
            "id",
            "status",
            "alertname",
            "severity",
            "service",
            "env",
            "version",
            "commit_sha",
            "summary",
            "started_at",
            "resolved_at",
        },
    )


def make_toolbox(
    incidents: IncidentSource, metrics: MetricSource, logs: LogSource
) -> dict[str, Callable[..., dict]]:
    """Return {tool name: function}. Failures raise ToolError with a readable message."""

    def list_incidents(
        status: Literal["open", "resolved", "all"] = "open", limit: int = 10
    ) -> dict:
        """List incidents, newest first, without their evidence."""
        limit = max(1, min(limit, MAX_INCIDENTS))
        try:
            found = incidents.list()
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        if status != "all":
            found = [i for i in found if i.status == status]
        return {
            "incidents": [_summary(i) for i in found[:limit]],
            "total_matching": len(found),
        }

    def get_incident(incident_id: int) -> dict:
        """One incident with its evidence: metrics, recent error logs and any gaps."""
        try:
            incident = incidents.get(incident_id)
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        if incident is None:
            raise ToolError(f"Incident {incident_id} not found")
        return incident.model_dump(mode="json")

    def query_metrics(promql: str) -> dict:
        """Run a PromQL instant query against Prometheus and return the labelled values.

        Example: sum by (version) (rate(shoplite_http_requests_total{status=~"5.."}[5m]))
        """
        try:
            series = metrics.query(promql)
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "series": [s.model_dump() for s in series[:MAX_SERIES]],
            "total_series": len(series),
            "truncated": len(series) > MAX_SERIES,
        }

    def search_logs(logql: str, minutes: int = 15, limit: int = 50) -> dict:
        """Search log lines in Loki with LogQL, newest first.

        Example: {service="shoplite", level="ERROR", version="0.1.0"}
        """
        minutes = max(1, min(minutes, MAX_LOG_MINUTES))
        limit = max(1, min(limit, MAX_LOG_LINES))
        try:
            lines = logs.search(logql, timedelta(minutes=minutes), limit)
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "lines": [line.model_dump(mode="json") for line in lines],
            "minutes_searched": minutes,
            "limit": limit,
        }

    return {f.__name__: f for f in (list_incidents, get_incident, query_metrics, search_logs)}
