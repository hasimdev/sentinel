"""What the deploy log (logs/deploys.log) says about a release.

Decided from facts, not by the AI: the version that ran just before the bad one was deployed,
and the ServiceNow change the bad release was deployed under. A rollback backs out that change,
so it needs no new ticket.
"""

import json
from pathlib import Path

from pydantic import BaseModel


class Release(BaseModel):
    version: str
    previous_version: str | None  # what ran before it: the rollback target
    change: str | None  # the change it was deployed under, e.g. CHG0031000


def release_of(deploy_log: str | Path, service: str | None, version: str | None) -> Release | None:
    """The latest deploy of `version`, or None if the log has no record of it."""
    path = Path(deploy_log)
    if not version or not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a half-written or foreign line
    for event in reversed(events):
        if event.get("service", service) != service or event.get("version") != version:
            continue
        previous = event.get("previous_version")
        return Release(
            version=version,
            previous_version=previous if previous and previous != version else None,
            change=event.get("change") or None,
        )
    return None


def rollback_target(
    deploy_log: str | Path, service: str | None, bad_version: str | None
) -> str | None:
    """The version that was running before the latest deploy of `bad_version`, or None."""
    release = release_of(deploy_log, service, bad_version)
    return release.previous_version if release else None
