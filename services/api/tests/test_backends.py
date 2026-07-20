"""Phase 10: real Qdrant/Neo4j/HDFS backend scaffolds (fake-driven, no live DB).

These exercise the pluggable backends behind the *same* scope contract as the
Phase 05 memory adapters:

- the memory default is unchanged (env unset -> in-memory backend);
- the Qdrant backend upserts/searches through a fake client, creating the
  collection on demand and enforcing the same filter descriptor;
- the Neo4j backend runs parameterised Cypher through a fake driver — user data
  is always a bound `$param`, never interpolated;
- `/admin/backends/status` is admin-only and never leaks secrets;
- the HDFS readiness probe plans a safe argv and only executes when enabled;
- an invalid backend env is rejected by the strict factory and falls back safely
  (with a warning) via the resolver.
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.models import User, UserStatus
from app.rbac import AccessDenied
from app.security import hash_password
from app import vector as vector_mod
from app import graph as graph_mod
from app.vector import (FakeQdrantClient, QdrantConfig, QdrantVectorBackend,
                        VectorScopeAdapter, VectorScopeError, build_vector_backend,
                        qdrant_filter_matches, resolve_vector_adapter)
from app.graph import (FakeNeo4jDriver, GraphScopeAdapter, GraphScopeError,
                       Neo4jConfig, Neo4jGraphBackend, build_graph_backend,
                       resolve_graph_adapter)
from app.hdfs import HdfsProvisioner, backend_status as hdfs_backend_status

client = TestClient(app)


def _user(uid, username, departments, status=UserStatus.active, is_admin=False):
    return User(id=uid, email=f"{username}@example.com", username=username,
                password_hash=hash_password("Password123!"), status=status,
                departments=departments, is_admin=is_admin)


# =========================================================================
# Backend selection: memory default is unchanged
# =========================================================================
def test_vector_default_backend_is_memory(monkeypatch):
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    adapter = resolve_vector_adapter()
    assert isinstance(adapter, VectorScopeAdapter)
    assert adapter.backend == "memory"


def test_graph_default_backend_is_memory(monkeypatch):
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    adapter = resolve_graph_adapter()
    assert isinstance(adapter, GraphScopeAdapter)
    assert adapter.backend == "memory"


def test_memory_backend_behavior_matches_phase05():
    # The default in-memory adapter still enforces personal/department scope.
    a = VectorScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    a.insert(alice, "ER", "docs", {"text": "mine"}, "personal")
    a.insert(alice, "ER", "docs", {"text": "shared"}, "department")
    assert {h["payload"]["text"] for h in a.search(alice, "ER", "docs")} == {"mine", "shared"}
    assert [h["payload"]["text"] for h in a.search(bob, "ER", "docs")] == ["shared"]


# =========================================================================
# Qdrant backend (fake client — no live Qdrant)
# =========================================================================
def test_qdrant_insert_creates_collection_and_upserts():
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake, config=QdrantConfig(url="http://qdrant:6333"))
    u = _user("u1", "alice", ["ER"])
    rec = backend.insert(u, "ER", "docs", {"text": "hi"}, "personal")
    # Collection is created on demand exactly once, and the point is upserted.
    assert fake.created == ["docs"]
    assert fake.collections["docs"]["vector_size"] == QdrantConfig().vector_size
    assert rec["metadata"] == {"owner_user_id": "u1"}
    assert fake.points["docs"][rec["id"]]["payload"] == {"text": "hi"}
    # Re-inserting into the same collection does not recreate it.
    backend.insert(u, "ER", "docs", {"text": "again"}, "personal")
    assert fake.created == ["docs"]


def test_qdrant_search_scopes_own_and_department():
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    backend.insert(alice, "ER", "docs", {"text": "mine"}, "personal")
    backend.insert(alice, "ER", "docs", {"text": "shared"}, "department")
    hits = backend.search(alice, "ER", "docs")
    assert {h["payload"]["text"] for h in hits} == {"mine", "shared"}
    # Returned rows carry the full record shape the endpoint wraps in VectorRecord.
    assert all({"id", "collection", "scope", "payload", "metadata"} <= set(h) for h in hits)


def test_qdrant_search_hides_other_users_personal_records():
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    backend.insert(alice, "ER", "docs", {"text": "alice-secret"}, "personal")
    backend.insert(bob, "ER", "docs", {"text": "bob-secret"}, "personal")
    assert [h["payload"]["text"] for h in backend.search(bob, "ER", "docs")] == ["bob-secret"]


def test_qdrant_search_cross_department_filtered_out():
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake)
    er = _user("u1", "alice", ["ER"])
    it = _user("u2", "bob", ["IT"])
    backend.insert(er, "ER", "docs", {"text": "er-doc"}, "department")
    assert backend.search(it, "IT", "docs") == []


def test_qdrant_filter_descriptor_shape_matches_memory():
    backend = QdrantVectorBackend(FakeQdrantClient())
    u = _user("u1", "alice", ["ER"])
    filt = backend.build_filter(u, "ER")
    assert {c["key"] for c in filt["should"]} == {
        "owner_user_id", "allowed_users", "department", "allowed_departments"}


def test_qdrant_upsert_payload_flattens_scope_to_top_level():
    # The upserted Qdrant payload must expose scope fields at its ROOT so the
    # filter (which keys on top-level fields) matches what a live Qdrant stores.
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    rec_p = backend.insert(alice, "ER", "docs", {"text": "mine"}, "personal")
    rec_d = backend.insert(alice, "ER", "docs", {"text": "shared"}, "department")
    stored_p = fake.points["docs"][rec_p["id"]]
    stored_d = fake.points["docs"][rec_d["id"]]
    # Personal record: owner_user_id lives at the payload root.
    assert stored_p["owner_user_id"] == "u1"
    # Department record: department + allowed_departments at the payload root.
    assert stored_d["department"] == "ER"
    assert stored_d["allowed_departments"] == ["ER"]
    # The original record shape is preserved so the API can return it verbatim.
    assert {"id", "collection", "scope", "payload", "metadata"} <= set(stored_p)
    assert stored_p["payload"] == {"text": "mine"}
    assert stored_p["metadata"] == {"owner_user_id": "u1"}


def test_qdrant_search_filter_keys_match_top_level_fields_not_nested_metadata():
    # Regression: the scope descriptor must filter on top-level payload fields
    # (owner_user_id/department/allowed_departments), never on `metadata.*`.
    fake = FakeQdrantClient()
    backend = QdrantVectorBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    backend.insert(alice, "ER", "docs", {"text": "mine"}, "personal")
    filt = backend.build_filter(alice, "ER")
    keys = {c["key"] for c in filt["should"]}
    assert keys == {"owner_user_id", "allowed_users", "department", "allowed_departments"}
    assert not any(k.startswith("metadata") or "." in k for k in keys)
    stored = next(iter(fake.points["docs"].values()))
    # The descriptor matches the stored point because the keys are at its root.
    assert qdrant_filter_matches(filt, stored)
    # A descriptor keyed on nested metadata.* would NOT match — proving the fix.
    nested = {"should": [{"key": "metadata.owner_user_id", "match": {"value": "u1"}}]}
    assert not qdrant_filter_matches(nested, stored)


def test_qdrant_filter_matches_semantics():
    desc = {"should": [{"key": "owner_user_id", "match": {"value": "u1"}},
                       {"key": "allowed_departments", "match": {"any": ["ER"]}}]}
    assert qdrant_filter_matches(desc, {"owner_user_id": "u1"})
    assert qdrant_filter_matches(desc, {"allowed_departments": ["ER", "IT"]})
    assert not qdrant_filter_matches(desc, {"owner_user_id": "u2", "department": "IT"})


def test_qdrant_backend_enforces_membership_and_scope():
    backend = QdrantVectorBackend(FakeQdrantClient())
    u = _user("u1", "alice", ["ER"])
    with pytest.raises(AccessDenied):
        backend.insert(u, "IT", "docs", {}, "department")
    with pytest.raises(VectorScopeError):
        backend.insert(u, "ER", "docs", {}, "global")
    with pytest.raises(VectorScopeError):
        backend.insert(u, "ER", "Bad Name", {}, "personal")


def test_qdrant_status_redacts_credentials():
    cfg = QdrantConfig(url="https://user:secret@qdrant.internal:6333/path?token=abc",
                       api_key="super-secret-key")
    status = QdrantVectorBackend(FakeQdrantClient(), config=cfg).status()
    blob = str(status)
    assert "super-secret-key" not in blob
    assert "secret" not in blob and "token=abc" not in blob
    assert status["url"] == "https://qdrant.internal:6333/path"
    assert status["api_key_set"] is True
    assert status["backend"] == "qdrant" and status["healthy"] is True


# =========================================================================
# Neo4j backend (fake driver — no live Neo4j)
# =========================================================================
def test_neo4j_insert_uses_bound_params_no_interpolation():
    fake = FakeNeo4jDriver()
    backend = Neo4jGraphBackend(fake)
    u = _user("u1", "alice", ["ER"])
    node = backend.insert_node(u, "ER", "Incident", {"title": "secret-title"},
                               "personal", node_id="n1")
    assert node["metadata"] == {"owner_user_id": "u1"}
    cypher, params = fake.calls[-1]
    # Only the validated label is inlined; the id/title/scope are bound params.
    assert cypher == "CREATE (n:Incident) SET n += $props RETURN n"
    assert "u1" not in cypher and "secret-title" not in cypher
    assert params["props"]["owner_user_id"] == "u1"
    assert params["props"]["title"] == "secret-title"
    assert params["props"]["id"] == "n1"


def test_neo4j_search_binds_scope_params_and_scopes_results():
    fake = FakeNeo4jDriver()
    backend = Neo4jGraphBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    backend.insert_node(alice, "ER", "Incident", {"title": "mine"}, "personal")
    backend.insert_node(alice, "ER", "Incident", {"title": "shared"}, "department")
    backend.insert_node(bob, "ER", "Incident", {"title": "bob"}, "personal")
    hits = backend.search_nodes(alice, "ER", "Incident")
    assert {h["properties"]["title"] for h in hits} == {"mine", "shared"}
    cypher, params = fake.calls[-1]
    assert cypher.startswith("MATCH (n:Incident) WHERE ")
    assert "$owner_user_id" in cypher and "$department" in cypher
    assert "u1" not in cypher  # user id is bound, never interpolated
    assert params == {"owner_user_id": "u1", "user_id": "u1", "department": "ER"}


def test_neo4j_search_query_is_not_interpolated():
    fake = FakeNeo4jDriver()
    backend = Neo4jGraphBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    backend.insert_node(alice, "ER", "Incident", {"title": "alpha"}, "personal")
    backend.insert_node(alice, "ER", "Incident", {"title": "beta"}, "personal")
    hits = backend.search_nodes(alice, "ER", "Incident", query="alpha")
    assert [h["properties"]["title"] for h in hits] == ["alpha"]
    # The query text never lands in the Cypher string (no injection surface).
    for cypher, _params in fake.calls:
        assert "alpha" not in cypher


def test_neo4j_cross_department_filtered_out():
    fake = FakeNeo4jDriver()
    backend = Neo4jGraphBackend(fake)
    er = _user("u1", "alice", ["ER"])
    it = _user("u2", "bob", ["IT"])
    backend.insert_node(er, "ER", "Incident", {"title": "er"}, "department")
    assert backend.search_nodes(it, "IT", "Incident") == []


def test_neo4j_relationship_roundtrip_scoped():
    fake = FakeNeo4jDriver()
    backend = Neo4jGraphBackend(fake)
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    backend.insert_relationship(alice, "ER", "RELATED_TO", "n1", "n2", {"w": 1},
                                "personal")
    assert len(backend.search_relationships(alice, "ER", "RELATED_TO")) == 1
    assert backend.search_relationships(bob, "ER", "RELATED_TO") == []
    create_cypher, create_params = fake.calls[0]
    assert "$start_id" in create_cypher and "$props" in create_cypher
    assert create_params["start_id"] == "n1"


def test_neo4j_backend_enforces_membership_and_labels():
    backend = Neo4jGraphBackend(FakeNeo4jDriver())
    u = _user("u1", "alice", ["ER"])
    with pytest.raises(AccessDenied):
        backend.insert_node(u, "IT", "Incident", {}, "department")
    with pytest.raises(GraphScopeError):
        backend.insert_node(u, "ER", "Bad Label", {}, "personal")


def test_neo4j_status_redacts_credentials():
    cfg = Neo4jConfig(uri="bolt://neo4j:7687", user="neo4j", password="hunter2")
    status = Neo4jGraphBackend(FakeNeo4jDriver(), config=cfg).status()
    blob = str(status)
    assert "hunter2" not in blob
    assert status["url"] == "bolt://neo4j:7687"
    assert status["password_set"] is True and status["user_set"] is True
    assert status["backend"] == "neo4j" and status["healthy"] is True


# =========================================================================
# HDFS readiness probe (fake runner — no live HDFS)
# =========================================================================
def test_hdfs_health_dry_run_plans_safe_argv_no_exec():
    prov = HdfsProvisioner(enabled=False)
    report = prov.health()
    assert report.enabled is False and report.dry_run is True
    assert report.checked is False and report.healthy is None
    assert report.result is None
    # The probe argv is fully constant — no user input, no shell string.
    assert report.plan.argv == ["hdfs", "dfs", "-test", "-d", "/naplatform"]
    assert report.plan.command == "hdfs dfs -test -d /naplatform"


def test_hdfs_health_enabled_runs_via_runner():
    calls = []

    def fake_runner(argv, timeout):
        calls.append(argv)
        return 0, "", ""
    prov = HdfsProvisioner(enabled=True, runner=fake_runner)
    report = prov.health()
    assert report.enabled is True and report.checked is True
    assert report.healthy is True
    assert calls == [["hdfs", "dfs", "-test", "-d", "/naplatform"]]


def test_hdfs_health_reports_unhealthy_on_nonzero():
    def fake_runner(argv, timeout):
        return 1, "", "No such file or directory"
    prov = HdfsProvisioner(enabled=True, runner=fake_runner)
    report = prov.health()
    assert report.healthy is False and report.result.returncode == 1


def test_hdfs_backend_status_is_dry_run_and_safe():
    status = hdfs_backend_status()
    assert status["backend"] == "hdfs"
    assert status["provisioning_enabled"] is False
    assert status["dry_run"] is True
    assert status["health_command"] == "hdfs dfs -test -d /naplatform"
    assert status["healthy"] is None  # dry-run status never executes


# =========================================================================
# Invalid backend env: strict rejection vs safe fallback
# =========================================================================
def test_build_vector_backend_rejects_invalid_mode():
    with pytest.raises(VectorScopeError):
        build_vector_backend("bogus")


def test_build_graph_backend_rejects_invalid_mode():
    with pytest.raises(GraphScopeError):
        build_graph_backend("bogus")


def test_resolve_vector_falls_back_to_memory_with_warning(monkeypatch):
    monkeypatch.setenv("VECTOR_BACKEND", "bogus")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter = resolve_vector_adapter()
    assert isinstance(adapter, VectorScopeAdapter)
    assert any("VECTOR_BACKEND" in str(w.message) for w in caught)


def test_resolve_graph_falls_back_to_memory_with_warning(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "bogus")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter = resolve_graph_adapter()
    assert isinstance(adapter, GraphScopeAdapter)
    assert any("GRAPH_BACKEND" in str(w.message) for w in caught)


def test_resolve_vector_qdrant_without_url_falls_back(monkeypatch):
    monkeypatch.setenv("VECTOR_BACKEND", "qdrant")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter = resolve_vector_adapter()
    assert isinstance(adapter, VectorScopeAdapter)
    assert any("QDRANT_URL" in str(w.message) for w in caught)


def test_resolve_graph_neo4j_without_uri_falls_back(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter = resolve_graph_adapter()
    assert isinstance(adapter, GraphScopeAdapter)
    assert any("NEO4J_URI" in str(w.message) for w in caught)


# =========================================================================
# /admin/backends/status endpoint (admin-only, no secrets)
# =========================================================================
def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def _make_active_user(email, username, departments):
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    token = client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']
    return u, token


def test_backends_status_admin_only():
    assert client.get('/admin/backends/status').status_code == 401
    _, token = _make_active_user('bkstat1@example.com', 'bkstatone', ['ER'])
    assert client.get('/admin/backends/status',
                      headers={'Authorization': f'Bearer {token}'}).status_code == 403


def test_backends_status_reports_modes_and_no_secrets():
    t = admin_token()
    r = client.get('/admin/backends/status', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"vector", "graph", "hdfs"}
    # Default stack is the memory/dry-run backends.
    assert body["vector"]["backend"] == "memory"
    assert body["graph"]["backend"] == "memory"
    assert body["hdfs"]["provisioning_enabled"] is False
    # No secret material anywhere in the payload.
    blob = r.text.lower()
    for leak in ("password", "api_key\":\"", "secret", "hunter2", "changeme"):
        assert leak not in blob
