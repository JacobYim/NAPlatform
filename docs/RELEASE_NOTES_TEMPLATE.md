# NAPlatform Release Notes — vX.Y.Z (YYYY-MM-DD)

> Copy this template per release. Fill in every section; delete the guidance
> lines in angle brackets. Do not include secrets, tokens, or credentials.

## Summary

<One paragraph: what this release delivers and who it is for.>

## Highlights

- <User- or operator-facing highlight.>
- <...>

## Phases included

<Which roadmap phases (docs/ROADMAP.md) this release promotes from `dev` to
`main`. e.g. "Phases 00–12".>

## Changes

### Added
- <...>

### Changed
- <...>

### Fixed
- <...>

### Security
- <Auth/RBAC, CORS/trusted-origin, security-header, or audit changes.>

## Configuration / migration notes

- New/changed environment variables (see `.env.production.example`):
  - <VAR — purpose, default, whether required in production.>
- Datastore migrations: <none / describe>.
- Backend selection changes (`VECTOR_BACKEND` / `GRAPH_BACKEND` / routing): <...>.

## Production readiness

- [ ] `PRODUCTION_MODE=true make release-check` passes (readiness gate green).
- [ ] `GET /admin/release/readiness` reports `ready: true` on the staging deploy.
- [ ] Secrets set/rotated; no shipped defaults (`ADMIN_PASSWORD` overridden).

## Verification performed

```bash
pytest -q
python -m compileall services/api/app services/hermes-agent/hermes_agent scripts
docker compose -f docker-compose.yml config
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml config
docker compose --env-file .env.production.example -f docker-compose.yml config   # make compose-config-prod
make smoke-all-dry-run
make smoke-all-routing
make smoke-resources
```

## Known issues / follow-ups

- <...>

## Rollback

<How to roll back this release if needed.>
