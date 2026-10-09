"""Sentinel orchestrator: receives alerts, opens incidents, attaches evidence and AI triage."""

import os

from fastapi import BackgroundTasks, FastAPI, HTTPException

from common.logging_setup import configure_logging
from mcp_server.tools import make_toolbox
from orchestrator.adapters.base import LogSource, MetricSource
from orchestrator.adapters.loki import LokiLogSource
from orchestrator.adapters.prometheus import PrometheusMetricSource
from orchestrator.models import AlertmanagerWebhook, Incident
from orchestrator.service import IncidentService, WebhookResult
from orchestrator.settings import Settings, load_settings
from orchestrator.store import IncidentStore
from orchestrator.triage import TriageAgent


def create_app(
    settings: Settings | None = None,
    metrics: MetricSource | None = None,
    logs: LogSource | None = None,
    triage_agent=None,
) -> FastAPI:
    settings = settings or load_settings()
    log = configure_logging(settings.tags(), settings.log_file, name="orchestrator")
    store = IncidentStore(settings.db_path)
    metrics = metrics or PrometheusMetricSource(settings.prometheus_url)
    logs = logs or LokiLogSource(settings.loki_url)

    skip_reason = "AI triage is turned off (TRIAGE_ENABLED=false)"
    if triage_agent is None and settings.triage_enabled:
        if os.environ.get("ANTHROPIC_API_KEY"):
            # The agent gets the same read-only tools as the MCP server, nothing more.
            triage_agent = TriageAgent(make_toolbox(store, metrics, logs), log=log)
        else:
            skip_reason = "No ANTHROPIC_API_KEY set, so AI triage was skipped"

    service = IncidentService(
        store=store,
        metrics=metrics,
        logs=logs,
        log=log,
        triage_agent=triage_agent,
        triage_skip_reason=skip_reason,
    )
    app = FastAPI(title="Sentinel Orchestrator", version=settings.version)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", **settings.tags()}

    @app.post("/alerts")
    def receive_alerts(webhook: AlertmanagerWebhook, background: BackgroundTasks) -> WebhookResult:
        """Alertmanager webhook. Replies at once; evidence is gathered just after."""
        result = service.handle(webhook)
        for incident_id in result.opened:
            background.add_task(service.investigate, incident_id)
        return result

    @app.get("/incidents")
    def list_incidents() -> list[Incident]:
        return service.store.list()

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: int) -> Incident:
        incident = service.store.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident

    log.info("orchestrator started")
    return app
