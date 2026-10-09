"""AI triage tests. Claude is simulated, so these are free, fast and repeatable."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from orchestrator.main import create_app
from orchestrator.models import Incident
from orchestrator.settings import Settings
from orchestrator.triage import MAX_TOOL_CALLS, MAX_TURNS, TOOLS, TriageAgent

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)

DIAGNOSIS = {
    "likely_cause": "Release 0.1.0 (abc1234) breaks checkout",
    "recommendation": "rollback",
    "reasoning": "Errors started with this release and only /checkout fails.",
    "confidence": "high",
    "evidence": ["Error rate 38% on 0.1.0", "Log: checkout failed: injected fault"],
}


# --- A simulated Anthropic client ---


def text_reply(payload) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return reply([SimpleNamespace(type="text", text=text)], "end_turn")


def tool_reply(*calls: tuple[str, dict]) -> SimpleNamespace:
    blocks = [
        SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return reply(blocks, "tool_use")


def reply(content, stop_reason) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-opus-5-5",
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
    )


class FakeClaude:
    """Plays back scripted replies and records every request it receives."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Snapshot the conversation as it was sent; the agent keeps appending to its list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        return item


def api_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls("boom", response=response, body=None)


def toolbox(calls: list):
    def record(name):
        def tool(**kwargs):
            calls.append((name, kwargs))
            if name == "get_incident" and kwargs.get("incident_id") == 99:
                raise ToolError("Incident 99 not found")
            return {"tool": name, "ok": True}

        return tool

    return {name: record(name) for name in TOOLS_BY_NAME}


TOOLS_BY_NAME = [t["name"] for t in TOOLS]


def incident() -> Incident:
    return Incident(
        id=7,
        fingerprint="fp",
        alertname="ShopLiteHighErrorRate",
        service="shoplite",
        env="local",
        version="0.1.0",
        commit_sha="abc1234",
        summary="ShopLite error rate is 38% on version 0.1.0 (abc1234)",
        started_at=NOW,
        last_seen_at=NOW,
    )


# --- The agent ---


def test_investigates_with_tools_then_returns_diagnosis():
    calls = []
    claude = FakeClaude(
        tool_reply(("get_incident", {"incident_id": 7})),
        tool_reply(
            ("query_metrics", {"promql": "up"}),
            ("search_logs", {"logql": "{}", "minutes": 15, "limit": 5}),
        ),
        text_reply(DIAGNOSIS),
    )
    result = TriageAgent(toolbox(calls), client=claude).triage(incident())

    assert result.status == "completed"
    assert result.diagnosis.recommendation == "rollback"
    assert result.diagnosis.confidence == "high"
    assert result.tool_calls == ["get_incident", "query_metrics", "search_logs"]
    assert calls[0] == ("get_incident", {"incident_id": 7})
    assert result.input_tokens == 3000 and result.output_tokens == 600

    # Parallel tool results go back together in one message, matched by id.
    last_messages = claude.requests[-1]["messages"]
    results = last_messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_0", "tu_1"]
    assert all(r["is_error"] is False for r in results)


def test_request_shape_is_safe_and_structured():
    claude = FakeClaude(text_reply(DIAGNOSIS))
    TriageAgent(toolbox([]), client=claude).triage(incident())
    req = claude.requests[0]

    assert req["model"] == "claude-opus-5-5"
    assert {t["name"] for t in req["tools"]} == set(TOOLS_BY_NAME)
    assert all(t["strict"] is True for t in req["tools"])
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["betas"]
    assert "tool_choice" not in req  # forced tool choice is rejected on this model
    first = req["messages"][0]["content"]
    assert "Incident #7" in first and "commit_sha=abc1234" in first
    assert "Incident started: 2026-10-08T10:00:00+00:00" in first
    assert "Current time: " in first


def test_current_time_can_be_set():
    claude = FakeClaude(text_reply(DIAGNOSIS))
    later = datetime(2026, 10, 8, 10, 30, tzinfo=UTC)
    TriageAgent(toolbox([]), client=claude).triage(incident(), now=later)
    assert "Current time: 2026-10-08T10:30:00+00:00" in claude.requests[0]["messages"][0]["content"]


def test_the_agent_only_has_the_four_read_only_tools():
    assert set(TOOLS_BY_NAME) == {"list_incidents", "get_incident", "query_metrics", "search_logs"}
    risky = ("create", "update", "delete", "restart", "rollback", "deploy", "write", "set_")
    for name in TOOLS_BY_NAME:
        assert not any(word in name for word in risky), name


def test_tool_errors_go_back_to_claude_as_errors():
    claude = FakeClaude(tool_reply(("get_incident", {"incident_id": 99})), text_reply(DIAGNOSIS))
    result = TriageAgent(toolbox([]), client=claude).triage(incident())
    tool_result = claude.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "Incident 99 not found" in tool_result["content"]
    assert result.status == "completed"


def test_unknown_tool_is_refused():
    claude = FakeClaude(tool_reply(("delete_everything", {})), text_reply(DIAGNOSIS))
    TriageAgent(toolbox([]), client=claude).triage(incident())
    tool_result = claude.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "Unknown tool" in tool_result["content"]


def test_lookup_cap_is_enforced():
    calls = []
    many = [("query_metrics", {"promql": f"q{i}"}) for i in range(MAX_TOOL_CALLS + 3)]
    claude = FakeClaude(tool_reply(*many), text_reply(DIAGNOSIS))
    result = TriageAgent(toolbox(calls), client=claude).triage(incident())
    assert len(calls) == MAX_TOOL_CALLS
    over = claude.requests[1]["messages"][-1]["content"][-1]
    assert over["is_error"] is True and "Look-up limit reached" in over["content"]
    assert result.status == "completed"


def test_endless_investigation_is_stopped():
    claude = FakeClaude(tool_reply(("query_metrics", {"promql": "up"})))  # never concludes
    result = TriageAgent(toolbox([]), client=claude).triage(incident())
    assert result.status == "failed"
    assert f"after {MAX_TURNS} rounds" in result.error
    assert len(claude.requests) == MAX_TURNS


def test_refusal_is_reported_not_crashed():
    claude = FakeClaude(reply([], "refusal"))
    result = TriageAgent(toolbox([]), client=claude).triage(incident())
    assert result.status == "failed"
    assert "declined" in result.error


def test_invalid_diagnosis_is_reported():
    claude = FakeClaude(text_reply({"likely_cause": "x"}))
    result = TriageAgent(toolbox([]), client=claude).triage(incident())
    assert result.status == "failed"
    assert "not a valid diagnosis" in result.error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (api_error(anthropic.AuthenticationError, 401), "API key missing or invalid"),
        (api_error(anthropic.RateLimitError, 429), "rate limit"),
        (api_error(anthropic.InternalServerError, 500), "Anthropic API error 500"),
    ],
)
def test_api_errors_become_readable_failures(error, message):
    result = TriageAgent(toolbox([]), client=FakeClaude(error)).triage(incident())
    assert result.status == "failed"
    assert message in result.error


# --- Inside the orchestrator ---


def alert_payload() -> dict:
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "ShopLiteHighErrorRate", "service": "shoplite"},
                "annotations": {"summary": "ShopLite error rate is 38%"},
                "startsAt": "2026-10-08T10:00:00Z",
                "fingerprint": "fp1",
            }
        ]
    }


class NoMetrics:
    def query(self, q):
        return []

    def instant(self, q):
        return None


class NoLogs:
    def search(self, q, since, limit):
        return []


def orchestrator(tmp_path, triage_agent=None, **settings):
    s = Settings(db_path=str(tmp_path / "db.sqlite"), **settings)
    return TestClient(create_app(s, NoMetrics(), NoLogs(), triage_agent=triage_agent))


def test_new_incident_gets_a_diagnosis(tmp_path):
    agent = TriageAgent(toolbox([]), client=FakeClaude(text_reply(DIAGNOSIS)))
    client = orchestrator(tmp_path, triage_agent=agent)
    client.post("/alerts", json=alert_payload())
    triage = client.get("/incidents/1").json()["triage"]
    assert triage["status"] == "completed"
    assert triage["diagnosis"]["recommendation"] == "rollback"


def test_no_api_key_means_triage_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = orchestrator(tmp_path)
    client.post("/alerts", json=alert_payload())
    incident = client.get("/incidents/1").json()
    assert incident["status"] == "open"  # the incident itself is unaffected
    assert incident["triage"]["status"] == "skipped"
    assert "ANTHROPIC_API_KEY" in incident["triage"]["error"]


def test_triage_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    client = orchestrator(tmp_path, triage_enabled=False)
    client.post("/alerts", json=alert_payload())
    assert client.get("/incidents/1").json()["triage"]["status"] == "skipped"


def test_a_crashing_agent_never_breaks_the_incident(tmp_path):
    class BrokenAgent:
        def triage(self, incident):
            raise RuntimeError("bug")

    client = orchestrator(tmp_path, triage_agent=BrokenAgent())
    client.post("/alerts", json=alert_payload())
    incident = client.get("/incidents/1").json()
    assert incident["evidence"] is not None
    assert incident["triage"]["status"] == "failed"
    assert "bug" in incident["triage"]["error"]
