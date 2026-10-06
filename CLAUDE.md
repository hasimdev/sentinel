# Sentinel – project rules

## What this is
An AI-assisted incident and release platform: ShopLite demo app, Grafana/Prometheus/Loki,
a FastAPI orchestrator, a Claude triage agent with read-only MCP tools, Harness for
release and rollback, ServiceNow for incidents and CAB, Jira for work items, Slack.

## About me
I am a TPM learning to code. Before any change: show a plan in plain English.
After any change: explain what you built and how to verify it.

## Conventions
- Python 3.12, FastAPI, Pydantic, pytest. Format with ruff and black.
- Every log, metric and deploy carries: service, env, version, commit_sha.
- Integrations go behind adapters: LogSource, MetricSource, Ticketing, ChangeMgmt,
  DeployControl, Chat.
- Small steps. Write tests with every feature and run them before saying done.

## Never
- Commit secrets or hard-code tokens; use .env (gitignored) and env vars.
- Disable security checks, tests or linters to make something pass.
- Give the AI agent write access to any system. It reads and recommends only.
