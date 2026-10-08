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
