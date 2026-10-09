# MCP server (read-only tools)

The toolbox the Claude triage agent uses to investigate incidents. Every tool only looks
things up; there is deliberately no tool that can change, restart, roll back or delete anything.

| Tool | What it does |
|---|---|
| `list_incidents` | Open (or all) incidents, newest first, without evidence |
| `get_incident` | One incident with its evidence: metrics, error logs, gaps |
| `query_metrics` | A PromQL query against Prometheus (max 50 series) |
| `search_logs` | A LogQL search in Loki (max 100 lines, max 24 h back) |

Data comes through the same read-only adapters the orchestrator uses (HTTP GET only).
Tools are marked `read_only_hint=True` and tests check that no write tools exist.

**Run:** Claude Code starts it automatically from `.mcp.json` (`python -m mcp_server`, over
stdio) after you approve it. It needs the monitoring stack (`make infra-up`) and the
orchestrator (`make run-orchestrator`) running to return real data.

Note: the folder is `mcp_server`, not `mcp`, so it doesn't clash with the official `mcp` library.
`.mcp.json` points at the Windows venv path (`.venv/Scripts/python.exe`); on macOS/Linux use
`.venv/bin/python`.
