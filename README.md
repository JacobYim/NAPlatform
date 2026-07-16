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
