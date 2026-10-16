"""What Sentinel writes into ServiceNow: the incident, its notes, and the emergency change."""

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


def rollback_change(incident: Incident) -> NewChange:
    diagnosis = incident.triage.diagnosis
    service, version = incident.service, incident.version
    return NewChange(
        short_description=(
            f"Emergency rollback of {service} {version} (Sentinel incident #{incident.id})"
        ),
        description=(
            f"Roll back {service} in {incident.env} from version {version} "
            f"(commit {incident.commit_sha}) to the previous good release.\n\n"
            f"Raised automatically by Sentinel because the AI recommends a rollback. "
            f"It needs CAB approval; nothing is deployed until a person approves."
        ),
        justification=(
            f"AI diagnosis ({diagnosis.confidence} confidence): {diagnosis.likely_cause}\n"
            f"{diagnosis.reasoning}"
        ),
        implementation_plan=(
            f"Redeploy the previous {service} release with the standard deploy pipeline. "
            "Watch the error rate on the dashboard."
        ),
        backout_plan=f"Redeploy version {version} if the rollback makes things worse.",
        test_plan="Error rate back under 5% and the alert clears within 5 minutes.",
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
