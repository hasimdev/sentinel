"""ServiceNow incidents, emergency changes and CAB approval from Slack.

ServiceNow is faked: nothing is created anywhere.
"""

import json
from datetime import UTC, datetime

import httpx2
import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.base import AdapterError
from orchestrator.adapters.change_mgmt import (
    Approver,
    NewChange,
    NewIncident,
    ServiceNowChangeMgmt,
)
from orchestrator.changes import diagnosis_note, resolution_notes, rollback_change
from orchestrator.main import create_app
from orchestrator.messages import APPROVE_ACTION, REJECT_ACTION, incident_message
from orchestrator.models import (
    ChatRef,
    Decision,
    Diagnosis,
    Incident,
    LogLine,
    RecordRef,
    TriageResult,
)
from orchestrator.settings import Settings, load_settings
from orchestrator.slack_listener import clicker_email, handle_click

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
APPROVER_EMAIL = "cab.member@example.com"


def diagnosis(recommendation="rollback"):
    return Diagnosis(
        likely_cause="Release 0.2.0 broke checkout. KeyError in promo_price for 'cap'.",
        recommendation=recommendation,
        reasoning="Errors began with 0.2.0; 0.1.0 was clean.",
        confidence="high",
        evidence=["51% of checkouts fail on 0.2.0", "0.1.0 had no errors"],
    )


def ref(table, number):
    return RecordRef(
        table=table, number=number, sys_id=f"id-{number}", url=f"https://sn.example/{number}"
    )


class FakeServiceNow:
    """Records every call. `down` makes every call fail; `fail` fails just the named calls."""

    def __init__(self, down=False, fail=()):
        self.down, self.fail = down, set(fail)
        self.calls: list[tuple] = []
        self.approvers = {APPROVER_EMAIL: Approver(approval_id="appr-1", name="Abu Ansari")}

    def _record(self, name, *args):
        if self.down or name in self.fail:
            raise AdapterError("ServiceNow unreachable: connection refused")
        self.calls.append((name, *args))

    def names(self):
        return [c[0] for c in self.calls]

    def open_incident(self, new):
        self._record("open_incident", new)
        return ref("incident", "INC0010002")

    def add_note(self, record, text):
        self._record("add_note", record.number, text)

    def resolve_incident(self, record, notes):
        self._record("resolve_incident", record.number, notes)

    def create_emergency_change(self, new):
        self._record("create_emergency_change", new)
        return ref("change_request", "CHG0030005")

    def link_change(self, incident, change):
        self._record("link_change", incident.number, change.number)

    def request_approval(self, change):
        self._record("request_approval", change.number)

    def pending_approver(self, change, email):
        self._record("pending_approver", change.number, email)
        return self.approvers.get(email)

    def record_approval(self, approver, approved, comment):
        self._record("record_approval", approver.name, approved, comment)

    def cancel_change(self, change, notes):
        self._record("cancel_change", change.number, notes)

    def start_implementation(self, change, note):
        self._record("start_implementation", change.number, note)

    def close_change(self, change, successful, notes):
        self._record("close_change", change.number, successful, notes)


class FakeChat:
    def __init__(self):
        self.posts, self.updates, self.replies = [], [], []

    def post(self, text, blocks):
        self.posts.append(blocks)
        return ChatRef(channel="C1", ts="1.1")

    def update(self, ref, text, blocks):
        self.updates.append(blocks)

    def reply(self, ref, text):
        self.replies.append(text)


class Agent:
    def __init__(self, recommendation="rollback"):
        self.recommendation = recommendation

    def triage(self, incident, now=None):
        return TriageResult(
            status="completed", diagnosis=diagnosis(self.recommendation), model="claude-test"
        )


class NoMetrics:
    def query(self, q):
        return []

    def instant(self, q):
        return None


class NoLogs:
    def search(self, q, since, limit):
        return [LogLine(ts=NOW, line="checkout failed")]


def alert(status):
    resolved = status == "resolved"
    return {
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "ShopLiteHighErrorRate",
                    "severity": "critical",
                    "service": "shoplite",
                    "env": "local",
                    "version": "0.2.0",
                    "commit_sha": "e8f12b0",
                },
                "annotations": {"summary": "ShopLite error rate is 35% on version 0.2.0"},
                "startsAt": "2026-10-16T10:00:00Z",
                "endsAt": "2026-10-16T10:09:00Z" if resolved else "0001-01-01T00:00:00Z",
                "fingerprint": "fp1",
            }
        ]
    }


def orchestrator(tmp_path, snow, recommendation="rollback", chat=None):
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    app = create_app(
        settings,
        NoMetrics(),
        NoLogs(),
        triage_agent=Agent(recommendation),
        chat=chat,
        change_mgmt=snow,
    )
    return TestClient(app)


def click(client, action_id=APPROVE_ACTION, email=APPROVER_EMAIL):
    body = {
        "actions": [{"action_id": action_id, "value": "1"}],
        "user": {"id": "U42", "name": "abu"},
    }
    return handle_click(client.app.state.service, body, email)


def incident(client):
    return client.get("/incidents/1").json()


# --- opening: the incident record and the emergency change ---


def test_rollback_opens_incident_raises_change_and_asks_the_cab(tmp_path):
    snow, chat = FakeServiceNow(), FakeChat()
    client = orchestrator(tmp_path, snow, chat=chat)
    client.post("/alerts", json=alert("firing"))
    assert snow.names() == [
        "open_incident",
        "add_note",  # the AI diagnosis
        "create_emergency_change",
        "link_change",
        "request_approval",
    ]
    inc = incident(client)
    assert inc["itsm_incident"]["number"] == "INC0010002"
    assert inc["change"]["number"] == "CHG0030005"
    assert snow.calls[0][1].urgent  # critical alert -> urgent incident
    shown = json.dumps(chat.posts[0])
    assert "INC0010002" in shown and "CHG0030005" in shown and "CAB approval" in shown


@pytest.mark.parametrize("recommendation", ["investigate", "no_action"])
def test_no_change_unless_the_ai_recommends_rollback(tmp_path, recommendation):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow, recommendation)
    client.post("/alerts", json=alert("firing"))
    assert snow.names() == ["open_incident", "add_note"]
    assert incident(client)["change"] is None


def test_servicenow_down_never_breaks_the_incident(tmp_path):
    chat = FakeChat()
    client = orchestrator(tmp_path, FakeServiceNow(down=True), chat=chat)
    client.post("/alerts", json=alert("firing"))
    inc = incident(client)
    assert inc["triage"]["status"] == "completed" and inc["itsm_incident"] is None
    assert len(chat.posts) == 1  # people still hear about it
    client.post("/alerts", json=alert("resolved"))
    assert incident(client)["status"] == "resolved"


def test_no_servicenow_configured_means_no_records(tmp_path):
    client = orchestrator(tmp_path, None)
    client.post("/alerts", json=alert("firing"))
    assert incident(client)["itsm_incident"] is None and incident(client)["change"] is None


# --- deciding: only a CAB approver ServiceNow is waiting on may decide ---


def test_cab_approver_click_is_recorded_in_servicenow_under_their_name(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    assert click(client) is None
    assert ("pending_approver", "CHG0030005", APPROVER_EMAIL) in snow.calls
    assert ("record_approval", "Abu Ansari", True, "Approved in Slack by abu, via Sentinel.") in (
        snow.calls
    )
    decision = incident(client)["decision"]
    assert decision["action"] == "approved"
    assert "CAB approver Abu Ansari" in decision["outcome"] and "CHG0030005" in decision["outcome"]
    assert "Simulated" in decision["outcome"]  # no deploy tool yet
    assert "cancel_change" not in snow.names()
    assert snow.calls[-1][0] == "add_note" and "Abu Ansari" in snow.calls[-1][2]


def test_someone_servicenow_did_not_ask_cannot_decide(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    note = click(client, email="someone.else@example.com")
    assert "not an approver for CHG0030005" in note
    assert incident(client)["decision"] is None
    assert "record_approval" not in snow.names()
    assert click(client) is None  # a real approver can still decide afterwards


def test_reject_by_cab_approver_cancels_the_change(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    assert click(client, REJECT_ACTION) is None
    assert ("record_approval", "Abu Ansari", False, "Rejected in Slack by abu, via Sentinel.") in (
        snow.calls
    )
    assert "cancel_change" in snow.names()
    assert "CHG0030005 was cancelled" in incident(client)["decision"]["outcome"]


def test_click_without_an_email_is_refused(tmp_path):
    client = orchestrator(tmp_path, FakeServiceNow())
    client.post("/alerts", json=alert("firing"))
    assert "couldn't read your email" in click(client, email=None)
    assert incident(client)["decision"] is None


@pytest.mark.parametrize("failing", ["pending_approver", "record_approval"])
def test_servicenow_trouble_while_deciding_records_nothing(tmp_path, failing):
    snow = FakeServiceNow(fail=[failing])
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    note = click(client)
    assert "Nothing was recorded" in note and "try again" in note
    assert incident(client)["decision"] is None


def test_second_click_does_not_touch_servicenow_again(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    click(client)
    before = len(snow.calls)
    assert "already decided" in click(client, REJECT_ACTION)
    assert len(snow.calls) == before


def test_without_a_change_anyone_can_decide_as_before(tmp_path):
    snow = FakeServiceNow(fail=["create_emergency_change"])
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    assert click(client, email=None) is None
    assert incident(client)["decision"]["action"] == "approved"
    assert "pending_approver" not in snow.names()


# --- resolving ---


def test_resolve_closes_incident_and_cancels_an_undecided_change(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    client.post("/alerts", json=alert("resolved"))
    cancel = next(c for c in snow.calls if c[0] == "cancel_change")
    assert "before anyone approved" in cancel[2]
    resolve = next(c for c in snow.calls if c[0] == "resolve_incident")
    assert resolve[1] == "INC0010002" and "Alert cleared at 2026-10-16 10:09 UTC" in resolve[2]


def test_resolve_after_approval_keeps_the_change(tmp_path):
    snow = FakeServiceNow()
    client = orchestrator(tmp_path, snow)
    client.post("/alerts", json=alert("firing"))
    click(client)
    client.post("/alerts", json=alert("resolved"))
    assert "cancel_change" not in snow.names()
    resolve = next(c for c in snow.calls if c[0] == "resolve_incident")
    assert "Decision in Slack: approved by abu" in resolve[2]


# --- what gets written ---


def sample_incident(**overrides):
    base = dict(
        id=7,
        fingerprint="f",
        alertname="ShopLiteHighErrorRate",
        service="shoplite",
        env="local",
        version="0.2.0",
        commit_sha="e8f12b0",
        started_at=NOW,
        last_seen_at=NOW,
        triage=TriageResult(status="completed", diagnosis=diagnosis(), model="claude-test"),
    )
    return Incident(**{**base, **overrides})


def test_change_names_version_commit_and_ai_reasoning():
    change = rollback_change(sample_incident())
    assert change.short_description == (
        "Emergency rollback of shoplite 0.2.0 (Sentinel incident #7)"
    )
    assert "commit e8f12b0" in change.description and "CAB approval" in change.description
    assert "KeyError in promo_price" in change.justification
    assert "0.2.0" in change.backout_plan


def test_diagnosis_note_lists_evidence_and_says_when_missing():
    note = diagnosis_note(sample_incident())
    assert "high confidence, model claude-test" in note and "- 0.1.0 had no errors" in note
    failed = sample_incident(triage=TriageResult(status="failed", error="timeout"))
    assert "no AI diagnosis (timeout)" in diagnosis_note(failed)


def test_resolution_notes_use_names_not_slack_ids():
    decided = sample_incident(
        resolved_at=NOW,
        decision=Decision(
            action="approved", by_user_id="U42", by_name="abu", at=NOW, outcome="<@U42> ..."
        ),
    )
    notes = resolution_notes(decided)
    assert "approved by abu" in notes and "<@U42>" not in notes


def test_slack_message_drops_the_cab_hint_once_decided():
    inc = sample_incident(
        itsm_incident=ref("incident", "INC1"),
        change=ref("change_request", "CHG1"),
    )
    _, blocks = incident_message(inc, "http://grafana")
    assert "needs CAB approval" in json.dumps(blocks)
    inc.decision = Decision(action="approved", by_user_id="U", by_name="a", at=NOW, outcome="ok")
    _, blocks = incident_message(inc, "http://grafana")
    assert "needs CAB approval" not in json.dumps(blocks) and "CHG1" in json.dumps(blocks)


# --- Slack: who clicked ---


class UsersClient:
    def __init__(self, fail=False):
        self.fail = fail

    def users_info(self, user):
        if self.fail:
            raise RuntimeError("missing_scope")
        return {"user": {"profile": {"email": "abu@example.com"}}}


def test_clicker_email_comes_from_slack_profile():
    assert clicker_email(UsersClient(), "U42") == "abu@example.com"
    assert clicker_email(UsersClient(fail=True), "U42") is None


# --- the ServiceNow adapter ---


def servicenow(handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    adapter = ServiceNowChangeMgmt(
        "https://sn.example/", "sentinel.integration", "pw", transport=httpx2.MockTransport(respond)
    )
    return adapter, seen


def ok(result):
    return httpx2.Response(200, json={"result": result})


def body(request):
    return json.loads(request.content)


CHANGE = ref("change_request", "CHG0030005")


def test_open_incident_posts_to_the_incident_table():
    snow, seen = servicenow(lambda r: ok({"sys_id": "abc", "number": "INC0010002"}))
    record = snow.open_incident(NewIncident(short_description="s", description="d", urgent=True))
    assert record.url == "https://sn.example/incident.do?sys_id=abc"
    assert seen[0].method == "POST" and seen[0].url.path == "/api/now/table/incident"
    assert body(seen[0])["impact"] == "1" and body(seen[0])["urgency"] == "1"


def test_emergency_change_uses_the_change_api_and_reads_its_value_fields():
    def handler(request):
        return ok({"sys_id": {"value": "c1", "display_value": "c1"}, "number": {"value": "CHG9"}})

    snow, seen = servicenow(handler)
    new = NewChange(
        short_description="s",
        description="d",
        justification="j",
        implementation_plan="i",
        backout_plan="b",
        test_plan="t",
    )
    record = snow.create_emergency_change(new)
    assert record.number == "CHG9" and record.table == "change_request"
    assert seen[0].url.path == "/api/sn_chg_rest/change/emergency"


def test_request_approval_assigns_the_group_and_moves_to_authorize():
    def handler(request):
        if request.method == "GET":
            return ok([{"sys_id": "grp1"}])
        return ok({})

    snow, seen = servicenow(handler)
    snow.request_approval(CHANGE)
    snow.request_approval(CHANGE)
    assert [r.method for r in seen] == ["GET", "PATCH", "PATCH"]  # group looked up once
    assert seen[0].url.params["sysparm_query"] == "name=Change Management"
    assert seen[1].url.path == "/api/sn_chg_rest/change/id-CHG0030005"
    assert body(seen[1]) == {"assignment_group": "grp1", "state": "-3"}


def test_pending_approver_found_only_when_servicenow_asked_them():
    def handler(request):
        if "sys_user" in request.url.path:
            return ok([{"sys_id": "u1", "name": "Abu Ansari"}])
        return ok([{"sys_id": "appr-9"}])

    snow, seen = servicenow(handler)
    assert snow.pending_approver(CHANGE, "abu@example.com") == Approver(
        approval_id="appr-9", name="Abu Ansari"
    )
    query = seen[1].url.params["sysparm_query"]
    assert query == "sysapproval=id-CHG0030005^approver=u1^state=requested"


@pytest.mark.parametrize(
    "users", [[], [{"sys_id": "a", "name": "A"}, {"sys_id": "b", "name": "B"}]]
)
def test_no_approver_when_the_email_matches_nobody_or_several_people(users):
    snow, seen = servicenow(lambda r: ok(users))
    assert snow.pending_approver(CHANGE, "abu@example.com") is None
    assert len(seen) == 1


def test_no_approver_when_servicenow_is_not_waiting_on_them():
    def handler(request):
        if "sys_user" in request.url.path:
            return ok([{"sys_id": "u1", "name": "Abu Ansari"}])
        return ok([])

    snow, _ = servicenow(handler)
    assert snow.pending_approver(CHANGE, "abu@example.com") is None


def test_email_cannot_inject_into_servicenow_queries():
    snow, seen = servicenow(lambda r: ok([{"sys_id": "u1", "name": "X"}]))
    assert snow.pending_approver(CHANGE, "x@example.com^ORactive=true") is None
    assert seen == []


def test_link_puts_a_note_on_each_record_naming_the_other():
    snow, seen = servicenow(lambda r: ok({}))
    snow.link_change(ref("incident", "INC1"), CHANGE)
    assert [r.url.path for r in seen] == [
        "/api/now/table/incident/id-INC1",
        "/api/now/table/change_request/id-CHG0030005",
    ]
    assert body(seen[0]) == {"work_notes": "Emergency change CHG0030005 raised for this incident."}
    assert body(seen[1]) == {"work_notes": "Raised for incident INC1."}


def test_record_approval_and_resolve_and_cancel_send_the_right_fields():
    snow, seen = servicenow(lambda r: ok({}))
    snow.record_approval(Approver(approval_id="appr-1", name="A"), True, "Approved in Slack")
    snow.resolve_incident(ref("incident", "INC1"), "notes")
    snow.cancel_change(CHANGE, "why")
    assert seen[0].url.path == "/api/now/table/sysapproval_approver/appr-1"
    assert body(seen[0]) == {"state": "approved", "comments": "Approved in Slack"}
    assert body(seen[1]) == {
        "state": "6",
        "close_code": "Workaround provided",
        "close_notes": "notes",
    }
    assert seen[2].url.path == "/api/sn_chg_rest/change/id-CHG0030005"
    assert body(seen[2])["state"] == "4"


def test_servicenow_errors_explain_what_went_wrong():
    error = {"error": {"message": "Operation Failed", "detail": "Data Policy Exception"}}
    snow, _ = servicenow(lambda r: httpx2.Response(403, json=error))
    with pytest.raises(AdapterError, match="403.*Operation Failed Data Policy Exception"):
        snow.add_note(CHANGE, "x")


def test_servicenow_unreachable_becomes_adapter_error():
    def refuse(request):
        raise httpx2.ConnectError("connection refused")

    snow, _ = servicenow(refuse)
    with pytest.raises(AdapterError, match="unreachable"):
        snow.open_incident(NewIncident(short_description="s", description="d"))


# --- settings ---


def test_servicenow_settings_from_environment_and_password_hidden(monkeypatch):
    monkeypatch.setenv("SERVICENOW_INSTANCE_URL", "https://sn.example")
    monkeypatch.setenv("SERVICENOW_USERNAME", "sentinel.integration")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "s3cret-pw")
    settings = load_settings()
    assert settings.servicenow_ready()
    assert settings.servicenow_change_group == "Change Management"
    assert "s3cret-pw" not in repr(settings) and "s3cret-pw" not in settings.model_dump_json()
    monkeypatch.delenv("SERVICENOW_PASSWORD")
    assert not load_settings().servicenow_ready()
