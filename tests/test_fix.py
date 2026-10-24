"""Deploying a fix under the change its priority calls for. ServiceNow, Harness and the metrics
are faked; the fake ServiceNow moves changes through the same states the real instance does.

P1/P2 (critical alert) -> emergency change: deployed as soon as someone on call clicks, then the
ECAB reviews it. P3/P4 (warning alert) -> normal change: Change Management, then the CAB, approve
it before it deploys.
"""

import json

import pytest
from fastapi.testclient import TestClient
from test_change_mgmt import (
    APPROVER_EMAIL,
    Agent,
    FakeChat,
    FakeServiceNow,
    NoLogs,
    alert,
    ref,
)
from test_rollback import ErrorRates, FakeHarness

from orchestrator.main import create_app
from orchestrator.messages import (
    CHANGE_APPROVE_ACTION,
    CHANGE_REJECT_ACTION,
    FIX_DEPLOY_ACTION,
    fix_message,
)
from orchestrator.settings import Settings
from orchestrator.slack_listener import handle_fix_click

# What the real instance does after each approval (see the probe in the learning log).
NEXT_STATE = {"assess": "authorize", "authorize": "scheduled"}


class ChangeFlow(FakeServiceNow):
    """A fake ServiceNow whose changes move on when approved, like the real one."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.state = None

    def create_emergency_change(self, new):
        self._record("create_emergency_change", new)
        self.state = "new"
        return ref("change_request", "CHG0031100")

    def create_normal_change(self, new):
        self._record("create_normal_change", new)
        self.state = "new"
        return ref("change_request", "CHG0031200")

    def request_assessment(self, change):
        self._record("request_assessment", change.number)
        self.state = "assess"

    def request_approval(self, change):
        self._record("request_approval", change.number)
        self.state = "authorize"

    def pending_approver(self, change, email):
        self._record("pending_approver", change.number, email)
        if self.state not in NEXT_STATE:
            return None  # nobody is being asked right now
        return self.approvers.get(email)

    def record_approval(self, approver, approved, comment):
        self._record("record_approval", approver.name, approved, comment)
        if approved:
            self.state = NEXT_STATE[self.state]

    def change_state(self, change):
        self._record("change_state", change.number)
        return self.state

    def start_implementation(self, change, note):
        self._record("start_implementation", change.number, note)
        self.state = "implement"

    def close_change(self, change, successful, notes):
        self._record("close_change", change.number, successful, notes)
        self.state = "closed"

    def cancel_change(self, change, notes):
        self._record("cancel_change", change.number, notes)
        self.state = "canceled"


def warning_alert(status):
    body = alert(status)
    body["alerts"][0]["labels"]["severity"] = "warning"
    body["alerts"][0]["labels"]["alertname"] = "ShopLiteElevatedErrorRate"
    return body


@pytest.fixture
def world(tmp_path):
    def make(severity="critical", harness=None, metrics=None, snow=None):
        snow = snow or ChangeFlow()
        harness = harness or FakeHarness()
        chat = FakeChat()
        settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
        app = create_app(
            settings,
            metrics or ErrorRates(0.0),
            NoLogs(),
            triage_agent=Agent("investigate"),
            chat=chat,
            change_mgmt=snow,
            deploy_control=harness,
        )
        service = app.state.service
        service._background = lambda job: job()
        service.rollback_poll_s = service.validate_poll_s = service.state_poll_s = 0
        service.validate_for_s = 0
        service.validate_timeout_s = 1
        client = TestClient(app)
        client.post(
            "/alerts", json=alert("firing") if severity == "critical" else warning_alert("firing")
        )
        return client, snow, harness, chat

    return make


def request_fix(client, version="0.2.1"):
    return client.post("/incidents/1/fix", json={"version": version, "requested_by": "dev.lee"})


def press(client, action_id, email=APPROVER_EMAIL, name="abu"):
    body = {
        "actions": [{"action_id": action_id, "value": "1"}],
        "user": {"id": "U42", "name": name},
    }
    return handle_fix_click(client.app.state.service, body, email)


def fix(client):
    return client.get("/incidents/1").json()["fix"]


# --- P1/P2: emergency change, deploy now, ECAB review afterwards ---


def test_p1_fix_raises_an_emergency_change_and_waits_for_on_call(world):
    client, snow, harness, chat = world("critical")
    resp = request_fix(client)
    assert resp.status_code == 200
    f = resp.json()
    assert (f["change_type"], f["priority"], f["stage"]) == (
        "emergency",
        "P1/P2",
        "awaiting_deploy",
    )
    assert f["change"]["number"] == "CHG0031100"
    assert "create_emergency_change" in snow.names() and "link_change" in snow.names()
    assert "request_assessment" not in snow.names() and harness.started == []
    shown = json.dumps(chat.posts[-1])
    assert "Deploy fix now" in shown and "ECAB reviews it afterwards" in shown


def test_on_call_deploys_the_emergency_fix_then_the_ecab_reviews_it(world):
    client, snow, harness, chat = world("critical")
    request_fix(client)
    note = press(client, FIX_DEPLOY_ACTION)
    assert "Deploying the fix now" in note
    assert harness.started == [
        ("0.2.1", "Fix for Sentinel incident #1 under CHG0031100", "CHG0031100")
    ]
    f = fix(client)
    assert f["stage"] == "awaiting_review" and f["deployed_by"] == "Abu Ansari"
    assert f["validation"] == "validated"
    assert "request_approval" in snow.names()  # to the ECAB, after the deploy
    assert "Approve (ECAB review)" in json.dumps(chat.updates[-1])

    note = press(client, CHANGE_APPROVE_ACTION)
    assert "ECAB review recorded" in note
    f = fix(client)
    assert f["stage"] == "done" and f["approvals"] == ["Abu Ansari (ECAB review)"]
    close = next(c for c in snow.calls if c[0] == "close_change")
    assert close[2] is True and "before review" in close[3] and "ECAB reviewed" in close[3]


def test_only_on_call_can_deploy_an_emergency_fix(world):
    client, snow, harness, _ = world("critical")
    request_fix(client)
    assert "not in ShopLite On-Call" in press(client, FIX_DEPLOY_ACTION, email="dev@example.com")
    assert harness.started == [] and fix(client)["stage"] == "awaiting_deploy"


def test_ecab_rejecting_after_the_deploy_is_recorded_for_people_to_follow_up(world):
    client, snow, _, _ = world("critical")
    request_fix(client)
    press(client, FIX_DEPLOY_ACTION)
    press(client, CHANGE_REJECT_ACTION)
    f = fix(client)
    assert f["stage"] == "failed" and "ECAB rejected" in f["note"]
    assert "close_change" not in snow.names()


# --- P3/P4: normal change, Change Management then CAB, then deploy ---


def test_p3_fix_raises_a_normal_change_sent_to_change_management(world):
    client, snow, harness, chat = world("warning")
    f = request_fix(client).json()
    assert (f["change_type"], f["priority"], f["stage"]) == ("normal", "P3/P4", "awaiting_approval")
    assert "create_normal_change" in snow.names() and "request_assessment" in snow.names()
    assert harness.started == []
    assert "Approve change" in json.dumps(chat.posts[-1])


def test_normal_fix_deploys_only_after_change_management_and_the_cab(world):
    client, snow, harness, _ = world("warning")
    request_fix(client)
    assert "Next: the CAB" in press(client, CHANGE_APPROVE_ACTION)
    assert harness.started == []
    assert fix(client)["approvals"] == ["Abu Ansari (Change Management)"]

    assert "deploys now" in press(client, CHANGE_APPROVE_ACTION)
    assert harness.started == [
        ("0.2.1", "Fix for Sentinel incident #1 under CHG0031200", "CHG0031200")
    ]
    f = fix(client)
    assert f["stage"] == "done" and f["validation"] == "validated"
    assert f["approvals"] == ["Abu Ansari (Change Management)", "Abu Ansari (CAB)"]
    names = snow.names()
    assert names.index("start_implementation") < names.index("close_change")
    close = next(c for c in snow.calls if c[0] == "close_change")
    assert close[2] is True and "stayed under 5%" in close[3]


def test_rejecting_a_normal_change_cancels_it_and_deploys_nothing(world):
    client, snow, harness, _ = world("warning")
    request_fix(client)
    press(client, CHANGE_REJECT_ACTION)
    assert fix(client)["stage"] == "rejected" and harness.started == []
    assert "cancel_change" in snow.names()


def test_someone_servicenow_did_not_ask_cannot_approve(world):
    client, snow, harness, _ = world("warning")
    request_fix(client)
    note = press(client, CHANGE_APPROVE_ACTION, email="dev@example.com")
    assert "not an approver for CHG0031200" in note
    assert "record_approval" not in snow.names() and fix(client)["approvals"] == []


def test_failed_deploy_closes_a_normal_change_as_unsuccessful(world):
    client, snow, _, _ = world("warning", harness=FakeHarness(statuses=("running", "failed")))
    request_fix(client)
    press(client, CHANGE_APPROVE_ACTION)
    press(client, CHANGE_APPROVE_ACTION)
    assert fix(client)["stage"] == "failed"
    close = next(c for c in snow.calls if c[0] == "close_change")
    assert close[2] is False and "A person needs to take over" in close[3]


def test_errors_that_do_not_settle_close_the_change_as_unsuccessful(world):
    client, snow, _, _ = world("warning", metrics=ErrorRates(0.30))
    request_fix(client)
    press(client, CHANGE_APPROVE_ACTION)
    press(client, CHANGE_APPROVE_ACTION)
    f = fix(client)
    assert f["stage"] == "failed" and f["validation"] == "not_validated"
    assert next(c for c in snow.calls if c[0] == "close_change")[2] is False


# --- asking for a fix ---


@pytest.mark.parametrize("version", ["latest", "0.2", "0.2.1; rm -rf /"])
def test_only_plain_versions_can_be_requested(world, version):
    client, snow, _, _ = world()
    resp = request_fix(client, version)
    assert resp.status_code == 409 and "Not a version" in resp.json()["detail"]
    assert "create_emergency_change" not in snow.names()


def test_one_fix_at_a_time_per_incident(world):
    client, _, _, _ = world()
    request_fix(client)
    resp = request_fix(client, "0.2.2")
    assert resp.status_code == 409 and "already has a fix in progress" in resp.json()["detail"]


def test_unknown_incident_or_servicenow_trouble_is_refused_clearly(world):
    client, _, _, _ = world(snow=ChangeFlow(fail=["create_emergency_change"]))
    resp = request_fix(client)
    assert resp.status_code == 409 and "didn't create the change" in resp.json()["detail"]
    resp = client.post("/incidents/99/fix", json={"version": "0.2.1"})
    assert resp.status_code == 409 and "No incident #99" in resp.json()["detail"]


def test_fix_needs_servicenow_and_harness(tmp_path):
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    client = TestClient(create_app(settings, ErrorRates(0.0), NoLogs()))
    client.post("/alerts", json=alert("firing"))
    resp = request_fix(client)
    assert resp.status_code == 409 and "needs ServiceNow and Harness" in resp.json()["detail"]


def test_clicks_on_a_finished_or_missing_fix_change_nothing(world):
    client, _, harness, _ = world("critical")
    assert "no fix waiting" in press(client, FIX_DEPLOY_ACTION)
    request_fix(client)
    assert "isn't waiting for approval" in press(client, CHANGE_APPROVE_ACTION)
    press(client, FIX_DEPLOY_ACTION)
    assert "isn't waiting to be deployed" in press(client, FIX_DEPLOY_ACTION)
    assert len(harness.started) == 1


def test_servicenow_refusing_an_approval_records_nothing(world):
    client, snow, harness, _ = world("warning", snow=ChangeFlow(fail=["record_approval"]))
    request_fix(client)
    note = press(client, CHANGE_APPROVE_ACTION)
    assert "Nothing was recorded" in note and fix(client)["approvals"] == []
    assert harness.started == []


# --- the Slack message ---


def test_fix_message_shows_the_right_buttons_at_each_stage(world):
    client, *_ = world("critical")
    request_fix(client)
    store = client.app.state.service.store
    incident = store.get(1)
    expected = {
        "awaiting_deploy": ["sentinel_fix_deploy"],
        "awaiting_approval": ["sentinel_change_approve", "sentinel_change_reject"],
        "awaiting_review": ["sentinel_change_approve", "sentinel_change_reject"],
        "deploying": [],
        "done": [],
    }
    for stage, actions in expected.items():
        incident.fix.stage = stage
        blocks = fix_message(incident)[1]
        shown = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        assert shown == actions, stage


def test_release_change_is_a_normal_change_that_the_rollback_will_back_out():
    from orchestrator.release_change import release_change

    change = release_change("0.2.0")
    assert change.short_description == "Release: deploy shoplite 0.2.0"
    assert "Change Management, then the CAB" in change.description
    assert "backs out this change" in change.backout_plan


# --- one deploy at a time per incident ---


def set_busy(client, kind):
    store = client.app.state.service.store
    incident = store.get(1)
    incident.deploy_in_progress = kind
    store.save(incident)


def test_emergency_fix_waits_while_a_rollback_is_running(world):
    client, _, harness, _ = world("critical")
    request_fix(client)
    set_busy(client, "rollback")
    note = press(client, FIX_DEPLOY_ACTION)
    assert "A rollback is already running" in note
    assert harness.started == [] and fix(client)["stage"] == "awaiting_deploy"


def test_cab_approval_waits_while_another_deploy_runs(world):
    client, snow, harness, _ = world("warning")
    request_fix(client)
    press(client, CHANGE_APPROVE_ACTION)  # Change Management: nothing deploys yet
    set_busy(client, "rollback")
    note = press(client, CHANGE_APPROVE_ACTION)  # the CAB's approval would deploy
    assert "A rollback is already running" in note
    assert [c[0] for c in snow.calls].count("record_approval") == 1 and harness.started == []


def test_the_deploy_slot_is_freed_when_a_deploy_ends(world):
    client, _, harness, _ = world("critical")
    request_fix(client)
    press(client, FIX_DEPLOY_ACTION)
    assert client.get("/incidents/1").json()["deploy_in_progress"] is None
    assert len(harness.started) == 1
