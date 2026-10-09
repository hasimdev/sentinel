"""Sentinel MCP server: READ-ONLY investigation tools for the triage agent.

Every tool only looks things up. There is deliberately no tool that can change,
restart, roll back or delete anything (see CLAUDE.md: the agent reads and recommends only).
"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from mcp_server.tools import IncidentSource, make_toolbox
from orchestrator.adapters.base import LogSource, MetricSource

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

INSTRUCTIONS = """\
Read-only tools for investigating Sentinel incidents. Start with list_incidents, then
get_incident for the evidence. Use query_metrics (PromQL) and search_logs (LogQL) to dig
further. Useful labels: service, env, version, commit_sha, level, route, status.
Example metric: shoplite_http_requests_total. These tools cannot change anything."""


def build_server(incidents: IncidentSource, metrics: MetricSource, logs: LogSource) -> MCPServer:
    server = MCPServer(name="sentinel", instructions=INSTRUCTIONS)
    for func in make_toolbox(incidents, metrics, logs).values():
        server.tool(annotations=READ_ONLY)(func)
    return server
