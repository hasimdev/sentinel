"""Claude triage agent: investigates an incident with READ-ONLY tools and recommends.

It never acts. Its only tools are the four look-up tools from mcp_server, and its output is
a recommendation that a human must approve (see CLAUDE.md).
"""

import json
import logging
import os
from datetime import UTC, datetime

import anthropic
from pydantic import ValidationError

from orchestrator.models import Diagnosis, Incident, TriageResult

MODEL = os.environ.get("TRIAGE_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("TRIAGE_EFFORT") or "medium"
MAX_TOOL_CALLS = 12  # hard cap on look-ups per incident (time and cost guardrail)
MAX_TURNS = 8  # hard cap on model round-trips
REQUEST_TIMEOUT_S = 120.0


DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "likely_cause": {"type": "string"},
        "recommendation": {"type": "string", "enum": ["rollback", "investigate", "no_action"]},
        "reasoning": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["likely_cause", "recommendation", "reasoning", "confidence", "evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are Sentinel's incident triage assistant for an on-call team. An alert has opened an
incident. Investigate it with your read-only tools, then give a short diagnosis a busy
engineer or manager can act on in under a minute.

You can only look things up; you cannot change any system. A human approves every action,
so recommend, don't instruct.

How to investigate:
- Start from the incident and its evidence (get_incident). Use query_metrics and search_logs
  only to confirm or rule out a cause, for example comparing error rates across versions or
  routes, or reading the actual error messages.
- Every log line and metric carries service, env, version and commit_sha. Use them to tie
  the problem to a specific release.
- Recommend "rollback" only when the evidence points to a specific release. Recommend
  "investigate" when the cause is unclear or not release-related, and "no_action" when the
  problem has already cleared.
- Base confidence on the evidence you actually saw. Say so if evidence was missing.
- Compare timestamps with the current time given in the request to tell whether the problem
  is still happening or has already cleared.

Metric names: shoplite_http_requests_total (labels: route, status, method, plus the four
tags) and shoplite_http_request_duration_seconds_bucket. Log labels: service, env, version,
commit_sha, level.

Write evidence items as short plain-English facts with numbers, e.g.
"Error rate 37% on version 0.1.0 (commit abc1234); 0% before 10:02."
"""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool(
        "list_incidents",
        "List incidents, newest first, without evidence.",
        {
            "status": {"type": "string", "enum": ["open", "resolved", "all"]},
            "limit": {"type": "integer"},
        },
        ["status", "limit"],
    ),
    _tool(
        "get_incident",
        "One incident with its evidence: metrics, recent error logs and any gaps.",
        {"incident_id": {"type": "integer"}},
        ["incident_id"],
    ),
    _tool(
        "query_metrics",
        "Run a PromQL instant query against Prometheus; returns labelled values.",
        {"promql": {"type": "string"}},
        ["promql"],
    ),
    _tool(
        "search_logs",
        "Search log lines in Loki with LogQL, newest first. minutes: how far back (max 1440).",
        {
            "logql": {"type": "string"},
            "minutes": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        ["logql", "minutes", "limit"],
    ),
]


class TriageAgent:
    """Runs one investigation per incident.

    `toolbox` is the MCP server's tool functions (read-only); `client` is an Anthropic
    client, or a fake one in tests.
    """

    def __init__(self, toolbox, client=None, log: logging.Logger | None = None) -> None:
        self.toolbox = toolbox
        self.client = client or anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=2)
        self.log = log or logging.getLogger("orchestrator")
        # The last investigation's full conversation (for evals and debugging).
        self.last_messages: list[dict] = []

    def triage(self, incident: Incident, now: datetime | None = None) -> TriageResult:
        """`now` is the current time the agent is told (defaults to the real clock)."""
        started = datetime.now(UTC)
        result = TriageResult(status="failed", model=MODEL, started_at=started, finished_at=started)
        try:
            result.diagnosis = self._investigate(incident, result, now or started)
            result.status = "completed"
        except anthropic.AuthenticationError:
            result.error = "Anthropic API key missing or invalid (set ANTHROPIC_API_KEY in .env)"
        except anthropic.RateLimitError:
            result.error = "Anthropic rate limit reached; try again shortly"
        except anthropic.APIStatusError as exc:
            result.error = f"Anthropic API error {exc.status_code}: {exc.message}"
        except anthropic.APIConnectionError:
            result.error = "Could not reach the Anthropic API (network problem or timeout)"
        except TriageError as exc:
            result.error = str(exc)
        result.finished_at = datetime.now(UTC)
        return result

    def _investigate(self, incident: Incident, result: TriageResult, now: datetime) -> Diagnosis:
        messages = [
            {
                "role": "user",
                "content": (
                    f"Incident #{incident.id}: {incident.summary or incident.alertname}"
                    f"\nservice={incident.service} env={incident.env} "
                    f"version={incident.version} commit_sha={incident.commit_sha}"
                    f"\nIncident started: {incident.started_at.isoformat()}"
                    f"\nCurrent time: {now.isoformat()}"
                    "\nInvestigate it and give your diagnosis."
                ),
            }
        ]
        self.last_messages = messages
        for _turn in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={
                    "effort": EFFORT,
                    "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA},
                },
                # If a safety classifier wrongly declines, Anthropic retries on a suitable model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens
            result.model = response.model  # the model that actually answered

            if response.stop_reason == "refusal":
                raise TriageError("Claude declined to analyse this incident")
            if response.stop_reason == "max_tokens":
                raise TriageError("Claude's answer was cut off (max_tokens)")

            # Keep the full assistant turn (including thinking blocks) unchanged.
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return self._parse_diagnosis(response)

            tool_results = []
            for block in tool_uses:
                if len(result.tool_calls) >= MAX_TOOL_CALLS:
                    content, is_error = (
                        "Look-up limit reached. Give your diagnosis now with the evidence "
                        "you already have.",
                        True,
                    )
                else:
                    result.tool_calls.append(block.name)
                    content, is_error = self._run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            # All results for this turn go back together in one message.
            messages.append({"role": "user", "content": tool_results})

        raise TriageError(f"No diagnosis after {MAX_TURNS} rounds; stopped to limit cost")

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        func = self.toolbox.get(name)
        if func is None:
            return f"Unknown tool {name}", True
        try:
            return json.dumps(func(**args), default=str), False
        except Exception as exc:  # tool errors go back to Claude so it can adjust
            return f"Tool {name} failed: {exc}", True

    @staticmethod
    def _parse_diagnosis(response) -> Diagnosis:
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return Diagnosis.model_validate_json(text)
        except ValidationError as exc:
            raise TriageError(f"Claude's answer was not a valid diagnosis: {exc}") from exc


class TriageError(Exception):
    """The investigation could not produce a diagnosis."""


def estimated_cost_usd(result: TriageResult) -> float:
    """Rough cost at Claude Opus 5.5 list prices ($4 in / $20 out per million tokens)."""
    return result.input_tokens * 4 / 1_000_000 + result.output_tokens * 20 / 1_000_000
