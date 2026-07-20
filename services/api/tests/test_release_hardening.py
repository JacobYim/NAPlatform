"""Phase 12: production hardening & release prep.

Covers the readiness validation (dev-permissive / production-strict / redaction),
the CORS + security-header middleware, the admin-only audit export
(authz/filtering/no password material), the dev defaults staying unchanged, and
the docs/Makefile release guard (main only ever moves at release-dev-to-main).
No live infrastructure — the API runs its default memory/dry-run/SQLite stack.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)
REPO_ROOT = Path(__file__).resolve().parents[3]

PROD_REQUIRED = ("admin_password", "database_url", "session_store", "trusted_origins")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} at {path}"
    return path.read_text(encoding="utf-8")


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def _make_active_user(email, username, departments):
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    from app.main import store
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    token = client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']
    return u, token


# =========================================================================
# Readiness: dev is permissive, production is strict
# =========================================================================
def test_dev_mode_is_permissive_and_ready(monkeypatch):
    monkeypatch.delenv("PRODUCTION_MODE", raising=False)
    report = config.readiness_report()
    assert report["production_mode"] is False
    assert report["mode"] == "development"
    assert report["ready"] is True
    # Every check is informational in dev — none is required.
    assert all(c["required"] is False for c in report["checks"])
    assert report["required_failed"] == []


def test_production_mode_fails_without_config(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    for var in ("ADMIN_PASSWORD", "DATABASE_URL", "REDIS_URL", "SESSION_STORE_STRICT",
                "TRUSTED_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    report = config.readiness_report()
    assert report["production_mode"] is True
    assert report["ready"] is False
    assert set(PROD_REQUIRED) <= set(report["required_failed"])
    # The required checks are marked error severity.
    failed = {c["name"]: c for c in report["checks"] if not c["ok"]}
    for name in PROD_REQUIRED:
        assert failed[name]["required"] is True
        assert failed[name]["severity"] == "error"


def test_production_mode_passes_with_full_config(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-unique-password")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nap:pw@postgres:5432/naplatform")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("TRUSTED_ORIGINS", "https://app.example.com")
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_ROUTING_ENABLED", raising=False)
    report = config.readiness_report()
    assert report["ready"] is True
    assert report["required_failed"] == []


def test_session_store_strict_satisfies_session_check(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("SESSION_STORE_STRICT", "true")
    session = next(c for c in config.readiness_report()["checks"] if c["name"] == "session_store")
    assert session["ok"] is True


def test_wildcard_trusted_origins_fails_in_production(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.setenv("TRUSTED_ORIGINS", "*")
    cors = next(c for c in config.readiness_report()["checks"] if c["name"] == "trusted_origins")
    assert cors["ok"] is False


def test_backend_and_routing_checks_only_when_selected(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_ROUTING_ENABLED", raising=False)
    names = {c["name"] for c in config.readiness_report()["checks"]}
    assert "qdrant_url" not in names and "neo4j_connection" not in names
    assert "agent_routing_urls" not in names
    # Selecting the real backends / routing adds the required connection checks.
    monkeypatch.setenv("VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("AGENT_ROUTING_ENABLED", "true")
    for var in ("QDRANT_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
                "HERMES_ER_URL", "HERMES_IT_URL", "HERMES_EHS_URL", "HERMES_QC_URL"):
        monkeypatch.delenv(var, raising=False)
    report = config.readiness_report()
    names = {c["name"] for c in report["checks"]}
    assert {"qdrant_url", "neo4j_connection", "agent_routing_urls"} <= names
    assert {"qdrant_url", "neo4j_connection", "agent_routing_urls"} <= set(report["required_failed"])


def test_readiness_report_redacts_secrets(monkeypatch):
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "super-secret-admin-pw")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nap:db-secret-pw@postgres:5432/naplatform")
    monkeypatch.setenv("REDIS_URL", "redis://:redis-secret@redis:6379/0")
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-secret-pw")
    blob = str(config.readiness_report())
    for secret in ("super-secret-admin-pw", "db-secret-pw", "redis-secret", "neo4j-secret-pw"):
        assert secret not in blob, f"secret {secret!r} leaked into readiness report"
    # The redacted DB URL keeps host/port but drops the credentials.
    db = next(c for c in config.readiness_report()["checks"] if c["name"] == "database_url")
    assert "postgres:5432" in db["detail"] and "db-secret-pw" not in db["detail"]


# =========================================================================
# /admin/release/readiness endpoint
# =========================================================================
def test_release_readiness_admin_only():
    assert client.get('/admin/release/readiness').status_code == 401
    _, token = _make_active_user('rel1@example.com', 'reluser1', ['ER'])
    assert client.get('/admin/release/readiness',
                      headers={'Authorization': f'Bearer {token}'}).status_code == 403


def test_release_readiness_returns_checklist_and_no_secrets(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "ChangeMe123!")  # default -> seeded admin login works
    t = admin_token()
    r = client.get('/admin/release/readiness', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["release_requires_explicit_approval"] is True
    assert "readiness" in body and "checklist" in body
    ids = {i["id"] for i in body["checklist"]}
    assert {"tests", "compile", "readiness", "approval"} <= ids
    # The approval item points at the only main-moving target.
    approval = next(i for i in body["checklist"] if i["id"] == "approval")
    assert approval["command"] == "make release-dev-to-main"
    assert "ChangeMe123!" not in r.text  # the admin password never appears


# =========================================================================
# Security headers + CORS
# =========================================================================
def test_security_headers_present_on_response():
    r = client.get('/health')
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert r.headers.get("Cross-Origin-Opener-Policy") == "same-origin"


def test_cors_allows_trusted_origin():
    origin = "http://localhost:3000"
    r = client.get('/health', headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_untrusted_origin():
    r = client.get('/health', headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 200
    # No CORS grant for an origin outside the allow-list.
    assert r.headers.get("access-control-allow-origin") in (None, "")


def test_cors_preflight_returns_allowed_methods():
    r = client.options('/health', headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_trusted_origins_defaults_are_safe_localhost(monkeypatch):
    monkeypatch.delenv("TRUSTED_ORIGINS", raising=False)
    origins = config.trusted_origins()
    assert "http://localhost:3000" in origins
    assert "*" not in origins


# =========================================================================
# /admin/audit/export
# =========================================================================
def test_audit_export_admin_only():
    assert client.get('/admin/audit/export').status_code == 401
    _, token = _make_active_user('rel2@example.com', 'reluser2', ['IT'])
    assert client.get('/admin/audit/export',
                      headers={'Authorization': f'Bearer {token}'}).status_code == 403


def test_audit_export_structured_and_filtered():
    # Generate a known-action event, then export filtered by that action.
    client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    t = admin_token()
    r = client.get('/admin/audit/export', params={'action': 'login', 'limit': 50},
                   headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filters"]["action"] == "login"
    assert "retention" in body and body["retention"]["destructive_deletion_enabled"] is False
    assert body["count"] == len(body["events"])
    assert body["count"] >= 1
    assert all(e["action"] == "login" for e in body["events"])


def test_audit_export_never_leaks_password_hash():
    t = admin_token()
    r = client.get('/admin/audit/export', params={'limit': 500},
                   headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200
    assert "password_hash" not in r.text


def test_audit_export_jsonl_format():
    t = admin_token()
    r = client.get('/admin/audit/export', params={'format': 'jsonl', 'action': 'login', 'limit': 10},
                   headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    import json
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert lines, "expected at least one JSON line"
    for ln in lines:
        rec = json.loads(ln)
        assert rec["action"] == "login"


# =========================================================================
# Docs + Makefile release guard
# =========================================================================
def test_env_production_example_documents_settings_without_secrets():
    text = _read(".env.production.example")
    for var in ("PRODUCTION_MODE", "ADMIN_PASSWORD", "DATABASE_URL", "REDIS_URL",
                "SESSION_STORE_STRICT", "TRUSTED_ORIGINS", "VECTOR_BACKEND",
                "QDRANT_URL", "GRAPH_BACKEND", "NEO4J_URI", "HDFS_NAMENODE",
                "AGENT_ROUTING_ENABLED", "HERMES_ER_URL", "AUDIT_RETENTION_DAYS"):
        assert var in text, f".env.production.example missing {var}"
    # Placeholders only — the shipped default admin password must NOT be a value.
    assert "ADMIN_PASSWORD=ChangeMe123!" not in text


def test_makefile_defines_release_targets():
    mk = _read("Makefile")
    for target in ("release-check:", "compose-config-prod:", "smoke-final:"):
        assert target in mk, f"Makefile missing {target}"


def test_release_check_does_not_touch_main():
    """release-check must run checks only — never push/merge/checkout main."""
    mk = _read("Makefile")
    lines = mk.splitlines()
    current = None
    in_release_check = []
    for line in lines:
        if line and not line[0].isspace() and ":" in line and not line.startswith("\t"):
            name = line.split(":", 1)[0].strip()
            if name and " " not in name and name != ".PHONY":
                current = name
        elif line.startswith("\t") and current == "release-check":
            in_release_check.append(line)
    assert in_release_check, "release-check recipe not found"
    body = "\n".join(in_release_check)
    assert "$(MAIN_BRANCH)" not in body
    assert "release-dev-to-main" not in body
    for verb in ("git push", "git merge", "git checkout"):
        assert verb not in body, f"release-check must not run {verb}"


def test_only_release_target_updates_main_including_new_targets():
    """Re-assert the invariant with the Phase 12 targets present."""
    mk = _read("Makefile")
    lines = mk.splitlines()
    current = None
    touchers = set()
    for line in lines:
        if line and not line[0].isspace() and ":" in line and not line.startswith("\t"):
            name = line.split(":", 1)[0].strip()
            if name and " " not in name and name != ".PHONY":
                current = name
        elif line.startswith("\t") and current:
            if "$(MAIN_BRANCH)" in line and ("git checkout" in line
                                             or "git merge" in line
                                             or "git push" in line):
                touchers.add(current)
    assert touchers == {"release-dev-to-main"}


def test_release_docs_exist_with_commands():
    checklist = _read("docs/FINAL_RELEASE_CHECKLIST.md")
    for cmd in ("pytest", "compileall", "compose-config-prod", "smoke-all-dry-run",
                "smoke-all-routing", "smoke-resources", "release-dev-to-main"):
        assert cmd in checklist, f"FINAL_RELEASE_CHECKLIST missing {cmd}"
    # main only moves after explicit approval.
    assert "approval" in checklist.lower()
    notes = _read("docs/RELEASE_NOTES_TEMPLATE.md")
    assert "Release Notes" in notes


def test_roadmap_tracks_phase_12():
    roadmap = _read("docs/ROADMAP.md")
    assert "Phase 12" in roadmap
    assert "in progress" in roadmap.lower()


def test_readme_and_container_guide_document_phase_12():
    for rel in ("README.md", "docs/ARCHITECTURE.md", "docs/CONTAINER_GUIDE.md"):
        text = _read(rel)
        assert "Phase 12" in text, f"{rel} missing Phase 12"
        assert "PRODUCTION_MODE" in text, f"{rel} missing PRODUCTION_MODE"
        assert "TRUSTED_ORIGINS" in text, f"{rel} missing TRUSTED_ORIGINS"
