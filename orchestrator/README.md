# Orchestrator

FastAPI service (port 8001) that turns alerts into incidents with evidence attached.

- `POST /alerts`: Alertmanager webhook. Opens an incident per new alert, de-duplicates
  repeats, and resolves it when the alert clears. Evidence is gathered just after replying.
- `GET /incidents`, `GET /incidents/{id}`: incidents, newest first, with evidence.
- Evidence (read-only): error rate, request rate and p95 latency from Prometheus, plus the
  latest ERROR log lines from Loki, all scoped to the failing version and commit.
  Anything that can't be collected is listed under `gaps` instead of failing.
- Adapters: `MetricSource` (Prometheus) and `LogSource` (Loki) in `adapters/`. Read-only:
  they only send HTTP GET queries.
- Incidents are stored in SQLite (`data/orchestrator.db`, gitignored).

Run: `make run-orchestrator`, then open http://127.0.0.1:8001/docs or run `make incidents`.
