"""Collect evidence for an incident from the read-only sources."""

import re
from datetime import UTC, datetime, timedelta

from orchestrator.adapters.base import AdapterError, LogSource, MetricSource, label_selector
from orchestrator.models import Evidence, Incident

LOG_WINDOW = timedelta(minutes=15)
MAX_LOG_LINES = 20
_METRIC_NAME_OK = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _selector(incident: Incident, **extra: str) -> str:
    return label_selector(
        service=incident.service,
        env=incident.env,
        version=incident.version,
        commit_sha=incident.commit_sha,
        **extra,
    )


def metric_queries(incident: Incident) -> dict[str, str]:
    """PromQL for the numbers a triager wants first, scoped to the failing version/commit."""
    sel = _selector(incident)
    requests = f"{incident.service}_http_requests_total"
    latency = f"{incident.service}_http_request_duration_seconds_bucket"
    errors = f'sum(rate({requests}{{{sel}, status=~"5.."}}[5m]))'
    total = f"sum(rate({requests}{{{sel}}}[5m]))"
    return {
        "error_rate": f"({errors} or vector(0)) / {total}",
        "requests_per_second": total,
        "p95_latency_seconds": (
            f"histogram_quantile(0.95, sum by (le) (rate({latency}{{{sel}}}[5m])))"
        ),
    }


def error_log_query(incident: Incident) -> str:
    return "{" + _selector(incident, level="ERROR") + "}"


def gather_evidence(incident: Incident, metrics: MetricSource, logs: LogSource) -> Evidence:
    """Never raises: anything that can't be collected is recorded as a gap instead."""
    evidence = Evidence(gathered_at=datetime.now(UTC))

    if not incident.service or not _METRIC_NAME_OK.match(incident.service):
        evidence.gaps.append("alert has no usable 'service' label; evidence not collected")
        return evidence

    for name, query in metric_queries(incident).items():
        try:
            evidence.metrics[name] = metrics.instant(query)
        except AdapterError as exc:
            evidence.metrics[name] = None
            evidence.gaps.append(f"metric '{name}' unavailable: {exc}")

    try:
        evidence.error_logs = logs.search(error_log_query(incident), LOG_WINDOW, MAX_LOG_LINES)
    except AdapterError as exc:
        evidence.gaps.append(f"error logs unavailable: {exc}")

    return evidence
