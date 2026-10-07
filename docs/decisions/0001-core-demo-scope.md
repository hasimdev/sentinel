# 0001 – Build the core demo first; Slack as the first business tool

- Status: Accepted
- Date: 2026-10-07

## Context
The full vision has ~13 parts (ShopLite, observability, orchestrator, adapters, MCP tools,
triage agent, evals, ServiceNow, Jira, Slack, Harness, web UI). AI incident triage is a
crowded commercial space, so the value of this project is learning, portfolio and an
internal pitch, which needs **one complete story working end to end**, not every integration.

## Decision
Build the core demo, in this order:
1. Logs in Grafana (Loki)
2. Alerts
3. Orchestrator
4. Read-only MCP tools (logs, metrics, deploys)
5. Claude triage agent (reads and recommends only)
6. Evals for the agent
7. Slack: post the diagnosis and ask a human to approve the rollback

Slack is the first business tool because it is the fastest to set up and gives the clearest
demo moment. Only the adapters actually used get built (LogSource, MetricSource, Chat).

Deferred until the core works: web dashboard, Jira, ServiceNow, Harness (rollback is
simulated at first), and the remaining adapters.

## Consequences
- The demo story is: bad release -> alert -> AI diagnosis -> recommendation in Slack ->
  human approves.
- Deferred integrations can be added later behind their adapters without reworking the core.
- ServiceNow / CAB, a strong enterprise story, is postponed; revisit after step 7.
