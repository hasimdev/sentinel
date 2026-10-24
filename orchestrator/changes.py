"""What Sentinel writes into ServiceNow: the incident, its notes, and a fix's change."""

from orchestrator.adapters.change_mgmt import NewChange, NewIncident
from orchestrator.models import Incident
from orchestrator.tickets import WORDS

WHEN = "%Y-%m-%d %H:%M UTC"


def _where(incident: Incident) -> str:
    return (
        f"Service {incident.service} ({incident.env}), version {incident.version} "
        f"(commit {incident.commit_sha})"
    )


def incident_record(incident: Incident, dashboard_url: str) -> NewIncident:
    summary = incident.summary or incident.alertname
    return NewIncident(
        short_description=f"[Sentinel #{incident.id}] {summary}",
        description="\n".join(
            [
                f"Opened automatically by Sentinel from alert {incident.alertname}.",
                f"{_where(incident)}.",
                f"Started: {incident.started_at:{WHEN}}",
                f"Dashboard: {dashboard_url}",
                "The AI diagnosis is added as a work note when ready. "
                "The AI only recommends; people decide.",
            ]
        ),
        urgent=incident.severity == "critical",
    )


def diagnosis_note(incident: Incident) -> str:
    triage = incident.triage
    diagnosis = triage.diagnosis if triage else None
    if diagnosis is None:
        reason = (triage.error if triage else None) or "no diagnosis"
        return f"Sentinel: no AI diagnosis ({reason}). Investigate manually."
    lines = [
        f"Sentinel AI diagnosis ({diagnosis.confidence} confidence, model {triage.model}):",
        diagnosis.likely_cause,
        "",
        f"Recommendation: {WORDS[diagnosis.recommendation]}. {diagnosis.reasoning}",
        "",
        "Evidence:",
        *[f"- {item}" for item in diagnosis.evidence],
    ]
    return "\n".join(lines)


def fix_change(incident: Incident, version: str, emergency: bool) -> NewChange:
    """The change for deploying fix `version`: emergency for P1/P2, normal for P3/P4."""
    diagnosis = incident.triage.diagnosis if incident.triage else None
    service = incident.service
    if emergency:
        kind = "Emergency"
        process = (
            "P1/P2 incident: an emergency change. It is deployed as soon as someone on call "
            "approves in Slack, then goes to the ECAB for review afterwards."
        )
    else:
        kind = "Normal"
        process = (
            "P3/P4 incident: a normal change. Change Management and then the CAB approve it "
            "before anything is deployed."
        )
    cause = (
        f"AI diagnosis ({diagnosis.confidence} confidence): {diagnosis.likely_cause}"
        if diagnosis
        else "No AI diagnosis on record."
    )
    return NewChange(
        short_description=(
            f"{kind} fix: deploy {service} {version} (Sentinel incident #{incident.id})"
        ),
        description=(
            f"Deploy {service} {version} in {incident.env} to fix the problem in release "
            f"{incident.version} (commit {incident.commit_sha}).\n\n{process}"
        ),
        justification=cause,
        implementation_plan=(
            f"Deploy {service} {version} with the Harness pipeline. Sentinel checks the error "
            "rate stays under 5% for 2 minutes afterwards."
        ),
        backout_plan="Roll back to the last good version with the Harness pipeline.",
        test_plan="Error rate under 5% for 2 minutes after the deploy; the alert stays clear.",
    )


def resolution_notes(incident: Incident) -> str:
    diagnosis = incident.triage.diagnosis if incident.triage else None
    lines = [f"Alert cleared at {incident.resolved_at:{WHEN}} (Sentinel incident #{incident.id})."]
    if diagnosis:
        lines.append(f"Likely cause (AI): {diagnosis.likely_cause}")
    if incident.decision:
        d = incident.decision
        lines.append(f"Decision in Slack: {d.action} by {d.by_name} at {d.at:{WHEN}}.")
    if incident.followup:
        lines.append(f"Root-cause fix tracked in {incident.followup.key}: {incident.followup.url}")
    return "\n".join(lines)
