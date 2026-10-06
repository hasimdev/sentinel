# Short commands for common tasks. Run `make dev` once after cloning.

ifeq ($(OS),Windows_NT)
BOOTSTRAP := py -3.12
PY := .venv\Scripts\python.exe
else
BOOTSTRAP := python3.12
PY := .venv/bin/python
endif

.PHONY: dev test lint

dev:
	$(BOOTSTRAP) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pre_commit install

test:
	$(PY) -m pytest

lint:
	$(PY) -m pre_commit run --all-files
