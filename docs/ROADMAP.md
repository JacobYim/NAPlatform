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

## Phase status (through Phase 09)

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
| **09** | **Resource E2E smoke + explicit phase upload/release workflow** | 🔄 **In progress** |
| 10+ | Live backing services (real Qdrant/Neo4j/HDFS drivers), production hardening, stable release to `main` | ⏳ Upcoming |

## Current phase — Phase 09 (in progress)

Phase 09 adds a **resource-focused** end-to-end smoke (HDFS workspace/provisioning,
vector and graph scope, cross-department denial, audit) that complements the Phase 08
routing smoke, and makes the git upload/release workflow explicit through Makefile
targets so `main` is never touched by accident.

Deliverables:

- `scripts/smoke_resources_e2e.py` — live resource smoke: admin login; idempotent
  active QC and IT users; `GET /workspace/hdfs` returns only the caller's own
  personal + department roots; `POST /admin/users/{id}/provision-hdfs` is a dry run
  (commands planned, nothing executed); personal/department vector and graph
  insert+search are scoped correctly (a QC user's records are never visible to IT);
  cross-department denial (`403`) for vector, graph, and resource routes; and the
  audit log contains the key resource events. It is idempotent and never prints
  secrets.
- `services/api/tests/test_smoke_resources_e2e.py` — unit tests driving the smoke
  logic against a fake API (`httpx.MockTransport`); **no Docker required**.
- Makefile targets `smoke-resources`, `smoke-all-dry-run`, `smoke-all-routing`.
- Explicit phase upload/release Makefile targets (see below).
- Docs: this roadmap plus resource-smoke and upload-workflow updates to
  `README.md`, `docs/CONTAINER_GUIDE.md`, and `docs/ARCHITECTURE.md`.

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
# Phase 09 example (main is never updated by steps 1–2):
make push-phase                         # pushes phase/09-resource-e2e-smoke
make merge-phase-to-dev                 # merges phase/09-... into dev, pushes dev
# ...only after ALL planned phases are done and a stable release is intended:
make release-dev-to-main                # the ONLY step that updates main
```

Override the branch explicitly if needed:

```bash
make push-phase PHASE_BRANCH=phase/09-resource-e2e-smoke
make merge-phase-to-dev PHASE_BRANCH=phase/09-resource-e2e-smoke
```

## Verify Phase 09 locally

```bash
make smoke-unit        # routing + resource smoke unit tests (no Docker)
make test              # full pytest suite
make compile           # byte-compile api + hermes-agent + scripts
make smoke-resources   # in-cluster resource smoke (needs a running stack)
make smoke-all-dry-run # routing (dry-run) + resource smoke against the default stack
```

`main` remains the stable baseline until the final release. Phase 09 is implemented
on `phase/09-resource-e2e-smoke` and **leaves `main` unchanged**.
