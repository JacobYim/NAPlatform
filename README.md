# NAPlatform

Docker Compose based multi-department Hermes Agent platform for ER, IT, EHS, QC.

Includes PRD/architecture, FastAPI RBAC scaffold, Redis/Postgres/Qdrant/Neo4j/HDFS Compose topology, UI placeholder, department Hermes agent containers, and tests.

## Verify
```bash
python -m pip install -r services/api/requirements-dev.txt
PYTHONPATH=services/api pytest -q
python -m compileall services/api/app
docker compose config
```

Local seeded admin: `admin@example.com` / `ChangeMe123!`.
