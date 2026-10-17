"""Sentinel's own numbers (time to decide, agree rate, ...), checked on known incident times."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from orchestrator.insights import IncidentMetrics
from orchestrator.main import create_app
from orchestrator.models import Decision, Diagnosis, Incident, RollbackRun, TriageResult
from orchestrator.settings import Settings
from orchestrator.store import IncidentStore

T0 = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
TAGS = {"service": "orchestrator", "env": "local", "version": "0.1.0", "commit_sha": "abc1234"}


def minutes(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def incident(rec="rollback", diagnosed=1.0, decided=None, action="approved", resolved=None,
             rollback=None) -> Incident:  # fmt: skip
    if rec == "none":
        triage = TriageResult(status="failed", error="x")
    else:
        triage = TriageResult(
            status="completed",
            finished_at=minutes(diagnosed),
            diagnosis=Diagnosis(
                likely_cause="c", recommendation=rec, reasoning="r", confidence="high", evidence=[]
            ),
        )
    return Incident(
        fingerprint="f",
        alertname="a",
        started_at=T0,
        last_seen_at=T0,
        resolved_at=minutes(resolved) if resolved is not None else None,
        status="resolved" if resolved is not None else "open",
        triage=triage,
        decision=(
            Decision(action=action, by_user_id="U", by_name="abu", at=minutes(decided), outcome="o")
            if decided is not None
            else None
        ),
        rollback=rollback,
    )


def scrape(tmp_path, *incidents) -> dict:
    """Store the incidents, scrape the collector, return {sample name + labels: value}."""
    store = IncidentStore(str(tmp_path / "db.sqlite"))
    for i in incidents:
        store.create(i)
    registry = CollectorRegistry()
    registry.register(IncidentMetrics(store, TAGS))
    samples = {}
    for family in text_string_to_metric_families(generate_latest(registry).decode()):
        for s in family.samples:
            extra = {k: v for k, v in s.labels.items() if k not in TAGS}
            assert {k: s.labels[k] for k in TAGS} == TAGS  # every metric carries the 4 tags
            key = s.name + (json.dumps(extra, sort_keys=True) if extra else "")
            samples[key] = s.value
    return samples


def test_time_to_decide_is_recommendation_ready_to_click(tmp_path):
    m = scrape(
        tmp_path,
        incident(diagnosed=1, decided=4),  # 3 min
        incident(diagnosed=1, decided=2),  # 1 min
        incident(diagnosed=1, decided=11),  # 10 min
    )
    assert m["sentinel_time_to_decide_median_seconds"] == 180
    assert m["sentinel_time_to_decide_seconds_count"] == 3
    assert m["sentinel_time_to_decide_seconds_sum"] == 840
    assert m['sentinel_time_to_decide_seconds_bucket{"le": "120.0"}'] == 1
    assert m['sentinel_time_to_decide_seconds_bucket{"le": "300.0"}'] == 2
    assert m['sentinel_time_to_decide_seconds_bucket{"le": "+Inf"}'] == 3


def test_agree_and_override_counts_per_recommendation(tmp_path):
    m = scrape(
        tmp_path,
        incident("rollback", decided=3, action="approved"),
        incident("rollback", decided=3, action="rejected"),
        incident("investigate", decided=3, action="approved"),
        incident("no_action"),  # nobody decided yet
    )
    assert m['sentinel_decisions_total{"decision": "approved", "recommendation": "rollback"}'] == 1
    assert m['sentinel_decisions_total{"decision": "rejected", "recommendation": "rollback"}'] == 1
    assert (
        m['sentinel_decisions_total{"decision": "approved", "recommendation": "investigate"}'] == 1
    )
    assert m['sentinel_incidents_total{"recommendation": "rollback"}'] == 2
    assert m['sentinel_incidents_total{"recommendation": "no_action"}'] == 1


def test_diagnose_recover_and_rollback_times(tmp_path):
    run = RollbackRun(
        target_version="0.1.0",
        run_id="r",
        url="u",
        status="succeeded",
        started_at=minutes(4),
        finished_at=minutes(4.25),
    )
    m = scrape(tmp_path, incident(diagnosed=0.5, decided=4, resolved=5, rollback=run))
    assert m["sentinel_time_to_diagnose_median_seconds"] == 30
    assert m["sentinel_time_to_recover_median_seconds"] == 300
    assert m["sentinel_rollback_duration_median_seconds"] == 15
    assert m['sentinel_rollbacks_total{"status": "succeeded"}'] == 1


def test_missing_data_gives_no_median_rather_than_a_fake_zero(tmp_path):
    m = scrape(tmp_path, incident("none"))  # no diagnosis, no decision, still open
    assert "sentinel_time_to_decide_median_seconds" not in m
    assert m["sentinel_time_to_decide_seconds_count"] == 0
    assert m['sentinel_incidents_total{"recommendation": "none"}'] == 1


def test_a_decision_stamped_before_the_diagnosis_is_ignored(tmp_path):
    m = scrape(tmp_path, incident(diagnosed=5, decided=2), incident(diagnosed=1, decided=3))
    assert m["sentinel_time_to_decide_seconds_count"] == 1  # clocks disagreeing isn't a measure
    assert m["sentinel_time_to_decide_median_seconds"] == 120


def test_metrics_endpoint_serves_the_numbers(tmp_path):
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    client = TestClient(create_app(settings))
    client.app.state.service.store.create(incident(decided=4))
    body = client.get("/metrics").text
    assert "sentinel_time_to_decide_median_seconds" in body
    assert 'service="orchestrator"' in body


# --- the dashboard and the scrape ---

INFRA = Path(__file__).resolve().parent.parent / "infra"


def test_prometheus_scrapes_the_orchestrator():
    import yaml

    config = yaml.safe_load((INFRA / "prometheus" / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["sentinel"]["static_configs"][0]["targets"] == ["host.docker.internal:8001"]


def test_dashboard_only_uses_metrics_the_orchestrator_exports(tmp_path):
    dashboard = json.loads((INFRA / "grafana" / "dashboards" / "sentinel.json").read_text("utf-8"))
    assert dashboard["uid"] == "sentinel"
    run = RollbackRun(
        target_version="0.1.0", run_id="r", url="u", started_at=T0, finished_at=minutes(1)
    )
    full = incident(decided=3, resolved=5, rollback=run)  # one of everything
    exported = {k.split("{")[0] for k in scrape(tmp_path, full)}
    exported |= {name.removesuffix("_total") for name in exported}
    for panel in dashboard["panels"]:
        for t in panel.get("targets", []):
            used = set(re.findall(r"sentinel_\w+", t["expr"]))
            assert (
                used <= exported
            ), f"panel {panel['title']!r} uses unknown metric {used - exported}"
