"""Follow-up tickets (Jira). Jira is faked: nothing is created anywhere."""

import json
from datetime import UTC, datetime

import httpx2
import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.base import AdapterError
from orchestrator.adapters.ticketing import JiraTicketing, NewTicket
from orchestrator.main import create_app
from orchestrator.models import ChatRef, Diagnosis, Incident, LogLine, TicketRef, TriageResult
from orchestrator.settings import Settings, load_settings
from orchestrator.tickets import followup_ticket, needs_followup

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def diagnosis(recommendation="rollback"):
    return Diagnosis(
        likely_cause="Release 0.2.0 broke checkout. KeyError in promo_price for 'cap'.",
        recommendation=recommendation,
        reasoning="Errors began with 0.2.0; 0.1.0 was clean.",
        confidence="high",
        evidence=["51% of checkouts fail on 0.2.0", "0.1.0 had no errors"],
    )


class FakeTickets:
    def __init__(self, fail=False):
        self.fail, self.created = fail, []

    def create(self, ticket):
        if self.fail:
            raise AdapterError("Jira unreachable: connection refused")
        self.created.append(ticket)
        key = f"SEN-{len(self.created)}"
        return TicketRef(key=key, url=f"https://example.atlassian.net/browse/{key}")


class FakeChat:
    def __init__(self):
        self.replies = []

    def post(self, text, blocks):
        return ChatRef(channel="C1", ts="1.1")

    def update(self, ref, text, blocks):
        pass

    def reply(self, ref, text):
        self.replies.append(text)


class Agent:
    def __init__(self, recommendation="rollback"):
        self.recommendation = recommendation

    def triage(self, incident, now=None):
        return TriageResult(status="completed", diagnosis=diagnosis(self.recommendation))


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
                    "service": "shoplite",
                    "env": "local",
                    "version": "0.2.0",
                    "commit_sha": "e8f12b0",
                },
                "annotations": {"summary": "ShopLite error rate is 35% on version 0.2.0"},
                "startsAt": "2026-10-15T10:00:00Z",
                "endsAt": "2026-10-15T10:09:00Z" if resolved else "0001-01-01T00:00:00Z",
                "fingerprint": "fp1",
            }
        ]
    }


def orchestrator(tmp_path, tickets, chat=None, recommendation="rollback"):
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    app = create_app(
        settings,
        NoMetrics(),
        NoLogs(),
        triage_agent=Agent(recommendation),
        chat=chat,
        ticketing=tickets,
    )
    return TestClient(app)


def run_incident(client):
    client.post("/alerts", json=alert("firing"))
    client.post("/alerts", json=alert("resolved"))
    return client.get("/incidents/1").json()


# --- the flow ---


def test_resolved_incident_files_one_followup_and_says_so_in_slack(tmp_path):
    tickets, chat = FakeTickets(), FakeChat()
    incident = run_incident(orchestrator(tmp_path, tickets, chat))
    assert incident["followup"] == {
        "key": "SEN-1",
        "url": "https://example.atlassian.net/browse/SEN-1",
    }
    assert len(tickets.created) == 1
    assert "<https://example.atlassian.net/browse/SEN-1|SEN-1>" in chat.replies[-1]


def test_no_duplicate_ticket_when_resolved_twice(tmp_path):
    tickets = FakeTickets()
    client = orchestrator(tmp_path, tickets)
    run_incident(client)
    client.app.state.service.file_followup(1)
    assert len(tickets.created) == 1


def test_no_ticket_when_nothing_needs_fixing(tmp_path):
    tickets = FakeTickets()
    incident = run_incident(orchestrator(tmp_path, tickets, recommendation="no_action"))
    assert incident["followup"] is None and tickets.created == []


def test_jira_down_never_breaks_the_incident(tmp_path):
    chat = FakeChat()
    incident = run_incident(orchestrator(tmp_path, FakeTickets(fail=True), chat))
    assert incident["status"] == "resolved" and incident["followup"] is None
    assert "Resolved" in chat.replies[-1] and "ticket" not in chat.replies[-1]


def test_no_jira_configured_means_no_tickets(tmp_path):
    incident = run_incident(orchestrator(tmp_path, None))
    assert incident["followup"] is None


# --- the ticket's content ---


def test_ticket_carries_cause_versions_evidence_and_timeline():
    inc = Incident(
        id=7,
        fingerprint="f",
        alertname="a",
        service="shoplite",
        env="local",
        version="0.2.0",
        commit_sha="e8f12b0",
        started_at=NOW,
        last_seen_at=NOW,
        resolved_at=NOW,
        status="resolved",
        triage=TriageResult(status="completed", diagnosis=diagnosis()),
    )
    t = followup_ticket(inc, "http://grafana/d/shoplite")
    assert t.summary == "Fix root cause of incident #7: Release 0.2.0 broke checkout"
    text = " ".join(t.paragraphs + t.bullets)
    assert "version 0.2.0 (commit e8f12b0)" in text and "high confidence" in text
    assert "51% of checkouts fail on 0.2.0" in t.bullets
    assert "Resolved: 2026-10-15 10:00 UTC" in t.bullets
    assert t.labels == ["sentinel", "shoplite", "version-0.2.0"]  # Jira labels have no spaces


def test_needs_followup_only_with_a_diagnosis():
    base = dict(fingerprint="f", alertname="a", started_at=NOW, last_seen_at=NOW)
    done = TriageResult(status="completed", diagnosis=diagnosis())
    assert not needs_followup(Incident(**base))
    assert not needs_followup(Incident(**base, triage=TriageResult(status="failed", error="x")))
    assert needs_followup(Incident(**base, triage=done))


# --- the Jira adapter ---


def jira(handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    adapter = JiraTicketing(
        "https://example.atlassian.net/",
        "me@example.com",
        "not-real",
        "SEN",
        transport=httpx2.MockTransport(respond),
    )
    return adapter, seen


def test_jira_create_sends_the_right_request():
    adapter, seen = jira(lambda r: httpx2.Response(201, json={"id": "1", "key": "SEN-3"}))
    ticket = NewTicket(summary="Fix it", paragraphs=["Why"], bullets=["a", "b"], labels=["x"])
    ref = adapter.create(ticket)
    assert ref == TicketRef(key="SEN-3", url="https://example.atlassian.net/browse/SEN-3")
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/rest/api/3/issue"
    assert req.headers["authorization"].startswith("Basic ")  # email + API token
    fields = json.loads(req.content)["fields"]
    assert fields["project"] == {"key": "SEN"} and fields["issuetype"] == {"name": "Task"}
    doc = fields["description"]
    assert doc["type"] == "doc" and doc["content"][-1]["type"] == "bulletList"


def test_jira_errors_explain_what_is_wrong():
    body = {"errorMessages": [], "errors": {"project": "valid project is required"}}
    adapter, _ = jira(lambda r: httpx2.Response(400, json=body))
    with pytest.raises(AdapterError, match="project: valid project is required"):
        adapter.create(NewTicket(summary="x"))


def test_jira_token_never_shows_in_settings(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-not-real-token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "SEN")
    s = load_settings()
    assert s.jira_ready() and "not-real" not in repr(s)
