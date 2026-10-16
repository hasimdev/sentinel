"""Change management adapter: incidents and emergency changes in ServiceNow (see CLAUDE.md).

The AI never uses this. The orchestrator keeps the ServiceNow records in step with the incident,
and records an approval only after a person clicks, and only if ServiceNow itself asked that
person to approve the change.
"""

import re
from typing import Protocol

import httpx2
from pydantic import BaseModel

from orchestrator.adapters.base import AdapterError
from orchestrator.models import RecordRef

# ServiceNow state codes (Change Management API and the incident table).
CHANGE_AUTHORIZE = "-3"
CHANGE_CANCELED = "4"
INCIDENT_RESOLVED = "6"
# The resolution codes this instance accepts (older "Solved (Work Around)" is rejected).
RESOLVE_CODE = "Workaround provided"

_EMAIL = re.compile(r"^[^\s@^,=]+@[^\s@^,=]+$")  # no ServiceNow query characters


class NewIncident(BaseModel):
    short_description: str
    description: str
    urgent: bool = False


class NewChange(BaseModel):
    short_description: str
    description: str
    justification: str
    implementation_plan: str
    backout_plan: str
    test_plan: str


class Approver(BaseModel):
    """A person ServiceNow is waiting on to approve a change."""

    approval_id: str  # their approval request (sysapproval_approver record)
    name: str


class ChangeMgmt(Protocol):
    def open_incident(self, new: NewIncident) -> RecordRef: ...
    def add_note(self, record: RecordRef, text: str) -> None: ...
    def resolve_incident(self, record: RecordRef, notes: str) -> None: ...
    def create_emergency_change(self, new: NewChange) -> RecordRef: ...
    def link_change(self, incident: RecordRef, change: RecordRef) -> None: ...
    def request_approval(self, change: RecordRef) -> None: ...
    def pending_approver(self, change: RecordRef, email: str) -> Approver | None: ...
    def record_approval(self, approver: Approver, approved: bool, comment: str) -> None: ...
    def cancel_change(self, change: RecordRef, notes: str) -> None: ...


class ServiceNowChangeMgmt:
    """ChangeMgmt backed by ServiceNow (Table API + Change Management API, basic auth)."""

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        change_group: str = "Change Management",
        transport=None,
    ) -> None:
        self.base_url = instance_url.rstrip("/")
        self.change_group = change_group
        self._group_id: str | None = None
        self._client = httpx2.Client(
            base_url=self.base_url,
            auth=(username, password),
            headers={"Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    # --- incidents ---

    def open_incident(self, new: NewIncident) -> RecordRef:
        level = "1" if new.urgent else "2"
        body = {
            "short_description": new.short_description[:160],
            "description": new.description,
            "impact": level,
            "urgency": level,
        }
        result = self._call("POST", "/api/now/table/incident", json=body)
        return self._ref("incident", result)

    def add_note(self, record: RecordRef, text: str) -> None:
        self._call(
            "PATCH", f"/api/now/table/{record.table}/{record.sys_id}", json={"work_notes": text}
        )

    def resolve_incident(self, record: RecordRef, notes: str) -> None:
        body = {"state": INCIDENT_RESOLVED, "close_code": RESOLVE_CODE, "close_notes": notes}
        self._call("PATCH", f"/api/now/table/incident/{record.sys_id}", json=body)

    # --- emergency changes ---

    def create_emergency_change(self, new: NewChange) -> RecordRef:
        result = self._call("POST", "/api/sn_chg_rest/change/emergency", json=new.model_dump())
        return self._ref("change_request", result)

    def link_change(self, incident: RecordRef, change: RecordRef) -> None:
        """A work note on each record naming the other. (The incident's "Change Request"
        field isn't visible to an itil user on every instance, and ServiceNow silently
        ignores fields it can't see, so a note is the link that always works.)"""
        self.add_note(incident, f"Emergency change {change.number} raised for this incident.")
        self.add_note(change, f"Raised for incident {incident.number}.")

    def request_approval(self, change: RecordRef) -> None:
        """Move the change to Authorize; ServiceNow then asks the CAB to approve it.

        State changes must go through the Change Management API: the plain Table API is
        blocked by the instance's state-transition rule.
        """
        body = {"assignment_group": self._change_group_id(), "state": CHANGE_AUTHORIZE}
        self._call("PATCH", f"/api/sn_chg_rest/change/{change.sys_id}", json=body)

    def pending_approver(self, change: RecordRef, email: str) -> Approver | None:
        """The person with this email, if ServiceNow is waiting on their approval of `change`."""
        if not _EMAIL.match(email):
            return None
        users = self._call(
            "GET",
            "/api/now/table/sys_user",
            params={"sysparm_query": f"email={email}^active=true", "sysparm_fields": "sys_id,name"},
        )
        if len(users) != 1:  # no match, or ambiguous: don't guess who clicked
            return None
        user = users[0]
        waiting = f"sysapproval={change.sys_id}^approver={user['sys_id']}^state=requested"
        requests = self._call(
            "GET",
            "/api/now/table/sysapproval_approver",
            params={
                "sysparm_query": waiting,
                "sysparm_fields": "sys_id",
                "sysparm_limit": "1",
            },
        )
        if not requests:
            return None
        return Approver(approval_id=requests[0]["sys_id"], name=user["name"])

    def record_approval(self, approver: Approver, approved: bool, comment: str) -> None:
        body = {"state": "approved" if approved else "rejected", "comments": comment}
        self._call(
            "PATCH", f"/api/now/table/sysapproval_approver/{approver.approval_id}", json=body
        )

    def cancel_change(self, change: RecordRef, notes: str) -> None:
        body = {"state": CHANGE_CANCELED, "close_notes": notes}
        self._call("PATCH", f"/api/sn_chg_rest/change/{change.sys_id}", json=body)

    # --- helpers ---

    def _change_group_id(self) -> str:
        if self._group_id is None:
            groups = self._call(
                "GET",
                "/api/now/table/sys_user_group",
                params={"sysparm_query": f"name={self.change_group}", "sysparm_fields": "sys_id"},
            )
            if not groups:
                raise AdapterError(f"ServiceNow group '{self.change_group}' not found")
            self._group_id = groups[0]["sys_id"]
        return self._group_id

    def _ref(self, table: str, result: dict) -> RecordRef:
        sys_id, number = _value(result["sys_id"]), _value(result["number"])
        return RecordRef(
            table=table,
            number=number,
            sys_id=sys_id,
            url=f"{self.base_url}/{table}.do?sys_id={sys_id}",
        )

    def _call(self, method: str, path: str, **kwargs):
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise AdapterError(f"ServiceNow unreachable: {exc}") from exc
        if resp.is_error:
            raise AdapterError(_servicenow_error(resp))
        return resp.json()["result"]


def _value(field):
    """The Change API returns {"value": ..., "display_value": ...}; the Table API plain values."""
    return field["value"] if isinstance(field, dict) else field


def _servicenow_error(resp) -> str:
    try:
        error = resp.json().get("error") or {}
        detail = " ".join(filter(None, [error.get("message"), error.get("detail")]))
    except (ValueError, AttributeError):
        detail = ""
    return f"ServiceNow refused ({resp.status_code}): {(detail or resp.reason_phrase)[:300]}"
