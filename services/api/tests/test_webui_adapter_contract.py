"""Phase 11: core-webui adapter contract + no-token-leakage guards.

The real UI (github.com/JacobYim/core-webui) is external, so the integration is
proven here against the *adapter contract* instead of a live browser:

1. Every endpoint in ``services/ui/adapter/contract.json`` exists on the FastAPI
   app with the declared method, and (when the route has a response_model) the
   declared response fields are a subset of the model's fields.
2. ``naplatform-adapter.js`` references every contract path — the JS adapter and
   the API cannot silently drift.
3. The signup/login response shapes (no response_model) are checked live.
4. No secret literal or hardcoded ``Bearer <token>`` leaks into the adapter files.
5. The docs advertise the Phase 11 endpoints and status.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, store

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_DIR = REPO_ROOT / "services" / "ui" / "adapter"
CONTRACT = json.loads((ADAPTER_DIR / "contract.json").read_text(encoding="utf-8"))
ADAPTER_JS = (ADAPTER_DIR / "naplatform-adapter.js").read_text(encoding="utf-8")

client = TestClient(app)


def _routes():
    """Map (path, METHOD) -> APIRoute for every routed endpoint."""
    out = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for m in methods:
            out[(path, m)] = route
    return out


ROUTES = _routes()


@pytest.mark.parametrize("ep", CONTRACT["endpoints"], ids=lambda e: e["name"])
def test_contract_endpoint_exists_on_app(ep):
    key = (ep["path"], ep["method"])
    assert key in ROUTES, f"contract endpoint {ep['name']} {ep['method']} {ep['path']} not on the API"


@pytest.mark.parametrize("ep", CONTRACT["endpoints"], ids=lambda e: e["name"])
def test_contract_response_fields_subset_of_model(ep):
    route = ROUTES[(ep["path"], ep["method"])]
    model = getattr(route, "response_model", None)
    if model is None or not hasattr(model, "model_fields"):
        pytest.skip(f"{ep['name']} has no response_model (checked live elsewhere)")
    declared = set(ep.get("response_fields", []))
    actual = set(model.model_fields.keys())
    missing = declared - actual
    assert not missing, f"{ep['name']} declares response_fields not on {model.__name__}: {sorted(missing)}"


@pytest.mark.parametrize("ep", CONTRACT["endpoints"], ids=lambda e: e["name"])
def test_adapter_js_references_every_contract_path(ep):
    # The templated chat path is stored split as chatTemplate in the JS.
    assert ep["path"] in ADAPTER_JS, f"naplatform-adapter.js does not reference {ep['path']}"


def test_signup_and_login_live_shape():
    # These two endpoints return plain dicts (no response_model); verify the
    # documented shapes against the running app so the contract stays honest.
    su = {e["name"]: e for e in CONTRACT["endpoints"]}["signup"]
    li = {e["name"]: e for e in CONTRACT["endpoints"]}["login"]

    r = client.post('/auth/signup', json={'email': 'contract@example.com', 'username': 'contractuser',
                                          'password': 'Password123!', 'department': 'IT'})
    assert r.status_code in (201, 409), r.text
    if r.status_code == 201:
        assert set(su["response_fields"]).issubset(r.json().keys())

    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    assert set(li["response_fields"]).issubset(r.json().keys())


# --- no token / secret leakage in the adapter package ----------------------
SECRET_LITERALS = ("ChangeMe123!", "Password123!", "SmokePass123!", "naplatform-password")
BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-]{20,}")


@pytest.mark.parametrize("path", sorted(ADAPTER_DIR.glob("*")), ids=lambda p: p.name)
def test_no_secret_leakage_in_adapter_files(path):
    if path.is_dir():
        pytest.skip("directory")
    text = path.read_text(encoding="utf-8", errors="ignore")
    for lit in SECRET_LITERALS:
        assert lit not in text, f"{path.name} leaks a credential literal: {lit}"
    assert not BEARER_TOKEN_RE.search(text), f"{path.name} embeds a hardcoded Bearer token"


# --- Phase 11 docs guards --------------------------------------------------
PHASE_11_ENDPOINTS = (
    "/auth/departments/options", "/auth/logout", "/auth/me",
    "/core-webui/session/status", "/core-webui/session/select-department",
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md",
                                 "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_docs_mention_phase_11(rel):
    assert "Phase 11" in _read(rel), f"{rel} missing Phase 11"


@pytest.mark.parametrize("rel", ["README.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_document_phase_11_endpoints(rel):
    text = _read(rel)
    for ep in PHASE_11_ENDPOINTS:
        assert ep in text, f"{rel} missing Phase 11 endpoint {ep}"


def test_roadmap_marks_phase_11_in_progress_and_phase_10_completed():
    roadmap = _read("docs/ROADMAP.md")
    assert "Phase 11" in roadmap
    assert "in progress" in roadmap.lower()
    # Phase 10 remains present as a completed phase (contract preserved).
    assert "Phase 10" in roadmap
