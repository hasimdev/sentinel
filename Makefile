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

.PHONY: dev test lint run-shoplite traffic infra-up infra-down

dev:
	$(BOOTSTRAP) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pre_commit install

test:
	$(PY) -m pytest

lint:
	$(PY) -m pre_commit run --all-files

run-shoplite:
	$(PY) -m uvicorn apps.shoplite.main:app --reload --port 8000

traffic:
	$(PY) -m apps.shoplite.traffic --rps 5

infra-up:
	$(COMPOSE) up -d
	@echo Grafana: http://localhost:3000   Prometheus: http://localhost:9090

infra-down:
	$(COMPOSE) down
