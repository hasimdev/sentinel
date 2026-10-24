"""Turns alerts into incidents: open, de-duplicate, resolve, attach evidence."""

import logging
import re
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
from orchestrator.changes import diagnosis_note, fix_change, incident_record, resolution_notes
from orchestrator.deploy_history import release_of
from orchestrator.evidence import gather_evidence, recent_error_rate_query
from orchestrator.messages import fix_message, incident_message
from orchestrator.models import (
    Alert,
    AlertmanagerWebhook,
    Decision,
    FixDeploy,
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


class FixRefused(Exception):
    """A fix that can't be requested; the message says why."""


VERSION = re.compile(r"^\d+\.\d+\.\d+$")


SIMULATED = "Simulated: no deploy tool is connected yet, so nothing was rolled back."


def _outcome(
    action: str,
    recommendation: str | None,
    user_id: str,
    on_call: str | None = None,
) -> str:
    who = f"<@{user_id}>"
    if on_call and action == "approve":
        return (
            f"Rollback approved by {who} (on call: {on_call}). No new change needed: a rollback "
            "backs out the release's own change. Harness is rolling back now."
        )
    if on_call:
        return f"{who} (on call: {on_call}) rejected the rollback. No action taken."
    if action != "approve":
        return f"{who} rejected the AI's recommendation. No action taken."
    if recommendation == "rollback":
        # Real rollbacks need ServiceNow and a deploy tool; without them this stays simulated.
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
        oncall_group: str = "ShopLite On-Call",
        validate_for_s: float = 120.0,
        validate_timeout_s: float = 600.0,
        validate_poll_s: float = 15.0,
        healthy_error_rate: float = 0.05,
        state_checks: int = 10,
        state_poll_s: float = 2.0,
    ) -> None:
        self.ticketing = ticketing
        self.change_mgmt = change_mgmt
        # Real rollbacks run only with both: a deploy tool AND ServiceNow, which checks that the
        # person approving is on call and holds the release's change record.
        self.deploy_control = deploy_control if change_mgmt is not None else None
        self.deploy_log = deploy_log
        self._background = background or _in_thread
        self.rollback_poll_s = rollback_poll_s
        self.rollback_timeout_s = rollback_timeout_s
        self.oncall_group = oncall_group
        # A rollback is validated once the error rate stays under this for validate_for_s.
        self.validate_for_s = validate_for_s
        self.validate_timeout_s = validate_timeout_s
        self.validate_poll_s = validate_poll_s
        self.healthy_error_rate = healthy_error_rate
        self.state_checks = state_checks
        self.state_poll_s = state_poll_s
        self.store = store
        self.metrics = metrics
        self.logs = logs
        self.log = log
        self.triage_agent = triage_agent
        self.triage_skip_reason = triage_skip_reason
        self.chat = chat
        self.dashboard_url = dashboard_url
        # Two people clicking at the same moment must not both get recorded.
        # Re-entrant: a job started while the lock is held may take it again when it ends.
        self._decision_lock = threading.RLock()

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
        AI triage, note the diagnosis, then tell people."""
        self.open_itsm_incident(incident_id)
        self.attach_evidence(incident_id)
        self.run_triage(incident_id)
        self.record_diagnosis(incident_id)
        self.notify(incident_id)

    # --- ServiceNow: the incident record ---
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
        """Add the AI diagnosis to the ServiceNow incident.

        A rollback raises no change: it backs out the change the release was deployed under.
        """
        incident = self.store.get(incident_id)
        if self.change_mgmt is None or incident is None or incident.itsm_incident is None:
            return
        self._servicenow(
            incident, self.change_mgmt.add_note, incident.itsm_incident, diagnosis_note(incident)
        )

    def close_itsm_records(self, incident_id: int) -> None:
        """When the alert clears: resolve the ServiceNow incident."""
        incident = self.store.get(incident_id)
        if self.change_mgmt is None or incident is None or incident.itsm_incident is None:
            return
        if self._servicenow(
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

        The first decision wins; later clicks change nothing. A rollback (real once a deploy
        tool and ServiceNow are connected) may only be decided by a member of the on-call group
        in ServiceNow; anyone else gets NotAllowed. It raises no new change: it backs out the
        release's own change. Without a deploy tool, approving a rollback is SIMULATED.
        """
        approve = action == "approve"
        on_call = None
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
            if recommendation == "rollback" and self.deploy_control is not None:
                on_call = self._verify_on_call(incident, email, "decide on this rollback")
                if approve:
                    self._claim_deploy(incident, "rollback")
            incident.decision = Decision(
                action="approved" if approve else "rejected",
                recommendation=recommendation,
                by_user_id=user_id,
                by_name=user_name,
                at=datetime.now(UTC),
                outcome=_outcome(action, recommendation, user_id, on_call=on_call),
            )
            self.store.save(incident)
        self._log(
            "decision recorded",
            incident,
            decision=incident.decision.action,
            recommendation=recommendation,
            decided_by=user_name,
            on_call=on_call,
        )
        if self.change_mgmt is not None and incident.itsm_incident is not None:
            self._servicenow(
                incident,
                self.change_mgmt.add_note,
                incident.itsm_incident,
                f"Decision in Slack by {on_call or user_name}: "
                f"{incident.decision.action} the AI recommendation ({recommendation}).",
            )
        self.notify(incident_id)
        if on_call and approve:
            # A rollback takes a minute or two; don't keep the Slack click waiting.
            self._background(lambda: self.execute_rollback(incident_id))
        return incident.decision, False

    # --- the real rollback (only after an on-call engineer approved it) ---

    def execute_rollback(self, incident_id: int) -> None:
        """Roll back through the deploy tool as the backout of the release's own change, then
        check the error rate is back to normal. Never raises."""
        try:
            self._execute_rollback(incident_id)
        finally:
            self._release_deploy(incident_id)

    def _execute_rollback(self, incident_id: int) -> None:
        incident = self.store.get(incident_id)
        if (
            incident is None
            or self.deploy_control is None
            or incident.rollback is not None
            or incident.decision is None
            or incident.decision.action != "approved"
        ):
            return
        release = release_of(self.deploy_log, incident.service, incident.version)
        if release is None or release.previous_version is None:
            self._rollback_not_started(
                incident,
                f"the deploy history doesn't show which version ran before {incident.version}",
            )
            return
        target, change_number = release.previous_version, release.change
        change = self._find_change(incident, change_number)
        reason = (
            f"Backout of {change_number} for Sentinel incident #{incident.id}"
            if change_number
            else f"Backout of release {incident.version} for Sentinel incident #{incident.id}"
        )
        try:
            run = self.deploy_control.start(target, reason, change=change_number)
        except AdapterError as exc:
            self._rollback_not_started(incident, f"Harness couldn't start it ({exc})")
            return
        incident = self.store.get(incident_id)
        incident.rollback = RollbackRun(
            target_version=target,
            run_id=run.run_id,
            url=run.url,
            started_at=datetime.now(UTC),
            release_change=change_number,
        )
        self.store.save(incident)
        self._log("rollback started", incident, target_version=target, run_id=run.run_id)
        backs_out = f", backing out {change_number}" if change_number else ""
        self._reply(
            incident,
            f":arrows_counterclockwise: Harness is rolling back {incident.service} to "
            f"{target}{backs_out}: <{run.url}|watch the run>",
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

        who = incident.decision.by_name
        if ok:
            note = (
                f"Backed out by Sentinel incident #{incident.id}: rolled back {incident.service} "
                f"from {incident.version} to {target} with Harness ({run.url}), approved in "
                f"Slack by {who}. No new change raised."
            )
        else:
            note = (
                f"Backout of {incident.version} failed in Harness "
                f"({rollback.error or run.url}). A person needs to take over."
            )
        if change is not None:
            self._servicenow(incident, self.change_mgmt.add_note, change, note)
        if incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.add_note, incident.itsm_incident, note)
        if ok:
            self._reply(
                incident,
                f":white_check_mark: Rollback done: {incident.service} {target} is running. "
                "Checking the error rate is back to normal...",
            )
            self.notify(incident_id)
            self.validate_rollback(incident_id)
        else:
            self._reply(
                incident,
                f":x: Rollback to {target} failed (<{run.url}|see the Harness run>). "
                "A person needs to take over.",
            )
            self.notify(incident_id)

    def validate_rollback(self, incident_id: int) -> None:
        """After a successful rollback: is the error rate back under the healthy level, and
        does it stay there for validate_for_s? Records the answer. Never raises."""
        incident = self.store.get(incident_id)
        rollback = incident.rollback if incident else None
        if rollback is None or rollback.status != "succeeded":
            return
        rollback.validation = "checking"
        self.store.save(incident)
        validated = self._healthy_for(incident, rollback.target_version)

        incident = self.store.get(incident_id)
        rollback = incident.rollback
        rollback.validation = "validated" if validated else "not_validated"
        rollback.validated_at = datetime.now(UTC) if validated else None
        self.store.save(incident)
        self._log("rollback validated" if validated else "rollback not validated", incident)
        threshold = f"{self.healthy_error_rate:.0%}"
        held = f"{round(self.validate_for_s / 60)} minutes"
        if validated:
            summary = (
                f"Rollback validated: errors on {rollback.target_version} stayed under "
                f"{threshold} for {held}."
            )
            self._reply(incident, f":white_check_mark: {summary}")
        else:
            summary = (
                f"Rollback not validated: errors on {rollback.target_version} didn't stay "
                f"under {threshold} for {held}. A person should check."
            )
            self._reply(incident, f":warning: {summary}")
        if self.change_mgmt is not None and incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.add_note, incident.itsm_incident, summary)
        self.notify(incident_id)

    # --- deploying a fix under a change (emergency for P1/P2, normal for P3/P4) ---

    def request_fix(self, incident_id: int, version: str, requested_by: str) -> FixDeploy:
        """An engineer has a fix version ready. Raise the change its priority calls for and
        ask the right people in Slack. Raises FixRefused if it can't be requested."""
        if self.change_mgmt is None or self.deploy_control is None:
            raise FixRefused("Deploying a fix needs ServiceNow and Harness connected.")
        if not VERSION.fullmatch(version):
            raise FixRefused(f"Not a version: {version!r} (expected e.g. 0.2.1)")
        with self._decision_lock:
            incident = self.store.get(incident_id)
            if incident is None:
                raise FixRefused(f"No incident #{incident_id}.")
            busy = incident.fix
            if busy is not None and busy.stage not in ("done", "rejected", "failed"):
                raise FixRefused(
                    f"Incident #{incident_id} already has a fix in progress ({busy.version})."
                )
            emergency = incident.severity == "critical"
            new = fix_change(incident, version, emergency)
            try:
                create = (
                    self.change_mgmt.create_emergency_change
                    if emergency
                    else self.change_mgmt.create_normal_change
                )
                change = create(new)
            except AdapterError as exc:
                raise FixRefused(f"ServiceNow didn't create the change ({exc}).") from exc
            incident.fix = FixDeploy(
                version=version,
                change_type="emergency" if emergency else "normal",
                priority="P1/P2" if emergency else "P3/P4",
                change=change,
                stage="awaiting_deploy" if emergency else "awaiting_approval",
                requested_by=requested_by,
                requested_at=datetime.now(UTC),
            )
            self.store.save(incident)
        self._log(
            "fix requested",
            incident,
            fix_version=version,
            change=change.number,
            change_type=incident.fix.change_type,
        )
        if incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.link_change, incident.itsm_incident, change)
        if not emergency:
            # Change Management reviews first; ServiceNow then asks the CAB by itself.
            self._servicenow(incident, self.change_mgmt.request_assessment, change)
        self._notify_fix(incident_id)
        return self.store.get(incident_id).fix

    def fix_click(self, incident_id: int, action: str, user_name: str, email: str | None) -> str:
        """A click on the fix's Slack message. Returns a short note for the clicker.

        "deploy" (emergency): someone on call deploys now. "approve"/"reject": a person
        ServiceNow is waiting on records their approval (normal: Change Management, then the
        CAB; emergency: the ECAB review after the deploy). Raises NotAllowed otherwise.
        """
        with self._decision_lock:
            incident = self.store.get(incident_id)
            fix = incident.fix if incident else None
            if fix is None or fix.change is None:
                raise NotAllowed("There's no fix waiting on this incident.")
            if action == "deploy":
                if fix.stage != "awaiting_deploy":
                    raise NotAllowed(f"This fix isn't waiting to be deployed ({fix.stage}).")
                on_call = self._verify_on_call(incident, email, "deploy this emergency fix")
                self._claim_deploy(incident, "fix")
                fix.stage, fix.deployed_by = "deploying", on_call
                fix.note = f"Emergency fix deployed on {on_call}'s say-so, before ECAB review."
                self.store.save(incident)
                self._background(lambda: self.deploy_fix(incident_id))
                result = "Deploying the fix now. The ECAB reviews the change afterwards."
            else:
                if fix.stage not in ("awaiting_approval", "awaiting_review"):
                    raise NotAllowed(f"This change isn't waiting for approval ({fix.stage}).")
                approve = action == "approve"
                before = self._change_state(incident, fix.change)
                if approve and fix.stage == "awaiting_approval" and before == "authorize":
                    self._check_deploy_free(incident)
                approver = self._verify_approver(incident, fix.change, email)
                verb = "Approved" if approve else "Rejected"
                try:
                    self.change_mgmt.record_approval(
                        approver, approve, f"{verb} in Slack by {user_name}, via Sentinel."
                    )
                except AdapterError as exc:
                    raise NotAllowed(
                        f"ServiceNow didn't accept your decision ({exc}). "
                        "Nothing was recorded; please try again."
                    ) from exc
                role = {"assess": "Change Management", "authorize": "CAB"}.get(before, "CAB")
                if fix.stage == "awaiting_review":
                    role = "ECAB review"
                fix.approvals.append(f"{approver.name} ({role}{'' if approve else ', rejected'})")
                self.store.save(incident)
                result = self._after_approval(incident_id, approve, approver.name)
        self._notify_fix(incident_id)
        return result

    def _after_approval(self, incident_id: int, approve: bool, name: str) -> str:
        """Called with the lock held, right after an approval was recorded in ServiceNow."""
        incident = self.store.get(incident_id)
        fix = incident.fix
        if not approve:
            reason = f"Rejected in Slack by {name}."
            if fix.stage == "awaiting_review":
                # Already deployed; the ECAB disagrees. Record it; people take it from here.
                fix.stage, fix.note = "failed", f"ECAB rejected the emergency fix: {reason}"
                self._servicenow(incident, self.change_mgmt.add_note, fix.change, fix.note)
            else:
                fix.stage, fix.note = "rejected", reason
                self._servicenow(incident, self.change_mgmt.cancel_change, fix.change, reason)
            self.store.save(incident)
            return "Recorded your rejection."
        state = self._wait_for_state(incident, fix.change, ("scheduled",))
        if fix.stage == "awaiting_review":
            if state == "scheduled":
                self._close_reviewed_emergency(incident)
                return "ECAB review recorded; the change is closed."
            return "Approval recorded; the ECAB review continues."
        if state == "scheduled":
            incident.deploy_in_progress = "fix"
            fix.stage, fix.note = "deploying", "Approved by the CAB. Deploying now."
            self.store.save(incident)
            self._background(lambda: self.deploy_fix(incident_id))
            return "Approval recorded. The CAB has approved it, so it deploys now."
        fix.note = "Change Management approved. Waiting for the CAB."
        self.store.save(incident)
        return "Approval recorded. Next: the CAB."

    def deploy_fix(self, incident_id: int) -> None:
        """Deploy the fix with Harness under its change, check it, then close or review the
        change. Never raises."""
        try:
            self._deploy_fix(incident_id)
        finally:
            self._release_deploy(incident_id)

    def _deploy_fix(self, incident_id: int) -> None:
        incident = self.store.get(incident_id)
        fix = incident.fix if incident else None
        if fix is None or fix.stage != "deploying" or self.deploy_control is None:
            return
        change = fix.change
        if fix.change_type == "normal":
            self._servicenow(
                incident,
                self.change_mgmt.start_implementation,
                change,
                f"Sentinel is deploying {incident.service} {fix.version} with Harness.",
            )
        reason = f"Fix for Sentinel incident #{incident.id} under {change.number}"
        try:
            run = self.deploy_control.start(fix.version, reason, change=change.number)
        except AdapterError as exc:
            self._fix_failed(incident_id, f"Harness couldn't start the deploy ({exc})")
            return
        incident = self.store.get(incident_id)
        incident.fix.run_url = run.url
        self.store.save(incident)
        self._notify_fix(incident_id)
        status = self._wait_for(run, incident)
        if status != "succeeded":
            self._fix_failed(incident_id, f"the Harness deploy didn't succeed ({run.url})")
            return
        incident = self.store.get(incident_id)
        incident.fix.deployed_at = datetime.now(UTC)
        incident.fix.validation = "checking"
        self.store.save(incident)
        self._notify_fix(incident_id)
        healthy = self._healthy_for(incident, fix.version)
        incident = self.store.get(incident_id)
        fix = incident.fix
        fix.validation = "validated" if healthy else "not_validated"
        done = f"Deployed {incident.service} {fix.version} with Harness ({run.url}); errors " + (
            "stayed under 5% for 2 minutes." if healthy else "did not settle under 5%."
        )
        if fix.change_type == "normal":
            self._servicenow(incident, self.change_mgmt.close_change, change, healthy, done)
            fix.stage = "done" if healthy else "failed"
            fix.note = done if healthy else done + " A person should check."
        else:
            # Emergency: it went out on the on-call engineer's say-so; now the ECAB reviews it.
            self._servicenow(incident, self.change_mgmt.add_note, change, done)
            self._servicenow(incident, self.change_mgmt.request_approval, change)
            fix.stage = "awaiting_review"
            fix.note = done + " Sent to the ECAB for review."
        self.store.save(incident)
        if incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.add_note, incident.itsm_incident, done)
        self._log("fix deployed", incident, fix_version=fix.version, validated=healthy)
        self._notify_fix(incident_id)

    def _close_reviewed_emergency(self, incident: Incident) -> None:
        """The ECAB approved an emergency fix that was already deployed: close the change."""
        fix = incident.fix
        notes = (
            f"Implemented as an emergency fix at {fix.deployed_at:%Y-%m-%d %H:%M UTC} on "
            f"{fix.deployed_by}'s approval, before review. ECAB reviewed and approved it "
            "afterwards."
        )
        self._servicenow(
            incident, self.change_mgmt.start_implementation, fix.change, "Emergency fix review."
        )
        ok = self._servicenow(incident, self.change_mgmt.close_change, fix.change, True, notes)
        fix.stage = "done" if ok else "failed"
        fix.note = notes if ok else "ECAB approved, but ServiceNow didn't close the change."
        self.store.save(incident)

    def _fix_failed(self, incident_id: int, why: str) -> None:
        incident = self.store.get(incident_id)
        fix = incident.fix
        note = f"Fix {fix.version} not deployed: {why}. A person needs to take over."
        if fix.change_type == "normal":
            self._servicenow(incident, self.change_mgmt.close_change, fix.change, False, note)
        else:
            self._servicenow(incident, self.change_mgmt.add_note, fix.change, note)
        fix.stage, fix.note = "failed", note
        self.store.save(incident)
        self._log("fix failed", incident, reason=why)
        self._notify_fix(incident_id)

    def _notify_fix(self, incident_id: int) -> None:
        """Post or refresh the fix's own Slack message. Never raises."""
        incident = self.store.get(incident_id)
        if self.chat is None or incident is None or incident.fix is None:
            return
        text, blocks = fix_message(incident)
        try:
            if incident.fix.chat is None:
                ref = self.chat.post(text, blocks)
                incident = self.store.get(incident_id)
                incident.fix.chat = ref
                self.store.save(incident)
            else:
                self.chat.update(incident.fix.chat, text, blocks)
        except AdapterError as exc:
            self._log("chat failed", incident, chat_error=str(exc))

    def _change_state(self, incident: Incident, change: RecordRef) -> str:
        try:
            return self.change_mgmt.change_state(change)
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            return ""

    def _wait_for_state(
        self, incident: Incident, change: RecordRef, wanted: tuple[str, ...]
    ) -> str:
        """ServiceNow moves a change on after an approval, shortly. Wait a little for it."""
        state = ""
        for _ in range(self.state_checks):
            state = self._change_state(incident, change)
            if state in wanted:
                return state
            time.sleep(self.state_poll_s)
        return state

    def _healthy_for(self, incident: Incident, version: str) -> bool:
        """Does `version`'s error rate stay under the healthy level for validate_for_s?"""
        query = recent_error_rate_query(incident.service, incident.env, version)
        healthy_since = None
        deadline = time.monotonic() + self.validate_timeout_s
        while query and time.monotonic() < deadline:
            try:
                rate = self.metrics.instant(query)
            except AdapterError as exc:
                self._log("validation query failed", incident, metrics_error=str(exc))
                rate = None
            if rate is not None and rate < self.healthy_error_rate:
                healthy_since = healthy_since or time.monotonic()
                if time.monotonic() - healthy_since >= self.validate_for_s:
                    return True
            elif rate is not None:
                healthy_since = None  # errors came back: start the clock again
            time.sleep(self.validate_poll_s)
        return False

    # --- one deploy at a time per incident ---

    def _check_deploy_free(self, incident: Incident) -> None:
        busy = incident.deploy_in_progress
        if busy:
            raise NotAllowed(
                f"A {busy} is already running for this incident, so nothing was recorded. "
                "Try again when it finishes."
            )

    def _claim_deploy(self, incident: Incident, kind: str) -> None:
        """Call with the lock held; the caller saves the incident."""
        self._check_deploy_free(incident)
        incident.deploy_in_progress = kind

    def _release_deploy(self, incident_id: int) -> None:
        with self._decision_lock:
            incident = self.store.get(incident_id)
            if incident is not None and incident.deploy_in_progress:
                incident.deploy_in_progress = None
                self.store.save(incident)

    def _find_change(self, incident: Incident, number: str | None) -> RecordRef | None:
        if number is None or self.change_mgmt is None:
            return None
        try:
            return self.change_mgmt.find_change(number)
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            return None

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

    def _rollback_not_started(self, incident: Incident, why: str) -> None:
        """Nothing was deployed: say so in the thread and on the incident."""
        self._log("rollback not started", incident, reason=why)
        note = f"Not rolled back: {why}. A person needs to take over."
        if self.change_mgmt is not None and incident.itsm_incident is not None:
            self._servicenow(incident, self.change_mgmt.add_note, incident.itsm_incident, note)
        self._reply(incident, f":x: Nothing was rolled back: {why}. A person needs to take over.")

    def _reply(self, incident: Incident, text: str) -> None:
        """Reply in the incident's chat thread. Never raises."""
        if self.chat is None or incident.chat is None:
            return
        try:
            self.chat.reply(incident.chat, text)
        except AdapterError as exc:
            self._log("chat failed", incident, chat_error=str(exc))

    def _verify_on_call(self, incident: Incident, email: str | None, what: str) -> str:
        """The clicker's name, if ServiceNow lists them in the on-call group; else NotAllowed."""
        group = self.oncall_group
        if not email:
            raise NotAllowed(
                "Sentinel couldn't read your email from Slack, so ServiceNow can't confirm "
                f"you're in {group}. Nothing was recorded."
            )
        try:
            name = self.change_mgmt.group_member(email, group)
        except AdapterError as exc:
            self._log("servicenow failed", incident, servicenow_error=str(exc))
            raise NotAllowed(
                f"Couldn't reach ServiceNow to check you're on call ({exc}). "
                "Nothing was recorded; please try again."
            ) from exc
        if name is None:
            raise NotAllowed(
                f"You're not in {group} in ServiceNow, so your click wasn't recorded. "
                f"Someone on call has to {what}."
            )
        return name

    def _verify_approver(
        self, incident: Incident, change: RecordRef, email: str | None
    ) -> Approver:
        """The clicker, if ServiceNow is waiting on their approval; otherwise NotAllowed."""
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
                "wasn't recorded. Only the approvers ServiceNow asked can decide."
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
