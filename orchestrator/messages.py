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


def servicenow_line(incident: Incident) -> str | None:
    """Links to the ServiceNow incident and emergency change, if there are any."""
    parts = []
    if incident.itsm_incident:
        r = incident.itsm_incident
        parts.append(f"incident <{r.url}|{r.number}>")
    if incident.change:
        c = incident.change
        waiting = incident.decision is None and incident.status == "open"
        note = " (needs CAB approval: only a CAB approver's click counts)" if waiting else ""
        parts.append(f"emergency change <{c.url}|{c.number}>{note}")
    return f":clipboard: ServiceNow: {' · '.join(parts)}" if parts else None


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

    records = servicenow_line(incident)
    if records:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": records}]})

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
    if incident.rollback:
        r = incident.rollback
        run = f"<{r.url}|Harness run>"
        backout = f", backout of {r.release_change}" if r.release_change else ""
        status_lines.append(
            {
                "running": f":arrows_counterclockwise: Rolling back to {r.target_version} "
                f"({run}{backout})",
                "succeeded": f":white_check_mark: Rolled back to {r.target_version} "
                f"at {_time(r.finished_at)} ({run}{backout})",
                "failed": f":x: Rollback to {r.target_version} failed ({run})",
            }[r.status]
        )
        if r.validation:
            validation = {
                "checking": ":hourglass_flowing_sand: Checking the error rate is back to normal",
                "validated": f":white_check_mark: Rollback validated at {_time(r.validated_at)}",
                "not_validated": ":warning: Rollback not validated: a person should check",
            }
            status_lines.append(validation[r.validation])
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


# --- a fix deployed under a change ---

FIX_DEPLOY_ACTION = "sentinel_fix_deploy"
CHANGE_APPROVE_ACTION = "sentinel_change_approve"
CHANGE_REJECT_ACTION = "sentinel_change_reject"

FIX_PROCESS = {
    "emergency": "P1/P2, so an emergency change: deploys as soon as someone on call approves; "
    "the ECAB reviews it afterwards.",
    "normal": "P3/P4, so a normal change: Change Management, then the CAB, approve it before "
    "it deploys.",
}
FIX_STAGES = {
    "awaiting_deploy": ":rotating_light: Waiting for someone on call to deploy the fix",
    "awaiting_approval": ":hourglass_flowing_sand: Waiting for approval",
    "deploying": ":arrows_counterclockwise: Deploying with Harness",
    "awaiting_review": ":eyes: Deployed. Waiting for the ECAB review",
    "done": ":white_check_mark: Done: change closed",
    "rejected": ":x: Rejected: nothing deployed",
    "failed": ":x: Failed: a person needs to take over",
}


def fix_message(incident: Incident) -> tuple[str, list[dict]]:
    fix = incident.fix
    change = fix.change
    number = f"<{change.url}|{change.number}>" if change else "no change record"
    text = f"Fix for incident #{incident.id}: deploy {incident.service} {fix.version}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _cut(text, 150)}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{fix.change_type.title()} change {number}*\n"
                    f"{FIX_PROCESS[fix.change_type]}\n"
                    f"Requested by {fix.requested_by} at {_time(fix.requested_at)}."
                ),
            },
        },
    ]
    buttons = []
    if fix.stage == "awaiting_deploy":
        buttons.append(_button(FIX_DEPLOY_ACTION, "Deploy fix now", incident.id, primary=True))
    elif fix.stage in ("awaiting_approval", "awaiting_review"):
        label = "Approve (ECAB review)" if fix.stage == "awaiting_review" else "Approve change"
        buttons += [
            _button(CHANGE_APPROVE_ACTION, label, incident.id, primary=True),
            _button(CHANGE_REJECT_ACTION, "Reject", incident.id),
        ]
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    lines = [FIX_STAGES[fix.stage]]
    if fix.approvals:
        lines.append("Approved by: " + ", ".join(fix.approvals))
    if fix.run_url:
        by = f" by {fix.deployed_by}" if fix.deployed_by else ""
        lines.append(f"<{fix.run_url}|Harness run>{by}")
    if fix.validation:
        lines.append(
            {
                "checking": "Checking the error rate is back to normal",
                "validated": ":white_check_mark: Validated: errors stayed under 5%",
                "not_validated": ":warning: Not validated: a person should check",
            }[fix.validation]
        )
    if fix.note:
        lines.append(fix.note)
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _cut(x)} for x in lines]}
    )
    return text, blocks


def _button(action_id: str, label: str, incident_id: int, primary: bool = False) -> dict:
    button = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label},
        "value": str(incident_id),
    }
    if primary:
        button["style"] = "primary"
    return button
