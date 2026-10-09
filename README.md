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

## Roadmap

Agreed scope: see [docs/decisions/0001-core-demo-scope.md](docs/decisions/0001-core-demo-scope.md).

| Step | Status |
|---|---|
| Repo foundation, quality and secret checks | Done |
| ShopLite demo shop (tagged JSON logs, fault injection) | Done |
| Metrics, Prometheus and Grafana dashboard | Done |
| Logs in Grafana (Loki) | Done |
| Alerts | Done |
| Orchestrator | Done |
| Read-only MCP tools | Done |
| Claude triage agent | Done |
| Evals | Next |
| Slack: diagnosis + human approval | |

Deferred: web dashboard, Jira, ServiceNow, Harness (rollback simulated first).

## Monitoring locally

```bash
make infra-up        # Prometheus :9090, Grafana :3000, Loki :3100, Alertmanager :9093
make run-shoplite    # in one terminal
make traffic         # in another; set SHOPLITE_FAIL_RATE=0.5 before run-shoplite to see errors
make alerts          # watch alerts arrive (fires after 1 min above 20% errors)
make run-orchestrator  # in another terminal: turns alerts into incidents (port 8001)
make incidents       # list incidents with their evidence
make infra-down
```
