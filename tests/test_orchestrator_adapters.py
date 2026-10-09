from datetime import timedelta

import httpx2
import pytest

from orchestrator.adapters.base import AdapterError, label_selector
from orchestrator.adapters.loki import LokiLogSource
from orchestrator.adapters.prometheus import PrometheusMetricSource


def fake_server(handler):
    """Record every request and answer with handler(request)."""
    seen: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    return httpx2.MockTransport(respond), seen


# --- label_selector ---


def test_label_selector_skips_missing_and_escapes_quotes():
    assert label_selector(service="shoplite", version=None, env='we"ird') == (
        'service="shoplite", env="we\\"ird"'
    )


# --- Prometheus ---


def prom_vector(value: str) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, value]}]}}


def test_prometheus_instant_returns_number_and_only_reads():
    transport, seen = fake_server(lambda r: httpx2.Response(200, json=prom_vector("0.37")))
    source = PrometheusMetricSource("http://prom", transport=transport)
    assert source.instant("up") == pytest.approx(0.37)
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/query"
    assert seen[0].url.params["query"] == "up"


def test_prometheus_query_returns_every_labelled_series():
    body = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"version": "0.1.0"}, "value": [0, "0.37"]},
                {"metric": {"version": "0.2.0"}, "value": [0, "NaN"]},
                {"metric": {"version": "0.3.0"}, "value": [0, "0"]},
            ],
        },
    }
    transport, _ = fake_server(lambda r: httpx2.Response(200, json=body))
    series = PrometheusMetricSource("http://prom", transport=transport).query("x")
    assert [(s.labels["version"], s.value) for s in series] == [("0.1.0", 0.37), ("0.3.0", 0.0)]


def test_prometheus_scalar_result():
    body = {"status": "success", "data": {"resultType": "scalar", "result": [0, "2"]}}
    transport, _ = fake_server(lambda r: httpx2.Response(200, json=body))
    assert PrometheusMetricSource("http://prom", transport=transport).instant("1+1") == 2.0


def test_prometheus_no_data_or_nan_is_none():
    empty = {"status": "success", "data": {"result": []}}
    transport, _ = fake_server(lambda r: httpx2.Response(200, json=empty))
    assert PrometheusMetricSource("http://prom", transport=transport).instant("x") is None
    transport, _ = fake_server(lambda r: httpx2.Response(200, json=prom_vector("NaN")))
    assert PrometheusMetricSource("http://prom", transport=transport).instant("x") is None


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="boom"),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"status": "error", "error": "bad query"}),
    ],
)
def test_prometheus_failures_become_adapter_errors(response):
    transport, _ = fake_server(lambda r: response)
    with pytest.raises(AdapterError):
        PrometheusMetricSource("http://prom", transport=transport).instant("x")


def test_prometheus_query_errors_explain_what_is_wrong():
    body = {"status": "error", "errorType": "bad_data", "error": "parse error: unclosed paren"}
    transport, _ = fake_server(lambda r: httpx2.Response(400, json=body))
    with pytest.raises(AdapterError, match="unclosed paren"):
        PrometheusMetricSource("http://prom", transport=transport).instant("sum((")


def test_loki_query_errors_explain_what_is_wrong():
    transport, _ = fake_server(lambda r: httpx2.Response(400, text="parse error: unexpected }"))
    with pytest.raises(AdapterError, match="unexpected }"):
        LokiLogSource("http://loki", transport=transport).search("}", timedelta(1), limit=1)


def test_prometheus_unreachable_becomes_adapter_error():
    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(AdapterError):
        PrometheusMetricSource("http://prom", transport=httpx2.MockTransport(refuse)).instant("x")


# --- Loki ---


def loki_streams() -> dict:
    return {
        "status": "success",
        "data": {
            "result": [
                {"stream": {"level": "ERROR"}, "values": [["1700000001000000000", "older"]]},
                {"stream": {"level": "ERROR"}, "values": [["1700000002000000000", "newer"]]},
            ]
        },
    }


def test_loki_search_returns_newest_first_and_only_reads():
    transport, seen = fake_server(lambda r: httpx2.Response(200, json=loki_streams()))
    lines = LokiLogSource("http://loki", transport=transport).search(
        '{level="ERROR"}', timedelta(minutes=15), limit=10
    )
    assert [line.line for line in lines] == ["newer", "older"]
    assert lines[0].labels == {"level": "ERROR"}
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/loki/api/v1/query_range"
    assert request.url.params["since"] == "900s"
    assert request.url.params["limit"] == "10"


def test_loki_respects_limit():
    transport, _ = fake_server(lambda r: httpx2.Response(200, json=loki_streams()))
    lines = LokiLogSource("http://loki", transport=transport).search("{}", timedelta(1), limit=1)
    assert [line.line for line in lines] == ["newer"]


def test_loki_failure_becomes_adapter_error():
    transport, _ = fake_server(lambda r: httpx2.Response(503, text="unavailable"))
    with pytest.raises(AdapterError):
        LokiLogSource("http://loki", transport=transport).search("{}", timedelta(1), limit=1)
