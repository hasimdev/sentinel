"""Sentinel orchestrator: receives alerts, opens incidents, attaches evidence and AI triage."""

import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from pydantic import BaseModel

from common.logging_setup import configure_logging
from mcp_server.tools import make_toolbox
from orchestrator.adapters.base import LogSource, MetricSource
from orchestrator.adapters.change_mgmt import ChangeMgmt, ServiceNowChangeMgmt
from orchestrator.adapters.chat import Chat, SlackChat
from orchestrator.adapters.deploy_control import DeployControl, HarnessDeployControl
from orchestrator.adapters.loki import LokiLogSource
from orchestrator.adapters.prometheus import PrometheusMetricSource
from orchestrator.adapters.ticketing import JiraTicketing, Ticketing
from orchestrator.insights import IncidentMetrics
from orchestrator.models import AlertmanagerWebhook, FixDeploy, Incident
from orchestrator.service import FixRefused, IncidentService, WebhookResult
from orchestrator.settings import Settings, load_settings
from orchestrator.store import IncidentStore
from orchestrator.triage import TriageAgent


class FixRequest(BaseModel):
    version: str
    requested_by: str = "an engineer"


def create_app(
    settings: Settings | None = None,
    metrics: MetricSource | None = None,
    logs: LogSource | None = None,
    triage_agent=None,
    chat: Chat | None = None,
    ticketing: Ticketing | None = None,
    change_mgmt: ChangeMgmt | None = None,
    deploy_control: DeployControl | None = None,
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

    use_slack = chat is None and settings.slack_ready()
    if use_slack:
        chat = SlackChat(settings.slack_bot_token.get_secret_value(), settings.slack_channel)

    if ticketing is None and settings.jira_ready():
        ticketing = JiraTicketing(
            settings.jira_base_url,
            settings.jira_email,
            settings.jira_api_token.get_secret_value(),
            settings.jira_project_key,
        )

    if change_mgmt is None and settings.servicenow_ready():
        change_mgmt = ServiceNowChangeMgmt(
            settings.servicenow_instance_url,
            settings.servicenow_username,
            settings.servicenow_password.get_secret_value(),
            settings.servicenow_change_group,
        )

    if deploy_control is None and settings.harness_ready():
        deploy_control = HarnessDeployControl(
            settings.harness_account_id,
            settings.harness_api_key.get_secret_value(),
            settings.harness_org,
            settings.harness_project,
            settings.harness_pipeline,
        )

    service = IncidentService(
        store=store,
        metrics=metrics,
        logs=logs,
        log=log,
        triage_agent=triage_agent,
        triage_skip_reason=skip_reason,
        chat=chat,
        dashboard_url=f"{settings.grafana_url.rstrip('/')}/d/shoplite",
        ticketing=ticketing,
        change_mgmt=change_mgmt,
        deploy_control=deploy_control,
        deploy_log=settings.deploy_log_path,
        oncall_group=settings.servicenow_oncall_group,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        handler = None
        if use_slack:
            # Imported here so tests and Slack-less runs never load the Slack connection code.
            from orchestrator.slack_listener import start_socket_mode

            try:
                handler = start_socket_mode(
                    settings.slack_bot_token.get_secret_value(),
                    settings.slack_app_token.get_secret_value(),
                    service,
                    log,
                )
                log.info("slack connected", extra={"fields": {"channel": settings.slack_channel}})
            except Exception as exc:  # Slack trouble must not stop incident handling
                log.error("slack connection failed", extra={"fields": {"error": str(exc)}})
        yield
        if handler is not None:
            handler.close()

    app = FastAPI(title="Sentinel Orchestrator", version=settings.version, lifespan=lifespan)
    app.state.service = service
    # Metrics about the AI + human loop, worked out from the incident store (see insights.py).
    registry = CollectorRegistry()
    registry.register(IncidentMetrics(store, settings.tags()))

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", **settings.tags()}

    @app.post("/alerts")
    def receive_alerts(webhook: AlertmanagerWebhook, background: BackgroundTasks) -> WebhookResult:
        """Alertmanager webhook. Replies at once; evidence is gathered just after."""
        result = service.handle(webhook)
        for incident_id in result.opened:
            background.add_task(service.investigate, incident_id)
        for incident_id in result.resolved:
            background.add_task(service.on_resolved, incident_id)
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

    @app.post("/incidents/{incident_id}/fix")
    def request_fix(incident_id: int, request: FixRequest) -> FixDeploy:
        """An engineer has a fix version ready (`make fix`). Sentinel raises the change its
        priority calls for and asks the right people in Slack. Nothing deploys until they do."""
        try:
            return service.request_fix(incident_id, request.version, request.requested_by)
        except FixRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    log.info("orchestrator started")
    return app
