import json
from datetime import UTC, datetime, timedelta

import anyio
import httpx2
import pytest
from mcp.client import Client

from mcp_server.incidents import OrchestratorIncidentSource
from mcp_server.server import build_server
from mcp_server.tools import MAX_LOG_LINES, MAX_LOG_MINUTES, MAX_SERIES
from orchestrator.adapters.base import AdapterError, MetricSeries
from orchestrator.models import Evidence, Incident, LogLine

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def make_incident(incident_id: int, status: str = "open") -> Incident:
    return Incident(
        id=incident_id,
        fingerprint=f"fp{incident_id}",
        status=status,
        alertname="ShopLiteHighErrorRate",
        severity="critical",
        service="shoplite",
        env="local",
        version="0.1.0",
        commit_sha="abc1234",
        summary="ShopLite error rate is 38% on version 0.1.0 (abc1234)",
        started_at=NOW,
        last_seen_at=NOW,
        evidence=Evidence(
            gathered_at=NOW,
            metrics={"error_rate": 0.38},
            error_logs=[LogLine(ts=NOW, line="checkout failed: injected fault")],
        ),
    )


class FakeIncidents:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.items = [make_incident(3), make_incident(2, "resolved"), make_incident(1)]

    def list(self) -> list[Incident]:
        if self.fail:
            raise AdapterError("Orchestrator unreachable")
        return self.items

    def get(self, incident_id: int) -> Incident | None:
        return next((i for i in self.list() if i.id == incident_id), None)


class FakeMetrics:
    def __init__(self, n_series: int = 2, fail: bool = False) -> None:
        self.n_series, self.fail, self.queries = n_series, fail, []

    def query(self, query: str) -> list[MetricSeries]:
        self.queries.append(query)
        if self.fail:
            raise AdapterError("Prometheus query failed: bad_data")
        return [
            MetricSeries(labels={"version": f"0.{i}.0"}, value=0.1 * i)
            for i in range(self.n_series)
        ]

    def instant(self, query: str) -> float | None:
        return None


class FakeLogs:
    def __init__(self) -> None:
        self.calls = []

    def search(self, query: str, since: timedelta, limit: int) -> list[LogLine]:
        self.calls.append((query, since, limit))
        return [LogLine(ts=NOW, line="checkout failed", labels={"level": "ERROR"})]


def server(incidents=None, metrics=None, logs=None):
    return build_server(incidents or FakeIncidents(), metrics or FakeMetrics(), logs or FakeLogs())


def call(srv, tool: str, args: dict | None = None):
    """Call a tool through the real MCP protocol (in memory) and return the result."""

    async def run():
        async with Client(srv) as client:
            return await client.call_tool(tool, args or {})

    return anyio.run(run)


def data(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def tools(srv):
    async def run():
        async with Client(srv) as client:
            return (await client.list_tools()).tools

    return anyio.run(run)


# --- Safety: the toolbox is read-only ---


def test_only_the_four_read_only_tools_exist():
    found = tools(server())
    assert {t.name for t in found} == {
        "list_incidents",
        "get_incident",
        "query_metrics",
        "search_logs",
    }
    for tool in found:
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


def test_no_tool_name_suggests_a_write_action():
    risky = ("create", "update", "delete", "restart", "rollback", "deploy", "write", "set_")
    for tool in tools(server()):
        assert not any(word in tool.name for word in risky), tool.name


# --- Incidents ---


def test_list_incidents_defaults_to_open_without_evidence():
    result = data(call(server(), "list_incidents"))
    assert [i["id"] for i in result["incidents"]] == [3, 1]
    assert result["total_matching"] == 2
    assert "evidence" not in result["incidents"][0]
    assert result["incidents"][0]["commit_sha"] == "abc1234"


def test_list_incidents_filter_and_limit():
    result = data(call(server(), "list_incidents", {"status": "all", "limit": 1}))
    assert [i["id"] for i in result["incidents"]] == [3]
    assert result["total_matching"] == 3


def test_list_incidents_rejects_unknown_status():
    assert call(server(), "list_incidents", {"status": "deleted"}).is_error


def test_get_incident_includes_evidence():
    result = data(call(server(), "get_incident", {"incident_id": 1}))
    assert result["evidence"]["metrics"]["error_rate"] == pytest.approx(0.38)
    assert result["evidence"]["error_logs"][0]["line"] == "checkout failed: injected fault"


def test_get_missing_incident_is_a_clear_error():
    result = call(server(), "get_incident", {"incident_id": 99})
    assert result.is_error
    assert "Incident 99 not found" in result.content[0].text


def test_orchestrator_down_is_a_clear_error():
    result = call(server(incidents=FakeIncidents(fail=True)), "list_incidents")
    assert result.is_error
    assert "Orchestrator unreachable" in result.content[0].text


# --- Metrics ---


def test_query_metrics_returns_labelled_values():
    metrics = FakeMetrics()
    result = data(call(server(metrics=metrics), "query_metrics", {"promql": "up"}))
    assert result["series"][1] == {"labels": {"version": "0.1.0"}, "value": 0.1}
    assert result["truncated"] is False
    assert metrics.queries == ["up"]


def test_query_metrics_caps_series():
    srv = server(metrics=FakeMetrics(n_series=MAX_SERIES + 5))
    result = data(call(srv, "query_metrics", {"promql": "up"}))
    assert len(result["series"]) == MAX_SERIES
    assert result["total_series"] == MAX_SERIES + 5
    assert result["truncated"] is True


def test_bad_promql_is_a_clear_error():
    result = call(server(metrics=FakeMetrics(fail=True)), "query_metrics", {"promql": "(("})
    assert result.is_error
    assert "bad_data" in result.content[0].text


# --- Logs ---


def test_search_logs_passes_query_and_defaults():
    logs = FakeLogs()
    result = data(call(server(logs=logs), "search_logs", {"logql": '{level="ERROR"}'}))
    assert result["lines"][0]["line"] == "checkout failed"
    assert logs.calls == [('{level="ERROR"}', timedelta(minutes=15), 50)]


def test_search_logs_clamps_window_and_limit():
    logs = FakeLogs()
    args = {"logql": "{}", "minutes": 999_999, "limit": 999_999}
    data(call(server(logs=logs), "search_logs", args))
    _, since, limit = logs.calls[0]
    assert since == timedelta(minutes=MAX_LOG_MINUTES)
    assert limit == MAX_LOG_LINES


# --- Orchestrator incident adapter ---


def orchestrator(handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    return OrchestratorIncidentSource("http://orch", transport=httpx2.MockTransport(respond)), seen


def test_incident_source_only_reads():
    body = [make_incident(1).model_dump(mode="json")]
    source, seen = orchestrator(lambda r: httpx2.Response(200, json=body))
    assert source.list()[0].id == 1
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/incidents")]


def test_incident_source_404_is_none():
    source, _ = orchestrator(lambda r: httpx2.Response(404, json={"detail": "Incident not found"}))
    assert source.get(5) is None


def test_incident_source_errors_become_adapter_errors():
    source, _ = orchestrator(lambda r: httpx2.Response(500, text="boom"))
    with pytest.raises(AdapterError):
        source.list()

    def refuse(request):
        raise httpx2.ConnectError("refused", request=request)

    unreachable = OrchestratorIncidentSource("http://orch", transport=httpx2.MockTransport(refuse))
    with pytest.raises(AdapterError, match="unreachable"):
        unreachable.get(1)
