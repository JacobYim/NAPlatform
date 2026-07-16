# NAPlatform

Docker Compose based multi-department Hermes Agent platform for ER, IT, EHS, QC.

Includes PRD/architecture, FastAPI RBAC scaffold, Redis/Postgres/Qdrant/Neo4j/HDFS Compose topology, core-webui runtime UI integration, department Hermes agent containers, and tests.

Phase 02 core-webui auth/agent adapter stub is ready: `GET /core-webui/session` bootstrap, `POST /agents/{department}/chat` (deterministic stub, RBAC-scoped), `GET /resources/{department}` HDFS-root enforcement, and `GET /admin/approvals/pending`. Real Hermes invocation is the next phase.

The actual post-login runtime UI is `github.com/JacobYim/core-webui`. The Compose `ui` service builds that repository and applies HMGMA branding with `BRAND_NAME=HMGMA` and the included `branding/logo.jpg` (`HMG Metaplant America`).

## Verify
```bash
python -m pip install -r services/api/requirements-dev.txt
pytest -q
python -m compileall services/api/app
docker compose config
```

Local seeded admin: `admin@example.com` / `ChangeMe123!`.
