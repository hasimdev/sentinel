"""How an incident looks in chat: a plain-text fallback plus Slack Block Kit blocks."""

from orchestrator.models import Incident

APPROVE_ACTION = "sentinel_approve"
REJECT_ACTION = "sentinel_reject"

RECOMMENDATION_WORDS = {
    "rollback": "Roll back",
    "investigate": "Investigate",
    "no_action": "No action needed",
}
APPROVE_LABELS = {
    "rollback": "Approve rollback",
    "investigate": "Agree: investigate",
    "no_action": "Agree: no action",
}
MAX_TEXT = 2900  # Slack's section text limit is 3000 characters


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _time(dt) -> str:
    return dt.strftime("%H:%M UTC") if dt else "?"


def incident_message(incident: Incident, dashboard_url: str) -> tuple[str, list[dict]]:
    triage = incident.triage
    diagnosis = triage.diagnosis if triage else None
    summary = incident.summary or incident.alertname
    text = f"Incident #{incident.id}: {summary}"
    if diagnosis:
        text += f" | AI recommends: {RECOMMENDATION_WORDS[diagnosis.recommendation]}"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _cut(f"Incident #{incident.id}: {summary}", 150),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{incident.severity or 'unknown'}* · {incident.service} · "
                        f"{incident.env} · version `{incident.version}` "
                        f"(`{incident.commit_sha}`) · started {_time(incident.started_at)}"
                    ),
                }
            ],
        },
    ]

    if diagnosis:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cut(
                        f"*AI diagnosis* ({diagnosis.confidence} confidence)\n"
                        f"{diagnosis.likely_cause}\n\n"
                        f"*Recommendation: {RECOMMENDATION_WORDS[diagnosis.recommendation]}*\n"
                        f"{diagnosis.reasoning}"
                    ),
                },
            }
        )
        if diagnosis.evidence:
            bullets = "\n".join(f"• {item}" for item in diagnosis.evidence[:6])
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _cut(f"*Evidence*\n{bullets}")},
                }
            )
    else:
        reason = (triage.error if triage else None) or "the AI diagnosis is still running"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _cut(f"*No AI diagnosis:* {reason}")},
            }
        )

    elements: list[dict] = []
    if incident.decision is None and incident.status == "open":
        recommendation = diagnosis.recommendation if diagnosis else None
        elements += [
            {
                "type": "button",
                "action_id": APPROVE_ACTION,
                "text": {
                    "type": "plain_text",
                    "text": APPROVE_LABELS.get(recommendation, "Approve"),
                },
                "style": "primary",
                "value": str(incident.id),
            },
            {
                "type": "button",
                "action_id": REJECT_ACTION,
                "text": {"type": "plain_text", "text": "Reject"},
                "value": str(incident.id),
            },
        ]
    elements.append(
        {
            "type": "button",
            "action_id": "sentinel_dashboard",
            "text": {"type": "plain_text", "text": "View dashboard"},
            "url": dashboard_url,
        }
    )
    blocks.append({"type": "actions", "elements": elements})

    status_lines = []
    if incident.decision:
        d = incident.decision
        mark = ":white_check_mark:" if d.action == "approved" else ":x:"
        status_lines.append(f"{mark} {d.outcome} ({_time(d.at)})")
    if incident.status == "resolved":
        status_lines.append(f":large_green_circle: Resolved at {_time(incident.resolved_at)}")
    status_lines.append("Sentinel only recommends. Nothing changes until a person approves.")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": line} for line in status_lines],
        }
    )
    return text, blocks
