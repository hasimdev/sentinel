from fastapi.testclient import TestClient

from apps.shoplite.main import create_app
from apps.shoplite.settings import Settings

TAGS = {"service": "shoplite", "env": "test", "version": "9.9.9", "commit_sha": "abc1234"}


def make_app(fail_rate: float = 0.0):
    app = create_app(Settings(**TAGS, fail_rate=fail_rate))
    return app, TestClient(app)


def request_count(app, route: str, status: str, method: str = "GET") -> float:
    value = app.state.metrics_registry.get_sample_value(
        "shoplite_http_requests_total",
        {**TAGS, "method": method, "route": route, "status": status},
    )
    return value or 0.0


def test_metrics_endpoint_exposes_shoplite_metrics():
    _, client = make_app()
    client.get("/products")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "shoplite_http_requests_total" in resp.text
    assert "shoplite_http_request_duration_seconds" in resp.text


def test_requests_are_counted_by_route_and_status():
    app, client = make_app()
    for _ in range(3):
        client.get("/products")
    client.post("/checkout", json={"product_id": "mug"})
    assert request_count(app, "/products", "200") == 3
    assert request_count(app, "/checkout", "200", method="POST") == 1


def test_injected_failures_are_counted_as_5xx_errors():
    app, client = make_app(fail_rate=1.0)
    for _ in range(4):
        client.post("/checkout", json={"product_id": "mug"})
    assert request_count(app, "/checkout", "503", method="POST") == 4
    assert request_count(app, "/checkout", "200", method="POST") == 0


def test_client_errors_are_counted_under_their_route():
    app, client = make_app()
    client.post("/checkout", json={"product_id": "spaceship"})
    assert request_count(app, "/checkout", "404", method="POST") == 1


def test_unknown_urls_share_one_label():
    app, client = make_app()
    client.get("/no-such-page")
    client.get("/another-missing-page")
    assert request_count(app, "unmatched", "404") == 2


def test_response_times_are_recorded():
    app, client = make_app()
    client.get("/health")
    client.get("/health")
    count = app.state.metrics_registry.get_sample_value(
        "shoplite_http_request_duration_seconds_count",
        {**TAGS, "method": "GET", "route": "/health"},
    )
    assert count == 2


def test_metrics_scrapes_are_not_counted():
    app, client = make_app()
    client.get("/metrics")
    client.get("/metrics")
    assert request_count(app, "/metrics", "200") == 0


def test_every_sample_carries_the_four_tags():
    app, client = make_app()
    client.get("/products")
    for metric in app.state.metrics_registry.collect():
        if not metric.name.startswith("shoplite_"):
            continue
        for sample in metric.samples:
            for key, value in TAGS.items():
                assert sample.labels[key] == value
