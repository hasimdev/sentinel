"""Adapter interfaces. Every integration sits behind one of these (see CLAUDE.md).

These two are READ-ONLY by design: they can query, never change anything.
"""

from datetime import timedelta
from typing import Protocol

from orchestrator.models import LogLine


class AdapterError(Exception):
    """A source could not be reached or returned something unusable."""


class MetricSource(Protocol):
    def instant(self, query: str) -> float | None:
        """Evaluate a query now and return a single number (None if there is no data)."""
        ...


class LogSource(Protocol):
    def search(self, query: str, since: timedelta, limit: int) -> list[LogLine]:
        """Return up to `limit` matching log lines from the last `since`, newest first."""
        ...


def label_selector(**labels: str | None) -> str:
    """Build `a="x", b="y"` for PromQL/LogQL, skipping missing values and escaping quotes."""
    parts = []
    for key, value in labels.items():
        if value is None:
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}="{escaped}"')
    return ", ".join(parts)
