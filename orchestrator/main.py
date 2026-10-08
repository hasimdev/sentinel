"""Sentinel orchestrator: receives alerts, opens incidents, attaches read-only evidence."""

from fastapi import BackgroundTasks, FastAPI, HTTPException

from common.logging_setup import configure_logging
from orchestrator.adapters.base import LogSource, MetricSource
from orchestrator.adapters.loki import LokiLogSource
from orchestrator.adapters.prometheus import PrometheusMetricSource
from orchestrator.models import AlertmanagerWebhook, Incident
from orchestrator.service import IncidentService, WebhookResult
from orchestrator.settings import Settings, load_settings
from orchestrator.store import IncidentStore


def create_app(
    settings: Settings | None = None,
    metrics: MetricSource | None = None,
    logs: LogSource | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    log = configure_logging(settings.tags(), settings.log_file, name="orchestrator")
    service = IncidentService(
        store=IncidentStore(settings.db_path),
        metrics=metrics or PrometheusMetricSource(settings.prometheus_url),
        logs=logs or LokiLogSource(settings.loki_url),
        log=log,
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
            background.add_task(service.attach_evidence, incident_id)
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
