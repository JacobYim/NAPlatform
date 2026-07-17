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

- **Command plan** — personal dir `/naplatform/users/{username}` gets `mkdir -p` → `chown/chgrp` (placeholder) → `chmod 700` → `setfacl -m user:{username}:rwx`; each department dir `/naplatform/departments/{DEP}` gets `chmod 770` → `setfacl -m group:naplatform-{dep}:rwx` plus `setfacl -m user:{username}:rwx`.
- **Validation** — usernames must match `^[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}$` (no leading dot/dash, no `..`), departments must be known, and every built path is re-checked to stay under `/naplatform` with no traversal.
- **Dry-run vs enabled** — with `HDFS_PROVISIONING_ENABLED` unset/false (the default) provisioning is a **dry run**: it returns the planned commands and spawns **no subprocess**. Set `HDFS_PROVISIONING_ENABLED=true` to actually run each command via `subprocess.run` (argv list, no shell) and capture `returncode/stdout/stderr`.
- **Endpoints** — `POST /admin/users/{user_id}/provision-hdfs` (admin-only) returns the provision plan/results for a user's personal + department dirs; `GET /workspace/hdfs` (active user) returns the caller's own personal root, department roots, and the dry-run plan with provisioning status.
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

The actual post-login runtime UI is `github.com/JacobYim/core-webui`. The Compose `ui` service builds that repository and applies HMGMA branding with `BRAND_NAME=HMGMA` and the included `branding/logo.jpg` (`HMG Metaplant America`).

## Verify
```bash
python -m pip install -r services/api/requirements-dev.txt
pytest -q
python -m compileall services/api/app
docker compose config
```

Local seeded admin: `admin@example.com` / `ChangeMe123!`.

## Branching

See `docs/BRANCHING.md`. Phase branches merge into `dev`; `main` remains the stable/default branch and is updated from `dev` only after all planned phases are complete.
