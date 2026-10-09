"""Start the Sentinel MCP server over stdio: `python -m mcp_server`."""

from mcp_server.incidents import OrchestratorIncidentSource
from mcp_server.server import build_server
from mcp_server.settings import load_settings
from orchestrator.adapters.loki import LokiLogSource
from orchestrator.adapters.prometheus import PrometheusMetricSource


def main() -> None:
    settings = load_settings()
    server = build_server(
        incidents=OrchestratorIncidentSource(settings.orchestrator_url),
        metrics=PrometheusMetricSource(settings.prometheus_url),
        logs=LokiLogSource(settings.loki_url),
    )
    server.run("stdio")


if __name__ == "__main__":
    main()
