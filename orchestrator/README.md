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

## Slack (people decide)

After triage, the incident is posted to a Slack channel with the AI's diagnosis, its
evidence, and buttons: **Approve** (worded after the recommendation, e.g. "Approve rollback")
and **Reject**, plus a link to the Grafana dashboard. The first click is recorded on the
incident (`decision`: who, when, outcome) and the message updates; later clicks change nothing.
When the alert clears, the message shows "Resolved" and a reply is added in its thread.

- **Approve rollback is simulated:** no deploy tool is connected yet, so nothing is rolled back.
- **Socket Mode:** Sentinel connects out to Slack, so no public address or tunnel is needed.
- **Setup:** create the app from `infra/slack/manifest.yaml`, add it to the channel, and put
  `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_CHANNEL` in `.env` (never in chat or git).
  Without all three, Slack is simply off.
- **Fails safe:** if Slack is unreachable or the bot isn't in the channel, the incident is
  unaffected and the reason is logged (`chat failed`). A failed post is not retried
  automatically yet.

## Jira (follow-up tickets)

When an incident resolves and the AI found a cause worth fixing (not "no action"), Sentinel
files one follow-up ticket: the AI's diagnosis and evidence, the version and commit, the
timeline (opened, resolved, who decided) and a dashboard link, labelled `sentinel`, the
service and the version. The ticket key is stored on the incident (`followup`) and posted in
the incident's Slack thread. Needs `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` and
`JIRA_PROJECT_KEY` in `.env`; without them, tickets are off. Jira trouble never affects the
incident (logged as `ticket failed`).

## ServiceNow (incident record, emergency change, CAB approval)

Each new incident gets a ServiceNow **incident**; the AI's diagnosis is added as a work note.
When the AI recommends a **rollback**, Sentinel raises an **emergency change**, links it to the
incident, assigns it to the `Change Management` group and moves it to *Authorize*, so
ServiceNow asks its CAB to approve it. The Slack message links both records.

Who may decide is ServiceNow's call, not Sentinel's: a Slack click on a change counts only if
ServiceNow is waiting on **that person's** approval. Sentinel reads the clicker's email from
Slack (`users:read.email`), finds the matching ServiceNow user, and records their approval or
rejection on their own approval request ("Approved in Slack by ..., via Sentinel"). Anyone
else gets a private "you're not an approver" reply and nothing is recorded. A rejection
cancels the change. When the alert clears, the incident is resolved ("Workaround provided")
and a change nobody decided on is cancelled. The rollback itself is still simulated until a
deploy tool is connected (Part 2, Step D).

Needs `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME` and `SERVICENOW_PASSWORD` in `.env`
(optional `SERVICENOW_CHANGE_GROUP`). The integration user needs the `itil` role, plus
`approval_admin` so it can record an approval on an approver's behalf. ServiceNow trouble
never affects the incident (logged as `servicenow failed`); a click that can't be verified is
refused, never recorded unverified.

Instance quirks learned the hard way: change state changes must go through the Change
Management API (`/api/sn_chg_rest/change/...`), as the Table API is blocked by a
state-transition rule; moving to *Authorize* needs an assignment group; on this instance the
emergency CAB is the `CAB Approval` group.

## Harness (real rollback after CAB approval)

When a CAB approver approves the change, the rollback really happens. Sentinel takes the
target from the deploy history (the version that ran before the bad one, not the AI's word),
moves the change to *Implement*, runs the Harness `shoplite_deploy` pipeline with that version,
watches it, and closes the change as *successful* (with the Harness run link) or
*unsuccessful*. Slack shows each step in the incident's thread and message. If there's no known
previous version or Harness won't start, nothing is deployed, the change is cancelled or
closed as unsuccessful, and Slack says a person must take over.

Real rollbacks need both Harness (`HARNESS_ACCOUNT_ID`, `HARNESS_API_KEY`) and ServiceNow:
without a ServiceNow-verified approval, rollbacks stay simulated. Optional settings:
`HARNESS_ORG`, `HARNESS_PROJECT`, `HARNESS_PIPELINE`, `DEPLOY_LOG_PATH`. Setup: deploy/README.md.

## Measuring the AI + human loop

`GET /metrics` (scraped by Prometheus as job `sentinel`) publishes numbers worked out from
every incident in the store at scrape time, so restarts lose nothing: incidents by AI
recommendation, decisions (agreed / overrode), rollbacks by result, and histograms plus
all-time medians for **time to diagnose** (alert → AI recommendation), **time to decide**
(AI recommendation → human click), **rollback duration** and **time to recover**. Grafana's
"Sentinel – AI & approvals" dashboard (`/d/sentinel`) shows them. See `orchestrator/insights.py`.
