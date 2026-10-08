from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.base import AdapterError
from orchestrator.evidence import error_log_query, gather_evidence, metric_queries
from orchestrator.main import create_app
from orchestrator.models import Incident, LogLine
from orchestrator.settings import Settings, load_settings

LABELS = {
    "alertname": "ShopLiteHighErrorRate",
    "severity": "critical",
    "service": "shoplite",
    "env": "local",
    "version": "0.1.0",
    "commit_sha": "abc1234",
}


class FakeMetrics:
    def __init__(self, value: float | None = 0.38, fail: bool = False) -> None:
        self.value, self.fail, self.queries = value, fail, []

    def instant(self, query: str) -> float | None:
        self.queries.append(query)
        if self.fail:
            raise AdapterError("prometheus down")
        return self.value


class FakeLogs:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.queries = fail, []

    def search(self, query: str, since: timedelta, limit: int) -> list[LogLine]:
        self.queries.append(query)
        if self.fail:
            raise AdapterError("loki down")
        return [LogLine(ts=datetime.now(UTC), line="checkout failed: injected fault")]


def alert(status: str = "firing", fingerprint: str = "fp1", **label_overrides) -> dict:
    return {
        "status": status,
        "labels": {**LABELS, **label_overrides},
        "annotations": {"summary": "ShopLite error rate is 38% on version 0.1.0 (abc1234)"},
        "startsAt": "2026-10-07T10:00:00Z",
        "endsAt": "2026-10-07T10:05:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
    }


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "data" / "incidents.db")


def make_client(db_path, metrics=None, logs=None) -> TestClient:
    settings = Settings(env="test", commit_sha="test123", db_path=db_path)
    return TestClient(create_app(settings, metrics or FakeMetrics(), logs or FakeLogs()))


# --- Incident lifecycle ---


def test_firing_alert_opens_incident_with_evidence(db_path):
    client = make_client(db_path)
    resp = client.post("/alerts", json={"alerts": [alert()]})
    assert resp.status_code == 200
    assert resp.json()["opened"] == [1]

    incident = client.get("/incidents/1").json()
    assert incident["status"] == "open"
    assert incident["service"] == "shoplite"
    assert incident["version"] == "0.1.0"
    assert incident["commit_sha"] == "abc1234"
    assert incident["summary"].startswith("ShopLite error rate is 38%")
    evidence = incident["evidence"]
    assert evidence["metrics"]["error_rate"] == pytest.approx(0.38)
    assert evidence["error_logs"][0]["line"] == "checkout failed: injected fault"
    assert evidence["gaps"] == []


def test_repeated_firing_alert_does_not_duplicate(db_path):
    client = make_client(db_path)
    client.post("/alerts", json={"alerts": [alert()]})
    resp = client.post("/alerts", json={"alerts": [alert()]})
    assert resp.json() == {"opened": [], "updated": [1], "resolved": []}
    assert len(client.get("/incidents").json()) == 1


def test_resolved_alert_closes_incident(db_path):
    client = make_client(db_path)
    client.post("/alerts", json={"alerts": [alert()]})
    resp = client.post("/alerts", json={"alerts": [alert(status="resolved")]})
    assert resp.json()["resolved"] == [1]
    incident = client.get("/incidents/1").json()
    assert incident["status"] == "resolved"
    assert incident["resolved_at"].startswith("2026-10-07T10:05")


def test_refiring_after_resolve_opens_a_new_incident(db_path):
    client = make_client(db_path)
    client.post("/alerts", json={"alerts": [alert()]})
    client.post("/alerts", json={"alerts": [alert(status="resolved")]})
    resp = client.post("/alerts", json={"alerts": [alert()]})
    assert resp.json()["opened"] == [2]


def test_resolved_alert_we_never_saw_is_ignored(db_path):
    client = make_client(db_path)
    resp = client.post("/alerts", json={"alerts": [alert(status="resolved")]})
    assert resp.json() == {"opened": [], "updated": [], "resolved": []}
    assert client.get("/incidents").json() == []


def test_different_alerts_get_separate_incidents(db_path):
    client = make_client(db_path)
    payload = {"alerts": [alert(fingerprint="a"), alert(fingerprint="b", version="0.2.0")]}
    assert client.post("/alerts", json=payload).json()["opened"] == [1, 2]
    newest_first = [i["version"] for i in client.get("/incidents").json()]
    assert newest_first == ["0.2.0", "0.1.0"]


def test_incidents_survive_a_restart(db_path):
    make_client(db_path).post("/alerts", json={"alerts": [alert()]})
    restarted = make_client(db_path)
    assert restarted.get("/incidents/1").json()["status"] == "open"


def test_unknown_incident_is_404(db_path):
    assert make_client(db_path).get("/incidents/99").status_code == 404


def test_malformed_webhook_is_rejected(db_path):
    resp = make_client(db_path).post("/alerts", json={"alerts": [{"status": "exploded"}]})
    assert resp.status_code == 422


# --- Evidence ---


def test_broken_sources_still_open_incident_and_record_gaps(db_path):
    client = make_client(db_path, FakeMetrics(fail=True), FakeLogs(fail=True))
    assert client.post("/alerts", json={"alerts": [alert()]}).json()["opened"] == [1]
    evidence = client.get("/incidents/1").json()["evidence"]
    assert evidence["metrics"] == {
        "error_rate": None,
        "requests_per_second": None,
        "p95_latency_seconds": None,
    }
    assert len(evidence["gaps"]) == 4
    assert any("loki down" in gap for gap in evidence["gaps"])


def test_alert_without_service_label_records_a_gap(db_path):
    metrics = FakeMetrics()
    client = make_client(db_path, metrics)
    labels = {k: v for k, v in LABELS.items() if k != "service"}
    payload = {"alerts": [{**alert(), "labels": labels}]}
    client.post("/alerts", json=payload)
    evidence = client.get("/incidents/1").json()["evidence"]
    assert "no usable 'service' label" in evidence["gaps"][0]
    assert metrics.queries == []


def incident(**overrides) -> Incident:
    now = datetime.now(UTC)
    base = {
        "fingerprint": "fp",
        "alertname": "x",
        "started_at": now,
        "last_seen_at": now,
        **{k: LABELS[k] for k in ("service", "env", "version", "commit_sha")},
    }
    return Incident(**{**base, **overrides})


def test_queries_are_scoped_to_the_failing_version_and_commit():
    queries = metric_queries(incident())
    for query in queries.values():
        assert 'version="0.1.0"' in query
        assert 'commit_sha="abc1234"' in query
    assert "shoplite_http_requests_total" in queries["error_rate"]
    assert 'status=~"5.."' in queries["error_rate"]
    assert error_log_query(incident()) == (
        '{service="shoplite", env="local", version="0.1.0", ' 'commit_sha="abc1234", level="ERROR"}'
    )


def test_service_label_cannot_inject_into_queries():
    metrics = FakeMetrics()
    evidence = gather_evidence(incident(service="x{evil}"), metrics, FakeLogs())
    assert metrics.queries == []
    assert evidence.gaps


# --- Settings ---


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom:9090")
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", "x/y.db")
    monkeypatch.setenv("LOKI_URL", "")  # blank in .env means "use the default"
    s = load_settings()
    assert s.prometheus_url == "http://prom:9090"
    assert s.db_path == "x/y.db"
    assert s.loki_url == "http://127.0.0.1:3100"
    assert s.tags()["service"] == "orchestrator"
