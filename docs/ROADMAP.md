# NAPlatform Development Roadmap & Status

This roadmap tracks the phased delivery of NAPlatform — a Docker Compose based
multi-department (ER / IT / EHS / QC) Hermes Agent platform with per-user RBAC over
HDFS, tools, MCP, Qdrant, and Neo4j.

**Branch stability:** `main` is the stable release baseline and stays **unchanged**
between phases. Every phase is built on a `phase/NN-*` branch, merged into `dev`
for integration, and `dev` is promoted to `main` **only after all planned phases
are complete** and ready for a stable release. See [BRANCHING.md](BRANCHING.md).

## Status legend

- ✅ **Completed** — merged into `dev`, tests/compose-config green.
- 🔄 **In progress** — active phase branch.
- ⏳ **Upcoming** — planned, not yet started.

## Phase status (through Phase 10)

| Phase | Title | Status |
|------:|-------|--------|
| 00 | PRD, Compose topology, RBAC scaffold | ✅ Completed |
| 01 | core-webui integration (HMGMA runtime UI) | ✅ Completed |
| 02 | core-webui auth/agent adapter (session, chat, resources, approvals) | ✅ Completed |
| 03 | Persistent auth/RBAC (SQLAlchemy/Postgres, Redis sessions, audit log) | ✅ Completed |
| 04 | HDFS workspace provisioning (dry-run by default) | ✅ Completed |
| 05 | Qdrant/Neo4j scope adapters (metadata-separated) | ✅ Completed |
| 06 | Department Hermes agent routing (dry-run by default, SSRF-safe) | ✅ Completed |
| 07 | Hermes agent HTTP service (deterministic by default) | ✅ Completed |
| 08 | Routing E2E Compose/smoke (default stack stays dry-run) | ✅ Completed |
| 09 | Resource E2E smoke + explicit phase upload/release workflow | ✅ Completed |
| **10** | **Real Qdrant/Neo4j/HDFS backend adapters (memory/dry-run by default)** | 🔄 **In progress** |
| 11+ | Production hardening (live-backend E2E, secrets/auth, stable release to `main`) | ⏳ Upcoming |

## Current phase — Phase 10 (in progress)

Phase 10 adds **real backend integration scaffolds** for Qdrant, Neo4j, and HDFS
behind the *same* scope/RBAC contract as the Phase 05 memory adapters, selectable
per backend by env. The default stays **memory / dry-run**, so no live backing
service is required for tests or the default stack; tests drive the real code paths
through fake clients/drivers/runners.

Deliverables:

- Backend selection envs (default memory/dry-run): `VECTOR_BACKEND=memory|qdrant`,
  `GRAPH_BACKEND=memory|neo4j`, plus the existing `HDFS_PROVISIONING_ENABLED`.
- `app/vector.py` — `QdrantVectorBackend` + client wrapper: uses `qdrant-client`
  when installed/configured (`QDRANT_URL`, optional `QDRANT_API_KEY`), creates the
  collection on demand with configurable `QDRANT_VECTOR_SIZE`/`QDRANT_DISTANCE`,
  and upserts/searches with the **same** metadata enforcement + Qdrant `Filter`
  descriptors. The in-memory `VectorScopeAdapter` is unchanged.
- `app/graph.py` — `Neo4jGraphBackend` + driver wrapper: uses the `neo4j` driver
  when installed/configured (`NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD`), runs
  **parameterised Cypher only** (values bound as `$params`, only validated
  labels/rel-types inlined), same metadata enforcement. The in-memory
  `GraphScopeAdapter` is unchanged.
- `app/hdfs.py` — `HdfsProvisioner.health()` readiness probe using a constant safe
  argv (`hdfs dfs -test -d /naplatform`); dry-run by default, executes only when
  enabled via the injected/subprocess runner.
- `GET /admin/backends/status` (admin-only) — active vector/graph/hdfs modes,
  redacted URLs, and a dry-run/fake health status that never leaks secrets
  (api keys / passwords are reported only as booleans).
- Resolvers fall back to the memory backend (with a logged warning) for an invalid
  backend env or an unconfigured/absent real client, so the API never fails to
  start because of a backend env.
- `services/api/tests/test_backends.py` — fake-driven tests: memory default
  unchanged, Qdrant upsert/search/filter/collection creation, Neo4j params/no
  interpolation, backends status admin-only + no secrets, HDFS health planning,
  invalid-env rejection + safe fallback. **No live Qdrant/Neo4j/HDFS required.**
- Requirements add `qdrant-client` and `neo4j` (optional at runtime); docs updated
  (`README.md`, `docs/ARCHITECTURE.md`, `docs/CONTAINER_GUIDE.md`, this roadmap).

Phase 09 (resource E2E smoke + explicit phase upload/release workflow) is complete
and merged into `dev`.

## Phase upload / release workflow (explicit, main stays stable)

The upload path is intentionally a set of separate, explicit steps. `main` moves
**only** when `release-dev-to-main` is invoked at a full release — no other target
touches it. All targets honor the `PHASE_BRANCH` variable (default: the current
branch).

| Step | Make target | Touches `main`? | What it does |
|------|-------------|-----------------|--------------|
| 1. Upload the phase branch | `make push-phase` | No | `git push -u origin $(PHASE_BRANCH)` |
| 2. Integrate into dev | `make merge-phase-to-dev` | No | checkout `dev`, merge `$(PHASE_BRANCH)`, push `dev` |
| 3. Release (final only) | `make release-dev-to-main` | **Yes** | checkout `main`, merge `dev`, push `main` |

```bash
# Phase 10 example (main is never updated by steps 1–2):
make push-phase                         # pushes phase/10-real-backend-adapters
make merge-phase-to-dev                 # merges phase/10-... into dev, pushes dev
# ...only after ALL planned phases are done and a stable release is intended:
make release-dev-to-main                # the ONLY step that updates main
```

Override the branch explicitly if needed:

```bash
make push-phase PHASE_BRANCH=phase/09-resource-e2e-smoke
make merge-phase-to-dev PHASE_BRANCH=phase/09-resource-e2e-smoke
```

## Verify Phase 10 locally

```bash
make smoke-unit        # routing + resource smoke unit tests (no Docker)
make test              # full pytest suite (incl. test_backends.py — fake backends)
make compile           # byte-compile api + hermes-agent + scripts
make compose-config    # validate the default (memory/dry-run) Compose config
make build             # build the api + hermes-agent images
```

Try the real backends without leaving dry-run for the rest of the stack:

```bash
# Bring up the shared services, then run the API with a real backend selected:
docker compose up -d qdrant neo4j
VECTOR_BACKEND=qdrant QDRANT_URL=http://localhost:6333 \
GRAPH_BACKEND=neo4j NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j \
NEO4J_PASSWORD=naplatform-password \
  uvicorn app.main:app --app-dir services/api --port 8080
# Inspect active modes (admin-only, secrets redacted):
curl -s localhost:8080/admin/backends/status -H "Authorization: Bearer $ADMIN_TOKEN"
```

`main` remains the stable baseline until the final release. Phase 10 is implemented
on `phase/10-real-backend-adapters` and **leaves `main` unchanged**.
