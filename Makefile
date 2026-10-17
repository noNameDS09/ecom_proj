# Developer entry points for the e-commerce microservices monorepo.
#
#   make up           build + start the whole stack (storefront at :3000)
#   make seed         create the demo catalogue via the admin API
#   make test         unit + integration suite (needs Postgres on $(TEST_ADMIN_DSN))
#   make test-kafka   full suite including the Kafka pipeline tests
#   make lint         ruff check .
#
# Override variables like so:  make logs SERVICE=order-service

SHELL := /bin/bash
PY       ?= .venv/bin/python
RUFF     ?= .venv/bin/ruff
COMPOSE  ?= docker compose
SERVICE  ?=
FRONTEND ?= frontend
# The test harness creates throwaway databases on this Postgres instance
# (5433 matches the compose stack's published port).
TEST_ADMIN_DSN  ?= postgresql://app:app@127.0.0.1:5433/postgres
KAFKA_BOOTSTRAP ?= localhost:19092

.PHONY: help venv up down nuke ps logs build seed test test-kafka test-cov lint fmt \
        frontend-install frontend-dev frontend-build frontend-test

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-17s %s\n", $$1, $$2}'

venv: ## Create .venv and install the dev dependencies (requires uv)
	uv venv .venv
	uv pip install --python .venv/bin/python -r pyproject.toml

up: ## Build images and start Postgres + Kafka + the four services + the storefront
	$(COMPOSE) up --build -d

down: ## Stop the stack (volumes are kept)
	$(COMPOSE) down

nuke: ## Stop the stack and delete the Postgres/Kafka volumes
	$(COMPOSE) down -v

ps: ## Show stack status
	$(COMPOSE) ps

logs: ## Tail logs (make logs SERVICE=order-service for one service)
	$(COMPOSE) logs -f $(SERVICE)

build: ## Build the service images
	$(COMPOSE) build

seed: ## Create the demo catalogue through the admin API (stack must be up)
	$(PY) scripts/seed_catalogue.py

frontend-install: ## Install storefront dependencies (npm ci)
	npm --prefix $(FRONTEND) ci

frontend-dev: ## Run the storefront dev server (proxies to localhost:8001-8004)
	npm --prefix $(FRONTEND) run dev

frontend-build: ## Type-check and build the storefront bundle
	npm --prefix $(FRONTEND) run build

frontend-test: ## Run the storefront unit tests
	npm --prefix $(FRONTEND) test

test: ## Run the unit + integration suite
	TEST_ADMIN_DSN="$(TEST_ADMIN_DSN)" $(PY) -m pytest libs services tests

test-kafka: ## Run the full suite including the Kafka pipeline tests
	TEST_ADMIN_DSN="$(TEST_ADMIN_DSN)" TEST_KAFKA_BOOTSTRAP="$(KAFKA_BOOTSTRAP)" $(PY) -m pytest libs services tests

test-cov: ## Run the suite with coverage (term report)
	TEST_ADMIN_DSN="$(TEST_ADMIN_DSN)" $(PY) -m pytest libs services tests --cov --cov-report=term-missing

lint: ## Ruff check the whole repository
	$(RUFF) check .

fmt: ## Ruff autofix (safe fixes)
	$(RUFF) check --fix .
