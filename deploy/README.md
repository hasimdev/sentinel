# Deploy

Versioned ShopLite releases. Every release is built from a Git tag `shoplite-v<version>`,
so a version always means the same code, and each image is stamped with its version and
commit (the four tags on every log, metric and deploy).

```bash
make release VERSION=0.2.0   # build sentinel/shoplite:0.2.0 from tag shoplite-v0.2.0
make deploy VERSION=0.2.0    # run it on 127.0.0.1:8000 (replaces what's running)
make deploy VERSION=0.1.0    # rollback = deploy an older version
make shoplite-status         # which version is running
make shoplite-stop
```

Each deploy and rollback is recorded in `logs/deploys.log` (shipped to Loki) with the
version, commit and previous version.

| Release | Branch | What |
|---|---|---|
| 0.1.0 | `main` | Good release |
| 0.2.0 | `release/0.2.0` (never merged) | Autumn promo prices, with a real bug: checkout crashes for products without a promo price. Its test only covers one that has one. Used as the bad release in demos. |

## Changes on every deploy

Every deploy records the ServiceNow change it ran under, so the history says which change a
version came from, and a rollback can back that change out with no new ticket:

```bash
make release-change VERSION=0.2.0             # raise the release's normal change; approve it in ServiceNow
make deploy VERSION=0.2.0 CHANGE=CHG0031001   # deploy under it
make fix VERSION=0.2.1 INCIDENT=7             # a fix for an incident: emergency (P1/P2) or normal (P3/P4) change
```

How each kind of change is approved: orchestrator/README.md.

## Harness (real rollbacks and fixes)

Harness runs the **same deploy code** through a pipeline, so rollbacks and fixes approved in
Slack are real, recorded deploys:

```bash
make harness-up         # build sentinel/deployer and start the Harness delegate on this PC
make harness-pipeline   # create/update the pipeline from infra/harness/ (needs edit rights)
make harness-down
```

- **Delegate**: Harness's worker, running in Docker here (`infra/harness/Dockerfile`, compose
  profile `harness`). It connects *out* to Harness; nothing connects in. It can start and stop
  containers on this PC, which is why only this project's pipeline reaches it.
- **Pipeline as code**: `infra/harness/shoplite-deploy.pipeline.yaml` is what Harness runs.
  Its inputs (`version`, `reason`, `change`) reach the script as environment variables, never
  pasted into the script text; the version must be a plain `x.y.z` and the change a real
  `CHG` number.
- **Deployer**: `deploy/Dockerfile` packages `deploy/shoplite.py` with the docker CLI. Each
  deploy is logged with `deployed_by` (`make`, or `harness:<run id>`) and `change`.
- **Keys**: the orchestrator's key (`HARNESS_API_KEY`) belongs to a service account whose only
  role is *Pipeline Executor*: it can run the pipeline, never change it. To change the
  pipeline, grant *Project Member* briefly, run `make harness-pipeline`, then remove it.
- Harness's new pipeline editor only accepts its newer YAML style; ours is the classic style,
  which is why the pipeline is created through the API instead of pasted in.
