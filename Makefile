# Short commands for common tasks. Run `make dev` once after cloning.

ifeq ($(OS),Windows_NT)
BOOTSTRAP := py -3.12
PY := .venv\Scripts\python.exe
else
BOOTSTRAP := python3.12
PY := .venv/bin/python
endif

# Tag local runs with the current git commit unless one is already set.
export COMMIT_SHA ?= $(shell git rev-parse --short HEAD)

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env

.PHONY: dev test lint run-shoplite traffic infra-up infra-down infra-check alerts run-orchestrator incidents release deploy shoplite-status shoplite-stop deployer harness-up harness-down harness-pipeline

dev:
	$(BOOTSTRAP) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pre_commit install

test:
	$(PY) -m pytest

lint:
	$(PY) -m pre_commit run --all-files

# Also write logs to a file so Alloy can ship them to Loki.
run-shoplite: export SHOPLITE_LOG_FILE ?= logs/shoplite.log
run-shoplite:
	$(PY) -m uvicorn apps.shoplite.main:app --reload --port 8000

# Receives alerts on port 8001, opens incidents. Logs also go to Loki.
run-orchestrator: export ORCHESTRATOR_LOG_FILE ?= logs/orchestrator.log
run-orchestrator:
	$(PY) -m uvicorn orchestrator.main:create_app --factory --reload --port 8001 --env-file .env

# Print the incidents the orchestrator has recorded.
incidents:
	$(PY) -c "import json, urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8001/incidents')), indent=2))"

traffic:
	$(PY) -m apps.shoplite.traffic --rps 5

infra-up:
	$(COMPOSE) up -d
	@echo Grafana: http://localhost:3000   Prometheus: http://localhost:9090   Loki: http://localhost:3100   Alertmanager: http://localhost:9093

infra-down:
	$(COMPOSE) down

# Validate the stack's config files with each tool's own checker.
infra-check:
	$(COMPOSE) config --quiet
	$(COMPOSE) run --rm --no-deps loki -config.file=/etc/loki/loki.yml -verify-config
	$(COMPOSE) run --rm --no-deps alloy validate /etc/alloy/config.alloy
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus test rules /etc/prometheus/tests/shoplite_test.yml
	$(COMPOSE) run --rm --no-deps --entrypoint amtool alertmanager check-config /etc/alertmanager/alertmanager.yml

# Follow alerts as they arrive (Ctrl+C to stop watching).
alerts:
	$(COMPOSE) logs -f alert-inbox

# --- ShopLite releases (built from Git tags shoplite-v<version>) ---
# make release VERSION=0.2.0  ->  build the image;  make deploy VERSION=0.1.0  ->  run it (rollback = older version)
release:
	$(PY) -m deploy.shoplite release $(VERSION)

deploy:
	$(PY) -m deploy.shoplite deploy $(VERSION)

shoplite-status:
	$(PY) -m deploy.shoplite status

shoplite-stop:
	$(PY) -m deploy.shoplite stop

# --- Harness (Part 2, Step D): pipelines deploy through a delegate on this PC ---
# The deploy code packaged for the delegate to run (same code as `make deploy`).
deployer:
	docker build -t sentinel/deployer:local -f deploy/Dockerfile .

# Start / stop the Harness delegate. Needs HARNESS_ACCOUNT_ID and HARNESS_DELEGATE_TOKEN in .env.
harness-up: export SENTINEL_HOST_LOGS ?= $(shell $(PY) -m deploy.shoplite host-path)
harness-up: deployer
	$(COMPOSE) --profile harness up -d --build harness-delegate
	@echo Delegate starting. It shows as connected in Harness after about a minute.

# Create/update the pipeline in Harness from infra/harness/shoplite-deploy.pipeline.yaml.
harness-pipeline:
	$(PY) infra/harness/sync_pipeline.py

harness-down:
	$(COMPOSE) --profile harness stop harness-delegate
