"""Slack / chat tests. Slack is faked: nothing is posted anywhere."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError

from orchestrator.adapters.base import AdapterError
from orchestrator.adapters.chat import SlackChat
from orchestrator.main import create_app
from orchestrator.messages import APPROVE_ACTION, REJECT_ACTION, incident_message
from orchestrator.models import ChatRef, Diagnosis, Incident, LogLine, TriageResult
from orchestrator.settings import Settings, load_settings
from orchestrator.slack_listener import handle_click

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)


class FakeChat:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.posts, self.updates, self.replies = [], [], []

    def post(self, text, blocks):
        if self.fail:
            raise AdapterError("Slack chat_postMessage failed: channel_not_found")
        self.posts.append((text, blocks))
        return ChatRef(channel="C123", ts=f"17000000.{len(self.posts)}")

    def update(self, ref, text, blocks):
        self.updates.append((ref, text, blocks))

    def reply(self, ref, text):
        self.replies.append((ref, text))


class FakeAgent:
    def __init__(self, recommendation="rollback"):
        self.recommendation = recommendation

    def triage(self, incident, now=None):
        return TriageResult(
            status="completed",
            model="claude-opus-5-5",
            diagnosis=Diagnosis(
                likely_cause="Release 0.2.0 broke checkout",
                recommendation=self.recommendation,
                reasoning="Errors began with 0.2.0; 0.1.0 was clean.",
                confidence="high",
                evidence=["Error rate 38% on 0.2.0", "0% on 0.1.0"],
            ),
        )


class NoMetrics:
    def query(self, q):
        return []

    def instant(self, q):
        return None


class NoLogs:
    def search(self, q, since, limit):
        return [LogLine(ts=NOW, line="checkout failed")]


def alert(status="firing"):
    return {
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "ShopLiteHighErrorRate",
                    "service": "shoplite",
                    "severity": "critical",
                    "version": "0.2.0",
                    "commit_sha": "b7e41c2",
                },
                "annotations": {"summary": "ShopLite error rate is 38% on version 0.2.0"},
                "startsAt": "2026-10-11T10:00:00Z",
                "endsAt": (
                    "2026-10-11T10:20:00Z" if status == "resolved" else "0001-01-01T00:00:00Z"
                ),
                "fingerprint": "fp1",
            }
        ]
    }


def make(tmp_path, chat=None, agent=None):
    settings = Settings(db_path=str(tmp_path / "db.sqlite"), triage_enabled=False)
    app = create_app(settings, NoMetrics(), NoLogs(), triage_agent=agent or FakeAgent(), chat=chat)
    return TestClient(app), app


def click(service, action_id, user="U42", name="abu"):
    body = {"actions": [{"action_id": action_id, "value": "1"}], "user": {"id": user, "name": name}}
    return handle_click(service, body)


@pytest.fixture
def world(tmp_path):
    chat = FakeChat()
    client, app = make(tmp_path, chat=chat)
    client.post("/alerts", json=alert())
    return client, chat, app.state.service


# --- the message ---


def incident(**overrides):
    base = dict(
        id=1,
        fingerprint="fp",
        alertname="ShopLiteHighErrorRate",
        severity="critical",
        service="shoplite",
        env="local",
        version="0.2.0",
        commit_sha="b7e41c2",
        summary="ShopLite error rate is 38%",
        started_at=NOW,
        last_seen_at=NOW,
    )
    return Incident(**{**base, **overrides})


def buttons(blocks):
    actions = next(b for b in blocks if b["type"] == "actions")
    return {e.get("action_id"): e for e in actions["elements"]}


def test_message_shows_diagnosis_and_rollback_buttons():
    inc = incident(triage=FakeAgent("rollback").triage(None))
    text, blocks = incident_message(inc, "http://grafana/d/shoplite")
    page = json.dumps(blocks)
    assert "Incident #1" in text and "Roll back" in text
    assert "Release 0.2.0 broke checkout" in page and "high confidence" in page
    assert "Error rate 38% on 0.2.0" in page
    b = buttons(blocks)
    assert b[APPROVE_ACTION]["text"]["text"] == "Approve rollback"
    assert b[REJECT_ACTION]["value"] == "1"
    assert b["sentinel_dashboard"]["url"] == "http://grafana/d/shoplite"
    assert "Nothing changes until a person approves" in page


@pytest.mark.parametrize(
    ("recommendation", "label"),
    [("investigate", "Agree: investigate"), ("no_action", "Agree: no action")],
)
def test_button_matches_the_recommendation(recommendation, label):
    inc = incident(triage=FakeAgent(recommendation).triage(None))
    assert buttons(incident_message(inc, "u")[1])[APPROVE_ACTION]["text"]["text"] == label


def test_message_without_diagnosis_says_why():
    inc = incident(triage=TriageResult(status="failed", error="Anthropic rate limit reached"))
    page = json.dumps(incident_message(inc, "u")[1])
    assert "No AI diagnosis" in page and "rate limit" in page


def test_long_text_is_cut_to_slack_limits():
    long = "x" * 5000
    inc = incident(summary=long, triage=FakeAgent().triage(None))
    for block in incident_message(inc, "u")[1]:
        if block["type"] == "header":
            assert len(block["text"]["text"]) <= 150
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000


# --- the flow ---


def test_new_incident_is_posted_after_diagnosis(world):
    client, chat, _ = world
    assert len(chat.posts) == 1
    assert "Approve rollback" in json.dumps(chat.posts[0][1])
    saved = client.get("/incidents/1").json()
    assert saved["chat"] == {"channel": "C123", "ts": "17000000.1"}


def test_approve_records_decision_and_updates_message(world):
    client, chat, service = world
    assert click(service, APPROVE_ACTION) is None
    decision = client.get("/incidents/1").json()["decision"]
    assert decision["action"] == "approved" and decision["by_user_id"] == "U42"
    assert decision["recommendation"] == "rollback"
    assert "Simulated" in decision["outcome"] and "nothing was rolled back" in decision["outcome"]
    _, _, blocks = chat.updates[-1]
    assert APPROVE_ACTION not in buttons(blocks)  # buttons gone after the decision
    assert "Rollback approved by <@U42>" in json.dumps(blocks)


def test_reject_records_decision(world):
    client, _, service = world
    click(service, REJECT_ACTION)
    decision = client.get("/incidents/1").json()["decision"]
    assert decision["action"] == "rejected"
    assert "No action taken" in decision["outcome"]


def test_second_click_changes_nothing(world):
    client, chat, service = world
    click(service, APPROVE_ACTION, user="U1")
    updates = len(chat.updates)
    note = click(service, REJECT_ACTION, user="U2")
    assert "already decided" in note
    assert client.get("/incidents/1").json()["decision"]["by_user_id"] == "U1"
    assert len(chat.updates) == updates


def test_resolved_incident_updates_message_and_thread(world):
    client, chat, _ = world
    client.post("/alerts", json=alert(status="resolved"))
    assert "Resolved at 10:20 UTC" in json.dumps(chat.updates[-1][2])
    assert "Resolved" in chat.replies[-1][1]
    assert APPROVE_ACTION not in buttons(chat.updates[-1][2])


def test_slack_down_never_breaks_the_incident(tmp_path):
    client, _ = make(tmp_path, chat=FakeChat(fail=True))
    assert client.post("/alerts", json=alert()).status_code == 200
    saved = client.get("/incidents/1").json()
    assert saved["triage"]["status"] == "completed" and saved["chat"] is None


def test_no_slack_configured_means_no_posting(tmp_path):
    client, _ = make(tmp_path, chat=None)  # Settings have no Slack tokens
    client.post("/alerts", json=alert())
    assert client.get("/incidents/1").json()["chat"] is None


# --- the Slack adapter and settings ---


class RecordingWebClient:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        if self.fail:
            raise SlackApiError("boom", {"ok": False, "error": self.fail})
        return {"channel": kw["channel"], "ts": "1.2"}

    def chat_update(self, **kw):
        self.calls.append(("update", kw))


def test_slack_adapter_posts_updates_and_replies_in_thread():
    web = RecordingWebClient()
    chat = SlackChat("unused", "C0C87AWGNEP", client=web)
    ref = chat.post("t", [])
    chat.update(ref, "t2", [])
    chat.reply(ref, "done")
    assert [c[0] for c in web.calls] == ["post", "update", "post"]
    assert web.calls[2][1]["thread_ts"] == "1.2"


def test_slack_errors_become_adapter_errors():
    chat = SlackChat("unused", "C1", client=RecordingWebClient(fail="not_in_channel"))
    with pytest.raises(AdapterError, match="not_in_channel"):
        chat.post("t", [])


def test_tokens_never_show_in_settings_output(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-not-real")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test-not-real")
    monkeypatch.setenv("SLACK_CHANNEL", "C1")
    s = load_settings()
    assert s.slack_ready()
    assert "not-real" not in repr(s) and "not-real" not in s.model_dump_json()
    assert s.slack_bot_token.get_secret_value() == "xoxb-test-not-real"


def test_timestamps_in_resolved_reply_are_utc(world):
    client, chat, _ = world
    client.post("/alerts", json=alert(status="resolved"))
    assert "10:20 UTC" in chat.replies[-1][1]
