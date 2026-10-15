"""What a follow-up ticket says about an incident."""

import re

from orchestrator.adapters.ticketing import NewTicket
from orchestrator.models import Incident

WORDS = {"rollback": "roll back", "investigate": "investigate", "no_action": "no action"}


def needs_followup(incident: Incident) -> bool:
    """Only incidents with a cause worth fixing: diagnosed, and not 'no action needed'."""
    diagnosis = incident.triage.diagnosis if incident.triage else None
    return bool(diagnosis) and diagnosis.recommendation != "no_action"


def _first_sentence(text: str, limit: int = 160) -> str:
    sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].rstrip(".")
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _label(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:255]


def followup_ticket(incident: Incident, dashboard_url: str) -> NewTicket:
    diagnosis = incident.triage.diagnosis
    when = "%Y-%m-%d %H:%M UTC"
    timeline = [
        f"Opened: {incident.started_at:{when}}",
        f"Resolved: {incident.resolved_at:{when}}" if incident.resolved_at else "Still open",
    ]
    if incident.decision:
        d = incident.decision
        timeline.append(f"Decision: {d.action} by {d.by_name} at {d.at:{when}}")
    return NewTicket(
        summary=(
            f"Fix root cause of incident #{incident.id}: "
            f"{_first_sentence(diagnosis.likely_cause)}"
        ),
        paragraphs=[
            f"Filed automatically by Sentinel after incident #{incident.id} resolved. "
            "The immediate problem is handled; this ticket is to fix the cause properly.",
            f"Service {incident.service} ({incident.env}), version {incident.version} "
            f"(commit {incident.commit_sha}).",
            f"AI diagnosis ({diagnosis.confidence} confidence): {diagnosis.likely_cause}",
            f"AI recommendation at the time: {WORDS[diagnosis.recommendation]}. "
            f"{diagnosis.reasoning}",
            "Evidence and timeline:",
        ],
        bullets=[*diagnosis.evidence, *timeline, f"Dashboard: {dashboard_url}"],
        labels=[
            "sentinel",
            _label(incident.service or "unknown"),
            _label(f"version-{incident.version}"),
        ],
    )
