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

## ServiceNow and Harness: how changes flow

Sentinel follows a real change process. Each new incident gets a ServiceNow **incident**; the
AI's diagnosis is added as a work note. Priority comes from the alert, never from the AI: a
`critical` alert is a **P1/P2**, a `warning` alert (more than 5% errors) a **P3/P4**.

| Situation | Ticket | Who approves | What runs |
|---|---|---|---|
| **Roll back** a bad release | **None new**: it is the backout of the release's own change | Someone in the ServiceNow **on-call group** (`ShopLite On-Call`), one click | Harness redeploys the previous version as "Backout of CHG..." |
| **Fix**, P1/P2 | **Emergency** change | On call clicks **Deploy fix now**; the **ECAB reviews afterwards** | Harness deploys the fix right away, then the change goes to the ECAB |
| **Fix**, P3/P4 | **Normal** change | **Change Management**, then the **CAB**, before anything deploys | Harness deploys once the CAB approves |
| **Release** | **Normal** change (`make release-change`) | Change Management, then the CAB, in ServiceNow | `make deploy VERSION=... CHANGE=CHG...` |

**Rollback.** Every deploy records the change it ran under (`logs/deploys.log`). The rollback
target and the change it backs out both come from that history, not the AI. Sentinel notes
"Backed out by incident #N" on the release's change and on the incident.

**Fix.** An engineer ships a fix version and runs `make fix VERSION=0.2.1 INCIDENT=7`. Sentinel
raises the change the priority calls for, links it to the incident, and posts a message with
the right buttons. Approvals are ServiceNow's call: a click counts only if ServiceNow is waiting
on **that person** (matched by their Slack email), and it is recorded under their own name.
Anyone else gets a private "you're not an approver" reply. A normal change moves itself from
Change Management to the CAB, then to *Scheduled*; Sentinel deploys, checks, and closes it. An
emergency change is closed after the ECAB review with "implemented as an emergency fix before
review".

**Validation.** After any rollback or fix, Sentinel checks that the version's error rate stays
under 5% for 2 minutes, then says "validated" in Slack and on the incident. If it doesn't
settle, a person is asked to check.

**Safety.** The AI has no write access to anything. Harness runs only after a verified human
click. ServiceNow trouble never breaks incident handling (logged as `servicenow failed`), and a
click that can't be verified is refused, never recorded unverified.

**Settings.** `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD`; optional
`SERVICENOW_CHANGE_GROUP` (default `Change Management`) and `SERVICENOW_ONCALL_GROUP` (default
`ShopLite On-Call`). The integration user needs `itil` and `approval_admin` (to record an
approval on an approver's behalf). Harness: `HARNESS_ACCOUNT_ID`, `HARNESS_API_KEY`; optional
`HARNESS_ORG`, `HARNESS_PROJECT`, `HARNESS_PIPELINE`, `DEPLOY_LOG_PATH`. Without both ServiceNow
and Harness, a rollback approval is only simulated. Setup: deploy/README.md.

**Instance quirks learned the hard way.** State changes must go through the Change Management
API (`/api/sn_chg_rest/change/...`); the Table API is blocked by a state-transition rule.
Moving to *Assess* or *Authorize* needs an assignment group. On this instance the CAB (and the
emergency CAB) is the `CAB Approval` group, and normal changes ask `Change Management` first.
Demo data reuses change numbers, so Sentinel looks changes up by record ID.

## Measuring the AI + human loop

`GET /metrics` (scraped by Prometheus as job `sentinel`) publishes numbers worked out from
every incident in the store at scrape time, so restarts lose nothing: incidents by AI
recommendation, decisions (agreed / overrode), rollbacks by result, and histograms plus
all-time medians for **time to diagnose** (alert → AI recommendation), **time to decide**
(AI recommendation → human click), **rollback duration** and **time to recover**. Grafana's
"Sentinel – AI & approvals" dashboard (`/d/sentinel`) shows them. See `orchestrator/insights.py`.
