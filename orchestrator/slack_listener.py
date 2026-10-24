"""Receives Approve / Reject clicks from Slack over Socket Mode.

Socket Mode means Sentinel opens the connection OUT to Slack, so the laptop needs no public
address, tunnel or firewall change.
"""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from orchestrator.messages import (
    APPROVE_ACTION,
    CHANGE_APPROVE_ACTION,
    CHANGE_REJECT_ACTION,
    FIX_DEPLOY_ACTION,
    REJECT_ACTION,
)
from orchestrator.service import NotAllowed


def clicker_email(client, user_id: str) -> str | None:
    """The clicker's email from their Slack profile (needs users:read.email), or None."""
    try:
        return client.users_info(user=user_id)["user"]["profile"].get("email")
    except Exception:  # missing scope, Slack trouble: ServiceNow checks then refuse the click
        return None


def handle_click(service, body: dict, email: str | None = None) -> str | None:
    """Turn a button click into a recorded decision.

    `email` is the clicker's Slack email; ServiceNow uses it to check they may approve.
    Returns a note for the clicker if nothing changed, otherwise None.
    """
    action = body["actions"][0]
    incident_id = int(action["value"])
    user = body.get("user", {})
    user_id = user.get("id", "unknown")
    user_name = user.get("name") or user.get("username") or user_id
    verb = "approve" if action["action_id"] == APPROVE_ACTION else "reject"
    try:
        decision, already = service.decide(incident_id, verb, user_id, user_name, email)
    except NotAllowed as exc:
        _log_refused(service, incident_id, user_name, exc)
        return str(exc)
    if decision is None:
        return f"Incident #{incident_id} no longer exists."
    if already:
        return f"Incident #{incident_id} was already decided: {decision.outcome}"
    return None


FIX_ACTIONS = {
    FIX_DEPLOY_ACTION: "deploy",
    CHANGE_APPROVE_ACTION: "approve",
    CHANGE_REJECT_ACTION: "reject",
}


def handle_fix_click(service, body: dict, email: str | None = None) -> str:
    """A click on a fix's message (deploy now, or approve/reject its change). Returns the
    note for the clicker: what happened, or why nothing did."""
    action = body["actions"][0]
    incident_id = int(action["value"])
    user = body.get("user", {})
    user_name = user.get("name") or user.get("username") or user.get("id", "unknown")
    try:
        return service.fix_click(incident_id, FIX_ACTIONS[action["action_id"]], user_name, email)
    except NotAllowed as exc:
        _log_refused(service, incident_id, user_name, exc)
        return str(exc)


def _log_refused(service, incident_id: int, user_name: str, exc: NotAllowed) -> None:
    """A refused click is worth a log line: it's how a "nothing happened" gets explained."""
    fields = {"incident_id": incident_id, "clicked_by": user_name, "reason": str(exc)}
    service.log.info("click refused", extra={"fields": fields})


def start_socket_mode(bot_token: str, app_token: str, service, log: logging.Logger):
    """Connect to Slack in the background. Returns the handler (call .close() on shutdown)."""
    bolt = App(token=bot_token, logger=log)

    def on_click(ack, body, client):
        ack()  # Slack needs an answer within 3 seconds
        note = handle_click(service, body, clicker_email(client, body["user"]["id"]))
        if note:
            client.chat_postEphemeral(
                channel=body["channel"]["id"], user=body["user"]["id"], text=note
            )

    def on_fix_click(ack, body, client):
        ack()
        note = handle_fix_click(service, body, clicker_email(client, body["user"]["id"]))
        client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text=note)

    bolt.action(APPROVE_ACTION)(on_click)
    bolt.action(REJECT_ACTION)(on_click)
    for fix_action in FIX_ACTIONS:
        bolt.action(fix_action)(on_fix_click)
    # "View dashboard" is a link button; Slack still reports the click, so acknowledge it.
    bolt.action("sentinel_dashboard")(lambda ack: ack())

    handler = SocketModeHandler(bolt, app_token)
    handler.connect()  # non-blocking
    return handler
