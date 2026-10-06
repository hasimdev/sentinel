# Sentinel

Sentinel is an AI-assisted incident and release platform. A demo shop app (ShopLite) is
observed with Grafana, Prometheus and Loki; when something goes wrong, a FastAPI
orchestrator hands the evidence to a Claude triage agent that uses read-only MCP tools to
investigate and recommend a fix or rollback. Releases and rollbacks run through Harness,
incidents and CAB approvals through ServiceNow, work items through Jira, and updates go to
Slack. The AI only reads and recommends; humans approve every change.

## Getting started

```bash
cp .env.example .env   # then fill in values locally; .env is never committed
make dev               # create .venv, install tools, enable pre-commit hooks
make lint              # ruff, black, gitleaks
make test              # pytest
```
