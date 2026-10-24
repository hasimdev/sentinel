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

## 2026-10-09 – AI triage agent
- When an incident opens, the orchestrator gathers evidence and then asks **Claude Opus 5.5**
  to investigate with the four read-only tools. Claude returns a structured diagnosis:
  likely cause, recommendation (rollback / investigate / no_action), reasoning, confidence,
  evidence. It is stored on the incident; a human decides.
- **Guardrails**: only the four look-up tools (unknown tools are refused), max 12 look-ups and
  8 rounds per incident, and every failure (no key, no credit, rate limit, refusal, bad answer)
  is recorded on the incident instead of breaking it. Tests use a simulated Claude: free.
- **Live result** (incident at 38% errors): 5 look-ups, 21 seconds, $0.21. Claude noticed every
  error said "injected fault", that fail_rate=0.5 was set at every startup, and that the
  *previous* commit failed just as much, so it recommended **investigate, not rollback**,
  because rolling back wouldn't help. That's the kind of judgement a simple rule would miss.
- Cost: Opus 5.5 is $4 per million input tokens and $20 per million output. Most of the cost is
  input: each round re-sends the conversation, including tool results.
- Lessons from the first attempts: an API key must be copied whole (about 100 characters);
  the API account needs its own credit, separate from a Claude subscription; and Docker
  Desktop can freeze, in which case restart it.

## 2026-10-09 – Evals: testing the AI's judgement
- An **eval** is an exam with an answer key: 16 practice incidents (5 rollback,
  7 investigate, 4 no action), each a scripted world of deploys, traffic, errors and logs.
  The real AI investigates each one; we score the call and the cause.
- **Marking:** the recommendation is checked automatically; an AI marker (Claude Sonnet 5.5,
  a different model) judges the cause. It was tested first on four sample answers (empty,
  "I don't know", confidently wrong, correct but reworded) and marked all four right.
- **The pilot paid for itself:** the first small run found that the AI didn't know the
  current time, so it couldn't tell "already fixed" from "just happened". The fix (tell it
  the time) took the "blip that cleared" case from wrong to right.
- **Half the "AI mistakes" were test bugs:** twice the AI was right about my scripted data
  (metrics ignored the time window; a traffic "surge" that never rose). Always read a failing
  answer before blaming the AI.
- **Final baseline:** right call 32/32, right cause 32/32, no wrong rollbacks, about 18 s and
  $0.19 per diagnosis. At 100% the test can't show further gains, so add harder cases first.
- A **safety gate** stops the test after its code changes until a person re-approves it.

## 2026-10-11 – Slack: the human decides
- Each diagnosed incident is posted to #new-channel with the AI's cause, evidence and
  buttons. A click records **who decided and when** on the incident; the message updates so
  nobody decides twice. "Approve rollback" is **simulated** until a deploy tool is connected.
- **Socket Mode**: the app connects *out* to Slack, so the laptop needs no public address.
- **Tokens are passwords.** A token pasted into chat is exposed: we revoked it and made a new
  one. Revoking all tokens also uninstalls the app, which removes the bot from its channels,
  so it had to be added back. `.env` is the only place for tokens; `.env.example` stays blank.
- **Leaving a failing system running costs money:** the shop failed for hours, the alert kept
  re-firing, and each new incident triggered a paid AI diagnosis (about $0.20 each).
  Stop demos when done.
- Live result: incidents #2 and #3 were posted automatically and both decisions
  ("agreed to investigate") were recorded with the person's Slack identity.

## 2026-10-15 – Part 2, Step A: real releases
- A **release** is a numbered, tagged version of the code (`shoplite-v0.1.0`), packaged as a
  Docker image stamped with its version and commit. Deploying swaps the running version;
  **rolling back is just deploying the older one**.
- 0.2.0 has a **real bug**: a promo-price feature crashes for products without a promo.
  Its test passed because it only checked a product that had one. That's how real bugs ship.
  It lives on its own branch, so `main` stays clean.
- Live run: 0.1.0 had no errors; deploying 0.2.0 raised errors to 35%, the incident opened
  in 109 s, and Claude recommended **roll back** with high confidence (3 look-ups, $0.10),
  naming the `KeyError` in `promo_price`. A manual rollback to 0.1.0 brought errors to zero
  and the incident resolved itself 76 s later.
- Every deploy and rollback is logged with the four tags, so deploy history is evidence too.

## 2026-10-15 – Part 2, Step B: Jira follow-up tickets
- Rolling back fixes the *symptom*. Someone still has to fix the *cause*, so when an
  incident resolves, Sentinel files a Jira task with the AI's diagnosis, evidence, versions
  and timeline, and posts the ticket number in the Slack thread. One ticket per incident.
- The `Ticketing` adapter means Jira could be swapped (e.g. for ServiceNow tasks) without
  touching the rest.
- Jira now calls projects "spaces"; the project key (SEN) is the ticket prefix.
- Live run: bad release, AI said roll back (high confidence), manual rollback, resolved,
  **SEN-1** filed with the exact `KeyError` in `promo_price` as its cause.

## 2026-10-16 – Part 2, Step C: ServiceNow and CAB approval from Slack
- Every incident now has a **ServiceNow incident** with the AI's diagnosis as a work note.
  When the AI says roll back, Sentinel raises an **emergency change** and ServiceNow asks the
  **CAB** to approve it. The rollback can't happen on the AI's word alone.
- **Who may approve is ServiceNow's call, not Sentinel's.** A Slack click counts only if
  ServiceNow is waiting on that person's approval (matched by email). Their approval is
  recorded **under their own name**; anyone else gets "you're not an approver".
- Real-instance surprises: state changes must go through the Change Management API; the
  emergency CAB on this instance is the `CAB Approval` group, not `eCAB Approval`; demo data
  reuses change numbers, so Sentinel always works by record ID; a field the integration user
  can't see is **silently ignored**, so the INC-CHG link is a work note on each record.
- Docker's Linux engine (WSL) hung again; `wsl --shutdown` and a restart fixed it.
- Live run: bad release, INC0010002 opened, AI said roll back (high confidence), CHG0030007
  went to the CAB. One click in Slack, and ServiceNow showed **"Approved in Slack by
  hasim.abu, via Sentinel"** on Abu Ansari's approval; the change moved to Scheduled and the
  other CAB approvals to "No longer required". After rollback the INC resolved itself with
  the cause, the decision and the Jira follow-up **SEN-2** in its close notes.

## 2026-10-17 – Part 2, Step D: Harness does the real rollback
- After a CAB approver clicks Approve, Sentinel now **actually rolls back**: it runs a Harness
  pipeline, which a **delegate** (Harness's worker, in Docker on the laptop) carries out with
  the same deploy code as `make deploy`. The change moves to Implement, then Closed with the
  Harness run as proof.
- **Which version to roll back to comes from the deploy history**, not from the AI.
- **Least privilege, proven:** the orchestrator's Harness key can run the pipeline but not
  edit it; Harness itself refused the edit ("missing core_pipeline_edit"). To create the
  pipeline we granted edit rights for two minutes, then removed them and re-checked.
- **Pipeline as code:** Harness's new editor rejected our classic YAML, so the pipeline is
  created from the file in git through the API. The repo is the source of truth.
- Pipeline inputs reach the script as environment variables, never pasted into the script,
  so a crafted "version" can't run commands. A test enforces it.
- Secrets slip-ups, again: a screenshot showed the delegate token (revoked, replaced), and a
  duplicate empty line in `.env` (mine) hid the real token. Check `.env` by key, not by eye.
- Live run (incident #3): bad release 06:19 → alert 06:20:33 → AI said roll back (high
  confidence) and CHG0030009 went to the CAB at 06:21 → **approved in Slack 06:24:06** →
  Harness rolled back to 0.1.0 in **16 s** → change **Closed / Successful** with the run link
  → INC0010003 resolved 06:25 → Jira **SEN-3**. Human decision time: ~3 minutes; machine
  time after the click: ~1 minute.

## 2026-10-24 – Part 3: the real change process
- Rebuilt the change flow around how change management actually works (the user ran a CAB
  for years): **a rollback needs no new ticket**. It is the backout of the change the
  release went out under, so it only needs someone **on call** (checked in ServiceNow).
- **Fixes** go out under the change their **priority** calls for, and priority comes from the
  alert, never the AI: P1/P2 → **emergency** change, deployed on the on-call click and reviewed
  by the **ECAB afterwards**; P3/P4 → **normal** change, approved by **Change Management, then
  the CAB**, before anything deploys. Releases go out under a normal change too.
- Every deploy records its change number, and Sentinel **validates** each rollback or fix:
  errors under 5% for 2 minutes before it says "validated".
- A **warning** alert (over 5% errors) creates P3 incidents; while the critical alert fires,
  Alertmanager silences the warning, so one problem never opens two incidents.
- Live run, all three paths: release 0.2.0 under CHG0030015 (CAB-approved) → incident #7 →
  on-call click → rollback **backing out CHG0030015** in 27 s, validated. Then fix 0.2.1 under
  emergency **CHG0030016**: deployed on the on-call click, validated, **ECAB reviewed
  afterwards**, closed. Then a milder outage (P3, incident #8) → normal **CHG0030017** →
  Change Management, then CAB → deployed, validated, closed.
- **The live run found a real gap:** on incident #8 the rollback and the fix were both
  approved at the same moment and two deploys ran at once. Both landed on the good version,
  but that was luck. Now each incident allows **one deploy at a time**; a second deploy click
  is refused until the first finishes (and validation is part of "finished").
- Small lessons: a first click that seemed to "do nothing" was answered with a private note
  that wasn't logged. Refused clicks are now logged. Harness offered no "Project Member" role
  this time, so the pipeline edit used Project Admin for two minutes, then removed and
  re-checked (403).
