# NAPlatform developer commands.
#
# Phase 08 adds routing E2E smoke targets. The default stack stays SAFE / DRY-RUN
# (AGENT_ROUTING_ENABLED=false); the enabled-routing targets layer the
# docker-compose.override.routing.yml file on top. `main` is never touched by any
# of these targets — see docs/BRANCHING.md (phase branches merge into `dev`,
# `main` only after a full release).

COMPOSE            ?= docker compose
BASE               := -f docker-compose.yml
ROUTING            := -f docker-compose.override.routing.yml
SMOKE              := -f docker-compose.smoke.yml

.PHONY: help test smoke-unit compile compose-config compose-config-routing build \
        up up-routing down smoke-dry smoke-routing

help:
	@echo "Targets:"
	@echo "  test               - run all pytest suites (api + hermes-agent + smoke unit)"
	@echo "  smoke-unit         - run only the smoke-script unit tests (no Docker)"
	@echo "  compile            - byte-compile api, hermes-agent, and scripts"
	@echo "  compose-config     - validate the default (dry-run) Compose config"
	@echo "  compose-config-routing - validate the enabled-routing Compose config"
	@echo "  build              - build the api and hermes-agent images"
	@echo "  up                 - start the default stack (dry-run, routing OFF)"
	@echo "  up-routing         - start the stack with routing ON (override applied)"
	@echo "  smoke-dry          - run the in-cluster smoke against the dry-run stack"
	@echo "  smoke-routing      - run the in-cluster smoke against the enabled stack"
	@echo "  down               - stop the stack and remove volumes"

# --- host-side checks (no Docker) ---------------------------------------
test:
	pytest -q

smoke-unit:
	pytest -q services/api/tests/test_smoke_routing_e2e.py

compile:
	python -m compileall services/api/app services/hermes-agent/hermes_agent scripts

# --- Compose validation / build -----------------------------------------
compose-config:
	$(COMPOSE) $(BASE) config

compose-config-routing:
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) config

build:
	$(COMPOSE) $(BASE) build api hermes-er

# --- stack lifecycle -----------------------------------------------------
up:
	$(COMPOSE) $(BASE) up -d --build

up-routing:
	$(COMPOSE) $(BASE) $(ROUTING) up -d --build

down:
	$(COMPOSE) $(BASE) down -v

# --- routing E2E smoke (in-cluster) -------------------------------------
# Dry-run: default stack, expect hermes_invoked=false.
smoke-dry:
	$(COMPOSE) $(BASE) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=false smoke

# Enabled: routing override applied, expect hermes_invoked=true.
smoke-routing:
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=true smoke
