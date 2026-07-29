# NAPlatform

Docker Compose based multi-department Hermes Agent platform for ER, IT, EHS, QC.

Includes PRD/architecture, FastAPI RBAC scaffold, Redis/Postgres/Qdrant/Neo4j/HDFS Compose topology, core-webui runtime UI integration, department Hermes agent containers, and tests.

Phase 02 core-webui auth/agent adapter stub is ready: `GET /core-webui/session` bootstrap, `POST /agents/{department}/chat` (deterministic stub, RBAC-scoped), `GET /resources/{department}` HDFS-root enforcement, and `GET /admin/approvals/pending`. Real Hermes invocation is the next phase.

## Phase 03 — Persistent auth/RBAC (Postgres-ready, Redis-ready)

Auth state is now backed by SQLAlchemy instead of in-process dicts, keeping the Phase 02 API contracts unchanged:

- **Database (`app/db.py`)** — SQLAlchemy tables `users`, `user_departments`, `audit_events`, `password_reset_tokens`. Tables are auto-created on startup (scaffold; no Alembic yet). Uses `DATABASE_URL` (`postgresql+psycopg://…` in Compose) and falls back to SQLite (in-memory for tests, file for `sqlite:///path.db`).
- **Store (`app/store.py`)** — `Store` class backed by SQLAlchemy for users, audit events, and reset tokens. It keeps the Phase 02 method surface (`seed_admin`, `add_user`, `get_user`, `get_user_by_email`, `create_session`, `get_session_user`), so existing imports/tests keep working. Construct `Store(database_url=..., session_store=...)` for isolated tests. `InMemoryStore` remains as a backwards-compatible alias.
- **Sessions (`app/session_store.py`)** — `SessionStore` interface with `RedisSessionStore` (uses `REDIS_URL`, TTL via `SETEX`) and `MemorySessionStore` fallback. `build_session_store()` picks Redis when `REDIS_URL` is set and reachable, otherwise in-memory — so tests and offline dev need no Redis. Session TTL defaults to `SESSION_TTL_SECONDS` (3600s).
- **Password reset** — `POST /auth/password-reset/request` persists a `password_reset_tokens` row with an `expires_at` TTL and never returns the token in the response.
- **Audit log** — signup, login success/failure, admin user update, password-reset request, and agent chat are recorded as `audit_events`. `GET /admin/audit?limit=N` (admin-only) lists them newest-first.

Environment: `DATABASE_URL`, `REDIS_URL`, `SESSION_TTL_SECONDS`, `ADMIN_PASSWORD`.

## Phase 04 — HDFS workspace provisioning (dry-run by default)

`app/hdfs.py`'s `HdfsProvisioner` turns the RBAC HDFS roots into safe, deterministic `hdfs dfs` command plans and only executes them when explicitly enabled:

- **Command plan / visible roots** — each user has a home page/root at `/naplatform/users/{username}`, but the agent/UI are only allowed to use the child roots `/naplatform/users/{username}/workspace` and `/naplatform/users/{username}/chat_history`. Each department is similarly exposed through `/naplatform/departments/{DEP}/department_shared`. The parent user/department directories are provisioned/visible to operators in NameNode but are **not** agent-accessible workspace roots.
- **ACL plan** — personal `workspace` and `chat_history` dirs get `mkdir -p` → `chown/chgrp` (placeholder) → `chmod 700` → `setfacl -m user:{username}:rwx`; each `department_shared` dir gets `chmod 770` → `setfacl -m group:naplatform-{dep}:rwx` plus `setfacl -m user:{username}:rwx`.
- **Validation** — usernames must match `^[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}$` (no leading dot/dash, no `..`), departments must be known, and every built path is re-checked to stay under `/naplatform` with no traversal.
- **Dry-run vs enabled** — with `HDFS_PROVISIONING_ENABLED` unset/false (the default) provisioning is a **dry run**: it returns the planned commands and spawns **no subprocess**. Set `HDFS_PROVISIONING_ENABLED=true` to actually run each command via `subprocess.run` (argv list, no shell) and capture `returncode/stdout/stderr`.
- **Endpoints** — `POST /admin/users/{user_id}/provision-hdfs` (admin-only) returns the provision plan/results for a user's `workspace`, `chat_history`, and department `department_shared` dirs; `GET /workspace/hdfs` (active user) returns `user_home_root`, `personal_root`, `history_root`, department roots, and the dry-run plan with provisioning status. `GET/POST /workspace/hdfs/file` and `POST /workspace/hdfs/chat-history` are RBAC-gated through the same roots.
- **Audit** — `hdfs_provision` and `workspace_view` events are recorded.

**Kerberos (production):** the `chown/chgrp` steps are placeholders — in production, directory ownership and access are enforced by Kerberos principals / proxy-users and HDFS ACLs, and provisioning runs as a keytab-authenticated service account, not by ad-hoc CLI calls.

Environment (Phase 04): `HDFS_PROVISIONING_ENABLED`, `HDFS_BIN`, `HDFS_DEPARTMENT_GROUP_PREFIX`.

## Phase 05 — Qdrant/Neo4j scope adapters (metadata-separated, tested scaffolds)

A **single shared Qdrant** and a **single shared Neo4j** back all four departments; tenants are separated by *metadata filters*, not by a collection/graph per department. Every vector point and every graph node/relationship carries scope metadata (`owner_user_id`, `allowed_users`, `department`, `allowed_departments`), and every read is constrained by a filter derived from the caller's RBAC scope — reusing the existing `rbac.qdrant_filter` / `rbac.neo4j_filter` semantics. The adapters are tested scaffolds: no live Qdrant/Neo4j is required.

- **`app/vector.py` — `VectorScopeAdapter`** — builds Qdrant-compatible `Filter` descriptors for the active user's personal + department scope, validates department membership (via `qdrant_filter`), validates collection names against `^[a-z][a-z0-9_]{2,63}$`, and keeps a deterministic in-memory store. Insert requires scope `personal` or `department`: personal points stamp `owner_user_id=user.id`; department points stamp `department=<active>` and `allowed_departments=[<active>]`. Search only returns points whose metadata matches the caller (own/allowed user, or active/allowed department).
- **`app/graph.py` — `GraphScopeAdapter`** — emits parameterised Cypher MATCH descriptors (`$owner_user_id`/`$user_id`/`$department` bound as params, never string-interpolated) enforcing `owner_user_id`/`allowed_users`/`department`/`allowed_departments`. Labels are validated against `^[A-Za-z][A-Za-z0-9_]{0,63}$` and relationship types against `^[A-Z][A-Z0-9_]{0,63}$` (they are the only identifiers that must be inlined into Cypher). Deterministic in-memory node/relationship insert+search stubs apply the same scope enforcement.
- **Endpoints** (all require an active user + department membership):
  - `POST /vector/{department}/records` — insert a point (`collection`, `scope`, `payload`).
  - `GET|POST /vector/{department}/search` — scoped search; the response echoes the generated Qdrant filter descriptor.
  - `POST /graph/{department}/nodes` — insert a node (`label`, `scope`, `properties`).
  - `GET|POST /graph/{department}/nodes/search` — scoped search; the response echoes the generated Cypher + params.
- **Audit** — `vector_insert`, `vector_search`, `graph_insert`, `graph_search` events are recorded.

Swapping the in-memory stores for real `qdrant_client` / `neo4j` drivers is a later phase; the filter/Cypher descriptors these adapters already emit are the payloads those drivers consume.

## Phase 06 — Department Hermes agent routing (dry-run by default, no live Hermes in tests)

`app/agent_router.py`'s `DepartmentAgentRouter` turns an `AgentContext` into a concrete agent invocation, routing by department to a *configured* endpoint. It stays a deterministic dry run unless routing is explicitly enabled — so tests and offline dev never require a live Hermes.

- **Routing** — each department resolves to an endpoint from `HERMES_ER_URL` / `HERMES_IT_URL` / `HERMES_EHS_URL` / `HERMES_QC_URL`, defaulting to the Compose service names (`http://hermes-er:8080`, …). The department key is validated (`normalize_department`) before lookup; **no user-supplied URL is ever dialed** (SSRF-safe), and every endpoint is re-validated to be an `http(s)://host` URL.
- **Clients** — `HttpAgentClient` does an HTTP JSON `POST` to `/chat` (falling back to `/invoke` on a 404) with the timeout from `AGENT_REQUEST_TIMEOUT_SECONDS`; `DryRunAgentClient` is the fallback whenever `AGENT_ROUTING_ENABLED` is not true or a department has no URL. The dry-run response echoes `request_id`, `department`, `hermes_invoked=false`, and a secret-free context summary.
- **Invocation payload** — carries `message`, user identity, the full `AgentContext`, `allowed_tools`, `allowed_mcp_servers`, `hdfs_roots`, `qdrant_filter`, `neo4j_filter`, and the personal `workspace_root`, so the agent honors the API-issued scope rather than re-deriving it.
- **Endpoints** — `POST /agents/{department}/chat` now routes through the router (deterministic dry-run default; real HTTP call only when enabled + URL configured). Timeouts map to `504` and upstream/unreachable errors to `502`, and both success and failure are audited (`agent_chat`). `GET /admin/agents/status` (admin-only) returns the per-department routing config plus the `enabled`/`dry_run` flags, with no secrets.
- **Audit** — `agent_chat` events record department, `hermes_invoked`, and `request_id` on success, and the failure kind (`timeout`/`upstream_error`/`routing_error`) on failure.

`httpx` is imported lazily and used with an injectable transport, so the enabled HTTP path is fully tested via `httpx.MockTransport` without a live agent.

Environment (Phase 06): `AGENT_ROUTING_ENABLED` (default false), `AGENT_REQUEST_TIMEOUT_SECONDS` (default 30), `AGENT_INVOKE_PATH` (default `/chat`), `HERMES_ER_URL`, `HERMES_IT_URL`, `HERMES_EHS_URL`, `HERMES_QC_URL`.

## Phase 07 — Hermes agent HTTP service (deterministic by default, no live CLI in tests)

The department Hermes agent containers no longer just tail forever — each now runs a small FastAPI service (`services/hermes-agent`, package `hermes_agent`) that the API routes to. It stays a deterministic responder unless real Hermes CLI execution is explicitly enabled, so tests and offline dev never need a live Hermes.

- **Service (`hermes_agent/main.py`)** — exposes `GET /health`, `POST /chat`, `POST /invoke`. It loads `DEPARTMENT`/`HERMES_PROFILE`/`API_BASE_URL`/`HDFS_NAMENODE`, prepares the profile files (`SOUL.md`, `config.yaml`) on startup exactly as the old `bootstrap-agent.sh` did, and accepts the exact payload `DepartmentAgentRouter` posts.
- **Defensive scope re-validation (`hermes_agent/validation.py`)** — the API is the security boundary, but the agent re-checks the scope it is handed: the payload `department` must match the container's `DEPARTMENT` (else `403`); every `hdfs_roots` entry must resolve with no traversal to a path under `/naplatform`; every `allowed_tools`/`allowed_mcp_servers` entry must be a safe identifier (`^[a-z0-9][a-z0-9_.-]{0,63}$`) — otherwise `400`. The default response is deterministic with `hermes_invoked=false`.
- **Optional execution (`hermes_agent/executor.py`)** — with `HERMES_AGENT_EXECUTION_ENABLED=true`, `/chat` drives the real Hermes CLI via `subprocess.run` (argv list, `shell=False`, timeout from `HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS`). The user `message` is passed as a single argv element, never interpolated into a shell. Disabled by default; tests exercise this path with a `FakeHermesRunner` — no real CLI. A timeout maps to `504`, a non-zero exit to `502`.
- **Compose** — `hermes-*` services serve HTTP internally only (`expose: ["8080"]`, no host port) with a `curl /health` healthcheck. The API's `AGENT_ROUTING_ENABLED` still defaults to `false`; flip it (and optionally `HERMES_AGENT_EXECUTION_ENABLED=true` on the agents) to route real HTTP calls.

Environment (Phase 07): `HERMES_AGENT_EXECUTION_ENABLED` (default false), `HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS` (default 60), `HERMES_BIN` (default `hermes`), plus the existing `DEPARTMENT`/`HERMES_PROFILE`/`API_BASE_URL`/`HDFS_NAMENODE`.

Tests live under `services/hermes-agent/tests` and run together with the API tests (`pytest.ini` lists both on `pythonpath`/`testpaths`; the agent package is named `hermes_agent` to avoid colliding with the API's `app`). An API-side test (`test_agent_service_shape.py`) proves via `httpx.MockTransport` that the router's payload matches the service's `InvokeRequest` contract and that the router ingests the service's response shape.

The actual post-login runtime UI is `github.com/JacobYim/core-webui`. The Compose `ui` service builds that repository and applies HMGMA branding with `BRAND_NAME=HMGMA` and the included `branding/logo.jpg` (`HMG Metaplant America`).

## Phase 08 — Routing E2E Compose/smoke (default stack stays dry-run)

Phase 08 makes the enabled-routing path runnable and verifiable end-to-end without changing the safe default. The plain `docker-compose.yml` keeps `AGENT_ROUTING_ENABLED=false`; a separate override flips it on, and an in-cluster smoke script exercises the real HTTP path.

- **`docker-compose.override.routing.yml`** — a `-f`-only override (never auto-loaded) that sets `AGENT_ROUTING_ENABLED=true` on the `api` service and reuses the existing `hermes-er/it/ehs/qc` services and their internal URLs (`http://hermes-<dep>:8080`). A plain `docker compose up` is unaffected and stays dry-run.
- **`docker-compose.smoke.yml`** — a one-shot `smoke` service (profile `smoke`) that runs `scripts/smoke_routing_e2e.py` from *inside* the Compose network (reusing the API image, mounting `./scripts`), so it can reach both the API and the internal-only agents by service name.
- **`scripts/smoke_routing_e2e.py`** — waits for API + Hermes health, logs in as the seeded admin, **idempotently** creates and approves a QC user, logs in as that user, calls `POST /agents/QC/chat`, and asserts `hermes_invoked` is `true` with the routing override and `false` in the default dry-run stack (cross-checked against `GET /admin/agents/status`). It also asserts the QC user is denied IT (`403`). It never prints secrets (passwords or session tokens) and is safe to re-run.
- **Tests (no Docker)** — `services/api/tests/test_smoke_routing_e2e.py` drives the smoke logic against a fake API (`httpx.MockTransport`), covering dry-run vs enabled, expectation mismatch, signup idempotency, and IT denial. `services/api/tests/test_routing_contract.py` wires the API router's HTTP path to the **real** `hermes_agent` app and proves that with `AGENT_ROUTING_ENABLED=true` but `HERMES_AGENT_EXECUTION_ENABLED=false` the API sees `hermes_invoked=true` (HTTP hop succeeded) while the agent body reports `hermes_invoked=false` (no CLI ran).

Commands (dry-run vs enabled routing):

```bash
# Dry-run stack + smoke (routing OFF; expects hermes_invoked=false)
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
  run --rm -e SMOKE_EXPECT_ROUTING=false smoke

# Enabled-routing stack + smoke (routing ON; expects hermes_invoked=true)
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml \
  run --rm -e SMOKE_EXPECT_ROUTING=true smoke
```

Or via the Makefile: `make smoke-dry` and `make smoke-routing` (see `make help`).

Environment (Phase 08 smoke): `SMOKE_API_BASE_URL`, `SMOKE_EXPECT_ROUTING`, `SMOKE_HERMES_HEALTH_URLS`, `SMOKE_ADMIN_PASSWORD`/`ADMIN_PASSWORD`, `SMOKE_QC_EMAIL`/`SMOKE_QC_USERNAME`/`SMOKE_QC_PASSWORD`, `SMOKE_HEALTH_RETRIES`, `SMOKE_HEALTH_INTERVAL`, `SMOKE_REQUEST_TIMEOUT`.

## Phase 09 — Resource E2E smoke + explicit phase upload/release (main stays stable)

Phase 09 adds a **resource-focused** end-to-end smoke that complements the Phase 08 routing smoke, and makes the git upload/release workflow explicit so `main` is never touched by accident. Full phase status lives in [`docs/ROADMAP.md`](docs/ROADMAP.md).

- **`scripts/smoke_resources_e2e.py`** — drives the real resource surface of a live stack. It waits for API health, logs in as the seeded admin, **idempotently** creates and approves both a **QC** user and an **IT** user, then asserts:
  - `GET /workspace/hdfs` returns **only** the caller's own personal root (`/naplatform/users/<username>`) and its own department root (`/naplatform/departments/QC`) — never another department's root;
  - `POST /admin/users/{id}/provision-hdfs` is a **dry run** — it returns the planned `hdfs dfs` commands (`targets[].plan[].command`) but executes nothing (`dry_run=true`, `enabled=false`, all `results` empty);
  - **vector** personal/department insert+search are scoped correctly — the IT user never sees the QC user's personal or department points;
  - **graph** personal/department insert+search are scoped correctly the same way;
  - **cross-department denial** — the QC user is `403` on `/vector/IT/records`, `/graph/IT/nodes`, and `/resources/IT`;
  - the audit log (`GET /admin/audit`) contains the key events (`vector_insert`/`vector_search`/`graph_insert`/`graph_search`/`hdfs_provision`/`workspace_view`/`admin_user_update`/`login`).
  It is **idempotent** (users reused; vector/graph records written with fixed ids) and **never prints secrets**.
- **Tests (no Docker)** — `services/api/tests/test_smoke_resources_e2e.py` drives the smoke logic against a fake API (`httpx.MockTransport`) that reproduces the real metadata scope rule, covering workspace scoping, dry-run provisioning, vector/graph isolation (including failure cases where isolation is broken), cross-department denial, audit completeness, signup idempotency, and secret redaction.

Commands (in-cluster smoke reuses the `smoke` service, overriding its command):

```bash
# Resource smoke against the default (dry-run) stack
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
  run --rm smoke python /scripts/smoke_resources_e2e.py
```

Or via the Makefile: `make smoke-resources`, plus `make smoke-all-dry-run` (routing dry-run + resource) and `make smoke-all-routing` (routing enabled + resource). See `make help`.

**Explicit phase upload / release (main only moves at the final release):** the git workflow is encoded as separate Makefile steps that honor the `PHASE_BRANCH` variable (default: current branch). Only `release-dev-to-main` ever updates `main`.

```bash
make push-phase          # push PHASE_BRANCH to origin (dev/main untouched)
make merge-phase-to-dev  # merge PHASE_BRANCH into dev and push (main untouched)
make release-dev-to-main # RELEASE ONLY — the one step that updates main
```

Environment (Phase 09 smoke): `SMOKE_API_BASE_URL`, `SMOKE_ADMIN_PASSWORD`/`ADMIN_PASSWORD`, `SMOKE_QC_EMAIL`/`SMOKE_QC_USERNAME`/`SMOKE_QC_PASSWORD`, `SMOKE_IT_EMAIL`/`SMOKE_IT_USERNAME`/`SMOKE_IT_PASSWORD`, `SMOKE_VECTOR_COLLECTION`, `SMOKE_GRAPH_LABEL`, `SMOKE_HEALTH_RETRIES`, `SMOKE_HEALTH_INTERVAL`, `SMOKE_REQUEST_TIMEOUT`.

## Phase 10 — Real Qdrant/Neo4j/HDFS backend adapters (memory/dry-run by default)

Phase 10 makes the vector, graph, and HDFS backends **pluggable behind the same scope/RBAC contract**. Each backend is selected by env and defaults to the Phase 05 in-memory / dry-run scaffold, so tests and the default stack need **no live Qdrant/Neo4j/HDFS**. The real drivers are exercised in tests through fake clients/drivers/runners. Full phase status lives in [`docs/ROADMAP.md`](docs/ROADMAP.md).

- **Backend selection envs (default memory/dry-run):** `VECTOR_BACKEND=memory|qdrant`, `GRAPH_BACKEND=memory|neo4j`, and the existing `HDFS_PROVISIONING_ENABLED=true|false`.
- **`app/vector.py` — `QdrantVectorBackend`** — uses `qdrant-client` when installed/configured (`QDRANT_URL`, optional `QDRANT_API_KEY`), **creates the collection on demand** with configurable `QDRANT_VECTOR_SIZE` (default `768`) / `QDRANT_DISTANCE` (default `Cosine`), and upserts/searches through a client wrapper enforcing the **same** scope metadata and Qdrant `Filter` descriptors as the memory adapter. `VectorScopeAdapter` (memory) is unchanged.
- **`app/graph.py` — `Neo4jGraphBackend`** — uses the `neo4j` driver when installed/configured (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`), runs **parameterised Cypher only** — every scope value / user property is a bound `$param`, and the only inlined identifiers are the strictly-validated label / relationship type. `GraphScopeAdapter` (memory) is unchanged.
- **`app/hdfs.py` — `HdfsProvisioner.health()`** — an HDFS readiness probe built from a **constant safe argv** (`hdfs dfs -test -d /naplatform`, no user input, no shell string). Dry run by default (planned, never executed); runs via the injected/subprocess runner only when provisioning is enabled.
- **`GET /admin/backends/status`** (admin-only) — reports the active vector/graph/hdfs modes, **redacted** connection URLs, and a dry-run/fake health status. Secrets never leak: api keys and passwords are reported only as booleans (`api_key_set`, `password_set`).
- **Safe fallback** — an invalid `VECTOR_BACKEND`/`GRAPH_BACKEND`, a missing driver library, or an unconfigured real backend degrades to the in-memory scaffold with a logged warning, so the API never fails to start because of a backend env. The strict factories (`build_vector_backend`/`build_graph_backend`) still reject an unknown mode.

The `/vector/{department}` and `/graph/{department}` API contracts are unchanged — they use whichever backend the env selects, defaulting to memory.

```bash
# Default (memory/dry-run) — no live backends needed:
docker compose -f docker-compose.yml up -d --build
curl -s localhost:8080/admin/backends/status -H "Authorization: Bearer $ADMIN_TOKEN"

# Opt into the real backends (qdrant/neo4j services already run in Compose):
docker compose up -d qdrant neo4j
VECTOR_BACKEND=qdrant QDRANT_URL=http://localhost:6333 \
GRAPH_BACKEND=neo4j NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j \
NEO4J_PASSWORD=naplatform-password \
  uvicorn app.main:app --app-dir services/api --port 8080
```

Real-backend deps (`qdrant-client`, `neo4j`) are pinned in `services/api/requirements.txt` but only imported when their backend is selected. Tests: `services/api/tests/test_backends.py` (fake-driven, no Docker).

## Phase 11 — core-webui auth/session UI integration adapter (no live UI in tests)

The real post-login UI is external (`github.com/JacobYim/core-webui`) and is **not**
vendored here. Phase 11 adds a **repo-controlled adapter** that connects that UI's
login/signup/session/department-selector flows to the NAPlatform API, so the
integration is tested here with **no live browser or external checkout**. Full phase
status lives in [`docs/ROADMAP.md`](docs/ROADMAP.md).

- **Adapter package (`services/ui/adapter/`)** — a dependency-free ES module
  (`naplatform-adapter.js`), a machine-readable `contract.json` (single source of
  truth both the JS adapter and the Python tests read), a static `index.html` demo,
  `package.json`, and a `README.md`. The session token lives only in memory; no
  secret is embedded in any adapter file (a test enforces this).
- **API support endpoints (additive; existing contracts preserved):**
  - `GET /auth/me` — alias of `/core-webui/session` (same bootstrap shape).
  - `GET /auth/departments/options` — public option list for the signup / selector.
  - `POST /auth/logout` — invalidates the Redis/memory session (idempotent safe
    logout; audited when a real session is present).
  - `GET /core-webui/session/status` — status for *any* valid session so the UI can
    render the **approval-waiting UX** for a pending/disabled account; an
    expired/invalid session is `401`.
  - `POST /core-webui/session/select-department` — validates department membership
    and returns the chat/context/resource routes (non-member `403`, unknown `400`).
- **Session bootstrap model (`app/webui.py`)** — pure helpers building the
  department routes (so the UI routes chat to `/agents/{department}/chat`), the
  public department options, and the approval-waiting UX contract. `/core-webui/session`
  and `/auth/me` now also carry `session_status`, `chat_route_template`,
  `department_routes[]`, and `approval` (all additive).
- **Pending / inactive handling** — a fresh signup is `pending` and cannot log in
  (`403`); an account revoked after login gets `403` on `/core-webui/session` but a
  `can_access:false` approval contract on `/core-webui/session/status`; an
  expired/invalid session is `401` everywhere.

```bash
# The department-selector option list is public (no session yet):
curl -s localhost:8080/auth/departments/options | jq .
# Bootstrap + department routes for an active session:
curl -s localhost:8080/auth/me -H "Authorization: Bearer $TOKEN" | jq '.department_routes'
# Log out (invalidates the session; idempotent):
curl -s -X POST localhost:8080/auth/logout -H "Authorization: Bearer $TOKEN" | jq .
```

Tests: `services/api/tests/test_webui_session.py` and
`services/api/tests/test_webui_adapter_contract.py` (no Docker, no browser).

## Phase 12 — Production hardening & release prep (main stays stable)

Phase 12 hardens the API for a real deployment and prepares the stable release,
**without touching `main`** and **without changing the permissive defaults** (dev
still runs memory/dry-run, SQLite, in-memory sessions, and never blocks startup):

- **Production env template (`.env.production.example`)** — documents every
  production setting (secrets, CORS/`TRUSTED_ORIGINS`, backend modes, routing
  flags, Redis/Postgres/Qdrant/Neo4j/HDFS/Hermes URLs) with dummy placeholders.
  No real secrets.
- **Runtime config validation (`app/config.py`)** — `readiness_report()` is
  permissive in dev and, when `PRODUCTION_MODE=true`, requires a non-default
  `ADMIN_PASSWORD`, a durable `DATABASE_URL`, `REDIS_URL` **or**
  `SESSION_STORE_STRICT`, concrete `TRUSTED_ORIGINS` (no `*`), `QDRANT_URL` when
  `VECTOR_BACKEND=qdrant`, `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` when
  `GRAPH_BACKEND=neo4j`, and a `HERMES_{DEP}_URL` when `AGENT_ROUTING_ENABLED=true`.
  The report is redacted — booleans and redacted URLs only.
- **`GET /admin/release/readiness`** (admin-only) — redacted readiness checks +
  the final release checklist status. Never promotes `main`.
- **CORS + security headers** — a configurable `TRUSTED_ORIGINS` allow-list
  (safe localhost dev defaults, credentials off for a wildcard) and baseline
  headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Cross-Origin-Opener-Policy`; HSTS only in production).
- **`GET /admin/audit/export`** (admin-only) — read-only audit export with
  `action`/`actor`/`user_id`/`success`/`limit` filters and `format=json|jsonl`.
  Retention is documented and **non-destructive by default** (`AUDIT_RETENTION_DAYS`,
  `AUDIT_RETENTION_ENFORCE=false`).
- **Release docs + Makefile** — `docs/RELEASE_NOTES_TEMPLATE.md`,
  `docs/FINAL_RELEASE_CHECKLIST.md`, and `make release-check` (pytest + compile +
  compose config + readiness gate; **never updates `main`**), `make
  compose-config-prod`, `make smoke-final`.

Environment (Phase 12): `PRODUCTION_MODE`, `TRUSTED_ORIGINS`, `SESSION_STORE_STRICT`,
`AUDIT_RETENTION_DAYS`, `AUDIT_RETENTION_ENFORCE` (plus the datastore/backend/routing
envs from Phases 03–10).

```bash
make readiness                 # redacted production-readiness report
make release-check             # pytest + compile + compose config + readiness gate (main untouched)
PRODUCTION_MODE=true make release-check   # enforce required checks with the prod env
make compose-config-prod       # compose config with .env.production.example applied
curl -s localhost:8080/admin/release/readiness -H "Authorization: Bearer $ADMIN_TOKEN" | jq .
```

`main` moves **only** at `make release-dev-to-main`, and only after explicit
approval — see `docs/FINAL_RELEASE_CHECKLIST.md`. Phase 12 is implemented on
`phase/12-production-hardening-release-prep` and **leaves `main` unchanged**.

## Phase 13 — Docker Model Runner (shared gemma4:31b for all agents) + PowerShell runbook

Phase 13 lets **every** department Hermes agent (ER / IT / EHS / QC) share **one**
model — `gemma4:31b` — served by [Docker Model Runner](https://docs.docker.com/desktop/features/model-runner/)
over an OpenAI-compatible endpoint, **without changing the safe default**. The
default stack stays dry-run; the model runner is a separate, explicitly-applied
Compose override.

- **Compose override (`docker-compose.model-runner.yml`)** — applied only with an
  explicit `-f`, it enables API routing + agent execution and declares the shared
  LLM envs (`HERMES_LLM_PROVIDER`, `DOCKER_MODEL_RUNNER_BASE_URL`,
  `DOCKER_MODEL_RUNNER_MODEL=gemma4:31b`) on the API and on **all** `hermes-*`
  services, so every agent points at the same model. Env-var defaults let
  `docker compose config` validate even with no model runner installed.
- **Shared model, isolated persona (`services/hermes-agent`)** — when
  `HERMES_AGENT_EXECUTION_ENABLED=true` **and** the provider/model envs are set,
  each agent's generated profile `config.yaml` gains an `llm:` block pointing at
  the shared Docker Model Runner / OpenAI-compatible endpoint and model
  (`gemma4:31b`). The per-department `SOUL.md` persona stays isolated; the model
  is identical across all four agents (no agent-specific model drift). With the
  envs unset the profile is model-less — dry-run safe.
- **Secret-free status** — the agent `/health` and the admin `GET /admin/agents/status`
  report a redacted `model_runtime` (provider, redacted base URL, model,
  `configured`, `api_key_present`) so an operator can confirm the runner is wired
  up without any secret leaving the process. The API key is referenced by env-var
  **name** only — never written to the on-disk profile or echoed.
- **Prerequisites** — actual model replies require **Docker Desktop 4.40+** with
  the Docker Model Runner feature enabled and `docker model pull gemma4:31b`, plus
  a Hermes CLI in the agent image. This is a scaffold: the model name/endpoint are
  wired exactly as requested; a live reply depends on local Docker Desktop / model
  runner support.

Environment (Phase 13): `HERMES_LLM_PROVIDER`, `DOCKER_MODEL_RUNNER_BASE_URL`,
`DOCKER_MODEL_RUNNER_MODEL` (default `gemma4:31b`), OpenAI-compatible fallbacks
`OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY`.

```bash
make compose-config-model-runner   # validate the shared gemma4:31b Compose config (no runner needed)
make up-model-runner               # start the stack with the shared model runner ON (needs local DMR)
make smoke-model-runner            # routing smoke against the model-runner stack (needs local DMR)
```

Windows users: see **[docs/POWERSHELL_RUNBOOK.md](docs/POWERSHELL_RUNBOOK.md)** for
the full clone → checkout → release-check → run → smoke → cleanup flow in native
**PowerShell** syntax (no `make` required; a note flags where Git Bash differs).
Phase 13 is implemented on `phase/13-docker-model-runner-gemma4-powershell` and
**leaves `main` unchanged**.

## Phase 14 — core-webui first-run preseed (no initial setup screen at localhost:3000)

On a fresh volume the external core-webui UI shows an **initial setup / onboarding
screen** at http://localhost:3000. Phase 14 removes it by **preconfiguring every
required first-run setting from this repo** and applying it automatically when
Docker brings the UI up, so the first load opens straight into the HMGMA
workspace — **no setup screen**. The approach is **non-invasive**: the external
core-webui image / entrypoint is never edited; the repo config is seeded into the
shared volume the UI reads, before it serves.

- **Repo-controlled config (`config/core-webui/`)** — `branding.yaml` (HMGMA,
  copied to `$HERMES_HOME/branding.yaml`), `webui-settings.json` (the first-run
  settings: `first_run: false` / `setup_completed: true` / `onboarding_completed:
  true`, the `http://api:8080` API base URL, the auth adapter config, and default
  endpoints — copied to `$HERMES_WEBUI_STATE_DIR/settings.json`), plus a `README.md`
  with PowerShell **and** Bash edit/start instructions. **No secrets** are written.
- **Preseed init (`preseed.sh` + the `ui-preseed` compose service)** — a
  dependency-free busybox one-shot service that shares the `ui-hermes-home` volume
  with `ui`, seeds the config + writes setup-completed markers, then exits. The
  `ui` service `depends_on` it with `condition: service_completed_successfully`, so
  the config is applied **before core-webui serves**. It is part of the **default**
  compose, so a plain `docker compose up ui` suppresses the setup screen.
- **Env override still possible** — `BRAND_NAME` / `BRAND_LOGO` /
  `NAPLATFORM_API_BASE_URL` win over the config files, and `ui` also carries
  `HERMES_WEBUI_SETUP_COMPLETED` / `HERMES_WEBUI_DISABLE_FIRST_RUN` env flags.
- **Guards** — `services/api/tests/test_phase14_ui_preseed.py` and the offline
  `scripts/_phase14_check.py` prove the compose mounts + waits for the preseed, the
  config disables first-run, no secrets leak, the docs say localhost:3000 shows no
  setup screen, and `main` stays guarded.

```bash
# PowerShell:  docker compose up -d --build ui ; Start-Process "http://localhost:3000"
# Bash:        docker compose up -d --build ui   # then open http://localhost:3000
python scripts/_phase14_check.py                 # offline compose/config wiring check
```

See **[config/core-webui/README.md](config/core-webui/README.md)** for editing the
config and starting Docker in PowerShell and Bash. Phase 14 is implemented on
`phase/14-core-webui-first-run-autoconfig` and **leaves `main` unchanged**.

## Phase 15 — login-required UI + endpoint-configurable model runner

Phase 15 fixes two operator-reported issues without touching `main`, and stays
dry-run-safe by default.

### A. The UI requires login (no more direct-to-chat)

Phase 14 correctly **skips the setup screen**, but the UI then landed **directly in
chat**. Phase 15 makes the UI **require login** after the setup skip — `/` redirects
to `/login`. This is repo/compose-controlled, with **no secret in any preseed file**:

- `config/core-webui/webui-settings.json` declares `login_required: true`,
  `require_auth: true`, `start_page: "login"`, and `defaults.landing: "login"`.
- The `ui` service in `docker-compose.yml` also carries
  `HERMES_WEBUI_LOGIN_REQUIRED` / `HERMES_WEBUI_REQUIRE_AUTH` /
  `HERMES_WEBUI_START_PAGE=login` (belt-and-suspenders for env-driven versions).
- `ui-preseed` forwards `CORE_WEBUI_LOGIN_REQUIRED`, and `preseed.sh` uses it to flip
  the login flags in the copied settings — still **never writing a password**.

**Login behavior & password.** The primary login path is the NAPlatform adapter
authenticating against the API `POST /auth/login` — sign in with the seeded admin
`admin@example.com` / `ChangeMe123!` (or any active user). For core-webui versions
with a built-in single-password gate, the `ui` service passes
`HERMES_WEBUI_PASSWORD: "${CORE_WEBUI_PASSWORD:-ChangeMe123!}"`. Override it with
`CORE_WEBUI_PASSWORD`; the default `ChangeMe123!` is a **documented dev-only** value
(matching the seeded admin) and is never written into a preseed file. **Production
must set `CORE_WEBUI_PASSWORD`** (and the API `ADMIN_PASSWORD`) to a strong value:

```bash
CORE_WEBUI_PASSWORD=a-strong-unique-password docker compose up -d --build ui
```

### B. Endpoint-configurable Docker Model Runner (gemma4:31b reachable)

The earlier _"Gemma4:31B connection cannot be reached"_ failure came from a
hardcoded internal endpoint. Phase 15 moves the shared endpoint + model into a
repo-controlled, **non-secret** env file and lets you pick where the runner lives:

- **`config/model-runner/model-runner.env`** (editable, non-secret) holds an
  indexed candidate list:
  `DOCKER_MODEL_RUNNER_DEFAULT_INDEX`, `DOCKER_MODEL_RUNNER_0_*`,
  `DOCKER_MODEL_RUNNER_1_*`, ... . Index `0` is the current workstation Docker
  Desktop runner via `host.docker.internal:12434`; index `1` is the network
  workstation `192.168.100.10:12434`.
- `docker-compose.model-runner.yml` loads it via `env_file:` on the API, WebUI
  preseed, and every `hermes-*` agent (one selected shared model, no per-agent
  drift), adds `extra_hosts: host.docker.internal:host-gateway`, and embeds
  **no model-runner service** — the stack only *connects* to a runner, it never
  starts one.

Choose the runner by editing `DOCKER_MODEL_RUNNER_DEFAULT_INDEX`:

1. **Index 0 — current workstation / Docker Desktop Model Runner**
   `http://host.docker.internal:12434/engines/v1`.
2. **Index 1 — network workstation Model Runner**
   `http://192.168.100.10:12434/engines/v1`.

Each candidate has both a department-agent model (`DOCKER_MODEL_RUNNER_<N>_MODEL`,
usually `gemma4:31b`) and a WebUI direct Hermes model
(`DOCKER_MODEL_RUNNER_<N>_WEBUI_MODEL`, usually `docker.io/ai/gemma4:31B`).

To use **no model runner at all**, simply **don't pass** `docker-compose.model-runner.yml`
— the default stack stays dry-run / model-less.

```bash
make compose-config-model-runner   # validate (no runner needed)
make up-model-runner               # start the FULL stack with the shared runner ON
```

### Full Docker Model Runner stack: all containers, not only UI/API

If you run Compose with a service name at the end, for example
`docker compose ... up -d --build ui` or `docker compose ... up -d --build api ui`,
Docker starts only that selected subset and its dependencies. That is useful for
quick UI work, but it **does not start** the HDFS workers or the four department
Hermes agents (`hermes-er`, `hermes-it`, `hermes-ehs`, `hermes-qc`).

For the complete platform requested here — Docker Model Runner `gemma4:31b`, API
routing, all department Hermes agents, HDFS Namenode/Datanodes, Qdrant, Neo4j,
Redis/Postgres, and core-webui — run the override **without any service name**:

```bash
cd /c/Users/jyim67/Documents/NAPlatform-work/NAPlatform

# If localhost:3000 is occupied, keep UI_HOST_PORT=3001 and open http://localhost:3001.
# If 3000 is free, omit UI_HOST_PORT and open http://localhost:3000.
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml \
  up -d --build --remove-orphans
```

Expected long-running containers after startup:

```text
api, ui, postgres, redis, qdrant, neo4j,
hdfs-namenode, hdfs-datanode-1, hdfs-datanode-2, hdfs-datanode-3,
hermes-er, hermes-it, hermes-ehs, hermes-qc
```

`ui-preseed` and `hdfs-init` are one-shot initializer services; it is normal if
they appear as `Exited (0)` after they finish.

Check everything:

```bash
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml ps

curl -fsS http://localhost:8080/health
curl -fsS http://localhost:3001/health   # use 3000 if UI_HOST_PORT was omitted
```

Chat-specific checks:

```bash
# WebUI must be able to import the Hermes Agent source baked into the UI image;
# otherwise chat fails with "AIAgent not available".
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T ui \
  bash -lc 'test "$HERMES_WEBUI_AGENT_DIR" = /opt/hermes && test -f /opt/hermes/run_agent.py && cd /app && . venv/bin/activate && python - <<"PY"
import os, sys
sys.path.insert(0, os.environ["HERMES_WEBUI_AGENT_DIR"])
from run_agent import AIAgent
print("AIAgent import OK from", os.environ["HERMES_WEBUI_AGENT_DIR"])
PY'

# The model-runner override also seeds ~/.hermes/config.yaml inside the WebUI
# volume so WebUI chat has provider=custom, model=gemma4:31b, and the shared
# Docker Model Runner endpoint.
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T ui \
  cat /home/hermeswebui/.hermes/config.yaml
```

If chat shows `No LLM provider configured`, rerun the full model-runner compose
command above so `ui-preseed` rewrites `/home/hermeswebui/.hermes/config.yaml`.
If chat shows `AIAgent not available`, rebuild the `ui` image from the current
core-webui branch. The UI image now bakes Hermes Agent into `/opt/hermes`; do
not set `HERMES_AGENT_DIR` and do not mount the host user's local Hermes
checkout into the container.

```bash
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml \
  build --no-cache ui
CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml \
  up -d --remove-orphans
```

### HDFS / Hadoop interface: workspace contents 확인

NameNode web UI is exposed on `localhost:9870`. Use it to inspect the HDFS
workspace tree directly:

```text
http://localhost:9870/explorer.html#/naplatform
http://localhost:9870/explorer.html#/naplatform/users/admin
http://localhost:9870/explorer.html#/naplatform/users/admin/workspace
http://localhost:9870/explorer.html#/naplatform/users/admin/chat_history
http://localhost:9870/explorer.html#/naplatform/departments/QC/department_shared
```

The WebUI's Workspace panel is intentionally aligned to those child roots, not
the parent `/naplatform/users/admin` page. In NAPlatform external-auth mode the
default WebUI workspace is an aggregate HDFS view named
`workspace + department_shared`; opening it shows:

- `workspace` → `/naplatform/users/<username>/workspace`
- `department_shared-<DEP>` → `/naplatform/departments/<DEP>/department_shared`

The agent receives only those allowed roots plus
`/naplatform/users/<username>/chat_history`; it must not list or mutate the
parent `/naplatform/users/<username>` or `/naplatform/departments/<DEP>` paths.

In NAPlatform external-auth mode, core-webui also mirrors every successfully
completed chat turn to HDFS as a best-effort backup. After the assistant response
is saved locally, the UI posts the full session transcript to
`POST /workspace/hdfs/chat-history`; the API writes it to
`/naplatform/users/<username>/chat_history/<session_id>.json`. HDFS/API failures
are logged but do not fail the browser chat turn.

The same data can be checked from Windows Git Bash with the Hadoop CLI inside the
NameNode container. Important: set `MSYS_NO_PATHCONV=1`; otherwise Git Bash may
rewrite `/naplatform/...` into a Windows `C:` path before Docker sees it.

```bash
# Full tree
MSYS_NO_PATHCONV=1 CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T hdfs-namenode \
  hdfs dfs -ls -R /naplatform

# User personal workspace
MSYS_NO_PATHCONV=1 CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T hdfs-namenode \
  hdfs dfs -ls /naplatform/users/admin/workspace

# User chat history
MSYS_NO_PATHCONV=1 CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T hdfs-namenode \
  hdfs dfs -ls /naplatform/users/admin/chat_history

# Department shared workspace
MSYS_NO_PATHCONV=1 CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T hdfs-namenode \
  hdfs dfs -ls /naplatform/departments/QC/department_shared

# Read a file
MSYS_NO_PATHCONV=1 CORE_WEBUI_CONTEXT=../core-webui UI_HOST_PORT=3001 \
  docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec -T hdfs-namenode \
  hdfs dfs -cat /naplatform/users/admin/workspace/hello.txt
```

WebHDFS JSON API is also exposed via the NameNode HTTP port:

```bash
curl -fsS 'http://localhost:9870/webhdfs/v1/naplatform/users/admin/workspace?op=LISTSTATUS&user.name=root' | python -m json.tool
curl -fsS 'http://localhost:9870/webhdfs/v1/naplatform/users/admin/workspace/hello.txt?op=OPEN&user.name=root'
```

If the UI/API has not yet touched a user's workspace, that user directory may not
exist until login/provisioning creates it. The HDFS browser should always stay
under `/naplatform/users/<username>/workspace`,
`/naplatform/users/<username>/chat_history`, and
`/naplatform/departments/<department>/department_shared`.

If the output only shows `api`, `ui`, database/cache/vector/graph containers and
not HDFS/Hermes, rerun the full command above. Do **not** append `ui`, `api`, or
`api ui` to the end.

Phase 15 is implemented on `phase/15-login-required-model-runner-config` and
**leaves `main` unchanged**. See **[config/model-runner/README.md](config/model-runner/README.md)**.

## Phase 16 — email/password register + admin approval WebUI gate

Phase 16 replaces the password-only core-webui gate with the NAPlatform account
flow the platform requires:

- `/login` now shows **Email + Password**, not password-only.
- The same page has a **Register** tab. Registration posts to NAPlatform
  `/auth/signup`, creates a `pending` user, and tells the user to wait for admin
  approval.
- Login posts to NAPlatform `/auth/login`. Pending/disabled users stay on the
  login page with an approval-waiting message (`403`).
- After an admin changes the user to `active`, the next email/password login
  mints the local WebUI cookie and redirects to the chat/workspace.
- Compose enables this with `HERMES_WEBUI_AUTH_MODE=naplatform` and does **not**
  configure `HERMES_WEBUI_PASSWORD`, so current NAPlatform builds cannot fall
  back to a password-only WebUI gate.

Recommended local UI run:

```bash
cd ../core-webui && git checkout phase/naplatform-email-register-auth && git pull --ff-only
cd ../NAPlatform
UI_HOST_PORT=3001 docker compose up -d --build ui
# open http://localhost:3001/login
```

Seeded admin for approvals remains `admin@example.com` / `ChangeMe123!`.

## Phase 17 — Admin Hub + workspace access management

Phase 17 adds the administrator landing page requested for admin logins:

- Admin email/password login redirects to `/admin` instead of directly to chat.
- `/admin` shows a designed hub with user counts, pending/active/admin metrics,
  the user table, and an editor panel.
- Admins can update email, username, status, departments, password, and admin role.
- The hub shows the effective HDFS roots for each user: user home
  `/naplatform/users/<username>` with allowed child roots `workspace` and
  `chat_history`, plus department roots
  `/naplatform/departments/<DEPARTMENT>/department_shared`. Changing
  username/departments updates the displayed workspace roots and the API RBAC
  scope.
- core-webui keeps the NAPlatform bearer token server-side only and proxies admin
  calls through `/api/naplatform/admin/users`; no NAPlatform token is exposed to
  the browser.

## Phase 18 — Admin session controls

Phase 18 hardens the admin/user session UX:

- Settings → System now includes a Logout button that calls `/api/auth/logout`, clears the WebUI cookie/server-side NAPlatform token, and returns to `/login`.
- Direct `/admin` navigation is allowed only for authenticated sessions whose NAPlatform login metadata has `is_admin=true`; non-admin sessions receive `403`.
- Admin user edits can request `reset_sessions=true`, which invalidates existing NAPlatform bearer sessions for the selected user. The admin hub exposes this as “Reset existing login sessions after save.”

## Verify
```bash
python -m pip install -r services/api/requirements-dev.txt
pytest -q                       # runs services/api + services/hermes-agent + smoke unit tests
python -m compileall services/api/app services/hermes-agent/hermes_agent scripts
docker compose -f docker-compose.yml config                                   # default (dry-run) stack
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml config
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml config   # shared gemma4:31b model runner
docker compose -f docker-compose.yml build hermes-er api
```
Or use the Makefile: `make test`, `make compile`, `make compose-config`, `make compose-config-routing`, `make compose-config-model-runner`, `make build`.

Local seeded admin: `admin@example.com` / `ChangeMe123!`.

## Branching

See `docs/BRANCHING.md` for the policy and `docs/ROADMAP.md` for full phase status. Phase branches merge into `dev`; `main` remains the stable/default branch and is updated from `dev` only after all planned phases are complete. The upload/release workflow is explicit via `make push-phase`, `make merge-phase-to-dev`, and (release only) `make release-dev-to-main`. Phase 11 is implemented on `phase/11-core-webui-auth-session-integration` and **leaves `main` unchanged**.

### UI port conflict: host port 3000 already in use

The UI container listens on port `8787` internally and publishes to host port `3000` by default. If Docker reports `address already in use` for `0.0.0.0:3000`, keep the same stack and choose another host port:

```bash
UI_HOST_PORT=3001 CORE_WEBUI_CONTEXT=../core-webui docker compose up -d --build ui
```

Then open:

```text
http://localhost:3001
```

PowerShell equivalent:

```powershell
$env:UI_HOST_PORT = "3001"
$env:CORE_WEBUI_CONTEXT = "..\core-webui"
docker compose up -d --build ui
```

To discover what owns port 3000 on Windows:

```powershell
netstat -ano | findstr :3000
```

The first-run preseed still runs exactly the same; only the host URL changes.

