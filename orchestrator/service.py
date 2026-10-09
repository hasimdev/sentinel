"""Turns alerts into incidents: open, de-duplicate, resolve, attach evidence."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from orchestrator.adapters.base import LogSource, MetricSource
from orchestrator.evidence import gather_evidence
from orchestrator.models import Alert, AlertmanagerWebhook, Incident, TriageResult
from orchestrator.store import IncidentStore


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
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.logs = logs
        self.log = log
        self.triage_agent = triage_agent
        self.triage_skip_reason = triage_skip_reason

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
        """Background job for a new incident: gather evidence, then AI triage."""
        self.attach_evidence(incident_id)
        self.run_triage(incident_id)

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
