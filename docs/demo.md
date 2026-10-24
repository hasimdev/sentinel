# Sentinel demo: a real change process, end to end

A bad release breaks checkout. The AI investigates and recommends a rollback; someone on call
approves it, and it runs with no new ticket (it backs out the release's change). Then an
engineer ships a fix, and it goes out under the change its priority calls for. Every record
updates itself. Costs one AI diagnosis per incident (about $0.10).

## Before you start (once)

- Docker Desktop running. If it hangs: quit it, `wsl --shutdown`, start it again.
- `.env` has the Anthropic, Slack, ServiceNow, Jira and Harness settings (see `.env.example`).
- Release images exist: `make release VERSION=0.1.0`, `0.2.0` and `0.2.1`.
- The Harness pipeline is current (`make harness-pipeline`, see deploy/README.md).
- In ServiceNow, your Slack email belongs to a user in **ShopLite On-Call**, **Change
  Management** and **CAB Approval**.

## Start

Three terminals:

```bash
make infra-up && make harness-up    # 1: monitoring + the Harness delegate
make run-orchestrator               # 2: Sentinel (Slack connects)
make traffic                        # 3: steady shop traffic
```

Deploy the good release, then raise and approve the bad release's change:

```bash
make deploy VERSION=0.1.0
make release-change VERSION=0.2.0   # approve it in ServiceNow: Change Management, then CAB
make deploy VERSION=0.2.0 CHANGE=CHG...
```

## Path 1: rollback, no new ticket (P1)

| When | What you see | Screenshot |
|---|---|---|
| ~1 min | Errors jump on 0.2.0; critical alert | Grafana (`/d/shoplite`) |
| ~1.5 min | Slack: AI cause, evidence, **Roll back**; ServiceNow INC | Slack message |
| you | Click **Approve rollback** (on-call check by ServiceNow) | Slack |
| +30 s | "Rolling back, backing out CHG..." then "Rollback done" | Harness run |
| +2 min | "Rollback validated"; backout note on the release change | ServiceNow CHG notes |

## Path 2: fix under an emergency change (P1/P2)

```bash
make fix VERSION=0.2.1 INCIDENT=<the incident number>
```

| When | What you see | Screenshot |
|---|---|---|
| now | Slack: **Emergency change CHG...**, button **Deploy fix now** | Slack fix message |
| you | Click **Deploy fix now** (on call) | |
| +30 s | Harness deploys 0.2.1; validated; change sent to the ECAB | Harness run |
| you | Click **Approve (ECAB review)** | |
| now | Change **Closed**: "implemented as an emergency fix before review" | ServiceNow CHG |

## Path 3: fix under a normal change (P3/P4)

For a P3 the alert must be the milder warning one. Restart traffic with fewer checkouts
(`make traffic CHECKOUT_SHARE=0.2`), deploy 0.2.0 again so errors sit around 10%, wait for the
warning incident, then `make fix VERSION=0.2.1 INCIDENT=<n>`.

| When | What you see |
|---|---|
| now | Slack: **Normal change CHG...**, **Approve change** |
| you | Approve (Change Management): "Next: the CAB" |
| you | Approve (CAB): deploys, validates, change **Closed / Successful** |

## Finish

Screenshot Grafana "Sentinel – AI & approvals" (`/d/sentinel`). Then stop everything:

```bash
make shoplite-stop && make harness-down && make infra-down
```

Leaving the shop broken keeps the alert firing. Stop the demo when done.
