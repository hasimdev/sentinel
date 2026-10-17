"""Turns alerts into incidents: open, de-duplicate, resolve, attach evidence."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from orchestrator.adapters.base import AdapterError, LogSource, MetricSource
from orchestrator.adapters.change_mgmt import Approver, ChangeMgmt
from orchestrator.adapters.chat import Chat
from orchestrator.adapters.deploy_control import DeployControl, DeployRun
from orchestrator.adapters.ticketing import Ticketing
from orchestrator.changes import diagnosis_note, incident_record, resolution_notes, rollback_change
from orchestrator.deploy_history import rollback_target
from orchestrator.evidence import gather_evidence
from orchestrator.messages import incident_message
from orchestrator.models import (
    Alert,
    AlertmanagerWebhook,
    Decision,
    Incident,
    RecordRef,
    RollbackRun,
    TicketRef,
    TriageResult,
)
from orchestrator.store import IncidentStore
from orchestrator.tickets import followup_ticket, needs_followup


class NotAllowed(Exception):
    """A click that must not be recorded; the message says why, for the person who clicked."""


SIMULATED = "Simulated: no deploy tool is connected yet, so nothing was rolled back."


def _outcome(
    action: str,
    recommendation: str | None,
    user_id: str,
    change: RecordRef | None = None,
    approver: Approver | None = None,
    deploys: bool = False,
) -> str:
    who = f"<@{user_id}>"
    if change and approver and action == "approve":
        then = "Harness is rolling back now." if deploys else SIMULATED
        return (
            f"Rollback approved by {who} as CAB approver {approver.name}; the approval is "
            f"recorded on {change.number}. {then}"
        )
    if change and approver:
        return (
            f"{who} ({approver.name}) rejected the rollback, so {change.number} was cancelled. "
            "No action taken."
        )
    if action != "approve":
        return f"{who} rejected the AI's recommendation. No action taken."
    if recommendation == "rollback":
        # Real rollbacks need an approved change; without one this stays simulated.
        return f"Rollback approved by {who}. {SIMULATED}"
    if recommendation == "investigate":
        return f"{who} agreed to investigate."
    if recommendation == "no_action":
        return f"{who} agreed no action is needed."
    return f"Approved by {who}."


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


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
        change_mgmt: ChangeMgmt | None = None,
        deploy_control: DeployControl | None = None,
        deploy_log: str = "logs/deploys.log",
        background: Callable[[Callable[[], None]], None] | None = None,
        rollback_poll_s: float = 5.0,
        rollback_timeout_s: float = 600.0,
    ) -> None:
        self.ticketing = ticketing
        self.change_mgmt = change_mgmt
        # Real rollbacks run only with both: a deploy tool AND ServiceNow to approve the change.
        self.deploy_control = deploy_control if change_mgmt is not None else None
        self.deploy_log = deploy_log
        self._background = background or _in_thread
        self.rollback_poll_s = rollback_poll_s
        self.rollback_timeout_s = rollback_timeout_s
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
        """Background job for a new incident: record it in ServiceNow, gather evidence,
        AI triage, raise a change if a rollback is recommended, then tell people."""
        self.open_itsm_incident(incident_id)
        self.attach_evidence(incident_id)
        self.run_triage(incident_id)
        self.record_diagnosis(incident_id)
        self.notify(incident_id)

    # --- ServiceNow: the incident record and the emergency change ---
    # These run in the orchestrator, never in the AI. None of them raise: ServiceNow being
    # down must not break incident handling.

    def open_itsm_incident(self, incident_id: int) -> None:
        incident = self.store.get(incident_id)
        if self.change_mgmt is None or incident is None or incident.itsm_incident is not None:
            return
        try:
            ref = self.change_mgmt.open_incident(incident_record(incident, self.dashboard_url))
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            return
        incident = self.store.get(incident_id)
        incident.itsm_incident = ref
        self.store.save(incident)
        self._log("servicenow incident opened", incident, servicenow_incident=ref.number)

    def record_diagnosis(self, incident_id: int) -> None:
        """Add the AI diagnosis to the ServiceNow incident. If it recommends a rollback,
        raise an emergency change and send it to the CAB for approval."""
        incident = self.store.get(incident_id)
        if self.change_mgmt is None or incident is None:
            return
        if incident.itsm_incident is not None:
            self._servicenow(
                incident,
                self.change_mgmt.add_note,
                incident.itsm_incident,
                diagnosis_note(incident),
            )
        diagnosis = incident.triage.diagnosis if incident.triage else None
        if diagnosis is None or diagnosis.recommendation != "rollback" or incident.change:
            return
        try:
            change = self.change_mgmt.create_emergency_change(rollback_change(incident))
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            return
        incident = self.store.get(incident_id)
        incident.change = change
        self.store.save(incident)
        self._log("emergency change raised", incident, change=change.number)
        if incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.link_change, incident.itsm_incident, change)
        self._servicenow(incident, self.change_mgmt.request_approval, change)

    def close_itsm_records(self, incident_id: int) -> None:
        """When the alert clears: resolve the ServiceNow incident, and cancel a change that
        nobody decided on (the rollback is no longer needed)."""
        incident = self.store.get(incident_id)
        if self.change_mgmt is None or incident is None:
            return
        if incident.change is not None and incident.decision is None:
            self._servicenow(
                incident,
                self.change_mgmt.cancel_change,
                incident.change,
                "Cancelled by Sentinel: the alert cleared before anyone approved the rollback.",
            )
        if incident.itsm_incident is not None and self._servicenow(
            incident,
            self.change_mgmt.resolve_incident,
            incident.itsm_incident,
            resolution_notes(incident),
        ):
            self._log("servicenow incident resolved", incident)

    def _servicenow(self, incident: Incident, call, *args) -> bool:
        """Run one ServiceNow call; log a failure instead of raising. True if it worked."""
        try:
            call(*args)
            return True
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            return False

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
        """Background job when an incident clears: refresh the message, file a follow-up
        ticket to fix the cause properly, close the ServiceNow records, note it in the thread."""
        self.notify(incident_id)
        ticket = self.file_followup(incident_id)
        self.close_itsm_records(incident_id)
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
        self,
        incident_id: int,
        action: str,
        user_id: str,
        user_name: str,
        email: str | None = None,
    ) -> tuple[Decision | None, bool]:
        """Record a person's decision. Returns (decision, was_already_decided).

        The first decision wins; later clicks change nothing. When an emergency change is
        waiting in ServiceNow, only a person ServiceNow asked to approve it may decide, and
        their approval or rejection is recorded there under their own name; anyone else gets
        NotAllowed. Approving a rollback is SIMULATED until a deploy tool is connected.
        """
        approve = action == "approve"
        approver = None
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
            if incident.change is not None and self.change_mgmt is not None:
                approver = self._verify_approver(incident, email)
                verb = "Approved" if approve else "Rejected"
                try:
                    self.change_mgmt.record_approval(
                        approver, approve, f"{verb} in Slack by {user_name}, via Sentinel."
                    )
                except AdapterError as exc:
                    self._log("servicenow failed", incident, servicenow_error=str(exc))
                    raise NotAllowed(
                        f"ServiceNow didn't accept your decision ({exc}). "
                        "Nothing was recorded; please try again."
                    ) from exc
            incident.decision = Decision(
                action="approved" if approve else "rejected",
                recommendation=recommendation,
                by_user_id=user_id,
                by_name=user_name,
                at=datetime.now(UTC),
                outcome=_outcome(
                    action,
                    recommendation,
                    user_id,
                    incident.change if approver else None,
                    approver,
                    deploys=self.deploy_control is not None,
                ),
            )
            self.store.save(incident)
        self._log(
            "decision recorded",
            incident,
            decision=incident.decision.action,
            recommendation=recommendation,
            decided_by=user_name,
            approver=approver.name if approver else None,
        )
        if approver and not approve:
            self._servicenow(
                incident,
                self.change_mgmt.cancel_change,
                incident.change,
                f"Rollback rejected in Slack by {approver.name}.",
            )
        if self.change_mgmt is not None and incident.itsm_incident is not None:
            self._servicenow(
                incident,
                self.change_mgmt.add_note,
                incident.itsm_incident,
                f"Decision in Slack by {approver.name if approver else user_name}: "
                f"{incident.decision.action} the AI recommendation ({recommendation}).",
            )
        self.notify(incident_id)
        if approver and approve and self.deploy_control is not None:
            # A rollback takes a minute or two; don't keep the Slack click waiting.
            self._background(lambda: self.execute_rollback(incident_id))
        return incident.decision, False

    # --- the real rollback (only after a CAB approver approved the change) ---

    def execute_rollback(self, incident_id: int) -> None:
        """Roll back through the deploy tool and close the change. Never raises."""
        incident = self.store.get(incident_id)
        if (
            incident is None
            or self.deploy_control is None
            or incident.rollback is not None
            or incident.change is None
            or incident.decision is None
            or incident.decision.action != "approved"
        ):
            return
        change = incident.change
        target = rollback_target(self.deploy_log, incident.service, incident.version)
        if target is None:
            self._rollback_not_started(
                incident,
                f"the deploy history doesn't show which version ran before {incident.version}",
                implementing=False,
            )
            return
        implementing = self._servicenow(
            incident,
            self.change_mgmt.start_implementation,
            change,
            f"Sentinel is rolling back {incident.service} from {incident.version} to {target} "
            "with Harness.",
        )
        reason = f"{change.number} approved for Sentinel incident #{incident.id}"
        try:
            run = self.deploy_control.start(target, reason)
        except AdapterError as exc:
            self._rollback_not_started(incident, f"Harness couldn't start it ({exc})", implementing)
            return
        incident = self.store.get(incident_id)
        incident.rollback = RollbackRun(
            target_version=target, run_id=run.run_id, url=run.url, started_at=datetime.now(UTC)
        )
        self.store.save(incident)
        self._log("rollback started", incident, target_version=target, run_id=run.run_id)
        self._reply(
            incident,
            f":arrows_counterclockwise: Harness is rolling back {incident.service} to "
            f"{target}: <{run.url}|watch the run>",
        )
        self.notify(incident_id)

        status = self._wait_for(run, incident)
        incident = self.store.get(incident_id)
        rollback = incident.rollback
        rollback.status = "succeeded" if status == "succeeded" else "failed"
        rollback.finished_at = datetime.now(UTC)
        if status == "running":
            minutes = round(self.rollback_timeout_s / 60)
            rollback.error = f"no result from Harness within {minutes} minutes"
        self.store.save(incident)
        ok = rollback.status == "succeeded"
        self._log("rollback finished", incident, rollback_status=rollback.status)

        approver = incident.decision.by_name
        notes = (
            f"Rolled back {incident.service} from {incident.version} to {target} with Harness: "
            f"{run.url}. Approved in Slack by {approver}."
            if ok
            else f"Rollback to {target} failed in Harness ({rollback.error or run.url}). "
            "A person needs to take over."
        )
        self._servicenow(incident, self.change_mgmt.close_change, change, ok, notes)
        if ok:
            text = (
                f":white_check_mark: Rollback done: {incident.service} {target} is running. "
                f"{change.number} closed as successful."
            )
        else:
            text = (
                f":x: Rollback to {target} failed (<{run.url}|see the Harness run>). "
                f"{change.number} closed as unsuccessful. A person needs to take over."
            )
        self._reply(incident, text)
        self.notify(incident_id)

    def _wait_for(self, run: DeployRun, incident: Incident) -> str:
        """Poll the deploy tool until the run finishes; 'running' if it never does in time."""
        deadline = time.monotonic() + self.rollback_timeout_s
        while time.monotonic() < deadline:
            try:
                status = self.deploy_control.status(run)
            except AdapterError as exc:  # a blip while polling: keep trying until the deadline
                self._log("rollback status failed", incident, deploy_error=str(exc))
                status = "running"
            if status != "running":
                return status
            time.sleep(self.rollback_poll_s)
        return "running"

    def _rollback_not_started(self, incident: Incident, why: str, implementing: bool) -> None:
        """Nothing was deployed. A change already in Implement is closed as unsuccessful;
        one still Scheduled can only be cancelled (ServiceNow allows no other way out)."""
        self._log("rollback not started", incident, reason=why)
        note = f"Not rolled back: {why}. A person needs to take over."
        if implementing:
            self._servicenow(incident, self.change_mgmt.close_change, incident.change, False, note)
            ended = "closed as unsuccessful"
        else:
            self._servicenow(incident, self.change_mgmt.cancel_change, incident.change, note)
            ended = "cancelled"
        self._reply(
            incident,
            f":x: Nothing was rolled back: {why}. {incident.change.number} {ended}. "
            "A person needs to take over.",
        )

    def _reply(self, incident: Incident, text: str) -> None:
        """Reply in the incident's chat thread. Never raises."""
        if self.chat is None or incident.chat is None:
            return
        try:
            self.chat.reply(incident.chat, text)
        except AdapterError as exc:
            self._log("chat failed", incident, chat_error=str(exc))

    def _verify_approver(self, incident: Incident, email: str | None) -> Approver:
        """The clicker, if ServiceNow is waiting on their approval; otherwise NotAllowed."""
        change = incident.change
        if not email:
            raise NotAllowed(
                "Sentinel couldn't read your email from Slack, so ServiceNow can't confirm "
                f"you're an approver for {change.number}. Nothing was recorded."
            )
        try:
            approver = self.change_mgmt.pending_approver(change, email)
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            raise NotAllowed(
                f"Couldn't reach ServiceNow to check you're an approver ({exc}). "
                "Nothing was recorded; please try again."
            ) from exc
        if approver is None:
            raise NotAllowed(
                f"You're not an approver for {change.number} in ServiceNow, so your click "
                "wasn't recorded. A CAB approver has to decide on this rollback."
            )
        return approver

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
