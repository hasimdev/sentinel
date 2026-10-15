"""Ticketing adapter: files follow-up work items (see CLAUDE.md adapters).

The AI never uses this. The orchestrator files a ticket after an incident resolves, so the
root cause gets fixed properly and not just rolled back.
"""

from typing import Protocol

import httpx2
from pydantic import BaseModel, Field

from orchestrator.adapters.base import AdapterError, explain_http_error
from orchestrator.models import TicketRef


class NewTicket(BaseModel):
    summary: str
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class Ticketing(Protocol):
    def create(self, ticket: NewTicket) -> TicketRef: ...


class JiraTicketing:
    """Ticketing backed by Jira Cloud (REST API v3, API-token auth)."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        issue_type: str = "Task",
        transport=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.issue_type = issue_type
        self._client = httpx2.Client(
            base_url=self.base_url, auth=(email, api_token), timeout=10, transport=transport
        )

    def create(self, ticket: NewTicket) -> TicketRef:
        body = {
            "fields": {
                "project": {"key": self.project_key},
                "issuetype": {"name": self.issue_type},
                "summary": ticket.summary[:250],
                "labels": ticket.labels,
                "description": _document(ticket),
            }
        }
        try:
            resp = self._client.post("/rest/api/3/issue", json=body)
        except httpx2.HTTPError as exc:
            raise AdapterError(f"Jira unreachable: {exc}") from exc
        if resp.is_error:
            raise AdapterError(_jira_error(resp))
        key = resp.json()["key"]
        return TicketRef(key=key, url=f"{self.base_url}/browse/{key}")


def _document(ticket: NewTicket) -> dict:
    """Jira's rich-text format (Atlassian Document Format): paragraphs, then a bullet list."""
    content: list[dict] = [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        for text in ticket.paragraphs
        if text
    ]
    if ticket.bullets:
        content.append(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": b}]}
                        ],
                    }
                    for b in ticket.bullets
                ],
            }
        )
    return {"type": "doc", "version": 1, "content": content}


def _jira_error(resp) -> str:
    try:
        body = resp.json()
        details = list(body.get("errorMessages", [])) + [
            f"{field}: {msg}" for field, msg in body.get("errors", {}).items()
        ]
        if details:
            return f"Jira rejected the ticket ({resp.status_code}): {'; '.join(details)[:300]}"
    except ValueError:
        pass
    return explain_http_error("Jira", resp)
