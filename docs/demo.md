# Sentinel demo: bad release to real rollback in about 5 minutes

The full loop: a bad release breaks checkout, the AI investigates and recommends a rollback,
ServiceNow asks the CAB, a CAB approver approves in Slack, Harness rolls back, every record
closes itself. Costs one AI diagnosis (about $0.10).

## Before you start (once)

- Docker Desktop running. If it hangs: quit it, `wsl --shutdown`, start it again.
- `.env` has the Anthropic, Slack, ServiceNow, Jira and Harness settings (see `.env.example`).
- Release images exist: `make release VERSION=0.1.0` and `make release VERSION=0.2.0`.
- The Harness pipeline exists (`make harness-pipeline`, see deploy/README.md).
- Your Slack email belongs to a ServiceNow user in the **CAB Approval** group.

## Run it

Three terminals:

```bash
make infra-up && make harness-up    # 1: monitoring + the Harness delegate
make deploy VERSION=0.1.0           #    the good release
make run-orchestrator               # 2: Sentinel (Slack connects)
make traffic                        # 3: steady shop traffic
```

Wait about 30 s, then break it:

```bash
make deploy VERSION=0.2.0
```

## What happens, and what to screenshot

| # | When | What you see | Screenshot |
|---|---|---|---|
| 1 | ~1 min | Error rate jumps on 0.2.0; red alert line | **Grafana** ShopLite dashboard (`/d/shoplite`) |
| 2 | ~1.5 min | Incident posted: AI cause, evidence, **Roll back**, ServiceNow INC + CHG links | **Slack** message |
| 3 | same | Emergency change in *Authorize*, CAB approvers *Requested* | **ServiceNow** CHG, Approvers tab |
| 4 | you | Click **Approve rollback** | Slack: "approved by … as CAB approver …" |
| 5 | +2 s | Your approval recorded under your name; change *Implement* | **ServiceNow** approver row |
| 6 | +20 s | Pipeline green; ShopLite back on 0.1.0 | **Harness** execution |
| 7 | +20 s | "Rollback done", change **Closed / Successful** with the run link | Slack thread + ServiceNow CHG |
| 8 | +1 min | Error rate back to 0; INC *Resolved*; Jira follow-up filed | Grafana, ServiceNow INC, **Jira** |
| 9 | after | Agree rate, time to decide, rollback time, time to recover | **Grafana** "Sentinel – AI & approvals" (`/d/sentinel`) |

## Stop everything

Stop the three terminals (Ctrl+C), then:

```bash
make shoplite-stop && make harness-down && make infra-down
```

Leaving the shop broken keeps the alert firing. Stop the demo when done.
