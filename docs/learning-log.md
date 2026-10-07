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
