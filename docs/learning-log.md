# Learning log

Running notes on what I learned at each step.

## 2026-10-06 – Repo skeleton
- Set up folders, pre-commit (ruff, black, gitleaks) and a Makefile.
- gitleaks blocks commits that contain secrets.

## 2026-10-07 – ShopLite minimal app
- An **API endpoint** is a URL plus a method (GET to read, POST to send data) that a program answers.
  ShopLite has three: `GET /health`, `GET /products`, `POST /checkout`.
- **Structured logs** are JSON instead of free text, so tools like Loki can filter by field
  (e.g. "all errors where version=0.1.0"). Every line carries service, env, version, commit_sha.
- **Fault injection**: `SHOPLITE_FAIL_RATE` makes a share of checkouts fail on purpose, so we
  have realistic incidents to detect and triage later.
- FastAPI generates a clickable test page at `/docs` automatically.

## 2026-10-07 – Metrics, Prometheus and Grafana
- **Metrics** are numbers over time (counts, durations), unlike logs which are individual events.
  ShopLite exposes them at `/metrics`: request counts by route and status, and response times.
- **Error rate** = 5xx requests / all requests. We don't need a separate error counter.
- **Prometheus** pulls ("scrapes") `/metrics` every 5 s and stores the history.
  **Grafana** draws dashboards from Prometheus queries (PromQL), e.g. `rate(...[1m])` = per-second rate over the last minute.
- **p95 response time**: 95% of requests were faster than this. Better than an average, which hides slow outliers.
- **Docker** runs Prometheus and Grafana in containers, so nothing is installed on the PC itself.
  Containers reach programs on the PC via `host.docker.internal`.
- Gotcha: on Windows, `localhost` tries IPv6 first; ShopLite listens on IPv4 only, so use `127.0.0.1` to avoid 2 s delays.
- Gotcha: starting `make run-shoplite` in several windows leaves several servers fighting over port 8000.

## 2026-10-07 – Logs in Grafana (Loki)
- **Metrics say *that* something broke; logs say *what* broke.** The error-rate chart and the
  error-log panel now sit on the same dashboard.
- **Loki** stores log lines and indexes only a few **labels** (service, env, version,
  commit_sha, level), which keeps it cheap. **Alloy** tails `logs/shoplite.log` and ships each line.
- **LogQL** examples: `{service="shoplite", level="ERROR"}` = all error lines;
  `{level="ERROR", commit_sha="f9892ad"}` = errors from one exact build.
- Verified end to end: 108 failed checkouts produced exactly 108 ERROR lines in Loki.
- `make infra-check` validates the configs with each tool's own checker before starting anything.

## 2026-10-07 – Alerts
- An **alert rule** is a saved question Prometheus asks every few seconds:
  "is the error rate above 20%?" The **`for: 1m`** clause means it must stay true for a full
  minute before firing, so a single blip doesn't wake anyone up.
- Alert life cycle: **inactive → pending** (condition true, waiting out the minute) **→ firing →
  resolved** (condition false again). Verified live: fired at 66 s, resolved ~1 min after errors stopped.
- **Alertmanager** routes fired alerts to receivers (today: a tiny inbox; next: the orchestrator; later: Slack).
  It batches related alerts and re-sends at most hourly, so people aren't spammed.
- **`promtool test rules`** unit-tests an alert with fake data: fires at 40% errors,
  stays quiet at 5% and with no traffic.
- Lesson: a ShopLite left running from yesterday was still failing at 50% and blocked port 8000.
  The alert even named its older commit (f9892ad), which is exactly the clue triage needs.
  Stop servers with Ctrl+C when done.
- Note: the RESOLVED message repeats the last measured error rate; that's normal for Alertmanager.

## 2026-10-08 – Orchestrator
- An **alert** is a signal; an **incident** is the record we manage: it has an ID, a status
  (open → resolved), and the **evidence** needed to diagnose it.
- **De-duplication**: Alertmanager re-sends a firing alert; each alert has a *fingerprint*, so a
  repeat updates the open incident instead of creating a new one.
- **Adapters** (`MetricSource`, `LogSource`) hide *which* tool we use. Swapping Prometheus for
  Datadog later means writing one new adapter, not rewriting the orchestrator. Ours are
  read-only: HTTP GET queries only.
- **Graceful degradation**: if Loki or Prometheus is down, the incident still opens and the
  missing pieces are listed under `gaps`.
- Live run: incident opened 91 s after errors began, with error rate 36%, 20 error log lines,
  scoped to commit d7b289c; resolved automatically ~2 min after errors stopped.
- Lesson: `uvicorn --reload` runs a hidden worker process. Stopping only the parent leaves
  the worker serving. Stop with Ctrl+C in its window, or kill the whole process tree.

## 2026-10-08 – Read-only MCP tools for the AI
- **MCP (Model Context Protocol)** is the standard way to give Claude tools. Our server offers
  four, all *look-up only*: `list_incidents`, `get_incident`, `query_metrics`, `search_logs`.
- **Safety by design**: there is simply no tool that can change anything; each tool is marked
  `read_only_hint`, and tests fail if a write-sounding tool ever appears. Limits cap answer size
  (50 series, 100 log lines, 24 h back).
- **One way to read data**: the tools reuse the orchestrator's read-only adapters (HTTP GET only).
- **Helpful errors**: a broken query returns the source's own explanation
  ("unclosed left parenthesis"), so the AI can fix its query instead of guessing.
- Live run: through the tools alone we found incident #1 (31% errors, commit ff69356), saw that
  only `/checkout` fails (~46%), and read the "checkout failed: injected fault" lines.
- `.mcp.json` tells Claude Code how to start the server; Claude Code asks you to approve it
  the first time a session starts in this folder.
- The folder is `mcp_server/`, not `mcp/`: a folder named `mcp` would hide the official library.
