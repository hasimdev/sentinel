"""Turns alerts into incidents: open, de-duplicate, resolve, attach evidence."""

import logging
import threading
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from orchestrator.adapters.base import AdapterError, LogSource, MetricSource
from orchestrator.adapters.chat import Chat
from orchestrator.adapters.ticketing import Ticketing
from orchestrator.evidence import gather_evidence
from orchestrator.messages import incident_message
from orchestrator.models import (
    Alert,
    AlertmanagerWebhook,
    Decision,
    Incident,
    TicketRef,
    TriageResult,
)
from orchestrator.store import IncidentStore
from orchestrator.tickets import followup_ticket, needs_followup


def _outcome(action: str, recommendation: str | None, user_id: str) -> str:
    who = f"<@{user_id}>"
    if action != "approve":
        return f"{who} rejected the AI's recommendation. No action taken."
    if recommendation == "rollback":
        return (
            f"Rollback approved by {who}. Simulated: no deploy tool is connected yet, "
            "so nothing was rolled back."
        )
    if recommendation == "investigate":
        return f"{who} agreed to investigate."
    if recommendation == "no_action":
        return f"{who} agreed no action is needed."
    return f"Approved by {who}."


class WebhookResult(BaseModel):
    opened: list[int] = Field(default_factory=list)
    updated: list[int] = Field(default_factory=list)
    resolved: list[int] = Field(default_factory=list)


class IncidentService:
    def __init__(
        self,
        store: IncidentStore,
        metrics: MetricSource,
        logs: LogSource,
        log: logging.Logger,
        triage_agent=None,
        triage_skip_reason: str = "AI triage is turned off",
        chat: Chat | None = None,
        dashboard_url: str = "http://127.0.0.1:3000/d/shoplite",
        ticketing: Ticketing | None = None,
    ) -> None:
        self.ticketing = ticketing
        self.store = store
        self.metrics = metrics
        self.logs = logs
        self.log = log
        self.triage_agent = triage_agent
        self.triage_skip_reason = triage_skip_reason
        self.chat = chat
        self.dashboard_url = dashboard_url
        # Two people clicking at the same moment must not both get recorded.
        self._decision_lock = threading.Lock()

    def handle(self, webhook: AlertmanagerWebhook) -> WebhookResult:
        result = WebhookResult()
        for alert in webhook.alerts:
            existing = self.store.find_open(alert.fingerprint)
            if alert.status == "firing" and existing is None:
                incident = self.store.create(self._new_incident(alert))
                result.opened.append(incident.id)
                self._log("incident opened", incident)
            elif alert.status == "firing":
                existing.last_seen_at = datetime.now(UTC)
                existing.summary = alert.annotations.get("summary", existing.summary)
                self.store.save(existing)
                result.updated.append(existing.id)
            elif existing is not None:  # resolved
                existing.status = "resolved"
                existing.resolved_at = alert.endsAt or datetime.now(UTC)
                self.store.save(existing)
                result.resolved.append(existing.id)
                self._log("incident resolved", existing)
            # A "resolved" for an alert we never saw open needs no action.
        return result

    def attach_evidence(self, incident_id: int) -> None:
        incident = self.store.get(incident_id)
        if incident is None:
            return
        evidence = gather_evidence(incident, self.metrics, self.logs)
        # Re-read before saving: the incident may have been updated or resolved meanwhile.
        incident = self.store.get(incident_id)
        incident.evidence = evidence
        self.store.save(incident)
        self._log(
            "evidence attached",
            incident,
            error_logs=len(evidence.error_logs),
            gaps=len(evidence.gaps),
        )

    def run_triage(self, incident_id: int) -> None:
        """Ask the AI for a diagnosis. Never raises; the outcome is stored on the incident."""
        incident = self.store.get(incident_id)
        if incident is None:
            return
        if self.triage_agent is None:
            result = TriageResult(status="skipped", error=self.triage_skip_reason)
        else:
            try:
                result = self.triage_agent.triage(incident)
            except Exception as exc:  # a bug in triage must never break incident handling
                result = TriageResult(status="failed", error=f"Unexpected error: {exc}")
        incident = self.store.get(incident_id)  # re-read: may have changed meanwhile
        incident.triage = result
        self.store.save(incident)
        diagnosis = result.diagnosis
        self._log(
            "triage finished",
            incident,
            triage_status=result.status,
            recommendation=diagnosis.recommendation if diagnosis else None,
            confidence=diagnosis.confidence if diagnosis else None,
            tool_calls=len(result.tool_calls),
            triage_error=result.error,
        )

    def investigate(self, incident_id: int) -> None:
        """Background job for a new incident: gather evidence, AI triage, tell people."""
        self.attach_evidence(incident_id)
        self.run_triage(incident_id)
        self.notify(incident_id)

    # --- chat: show the incident to people and record what they decide ---

    def notify(self, incident_id: int) -> None:
        """Post the incident to chat, or refresh the message if it's already there.

        Never raises: chat being down must not break incident handling.
        """
        if self.chat is None:
            return
        incident = self.store.get(incident_id)
        if incident is None:
            return
        text, blocks = incident_message(incident, self.dashboard_url)
        try:
            if incident.chat is None:
                ref = self.chat.post(text, blocks)
                incident = self.store.get(incident_id)
                incident.chat = ref
                self.store.save(incident)
                self._log("posted to chat", incident)
            else:
                self.chat.update(incident.chat, text, blocks)
        except AdapterError as exc:
            self._log("chat failed", incident, chat_error=str(exc))

    def on_resolved(self, incident_id: int) -> None:
        """Background job when an incident clears: refresh the message, note it in the
        thread, and file a follow-up ticket to fix the cause properly."""
        self.notify(incident_id)
        ticket = self.file_followup(incident_id)
        incident = self.store.get(incident_id)
        if self.chat is None or incident is None or incident.chat is None:
            return
        text = (
            f":large_green_circle: Resolved: the alert cleared at {incident.resolved_at:%H:%M} UTC."
        )
        if ticket:
            text += f"\n:ticket: Follow-up ticket to fix the cause: <{ticket.url}|{ticket.key}>"
        try:
            self.chat.reply(incident.chat, text)
        except AdapterError as exc:
            self._log("chat failed", incident, chat_error=str(exc))

    def file_followup(self, incident_id: int) -> TicketRef | None:
        """Create the follow-up ticket once. Never raises; returns the ticket or None."""
        if self.ticketing is None:
            return None
        with self._decision_lock:  # one ticket per incident, even if called twice
            incident = self.store.get(incident_id)
            if incident is None or incident.followup is not None or not needs_followup(incident):
                return incident.followup if incident else None
            try:
                ticket = self.ticketing.create(followup_ticket(incident, self.dashboard_url))
            except AdapterError as exc:
                self._log("ticket failed", incident, ticket_error=str(exc))
                return None
            incident.followup = ticket
            self.store.save(incident)
        self._log("follow-up ticket filed", incident, ticket=ticket.key)
        return ticket

    def decide(
        self, incident_id: int, action: str, user_id: str, user_name: str
    ) -> tuple[Decision | None, bool]:
        """Record a person's decision. Returns (decision, was_already_decided).

        The first decision wins; later clicks change nothing. Approving a rollback is
        recorded as SIMULATED: nothing is deployed until a deploy tool is connected.
        """
        with self._decision_lock:
            incident = self.store.get(incident_id)
            if incident is None:
                return None, False
            if incident.decision is not None:
                return incident.decision, True
            recommendation = (
                incident.triage.diagnosis.recommendation
                if incident.triage and incident.triage.diagnosis
                else None
            )
            incident.decision = Decision(
                action="approved" if action == "approve" else "rejected",
                recommendation=recommendation,
                by_user_id=user_id,
                by_name=user_name,
                at=datetime.now(UTC),
                outcome=_outcome(action, recommendation, user_id),
            )
            self.store.save(incident)
        self._log(
            "decision recorded",
            incident,
            decision=incident.decision.action,
            recommendation=recommendation,
            decided_by=user_name,
        )
        self.notify(incident_id)
        return incident.decision, False

    @staticmethod
    def _new_incident(alert: Alert) -> Incident:
        labels = alert.labels
        return Incident(
            fingerprint=alert.fingerprint,
            alertname=labels.get("alertname", "unknown"),
            severity=labels.get("severity"),
            service=labels.get("service"),
            env=labels.get("env"),
            version=labels.get("version"),
            commit_sha=labels.get("commit_sha"),
            summary=alert.annotations.get("summary"),
            started_at=alert.startsAt,
            last_seen_at=datetime.now(UTC),
        )

    def _log(self, msg: str, incident: Incident, **extra) -> None:
        # The incident's own service/version are prefixed so they don't clash with
        # the orchestrator's tags on the same log line.
        fields = {
            "incident_id": incident.id,
            "alertname": incident.alertname,
            "incident_service": incident.service,
            "incident_version": incident.version,
            "incident_commit_sha": incident.commit_sha,
            **extra,
        }
        self.log.info(msg, extra={"fields": fields})
