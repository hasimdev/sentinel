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

Part 2 adds Harness: it will call the same deploy step, after a human approves in Slack.
