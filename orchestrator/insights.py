"""How well the AI + human loop works, as Prometheus metrics.

Worked out from every incident in the store each time Prometheus scrapes, so nothing is lost
when the orchestrator restarts and past incidents count too.

  time to decide    AI recommendation ready -> a person clicks Approve or Reject
  agree rate        approved / all decisions (an override = rejecting the AI's call)
  time to diagnose  alert fired -> AI recommendation ready
  time to recover   alert fired -> alert cleared
"""

from collections import Counter
from statistics import median

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client.registry import Collector

from common.logging_setup import TAG_FIELDS
from orchestrator.models import Incident
from orchestrator.store import IncidentStore

DECIDE_BUCKETS = (30, 60, 120, 300, 600, 1800, 3600)
RECOVER_BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200)
ROLLBACK_BUCKETS = (10, 30, 60, 120, 300, 600)


def _seconds(start, end) -> float | None:
    if start is None or end is None:
        return None
    value = (end - start).total_seconds()
    return value if value >= 0 else None  # clocks disagreeing is not a measurement


def recommendation(incident: Incident) -> str:
    diagnosis = incident.triage.diagnosis if incident.triage else None
    return diagnosis.recommendation if diagnosis else "none"


def time_to_decide(incident: Incident) -> float | None:
    if incident.decision is None or incident.triage is None:
        return None
    return _seconds(incident.triage.finished_at, incident.decision.at)


def time_to_diagnose(incident: Incident) -> float | None:
    if incident.triage is None or incident.triage.diagnosis is None:
        return None
    return _seconds(incident.started_at, incident.triage.finished_at)


def time_to_recover(incident: Incident) -> float | None:
    return _seconds(incident.started_at, incident.resolved_at)


def rollback_duration(incident: Incident) -> float | None:
    r = incident.rollback
    return _seconds(r.started_at, r.finished_at) if r else None


def _histogram(name, doc, tags, values, buckets):
    family = HistogramMetricFamily(name, doc, labels=list(tags))
    cumulative = [(str(float(b)), float(sum(v <= b for v in values))) for b in buckets]
    cumulative.append(("+Inf", float(len(values))))
    family.add_metric(list(tags.values()), cumulative, sum_value=float(sum(values)))
    return family


def _median(name, doc, tags, values):
    family = GaugeMetricFamily(name, doc, labels=list(tags))
    if values:  # no data -> no sample, so Grafana shows "No data" instead of a fake 0
        family.add_metric(list(tags.values()), float(median(values)))
    return family


class IncidentMetrics(Collector):
    """Prometheus collector: reads the incident store at scrape time."""

    def __init__(self, store: IncidentStore, tags: dict[str, str]) -> None:
        self.store = store
        self.tags = {k: tags[k] for k in TAG_FIELDS}

    def collect(self):
        incidents = self.store.list()
        tags, keys = self.tags, list(self.tags)
        values = list(tags.values())

        by_recommendation = Counter(recommendation(i) for i in incidents)
        family = CounterMetricFamily(
            "sentinel_incidents",
            "Incidents, by what the AI recommended (none = no diagnosis).",
            labels=[*keys, "recommendation"],
        )
        for rec, count in sorted(by_recommendation.items()):
            family.add_metric([*values, rec], count)
        yield family

        decisions = Counter((recommendation(i), i.decision.action) for i in incidents if i.decision)
        family = CounterMetricFamily(
            "sentinel_decisions",
            "People's decisions on the AI's recommendation: approved (agree) or rejected "
            "(override).",
            labels=[*keys, "recommendation", "decision"],
        )
        for (rec, action), count in sorted(decisions.items()):
            family.add_metric([*values, rec, action], count)
        yield family

        rollbacks = Counter(i.rollback.status for i in incidents if i.rollback)
        family = CounterMetricFamily(
            "sentinel_rollbacks",
            "Real rollbacks run through the deploy tool, by result.",
            labels=[*keys, "status"],
        )
        for status, count in sorted(rollbacks.items()):
            family.add_metric([*values, status], count)
        yield family

        measures = [
            ("time_to_decide", time_to_decide, DECIDE_BUCKETS,
             "AI recommendation ready -> a person decided"),
            ("time_to_diagnose", time_to_diagnose, DECIDE_BUCKETS,
             "Alert fired -> AI recommendation ready"),
            ("time_to_recover", time_to_recover, RECOVER_BUCKETS,
             "Alert fired -> alert cleared"),
            ("rollback_duration", rollback_duration, ROLLBACK_BUCKETS,
             "Deploy tool started the rollback -> it finished"),
        ]  # fmt: skip
        for name, measure, buckets, doc in measures:
            found = [v for v in (measure(i) for i in incidents) if v is not None]
            yield _histogram(f"sentinel_{name}_seconds", f"{doc}.", tags, found, buckets)
            yield _median(
                f"sentinel_{name}_median_seconds", f"Median: {doc}, all incidents.", tags, found
            )
