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
