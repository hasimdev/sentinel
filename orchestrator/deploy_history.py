"""Which version to roll back to, from the deploy log (logs/deploys.log).

Decided from facts, not by the AI: the version that ran just before the bad one was deployed.
"""

import json
from pathlib import Path


def rollback_target(
    deploy_log: str | Path, service: str | None, bad_version: str | None
) -> str | None:
    """The version that was running before the latest deploy of `bad_version`, or None."""
    path = Path(deploy_log)
    if not bad_version or not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a half-written or foreign line
    for event in reversed(events):
        if event.get("service", service) != service or event.get("version") != bad_version:
            continue
        previous = event.get("previous_version")
        return previous if previous and previous != bad_version else None
    return None
