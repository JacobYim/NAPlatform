"""Phase 12: production hardening — runtime config validation + release readiness.

This module is the single source of truth for "is this process configured to run
in production?" It never *enforces* anything at import time (so tests and local
dev keep their permissive, no-infrastructure defaults) — instead it produces a
**redacted** readiness report that the `/admin/release/readiness` endpoint and
the `make release-check` target consume.

Design rules:
- **Dev stays permissive.** When ``PRODUCTION_MODE`` is not truthy the report is
  always ``ready`` and every check is informational, never blocking. Nothing here
  changes the default (memory/dry-run, SQLite, in-memory session) behavior.
- **Production is strict but non-fatal here.** When ``PRODUCTION_MODE=true`` the
  required checks decide ``ready``; the API still boots (so an operator can hit
  the endpoint to see *why* it is not ready) — promotion to ``main`` is the gate,
  not process startup.
- **No secrets ever leave this module.** Checks report booleans and redacted
  URLs (userinfo/query stripped), never the secret value itself.
"""
import os
from urllib.parse import urlparse

# The seeded-admin default password. A production deployment MUST override it.
DEFAULT_ADMIN_PASSWORD = "ChangeMe123!"

# Safe dev defaults for browser access. Production must set TRUSTED_ORIGINS to the
# real front-end origin(s); a bare "*" is rejected in production (credentials +
# wildcard origin is unsafe and disallowed by browsers anyway).
DEFAULT_TRUSTED_ORIGINS = ("http://localhost:3000", "http://localhost:8080")

# Default retention window documented for operators. Retention is **advisory** by
# default — nothing in this scaffold deletes audit rows unless an operator opts in
# by wiring an external job; `AUDIT_RETENTION_ENFORCE` gates any destructive path.
DEFAULT_AUDIT_RETENTION_DAYS = 365


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def production_mode() -> bool:
    """True when the process is explicitly told it is a production deployment."""
    return _truthy("PRODUCTION_MODE")


def _redact_url(url: str) -> str:
    """Strip userinfo and query so a connection string can be echoed safely."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable>"
    if not parsed.scheme:
        # Not a URL (e.g. a bare host); return only the scheme-less shape.
        return url.split("?", 1)[0]
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{port}{path}".rstrip("/") or f"{parsed.scheme}://"


def trusted_origins() -> list[str]:
    """Resolve the CORS trusted-origin allow-list.

    ``TRUSTED_ORIGINS`` is a comma-separated list. Unset falls back to the safe
    dev defaults (localhost UI + API). ``*`` is honored only as an explicit
    single value and is treated as "allow all" for local dev; production
    readiness flags it as unsafe.
    """
    raw = os.environ.get("TRUSTED_ORIGINS")
    if raw is None or not raw.strip():
        return list(DEFAULT_TRUSTED_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


# Phase 13: provider labels that all mean "Docker Model Runner".
_DMR_PROVIDER_ALIASES = ("docker-model-runner", "docker_model_runner",
                         "model-runner", "modelrunner", "dmr")


def _model_runner_candidates(env: dict | None = None) -> list[dict]:
    """Return numbered, non-secret Docker Model Runner candidates."""
    env = os.environ if env is None else env
    candidates: list[dict] = []
    for idx in range(20):
        base_url = (env.get(f"DOCKER_MODEL_RUNNER_{idx}_BASE_URL") or "").strip()
        model = (env.get(f"DOCKER_MODEL_RUNNER_{idx}_MODEL") or "").strip()
        if not base_url and not model:
            continue
        candidates.append({
            "index": idx,
            "name": (env.get(f"DOCKER_MODEL_RUNNER_{idx}_NAME") or f"runner-{idx}").strip(),
            "base_url": base_url,
            "model": model,
            "webui_model": (env.get(f"DOCKER_MODEL_RUNNER_{idx}_WEBUI_MODEL") or "").strip(),
        })
    return candidates


def _selected_model_runner_from_list(env: dict | None = None) -> dict | None:
    env = os.environ if env is None else env
    candidates = _model_runner_candidates(env)
    if not candidates:
        return None
    try:
        selected = int((env.get("DOCKER_MODEL_RUNNER_DEFAULT_INDEX") or "0").strip())
    except (TypeError, ValueError):
        selected = 0
    for item in candidates:
        if item["index"] == selected and item.get("base_url") and item.get("model"):
            return item
    for item in candidates:
        if item.get("base_url") and item.get("model"):
            return item
    return None


def model_runtime_status() -> dict:
    """Redacted view of the shared LLM / Docker Model Runner config (secret-free).

    All department Hermes agents share one selected model-runner candidate from
    ``DOCKER_MODEL_RUNNER_<N>_*`` entries, unless a single-value runtime env
    override is supplied. The base URL is redacted (userinfo/query stripped) and
    the API key is reported only as a boolean.
    """
    provider = (os.environ.get("HERMES_LLM_PROVIDER")
                or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if provider in _DMR_PROVIDER_ALIASES:
        provider = "docker-model-runner"
    selected_runner = _selected_model_runner_from_list()
    listed_base_url = (selected_runner or {}).get("base_url", "")
    listed_model = (selected_runner or {}).get("model", "")
    base_url = (os.environ.get("DOCKER_MODEL_RUNNER_BASE_URL")
                or listed_base_url
                or os.environ.get("OPENAI_BASE_URL") or "").strip()
    model = (os.environ.get("DOCKER_MODEL_RUNNER_MODEL")
             or listed_model
             or os.environ.get("OPENAI_MODEL") or "").strip()
    api_key_present = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    selected_index = selected_runner.get("index") if selected_runner else None
    candidates = []
    for item in _model_runner_candidates():
        candidates.append({
            "index": item["index"],
            "name": item["name"],
            "base_url": _redact_url(item["base_url"]) or None,
            "model": item["model"] or None,
            "webui_model": item["webui_model"] or None,
            "selected": item["index"] == selected_index,
        })
    return {
        "provider": provider or None,
        "configured": bool(provider and base_url and model),
        "base_url": _redact_url(base_url) or None,
        "model": model or None,
        "openai_compatible": True,
        "api_key_present": api_key_present,
        "selected_runner_index": selected_index,
        "runners": candidates,
    }


def retention_policy() -> dict:
    """Audit retention policy — documented, non-destructive by default."""
    try:
        days = int(os.environ.get("AUDIT_RETENTION_DAYS", str(DEFAULT_AUDIT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_AUDIT_RETENTION_DAYS
    return {
        "retention_days": days,
        # Destructive deletion is OFF unless an operator explicitly enables it.
        # This scaffold never deletes audit rows; the flag documents the contract
        # for an external retention job.
        "destructive_deletion_enabled": _truthy("AUDIT_RETENTION_ENFORCE"),
    }


def _check(name: str, ok: bool, required: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "required": bool(required),
            "severity": "error" if (required and not ok) else ("info" if ok else "warning"),
            "detail": detail}


def readiness_checks() -> list[dict]:
    """Build the redacted list of production-readiness checks.

    In dev mode (``PRODUCTION_MODE`` unset) every check is ``required=False`` and
    purely informational. In production mode the checks that gate a real
    deployment become ``required`` and their failure makes the report not-ready.
    """
    prod = production_mode()
    checks: list[dict] = []

    # --- admin password must not be the shipped default -------------------
    admin_pw = os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
    admin_ok = bool(admin_pw) and admin_pw != DEFAULT_ADMIN_PASSWORD
    checks.append(_check(
        "admin_password", admin_ok, prod,
        "ADMIN_PASSWORD is overridden" if admin_ok
        else "ADMIN_PASSWORD is unset or still the shipped default"))

    # --- durable database (not the in-memory/SQLite scaffold) -------------
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    db_ok = bool(db_url) and not db_url.startswith("sqlite")
    checks.append(_check(
        "database_url", db_ok, prod,
        f"DATABASE_URL={_redact_url(db_url)}" if db_ok
        else "DATABASE_URL is unset or a SQLite scaffold URL"))

    # --- session backend: Redis, or explicitly strict --------------------
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    strict = _truthy("SESSION_STORE_STRICT")
    session_ok = bool(redis_url) or strict
    checks.append(_check(
        "session_store", session_ok, prod,
        (f"REDIS_URL={_redact_url(redis_url)}" if redis_url else "SESSION_STORE_STRICT enabled")
        if session_ok else "neither REDIS_URL nor SESSION_STORE_STRICT is set"))

    # --- CORS trusted origins configured and not a wildcard --------------
    origins_raw = (os.environ.get("TRUSTED_ORIGINS") or "").strip()
    origins = trusted_origins()
    cors_ok = bool(origins_raw) and "*" not in origins
    checks.append(_check(
        "trusted_origins", cors_ok, prod,
        f"TRUSTED_ORIGINS={origins}" if cors_ok
        else "TRUSTED_ORIGINS is unset or contains a wildcard '*'"))

    # --- backend URLs required only when the real backend is selected -----
    vector_backend = (os.environ.get("VECTOR_BACKEND", "memory").strip().lower() or "memory")
    if vector_backend == "qdrant":
        qurl = (os.environ.get("QDRANT_URL") or "").strip()
        checks.append(_check(
            "qdrant_url", bool(qurl), prod,
            f"QDRANT_URL={_redact_url(qurl)}" if qurl
            else "VECTOR_BACKEND=qdrant but QDRANT_URL is unset"))

    graph_backend = (os.environ.get("GRAPH_BACKEND", "memory").strip().lower() or "memory")
    if graph_backend == "neo4j":
        nuri = (os.environ.get("NEO4J_URI") or "").strip()
        nuser = (os.environ.get("NEO4J_USER") or "").strip()
        npass = (os.environ.get("NEO4J_PASSWORD") or "").strip()
        neo_ok = bool(nuri) and bool(nuser) and bool(npass)
        checks.append(_check(
            "neo4j_connection", neo_ok, prod,
            f"NEO4J_URI={_redact_url(nuri)} (user/password set)" if neo_ok
            else "GRAPH_BACKEND=neo4j but NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD are incomplete"))

    # --- routing URLs required only when routing is enabled ---------------
    routing_enabled = _truthy("AGENT_ROUTING_ENABLED")
    if routing_enabled:
        env_keys = {"ER": "HERMES_ER_URL", "IT": "HERMES_IT_URL",
                    "EHS": "HERMES_EHS_URL", "QC": "HERMES_QC_URL"}
        configured = sorted(dep for dep, key in env_keys.items()
                            if (os.environ.get(key) or "").strip())
        routing_ok = bool(configured)
        checks.append(_check(
            "agent_routing_urls", routing_ok, prod,
            f"AGENT_ROUTING_ENABLED with configured departments {configured}" if routing_ok
            else "AGENT_ROUTING_ENABLED=true but no HERMES_{DEP}_URL is configured"))

    return checks


def readiness_report() -> dict:
    """Aggregate the readiness checks into a redacted, decision-ready report."""
    prod = production_mode()
    checks = readiness_checks()
    required_failed = [c["name"] for c in checks if c["required"] and not c["ok"]]
    # Dev mode is always ready (permissive); production readiness gates on the
    # required checks only.
    ready = (not prod) or not required_failed
    return {
        "production_mode": prod,
        "mode": "production" if prod else "development",
        "ready": ready,
        "checks": checks,
        "required_failed": required_failed,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "failed": sum(1 for c in checks if not c["ok"]),
            "required_failed": len(required_failed),
        },
        "cors": {"trusted_origins": trusted_origins()},
        "retention": retention_policy(),
    }


# The final release checklist mirrored by docs/FINAL_RELEASE_CHECKLIST.md. Each
# item is a gate an operator confirms *before* invoking `make release-dev-to-main`.
# `automated` items are the ones `make release-check` / CI can verify; the rest are
# human sign-offs. This structure is redaction-free (no secrets) by construction.
RELEASE_CHECKLIST = [
    {"id": "tests", "title": "pytest suite green", "automated": True,
     "command": "pytest -q"},
    {"id": "compile", "title": "byte-compile clean", "automated": True,
     "command": "python -m compileall services/api/app services/hermes-agent/hermes_agent scripts"},
    {"id": "compose_default", "title": "default (dry-run) compose config valid", "automated": True,
     "command": "docker compose -f docker-compose.yml config"},
    {"id": "compose_routing", "title": "routing compose config valid", "automated": True,
     "command": "docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml config"},
    {"id": "readiness", "title": "production readiness checks pass (PRODUCTION_MODE=true)", "automated": True,
     "command": "make release-check"},
    {"id": "smoke_dry", "title": "smoke-all-dry-run green", "automated": False,
     "command": "make smoke-all-dry-run"},
    {"id": "smoke_routing", "title": "smoke-all-routing green", "automated": False,
     "command": "make smoke-all-routing"},
    {"id": "secrets_rotated", "title": "production secrets set/rotated (no shipped defaults)", "automated": False,
     "command": None},
    {"id": "approval", "title": "explicit human approval to promote dev to main", "automated": False,
     "command": "make release-dev-to-main"},
]


def release_checklist_status() -> dict:
    """The final release checklist plus the automated readiness result.

    The automated `readiness` item's status is derived from the live report; every
    other item's ``status`` is ``pending`` until an operator confirms it (this
    scaffold never auto-marks a human sign-off complete, and never promotes main).
    """
    report = readiness_report()
    items = []
    for item in RELEASE_CHECKLIST:
        status = "pending"
        if item["id"] == "readiness":
            status = "passed" if report["ready"] else "blocked"
        items.append({**item, "status": status})
    return {
        "ready_for_release": report["ready"],
        "release_requires_explicit_approval": True,
        "note": ("This endpoint never promotes `main`. Run `make release-dev-to-main` "
                 "only after explicit approval and all items are confirmed."),
        "readiness": report,
        "checklist": items,
    }
