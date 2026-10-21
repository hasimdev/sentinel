"""Real rollbacks through Harness. A rollback needs no new ticket: it backs out the change the
release was deployed under. Someone on call approves it, then Sentinel checks the error rate is
back to normal. Harness, ServiceNow and the metrics are faked."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_change_mgmt import (
    NOW,
    Agent,
    FakeChat,
    FakeServiceNow,
    NoLogs,
    NoMetrics,
    alert,
    click,
    sample_incident,
)

from orchestrator.adapters.base import AdapterError
from orchestrator.adapters.deploy_control import DeployRun, HarnessDeployControl
from orchestrator.deploy_history import release_of, rollback_target
from orchestrator.main import create_app
from orchestrator.messages import REJECT_ACTION, incident_message
from orchestrator.models import RollbackRun
from orchestrator.settings import Settings, load_settings


class FakeHarness:
    """Starts runs and reports the scripted statuses in turn (the last one repeats)."""

    def __init__(self, statuses=("running", "succeeded"), fail_start=False, fail_polls=0):
        self.statuses, self.fail_start, self.fail_polls = list(statuses), fail_start, fail_polls
        self.started: list[tuple[str, str, str | None]] = []

    def start(self, version, reason, change=None):
        if self.fail_start:
            raise AdapterError("Harness refused (401): invalid API key")
        self.started.append((version, reason, change))
        return DeployRun(run_id="exec1", url="https://harness.example/exec1")

    def status(self, run):
        if self.fail_polls:
            self.fail_polls -= 1
            raise AdapterError("Harness unreachable: timeout")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


class ErrorRates(NoMetrics):
    """Answers the validation query with the scripted error rates in turn (the last repeats)."""

    def __init__(self, *rates):
        self.rates = list(rates) or [0.0]

    def instant(self, q):
        if "1m" not in q:  # evidence queries use 5m windows
            return None
        rate = self.rates.pop(0) if len(self.rates) > 1 else self.rates[0]
        if isinstance(rate, Exception):
            raise rate
        return rate


def write_history(path, *events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def deploy_event(version, previous, change=None):
    return {
        "service": "shoplite",
        "version": version,
        "previous_version": previous,
        "change": change,
    }


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "deploys.log"
    write_history(
        path,
        deploy_event("0.1.0", None, "CHG0031000"),
        deploy_event("0.2.0", "0.1.0", "CHG0031001"),
    )
    return path


def orchestrator(tmp_path, harness, history, snow=None, chat=None, metrics=None, timeout_s=5.0):
    settings = Settings(
        db_path=str(tmp_path / "db.sqlite"), triage_enabled=False, deploy_log_path=str(history)
    )
    app = create_app(
        settings,
        metrics or ErrorRates(0.0),
        NoLogs(),
        triage_agent=Agent("rollback"),
        chat=chat or FakeChat(),
        change_mgmt=snow or FakeServiceNow(),
        deploy_control=harness,
    )
    service = app.state.service
    service._background = lambda job: job()  # run the rollback right away, in the test
    service.rollback_poll_s = 0
    service.rollback_timeout_s = timeout_s
    service.validate_poll_s = 0
    service.validate_for_s = 0  # "stays healthy" = the first healthy reading, in tests
    service.validate_timeout_s = 1
    return TestClient(app)


def approve(client, action=None, email=None):
    client.post("/alerts", json=alert("firing"))
    kwargs = {"email": email} if email is not None else {}
    note = click(client, action, **kwargs) if action else click(client, **kwargs)
    return note, client.get("/incidents/1").json()


# --- the flow ---


def test_on_call_approval_backs_out_the_release_change_with_no_new_ticket(tmp_path, history):
    harness, snow, chat = FakeHarness(), FakeServiceNow(), FakeChat()
    note, incident = approve(orchestrator(tmp_path, harness, history, snow, chat))
    assert note is None
    assert ("group_member", "cab.member@example.com", "ShopLite On-Call") in snow.calls
    assert harness.started == [
        ("0.1.0", "Backout of CHG0031001 for Sentinel incident #1", "CHG0031001")
    ]
    outcome = incident["decision"]["outcome"]
    assert "on call: Abu Ansari" in outcome and "No new change needed" in outcome
    rollback = incident["rollback"]
    assert rollback["status"] == "succeeded" and rollback["release_change"] == "CHG0031001"
    assert rollback["validation"] == "validated"
    # no change is raised, approved or closed: the release change gets a backout note
    for name in ("create_emergency_change", "request_approval", "record_approval", "close_change"):
        assert name not in snow.names()
    notes = [c for c in snow.calls if c[0] == "add_note"]
    assert any(c[1] == "CHG0031001" and "Backed out by Sentinel incident #1" in c[2] for c in notes)
    assert any(c[1] == "INC0010002" and "Rollback validated" in c[2] for c in notes)
    assert "backing out CHG0031001" in chat.replies[0]
    assert "Rollback validated" in chat.replies[-1]
    shown = json.dumps(chat.updates[-1])
    assert "backout of CHG0031001" in shown and "Rollback validated" in shown


def test_someone_not_on_call_cannot_approve_a_rollback(tmp_path, history):
    harness, snow = FakeHarness(), FakeServiceNow()
    client = orchestrator(tmp_path, harness, history, snow)
    note, incident = approve(client, email="someone.else@example.com")
    assert "not in ShopLite On-Call" in note
    assert incident["decision"] is None and harness.started == []
    assert click(client) is None  # someone on call still can


def test_rollback_click_without_an_email_is_refused(tmp_path, history):
    harness = FakeHarness()
    client = orchestrator(tmp_path, harness, history)
    client.post("/alerts", json=alert("firing"))
    assert "couldn't read your email" in click(client, email=None)
    assert harness.started == []


def test_release_deployed_without_a_change_is_still_backed_out(tmp_path):
    path = tmp_path / "d.log"
    write_history(path, deploy_event("0.1.0", None), deploy_event("0.2.0", "0.1.0"))
    harness, snow = FakeHarness(), FakeServiceNow()
    _, incident = approve(orchestrator(tmp_path, harness, path, snow))
    assert harness.started == [("0.1.0", "Backout of release 0.2.0 for Sentinel incident #1", None)]
    assert "find_change" not in snow.names() and incident["rollback"]["release_change"] is None


def test_errors_that_do_not_settle_mean_not_validated(tmp_path, history):
    chat = FakeChat()
    client = orchestrator(tmp_path, FakeHarness(), history, chat=chat, metrics=ErrorRates(0.30))
    _, incident = approve(client)
    assert incident["rollback"]["validation"] == "not_validated"
    assert (
        "Rollback not validated" in chat.replies[-1] and "A person should check" in chat.replies[-1]
    )


def test_validation_waits_for_errors_to_settle(tmp_path, history):
    metrics = ErrorRates(0.30, None, AdapterError("Prometheus down"), 0.01)
    _, incident = approve(orchestrator(tmp_path, FakeHarness(), history, metrics=metrics))
    assert incident["rollback"]["validation"] == "validated"


def test_failed_harness_run_is_reported_and_not_validated(tmp_path, history):
    harness, snow, chat = FakeHarness(statuses=("running", "failed")), FakeServiceNow(), FakeChat()
    _, incident = approve(orchestrator(tmp_path, harness, history, snow, chat))
    assert incident["rollback"]["status"] == "failed"
    assert incident["rollback"]["validation"] is None
    note = next(c for c in snow.calls if c[0] == "add_note" and c[1] == "CHG0031001")
    assert "failed in Harness" in note[2] and "A person needs to take over" in note[2]
    assert "Rollback to 0.1.0 failed" in chat.replies[-1]


def test_harness_refusing_to_start_is_reported_and_nothing_is_recorded_as_run(tmp_path, history):
    snow, chat = FakeServiceNow(), FakeChat()
    _, incident = approve(orchestrator(tmp_path, FakeHarness(fail_start=True), history, snow, chat))
    assert incident["rollback"] is None
    assert "Nothing was rolled back" in chat.replies[-1] and "invalid API key" in chat.replies[-1]
    note = next(c for c in snow.calls if c[0] == "add_note" and "Not rolled back" in c[2])
    assert note[1] == "INC0010002"


def test_unknown_previous_version_deploys_nothing(tmp_path):
    empty = tmp_path / "none.log"
    harness, chat = FakeHarness(), FakeChat()
    _, incident = approve(orchestrator(tmp_path, harness, empty, chat=chat))
    assert harness.started == [] and incident["rollback"] is None
    assert "doesn't show which version ran before 0.2.0" in chat.replies[-1]


def test_no_answer_from_harness_in_time_counts_as_failed(tmp_path, history):
    harness = FakeHarness(statuses=("running",))
    _, incident = approve(orchestrator(tmp_path, harness, history, timeout_s=0))
    assert incident["rollback"]["status"] == "failed"
    assert "no result from Harness" in incident["rollback"]["error"]


def test_a_blip_while_watching_the_run_is_retried(tmp_path, history):
    harness = FakeHarness(statuses=("succeeded",), fail_polls=2)
    _, incident = approve(orchestrator(tmp_path, harness, history))
    assert incident["rollback"]["status"] == "succeeded"


def test_rejection_never_deploys(tmp_path, history):
    harness = FakeHarness()
    _, incident = approve(orchestrator(tmp_path, harness, history), REJECT_ACTION)
    assert harness.started == [] and incident["rollback"] is None


def test_without_servicenow_rollbacks_stay_simulated(tmp_path, history):
    harness = FakeHarness()
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    app = create_app(
        settings, NoMetrics(), NoLogs(), triage_agent=Agent("rollback"), deploy_control=harness
    )
    client = TestClient(app)
    assert app.state.service.deploy_control is None  # no ServiceNow, no real rollback
    client.post("/alerts", json=alert("firing"))
    click(client, email=None)
    incident = client.get("/incidents/1").json()
    assert "Simulated" in incident["decision"]["outcome"] and harness.started == []


def test_rollback_runs_once_even_if_asked_twice(tmp_path, history):
    harness = FakeHarness()
    client = orchestrator(tmp_path, harness, history)
    approve(client)
    client.app.state.service.execute_rollback(1)
    assert len(harness.started) == 1


# --- which version to roll back to ---


def test_target_is_the_version_before_the_latest_deploy_of_the_bad_one(tmp_path):
    path = tmp_path / "d.log"
    write_history(
        path,
        deploy_event("0.1.0", None),
        deploy_event("0.2.0", "0.1.0"),
        deploy_event("0.1.0", "0.2.0"),  # an earlier rollback
        deploy_event("0.2.0", "0.1.0"),
    )
    assert rollback_target(path, "shoplite", "0.2.0") == "0.1.0"


def test_no_target_when_history_is_missing_or_unhelpful(tmp_path):
    path = tmp_path / "d.log"
    assert rollback_target(path, "shoplite", "0.2.0") is None  # no file
    path.write_text("not json\n" + json.dumps(deploy_event("0.2.0", None)) + "\n")
    assert rollback_target(path, "shoplite", "0.2.0") is None  # first-ever deploy
    assert rollback_target(path, "shoplite", None) is None
    write_history(path, {"service": "other", "version": "0.2.0", "previous_version": "0.1.0"})
    assert rollback_target(path, "shoplite", "0.2.0") is None  # another service


def test_release_of_reads_the_change_it_was_deployed_under(tmp_path):
    path = tmp_path / "d.log"
    write_history(
        path,
        deploy_event("0.1.0", None, "CHG0031000"),
        deploy_event("0.2.0", "0.1.0", "CHG0031001"),
    )
    release = release_of(path, "shoplite", "0.2.0")
    assert (release.previous_version, release.change) == ("0.1.0", "CHG0031001")
    assert release_of(path, "shoplite", "9.9.9") is None


# --- the Slack message ---


@pytest.mark.parametrize(
    ("status", "text"),
    [
        ("running", "Rolling back to 0.1.0"),
        ("succeeded", "Rolled back to 0.1.0"),
        ("failed", "Rollback to 0.1.0 failed"),
    ],
)
def test_message_shows_the_rollback_status(status, text):
    inc = sample_incident(
        rollback=RollbackRun(
            target_version="0.1.0",
            run_id="e",
            url="https://h/e",
            status=status,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    shown = json.dumps(incident_message(inc, "http://grafana")[1])
    assert text in shown and "https://h/e" in shown


# --- the Harness adapter ---


def harness(handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    adapter = HarnessDeployControl(
        "acct1", "sat.key", transport=httpx2.MockTransport(respond), api_url="https://h.example"
    )
    return adapter, seen


def test_start_runs_the_pipeline_with_the_version_and_reason():
    adapter, seen = harness(
        lambda r: httpx2.Response(200, json={"data": {"planExecution": {"uuid": "ex9"}}})
    )
    run = adapter.start("0.1.0", "CHG1 approved")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/pipeline/api/pipeline/execute/shoplite_deploy"
    assert request.url.params["projectIdentifier"] == "sentinel"
    assert request.url.params["accountIdentifier"] == "acct1"
    assert request.headers["x-api-key"] == "sat.key"
    inputs = json.loads(request.content)["pipeline"]
    assert inputs["identifier"] == "shoplite_deploy"
    assert {"name": "version", "type": "String", "value": "0.1.0"} in inputs["variables"]
    assert run.run_id == "ex9"
    assert run.url == (
        "https://app.harness.io/ng/account/acct1/all/orgs/default/projects/sentinel"
        "/pipelines/shoplite_deploy/executions/ex9/pipeline"
    )


def test_start_refuses_anything_but_a_plain_version():
    adapter, seen = harness(lambda r: httpx2.Response(200, json={}))
    with pytest.raises(AdapterError, match="Not a version"):
        adapter.start("0.1.0; rm -rf /", "x")
    assert seen == []


@pytest.mark.parametrize(
    ("harness_status", "ours"),
    [
        ("Running", "running"),
        ("AsyncWaiting", "running"),
        ("Success", "succeeded"),
        ("Failed", "failed"),
        ("Aborted", "failed"),
        ("Expired", "failed"),
    ],
)
def test_harness_statuses_map_to_ours(harness_status, ours):
    body = {"data": {"pipelineExecutionSummary": {"status": harness_status}}}
    adapter, seen = harness(lambda r: httpx2.Response(200, json=body))
    assert adapter.status(DeployRun(run_id="ex9", url="u")) == ours
    assert seen[0].url.path == "/pipeline/api/pipelines/execution/v2/ex9"


def test_harness_errors_explain_what_went_wrong():
    adapter, _ = harness(lambda r: httpx2.Response(401, json={"message": "Invalid API key"}))
    with pytest.raises(AdapterError, match="401.*Invalid API key"):
        adapter.start("0.1.0", "x")


def test_harness_unreachable_becomes_adapter_error():
    def refuse(request):
        raise httpx2.ConnectError("connection refused")

    adapter, _ = harness(refuse)
    with pytest.raises(AdapterError, match="unreachable"):
        adapter.status(DeployRun(run_id="e", url="u"))


def test_harness_settings_from_environment_and_key_hidden(monkeypatch):
    monkeypatch.setenv("HARNESS_ACCOUNT_ID", "acct1")
    monkeypatch.setenv("HARNESS_API_KEY", "sat.s3cret")
    settings = load_settings()
    assert settings.harness_ready() and settings.harness_pipeline == "shoplite_deploy"
    assert "sat.s3cret" not in repr(settings) and "sat.s3cret" not in settings.model_dump_json()
    monkeypatch.delenv("HARNESS_API_KEY")
    assert not load_settings().harness_ready()


def test_start_passes_the_change_number_and_refuses_a_malformed_one():
    adapter, seen = harness(
        lambda r: httpx2.Response(200, json={"data": {"planExecution": {"uuid": "ex9"}}})
    )
    adapter.start("0.1.0", "Backout of CHG0031001", change="CHG0031001")
    variables = json.loads(seen[0].content)["pipeline"]["variables"]
    assert {"name": "change", "type": "String", "value": "CHG0031001"} in variables
    adapter.start("0.1.0", "manual")
    variables = json.loads(seen[1].content)["pipeline"]["variables"]
    assert {"name": "change", "type": "String", "value": "none"} in variables
    with pytest.raises(AdapterError, match="Not a change number"):
        adapter.start("0.1.0", "x", change="CHG1; rm -rf /")
    assert len(seen) == 2
