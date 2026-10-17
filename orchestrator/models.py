"""Data shapes: what Alertmanager sends us, and the incidents we keep."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# --- Incoming: Alertmanager webhook (only the fields we use) ---
class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime  # Alertmanager's field names
    endsAt: datetime | None = None
    fingerprint: str


class AlertmanagerWebhook(BaseModel):
    alerts: list[Alert]


# --- Evidence gathered from read-only sources ---
class LogLine(BaseModel):
    ts: datetime
    line: str
    labels: dict[str, str] = Field(default_factory=dict)


class Evidence(BaseModel):
    gathered_at: datetime
    metrics: dict[str, float | None] = Field(default_factory=dict)
    error_logs: list[LogLine] = Field(default_factory=list)
    # Anything we tried to collect but couldn't, so a reader knows the picture is incomplete.
    gaps: list[str] = Field(default_factory=list)


# --- The AI's investigation (a recommendation only; a human decides) ---
class Diagnosis(BaseModel):
    likely_cause: str
    recommendation: Literal["rollback", "investigate", "no_action"]
    reasoning: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[str]


class TriageResult(BaseModel):
    status: Literal["completed", "failed", "skipped"]
    diagnosis: Diagnosis | None = None
    error: str | None = None
    model: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


# --- People: where the incident was posted, and what a person decided ---
class ChatRef(BaseModel):
    """The chat message that shows this incident (so it can be updated in place)."""

    channel: str
    ts: str


class TicketRef(BaseModel):
    """A ticket in the team's tracker (e.g. Jira SEN-1)."""

    key: str
    url: str


class RecordRef(BaseModel):
    """A ServiceNow record, e.g. incident INC0010002 or change_request CHG0030005."""

    table: str
    number: str
    sys_id: str
    url: str


class RollbackRun(BaseModel):
    """A real rollback, run by the deploy tool (Harness) after a CAB approval."""

    target_version: str
    run_id: str
    url: str
    status: Literal["running", "succeeded", "failed"] = "running"
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class Decision(BaseModel):
    action: Literal["approved", "rejected"]
    recommendation: str | None = None  # what the AI recommended at the time
    by_user_id: str
    by_name: str
    at: datetime
    outcome: str  # what happened as a result, in plain words


# --- Our record of an incident ---
class Incident(BaseModel):
    id: int | None = None
    fingerprint: str
    status: Literal["open", "resolved"] = "open"
    alertname: str
    severity: str | None = None
    service: str | None = None
    env: str | None = None
    version: str | None = None
    commit_sha: str | None = None
    summary: str | None = None
    started_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    evidence: Evidence | None = None
    triage: TriageResult | None = None
    chat: ChatRef | None = None
    decision: Decision | None = None
    followup: TicketRef | None = None  # the ticket to fix the root cause properly
    itsm_incident: RecordRef | None = None  # the incident record in ServiceNow
    change: RecordRef | None = None  # emergency change raised when the AI recommends rollback
    rollback: RollbackRun | None = None  # the real rollback, once a CAB approver approved
