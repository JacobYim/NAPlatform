# NAPlatform developer commands.
#
# Phase 08 added routing E2E smoke targets; Phase 09 adds resource E2E smoke and
# makes the phase upload/release workflow explicit. The default stack stays SAFE /
# DRY-RUN (AGENT_ROUTING_ENABLED=false); the enabled-routing targets layer the
# docker-compose.override.routing.yml file on top.
#
# `main` is never touched by any target except `release-dev-to-main`, which is the
# single, explicit release step. Phase branches push with `push-phase` and merge
# into `dev` with `merge-phase-to-dev`; `main` only moves at a full release — see
# docs/BRANCHING.md and docs/ROADMAP.md.

COMPOSE            ?= docker compose
BASE               := -f docker-compose.yml
ROUTING            := -f docker-compose.override.routing.yml
SMOKE              := -f docker-compose.smoke.yml
# Phase 13: Docker Model Runner override (shared gemma4:31b for all agents).
MODEL_RUNNER       := -f docker-compose.model-runner.yml

# Phase upload/release variables. PHASE_BRANCH defaults to the current branch, so
# the upload helpers operate on whatever phase you have checked out.
PHASE_BRANCH       ?= $(shell git rev-parse --abbrev-ref HEAD)
DEV_BRANCH         ?= dev
MAIN_BRANCH        ?= main
GIT_REMOTE         ?= origin

# In-cluster resource smoke reuses the `smoke` service but overrides its command
# to run the Phase 09 resource script instead of the Phase 08 routing script.
RESOURCE_CMD       := python /scripts/smoke_resources_e2e.py

.PHONY: help test smoke-unit compile compose-config compose-config-routing \
        compose-config-prod compose-config-model-runner build \
        up up-routing up-model-runner down smoke-dry smoke-routing smoke-resources \
        smoke-model-runner \
        smoke-all-dry-run smoke-all-routing smoke-final \
        readiness release-check \
        push-phase merge-phase-to-dev release-dev-to-main

# Phase 12: production env template consumed by the prod compose config check.
PROD_ENV_FILE      ?= .env.production.example

help:
	@echo "Host-side checks (no Docker):"
	@echo "  test               - run all pytest suites (api + hermes-agent + smoke unit)"
	@echo "  smoke-unit         - run only the smoke-script unit tests (routing + resources)"
	@echo "  compile            - byte-compile api, hermes-agent, and scripts"
	@echo "  compose-config     - validate the default (dry-run) Compose config"
	@echo "  compose-config-routing - validate the enabled-routing Compose config"
	@echo "  compose-config-prod- validate Compose config against the prod env template"
	@echo "  compose-config-model-runner - validate the Docker Model Runner (gemma4:31b) Compose config"
	@echo "  build              - build the api and hermes-agent images"
	@echo ""
	@echo "Release preparation (Phase 12; never touches main):"
	@echo "  readiness          - print the redacted production-readiness report"
	@echo "  release-check      - pytest + compile + compose config + readiness gate (main untouched)"
	@echo "  smoke-final        - full smoke: dry-run + enabled-routing (needs Docker)"
	@echo ""
	@echo "Stack lifecycle:"
	@echo "  up                 - start the default stack (dry-run, routing OFF)"
	@echo "  up-routing         - start the stack with routing ON (override applied)"
	@echo "  up-model-runner    - start the stack with Docker Model Runner (shared gemma4:31b) ON"
	@echo "  down               - stop the stack and remove volumes"
	@echo ""
	@echo "In-cluster smoke:"
	@echo "  smoke-dry          - routing smoke against the dry-run stack (hermes_invoked=false)"
	@echo "  smoke-routing      - routing smoke against the enabled stack (hermes_invoked=true)"
	@echo "  smoke-model-runner - routing smoke against the model-runner stack (needs local Docker Model Runner)"
	@echo "  smoke-resources    - resource smoke (hdfs/vector/graph/audit scope) against the default stack"
	@echo "  smoke-all-dry-run  - routing (dry-run) + resource smoke against the default stack"
	@echo "  smoke-all-routing  - routing (enabled) + resource smoke against the enabled stack"
	@echo ""
	@echo "Phase upload / release (main only moves at release-dev-to-main):"
	@echo "  push-phase         - push PHASE_BRANCH ($(PHASE_BRANCH)) to $(GIT_REMOTE); main untouched"
	@echo "  merge-phase-to-dev - merge PHASE_BRANCH into $(DEV_BRANCH) and push; main untouched"
	@echo "  release-dev-to-main- RELEASE: merge $(DEV_BRANCH) into $(MAIN_BRANCH) and push (updates main)"

# --- host-side checks (no Docker) ---------------------------------------
test:
	pytest -q

smoke-unit:
	pytest -q services/api/tests/test_smoke_routing_e2e.py services/api/tests/test_smoke_resources_e2e.py

compile:
	python -m compileall services/api/app services/hermes-agent/hermes_agent scripts

# --- Compose validation / build -----------------------------------------
compose-config:
	$(COMPOSE) $(BASE) config

compose-config-routing:
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) config

# Validate the default compose topology with the production env template applied,
# so the prod-oriented variable substitution is exercised. Non-destructive.
compose-config-prod:
	$(COMPOSE) --env-file $(PROD_ENV_FILE) $(BASE) config

# Phase 13: validate the Docker Model Runner topology (shared gemma4:31b for all
# agents). Uses env-var defaults so it validates with no model runner installed.
compose-config-model-runner:
	$(COMPOSE) $(BASE) $(MODEL_RUNNER) config

build:
	$(COMPOSE) $(BASE) build api hermes-er

# --- stack lifecycle -----------------------------------------------------
up:
	$(COMPOSE) $(BASE) up -d --build --remove-orphans

up-routing:
	$(COMPOSE) $(BASE) $(ROUTING) up -d --build --remove-orphans

# Phase 13/20: start the FULL stack with the shared Docker Model Runner
# (gemma4:31b) wired to the API + every agent. Do not append a service name like
# `ui` or `api`: doing so starts only that subset and its dependencies, which
# skips the department Hermes agents and HDFS workers. Requires a working local
# Docker Model Runner + pulled model (see docker-compose.model-runner.yml).
up-model-runner:
	$(COMPOSE) $(BASE) $(MODEL_RUNNER) up -d --build --remove-orphans

down:
	$(COMPOSE) $(BASE) down -v

# --- routing E2E smoke (in-cluster) -------------------------------------
# Dry-run: default stack, expect hermes_invoked=false.
smoke-dry:
	$(COMPOSE) $(BASE) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=false smoke

# Enabled: routing override applied, expect hermes_invoked=true.
smoke-routing:
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=true smoke

# Phase 13: routing smoke against the Docker Model Runner stack (shared gemma4:31b).
# Routing is ON, so hermes_invoked=true is expected. This needs a working local
# Docker Model Runner + a Hermes CLI in the agent image for a real model reply;
# without them the agent execution errors and the smoke fails loudly (by design).
smoke-model-runner:
	$(COMPOSE) $(BASE) $(MODEL_RUNNER) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=true smoke

# --- resource E2E smoke (in-cluster) ------------------------------------
# HDFS workspace/provisioning + vector/graph scope + cross-department denial +
# audit. Routing-agnostic, so it runs against the default (dry-run) stack.
smoke-resources:
	$(COMPOSE) $(BASE) $(SMOKE) run --rm smoke $(RESOURCE_CMD)

# Both smokes against the default (dry-run) stack.
smoke-all-dry-run:
	$(COMPOSE) $(BASE) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=false smoke
	$(COMPOSE) $(BASE) $(SMOKE) run --rm smoke $(RESOURCE_CMD)

# Both smokes against the enabled-routing stack.
smoke-all-routing:
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) run --rm -e SMOKE_EXPECT_ROUTING=true smoke
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) run --rm smoke $(RESOURCE_CMD)

# --- Phase 12: release preparation (main is NEVER touched here) ----------
# Full smoke pass for a release: default (dry-run) and enabled-routing stacks.
smoke-final: smoke-all-dry-run smoke-all-routing

# Print the redacted production-readiness report (booleans + redacted URLs only).
readiness:
	python -c "import sys,json; sys.path.insert(0,'services/api'); from app.config import readiness_report; print(json.dumps(readiness_report(), indent=2))"

# Release gate: host-side checks + the production-readiness gate. This target does
# NOT push, merge, or check out any branch — `main` stays untouched. In dev mode
# (PRODUCTION_MODE unset) readiness is permissive and always passes; set
# PRODUCTION_MODE=true (with the prod env) to enforce the required checks.
release-check:
	@echo "release-check: host-side checks + readiness gate (main untouched)"
	pytest -q
	python -m compileall services/api/app services/hermes-agent/hermes_agent scripts
	$(COMPOSE) $(BASE) config >/dev/null
	$(COMPOSE) $(BASE) $(ROUTING) $(SMOKE) config >/dev/null
	python -c "import sys,json; sys.path.insert(0,'services/api'); from app.config import readiness_report; r=readiness_report(); print(json.dumps(r, indent=2)); sys.exit(0 if r['ready'] else 1)"

# --- phase upload / release ---------------------------------------------
# These make the git workflow explicit and keep `main` stable. Only
# `release-dev-to-main` ever updates `main`.

# Step 1: push the phase branch. Never touches dev or main.
push-phase:
	@echo "push-phase: pushing '$(PHASE_BRANCH)' to $(GIT_REMOTE) (dev/main untouched)"
	git push -u $(GIT_REMOTE) $(PHASE_BRANCH)

# Step 2: integrate the phase branch into dev. Never touches main.
merge-phase-to-dev:
	@echo "merge-phase-to-dev: merging '$(PHASE_BRANCH)' into '$(DEV_BRANCH)' (main untouched)"
	git checkout $(DEV_BRANCH)
	git merge --no-ff $(PHASE_BRANCH)
	git push $(GIT_REMOTE) $(DEV_BRANCH)
	git checkout $(PHASE_BRANCH)

# Step 3 (RELEASE ONLY): promote dev to main. This is the *only* target that
# updates main, and only when explicitly invoked.
release-dev-to-main:
	@echo "release-dev-to-main: RELEASE — merging '$(DEV_BRANCH)' into '$(MAIN_BRANCH)' (updates main)"
	git checkout $(MAIN_BRANCH)
	git merge --no-ff $(DEV_BRANCH)
	git push $(GIT_REMOTE) $(MAIN_BRANCH)
	git checkout $(DEV_BRANCH)
