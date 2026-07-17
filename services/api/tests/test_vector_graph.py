"""Phase 05: Qdrant/Neo4j scope adapters and their endpoints.

These exercise both the pure adapters (in-memory, no live Qdrant/Neo4j) and the
API contracts: allowed personal/department insert+search, cross-user and
cross-department filtering, invalid collection/label rejection, pending/nonmember
denial, audit events, and the generated Qdrant/Neo4j filter descriptors.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.models import User, UserStatus
from app.rbac import AccessDenied
from app.security import hash_password
from app.vector import VectorScopeAdapter, VectorScopeError, validate_collection
from app.graph import GraphScopeAdapter, GraphScopeError, validate_label, validate_rel_type

client = TestClient(app)


def _user(uid, username, departments, status=UserStatus.active, is_admin=False):
    return User(id=uid, email=f"{username}@example.com", username=username,
                password_hash=hash_password("Password123!"), status=status,
                departments=departments, is_admin=is_admin)


# =========================================================================
# VectorScopeAdapter (unit)
# =========================================================================
def test_vector_personal_insert_stamps_owner():
    a = VectorScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    rec = a.insert(u, "ER", "docs", {"text": "hi"}, "personal")
    assert rec["metadata"] == {"owner_user_id": "u1"}
    assert rec["scope"] == "personal"


def test_vector_department_insert_stamps_department():
    a = VectorScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    rec = a.insert(u, "ER", "docs", {"text": "hi"}, "department")
    assert rec["metadata"] == {"department": "ER", "allowed_departments": ["ER"]}


def test_vector_search_returns_own_personal_and_department():
    a = VectorScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    a.insert(alice, "ER", "docs", {"text": "mine"}, "personal")
    a.insert(alice, "ER", "docs", {"text": "shared"}, "department")
    hits = a.search(alice, "ER", "docs")
    assert {h["payload"]["text"] for h in hits} == {"mine", "shared"}


def test_vector_search_hides_other_users_personal_records():
    a = VectorScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    a.insert(alice, "ER", "docs", {"text": "alice-secret"}, "personal")
    a.insert(bob, "ER", "docs", {"text": "bob-secret"}, "personal")
    # Both are ER members, but personal records stay private to their owner.
    assert [h["payload"]["text"] for h in a.search(bob, "ER", "docs")] == ["bob-secret"]


def test_vector_search_cross_department_filtered_out():
    a = VectorScopeAdapter()
    er = _user("u1", "alice", ["ER"])
    it = _user("u2", "bob", ["IT"])
    a.insert(er, "ER", "docs", {"text": "er-doc"}, "department")
    # bob is IT-only; searching his own IT scope must not see ER department docs.
    assert a.search(it, "IT", "docs") == []


def test_vector_insert_nonmember_denied():
    a = VectorScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    with pytest.raises(AccessDenied):
        a.insert(u, "IT", "docs", {"text": "x"}, "department")


def test_vector_invalid_scope_rejected():
    a = VectorScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    with pytest.raises(VectorScopeError):
        a.insert(u, "ER", "docs", {}, "global")


@pytest.mark.parametrize("bad", ["Docs", "1docs", "do", "docs-x", "docs space", "../x", ""])
def test_vector_invalid_collection_rejected(bad):
    with pytest.raises(VectorScopeError):
        validate_collection(bad)


def test_vector_build_filter_descriptor_shape():
    a = VectorScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    filt = a.build_filter(u, "ER")
    keys = {c["key"] for c in filt["should"]}
    assert keys == {"owner_user_id", "allowed_users", "department", "allowed_departments"}


def test_vector_admin_can_scope_any_department():
    a = VectorScopeAdapter()
    admin = _user("admin", "root", ["ER"], is_admin=True)
    rec = a.insert(admin, "QC", "docs", {"text": "q"}, "department")
    assert rec["metadata"]["department"] == "QC"


# =========================================================================
# GraphScopeAdapter (unit)
# =========================================================================
def test_graph_personal_node_stamps_owner():
    g = GraphScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    node = g.insert_node(u, "ER", "Incident", {"title": "t"}, "personal")
    assert node["metadata"] == {"owner_user_id": "u1"}


def test_graph_department_node_stamps_department():
    g = GraphScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    node = g.insert_node(u, "ER", "Incident", {"title": "t"}, "department")
    assert node["metadata"] == {"department": "ER", "allowed_departments": ["ER"]}


def test_graph_search_scopes_own_and_department():
    g = GraphScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    g.insert_node(alice, "ER", "Incident", {"title": "mine"}, "personal")
    g.insert_node(alice, "ER", "Incident", {"title": "shared"}, "department")
    hits = g.search_nodes(alice, "ER", "Incident")
    assert {h["properties"]["title"] for h in hits} == {"mine", "shared"}


def test_graph_search_hides_other_users_personal_nodes():
    g = GraphScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    g.insert_node(alice, "ER", "Incident", {"title": "a"}, "personal")
    g.insert_node(bob, "ER", "Incident", {"title": "b"}, "personal")
    assert [h["properties"]["title"] for h in g.search_nodes(bob, "ER", "Incident")] == ["b"]


def test_graph_search_cross_department_filtered_out():
    g = GraphScopeAdapter()
    er = _user("u1", "alice", ["ER"])
    it = _user("u2", "bob", ["IT"])
    g.insert_node(er, "ER", "Incident", {"title": "er"}, "department")
    assert g.search_nodes(it, "IT", "Incident") == []


def test_graph_relationship_stubs_enforce_scope():
    g = GraphScopeAdapter()
    alice = _user("u1", "alice", ["ER"])
    bob = _user("u2", "bob", ["ER"])
    g.insert_relationship(alice, "ER", "RELATED_TO", "n1", "n2", {"w": 1}, "personal")
    assert len(g.search_relationships(alice, "ER", "RELATED_TO")) == 1
    # bob (ER member) cannot see alice's personal relationship
    assert g.search_relationships(bob, "ER", "RELATED_TO") == []


def test_graph_node_match_descriptor_is_parameterised():
    g = GraphScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    desc = g.build_node_match(u, "ER", "Incident")
    assert desc["cypher"].startswith("MATCH (n:Incident) WHERE ")
    assert "$owner_user_id" in desc["cypher"] and "$department" in desc["cypher"]
    # user id must be passed as a bound param, never inlined into the query text.
    assert "u1" not in desc["cypher"]
    assert desc["params"] == {"owner_user_id": "u1", "user_id": "u1", "department": "ER"}


def test_graph_relationship_match_descriptor():
    g = GraphScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    desc = g.build_relationship_match(u, "ER", "RELATED_TO")
    assert "[r:RELATED_TO]" in desc["cypher"]
    assert desc["params"]["department"] == "ER"


@pytest.mark.parametrize("bad", ["1Node", "Node-1", "Node Space", "`Node`", "", "Node;DROP"])
def test_graph_invalid_label_rejected(bad):
    with pytest.raises(GraphScopeError):
        validate_label(bad)


@pytest.mark.parametrize("bad", ["related_to", "RELATED-TO", "REL TYPE", "1REL", ""])
def test_graph_invalid_rel_type_rejected(bad):
    with pytest.raises(GraphScopeError):
        validate_rel_type(bad)


def test_graph_insert_nonmember_denied():
    g = GraphScopeAdapter()
    u = _user("u1", "alice", ["ER"])
    with pytest.raises(AccessDenied):
        g.insert_node(u, "IT", "Incident", {}, "department")


# =========================================================================
# API endpoints
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


def _h(token):
    return {'Authorization': f'Bearer {token}'}


# --- vector endpoints ----------------------------------------------------
def test_api_vector_personal_insert_and_search():
    _, token = _make_active_user('vec1@example.com', 'vecone', ['ER'])
    r = client.post('/vector/ER/records', headers=_h(token),
                    json={'collection': 'notes', 'scope': 'personal', 'payload': {'text': 'hello'}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body['record']['metadata']['owner_user_id']
    r = client.post('/vector/ER/search', headers=_h(token), json={'collection': 'notes'})
    assert r.status_code == 200, r.text
    sb = r.json()
    assert sb['count'] >= 1
    assert {c['key'] for c in sb['filter']['should']} == {'owner_user_id', 'allowed_users', 'department', 'allowed_departments'}


def test_api_vector_department_insert_visible_to_member_only():
    _, alice = _make_active_user('vec2@example.com', 'vectwo', ['IT'])
    _, bob = _make_active_user('vec3@example.com', 'vecthree', ['QC'])
    r = client.post('/vector/IT/records', headers=_h(alice),
                    json={'collection': 'shared', 'scope': 'department', 'payload': {'text': 'it-only'}})
    assert r.status_code == 201, r.text
    # QC user searching QC must not see the IT department record.
    r = client.get('/vector/QC/search', params={'collection': 'shared'}, headers=_h(bob))
    assert r.status_code == 200, r.text
    assert all('it-only' not in str(x['payload']) for x in r.json()['results'])


def test_api_vector_cross_user_personal_hidden():
    _, alice = _make_active_user('vec4@example.com', 'vecfour', ['ER'])
    _, bob = _make_active_user('vec5@example.com', 'vecfive', ['ER'])
    client.post('/vector/ER/records', headers=_h(alice),
                json={'collection': 'priv', 'scope': 'personal', 'payload': {'text': 'alice'}})
    r = client.get('/vector/ER/search', params={'collection': 'priv'}, headers=_h(bob))
    assert r.status_code == 200
    assert all('alice' not in str(x['payload']) for x in r.json()['results'])


def test_api_vector_invalid_collection_rejected():
    _, token = _make_active_user('vec6@example.com', 'vecsix', ['ER'])
    r = client.post('/vector/ER/records', headers=_h(token),
                    json={'collection': 'Bad Name', 'scope': 'personal', 'payload': {}})
    assert r.status_code == 400, r.text


def test_api_vector_nonmember_denied():
    _, token = _make_active_user('vec7@example.com', 'vecseven', ['ER'])
    r = client.post('/vector/IT/records', headers=_h(token),
                    json={'collection': 'notes', 'scope': 'department', 'payload': {}})
    assert r.status_code == 403, r.text


def test_api_vector_requires_auth():
    assert client.post('/vector/ER/records', json={'collection': 'notes', 'scope': 'personal'}).status_code == 401


def test_api_vector_pending_denied():
    client.post('/auth/signup', json={'email': 'vecpend@example.com', 'username': 'vecpend',
                                      'password': 'Password123!', 'department': 'ER'})
    u = store.get_user_by_email('vecpend@example.com')
    t = admin_token()
    client.patch(f'/admin/users/{u.id}', headers=_h(t), json={'status': 'active', 'departments': ['ER']})
    ut = client.post('/auth/login', json={'email': 'vecpend@example.com', 'password': 'Password123!'}).json()['token']
    client.patch(f'/admin/users/{u.id}', headers=_h(t), json={'status': 'pending'})
    r = client.post('/vector/ER/records', headers=_h(ut),
                    json={'collection': 'notes', 'scope': 'personal', 'payload': {}})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("bad_limit", [-1, 1001])
def test_api_vector_search_get_limit_out_of_range_422(bad_limit):
    _, token = _make_active_user(f'veclim{bad_limit}@example.com', f'veclim{"n" if bad_limit < 0 else "h"}', ['ER'])
    r = client.get('/vector/ER/search', params={'collection': 'notes', 'limit': bad_limit}, headers=_h(token))
    assert r.status_code == 422, r.text


# --- graph endpoints -----------------------------------------------------
def test_api_graph_personal_node_and_search():
    _, token = _make_active_user('gr1@example.com', 'grone', ['ER'])
    r = client.post('/graph/ER/nodes', headers=_h(token),
                    json={'label': 'Incident', 'scope': 'personal', 'properties': {'title': 't1'}})
    assert r.status_code == 201, r.text
    r = client.post('/graph/ER/nodes/search', headers=_h(token), json={'label': 'Incident'})
    assert r.status_code == 200, r.text
    sb = r.json()
    assert sb['count'] >= 1
    assert sb['cypher'].startswith('MATCH (n:Incident) WHERE ')
    assert sb['params']['department'] == 'ER'


def test_api_graph_cross_department_filtered():
    _, alice = _make_active_user('gr2@example.com', 'grtwo', ['IT'])
    _, bob = _make_active_user('gr3@example.com', 'grthree', ['QC'])
    client.post('/graph/IT/nodes', headers=_h(alice),
                json={'label': 'Ticket', 'scope': 'department', 'properties': {'k': 'it'}})
    r = client.get('/graph/QC/nodes/search', params={'label': 'Ticket'}, headers=_h(bob))
    assert r.status_code == 200
    assert r.json()['results'] == []


def test_api_graph_invalid_label_rejected():
    _, token = _make_active_user('gr4@example.com', 'grfour', ['ER'])
    r = client.post('/graph/ER/nodes', headers=_h(token),
                    json={'label': 'Bad Label', 'scope': 'personal', 'properties': {}})
    assert r.status_code == 400, r.text


def test_api_graph_nonmember_denied():
    _, token = _make_active_user('gr5@example.com', 'grfive', ['ER'])
    r = client.post('/graph/IT/nodes', headers=_h(token),
                    json={'label': 'Incident', 'scope': 'department', 'properties': {}})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("bad_limit", [-1, 1001])
def test_api_graph_search_get_limit_out_of_range_422(bad_limit):
    _, token = _make_active_user(f'grlim{bad_limit}@example.com', f'grlim{"n" if bad_limit < 0 else "h"}', ['ER'])
    r = client.get('/graph/ER/nodes/search', params={'label': 'Incident', 'limit': bad_limit}, headers=_h(token))
    assert r.status_code == 422, r.text


# --- audit ---------------------------------------------------------------
def test_api_vector_graph_events_audited():
    u, token = _make_active_user('aud5@example.com', 'audfive', ['ER'])
    client.post('/vector/ER/records', headers=_h(token),
                json={'collection': 'notes', 'scope': 'personal', 'payload': {'text': 'x'}})
    client.post('/vector/ER/search', headers=_h(token), json={'collection': 'notes'})
    client.post('/graph/ER/nodes', headers=_h(token),
                json={'label': 'Incident', 'scope': 'personal', 'properties': {}})
    client.post('/graph/ER/nodes/search', headers=_h(token), json={'label': 'Incident'})
    t = admin_token()
    audit = client.get('/admin/audit', headers=_h(t), params={'limit': 500}).json()
    actions = {e['action'] for e in audit}
    assert {'vector_insert', 'vector_search', 'graph_insert', 'graph_search'} <= actions
