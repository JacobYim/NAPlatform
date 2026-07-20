# NAPlatform Final Release Checklist (Phase 12)

The single gate between the integrated `dev` branch and the stable `main`
baseline. `main` moves **only** at `make release-dev-to-main`, and **only after
explicit human approval** and every item below is confirmed. No automated step —
including `make release-check` — ever pushes, merges, or checks out `main`.

See [ROADMAP.md](ROADMAP.md) for phase status and [BRANCHING.md](BRANCHING.md)
for the branch policy.

## 1. Host-side checks (no Docker)

```bash
pytest -q
python -m compileall services/api/app services/hermes-agent/hermes_agent scripts
```

## 2. Compose config validation

```bash
# default (dry-run) stack
docker compose -f docker-compose.yml config
# enabled-routing stack
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml config
# production env template applied (make compose-config-prod)
docker compose --env-file .env.production.example -f docker-compose.yml config
```

## 3. Production readiness gate

```bash
# Permissive in dev; enforces the required checks when PRODUCTION_MODE=true.
make release-check
# With the real production env filled in (.env.production, git-ignored):
PRODUCTION_MODE=true make release-check
```

`make release-check` runs pytest + compileall + both compose configs + the
readiness gate. It **does not touch `main`**. On a running API, confirm the same
via the admin endpoint (redacted, no secrets):

```bash
curl -s localhost:8080/admin/release/readiness \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.ready_for_release, .readiness.required_failed'
```

Required checks (only enforced when `PRODUCTION_MODE=true`):

- `ADMIN_PASSWORD` overridden (not the shipped default).
- `DATABASE_URL` set to a durable (non-SQLite) database.
- `REDIS_URL` set **or** `SESSION_STORE_STRICT=true`.
- `TRUSTED_ORIGINS` set to concrete origins (no wildcard `*`).
- `QDRANT_URL` set when `VECTOR_BACKEND=qdrant`.
- `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` set when `GRAPH_BACKEND=neo4j`.
- At least one `HERMES_{DEP}_URL` set when `AGENT_ROUTING_ENABLED=true`.

## 4. In-cluster smoke (needs Docker)

```bash
make smoke-all-dry-run     # routing (dry-run) + resource smoke, default stack
make smoke-all-routing     # routing (enabled) + resource smoke, routing stack
make smoke-resources       # resource smoke only (hdfs/vector/graph/audit scope)
make smoke-final           # smoke-all-dry-run + smoke-all-routing
```

## 5. Secrets & configuration

- [ ] `.env.production` filled from `.env.production.example`; **no shipped
      defaults** survive (`ADMIN_PASSWORD` changed, DB/Redis/Neo4j passwords set).
- [ ] No secret is committed; `.env.production` is git-ignored.
- [ ] Audit retention reviewed (`AUDIT_RETENTION_DAYS`); destructive deletion
      stays **off** (`AUDIT_RETENTION_ENFORCE=false`) unless explicitly intended.

## 6. Release notes

- [ ] `docs/RELEASE_NOTES_TEMPLATE.md` copied and filled for this version.

## 7. Promote to main — explicit approval only

> Do this **only** after all of the above are confirmed and a human has explicitly
> approved the release. This is the single step that updates `main`.

```bash
make release-dev-to-main   # merges dev into main and pushes (updates main)
```

Until this command is run with approval, `main` remains the stable baseline and
is untouched by every other target.
