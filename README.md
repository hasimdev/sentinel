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
| Evals | Done |
| Slack: diagnosis + human approval | Done |

Deferred: web dashboard.

### Part 2: AI meets change management

| Step | Status |
|---|---|
| A. Versioned releases (good 0.1.0, buggy 0.2.0), make deploy / rollback | Done |
| B. Jira follow-up tickets | Done |
| C. ServiceNow incident, emergency change, CAB approval from Slack | Done |
| D. Harness deploy and real rollback after Slack approval | Done |
| E. Measure the approval step (Grafana), full demo (docs/demo.md), Part 2 post | Done |

### Part 3: the real change process

| Step | Status |
|---|---|
| Rollback with no new ticket: backout of the release change, on-call approval, validation | Built |
| Fixes by priority: P1/P2 emergency change (ECAB afterwards), P3/P4 normal change (CAB first) | Built |
| Releases under a normal change; warning alert for P3/P4 | Built |
| Live demo of every path, then the Part 2 post | Next |

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
