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

## AI triage

After the evidence is gathered, `triage.py` asks Claude (Opus 5.5 by default) to investigate
with the same four read-only tools as the MCP server, and stores a diagnosis on the incident
(`triage`: cause, recommendation, reasoning, confidence, evidence, tools used, tokens).

- Needs `ANTHROPIC_API_KEY` in `.env` (`make run-orchestrator` loads it). Without it,
  triage is recorded as `skipped`. `TRIAGE_ENABLED=false` turns it off.
- Limits: 12 look-ups and 8 rounds per incident. Failures are recorded, never raised.
- Typical cost: about $0.20 per incident (measured: 46k input, 1.5k output tokens).
