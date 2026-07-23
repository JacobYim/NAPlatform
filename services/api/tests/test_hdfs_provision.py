"""Phase 04: HDFS workspace provisioning plan + ACL scaffold and its endpoints.

These tests exercise the command-plan builder and both endpoints without ever
touching a live HDFS: the dry-run default never spawns a subprocess, and the
enabled path is driven through an injected fake runner.
"""
import pytest
from fastapi.testclient import TestClient

from app import hdfs as hdfs_mod
from app.hdfs import HdfsProvisioner, ProvisioningError
from app.main import app, store
from app.models import User, UserStatus
from app.security import hash_password

client = TestClient(app)


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def _make_active_user(email, username, departments):
    """Sign a user up and activate them via the admin API; return their token."""
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    token = client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']
    return store.get_user_by_email(email), token


# --- command plan --------------------------------------------------------
def test_personal_plan_is_700_with_user_acl():
    prov = HdfsProvisioner(enabled=False)
    target, _argvs = prov.personal_target("alice")
    assert target.path == "/naplatform/users/alice/workspace"
    assert target.kind == "personal" and target.mode == "700"
    cmds = [c.command for c in target.plan]
    assert "hdfs dfs -mkdir -p /naplatform/users/alice/workspace" in cmds
    assert "hdfs dfs -chmod 700 /naplatform/users/alice/workspace" in cmds
    assert any(c.startswith("hdfs dfs -chown alice:alice ") for c in cmds)
    assert any("-setfacl -m user:alice:rwx" in c for c in cmds)


def test_department_plan_is_770_with_group_and_user_acl():
    prov = HdfsProvisioner(enabled=False)
    target, _argvs = prov.department_target("alice", "ER")
    assert target.path == "/naplatform/departments/ER/department_shared"
    assert target.kind == "department" and target.mode == "770"
    cmds = [c.command for c in target.plan]
    assert "hdfs dfs -mkdir -p /naplatform/departments/ER/department_shared" in cmds
    assert "hdfs dfs -chmod 770 /naplatform/departments/ER/department_shared" in cmds
    assert any("-setfacl -m group:naplatform-er:rwx" in c for c in cmds)
    assert any("-setfacl -m user:alice:rwx" in c for c in cmds)


def test_build_targets_covers_personal_and_each_department():
    prov = HdfsProvisioner(enabled=False)
    plan = prov.plan("bob", ["ER", "IT"])
    paths = {p.path for p in plan}
    assert paths == {"/naplatform/users/bob/workspace",
                     "/naplatform/users/bob/chat_history",
                     "/naplatform/departments/ER/department_shared",
                     "/naplatform/departments/IT/department_shared"}


# --- validation / traversal ---------------------------------------------
@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", ".", "-alice", "al", "bad\\name", "a b"])
def test_invalid_usernames_rejected(bad):
    with pytest.raises(ProvisioningError):
        HdfsProvisioner(enabled=False).personal_target(bad)


def test_traversal_username_rejected():
    with pytest.raises(ProvisioningError):
        HdfsProvisioner(enabled=False).plan("foo..bar", ["ER"])


def test_unknown_department_rejected():
    with pytest.raises(ValueError):
        HdfsProvisioner(enabled=False).department_target("alice", "HR")


# --- dry-run default: no subprocess -------------------------------------
def test_dry_run_default_does_not_execute(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess must not run in dry-run mode")
    monkeypatch.setattr(hdfs_mod.subprocess, "run", boom)
    prov = HdfsProvisioner()  # env not set -> disabled by default
    report = prov.provision("carol", ["ER"])
    assert prov.enabled is False and report.dry_run is True
    assert all(t.results == [] for t in report.targets)


def test_enabled_executes_via_runner():
    calls = []

    def fake_runner(argv, timeout):
        calls.append(argv)
        return 0, "ok", ""
    prov = HdfsProvisioner(enabled=True, runner=fake_runner)
    report = prov.provision("dave", ["ER"], user_id="u-dave")
    assert report.enabled is True and report.dry_run is False
    assert report.user_id == "u-dave"
    assert calls, "runner should have been invoked when enabled"
    for t in report.targets:
        assert len(t.results) == len(t.plan)
        assert all(r.executed and r.returncode == 0 for r in t.results)


# --- endpoints -----------------------------------------------------------
def test_admin_can_provision_user_dry_run_default():
    user, _ = _make_active_user('hdfs1@example.com', 'hdfsone', ['ER'])
    t = admin_token()
    r = client.post(f'/admin/users/{user.id}/provision-hdfs', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['dry_run'] is True and body['enabled'] is False
    paths = {d['path'] for d in body['targets']}
    assert '/naplatform/users/hdfsone/workspace' in paths
    assert '/naplatform/users/hdfsone/chat_history' in paths
    assert '/naplatform/departments/ER/department_shared' in paths
    assert all(d['results'] == [] for d in body['targets'])


def test_provision_unknown_user_404():
    t = admin_token()
    r = client.post('/admin/users/does-not-exist/provision-hdfs', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 404


def test_non_admin_cannot_provision():
    user, token = _make_active_user('hdfs2@example.com', 'hdfstwo', ['IT'])
    # A non-admin trying to provision anyone (even themselves) is forbidden.
    r = client.post(f'/admin/users/{user.id}/provision-hdfs', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 403


def test_provision_requires_auth():
    user, _ = _make_active_user('hdfs3@example.com', 'hdfsthree', ['QC'])
    assert client.post(f'/admin/users/{user.id}/provision-hdfs').status_code == 401


def test_workspace_hdfs_returns_only_own_roots():
    _, token = _make_active_user('hdfs4@example.com', 'hdfsfour', ['EHS'])
    r = client.get('/workspace/hdfs', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['user_home_root'] == '/naplatform/users/hdfsfour'
    assert body['personal_root'] == '/naplatform/users/hdfsfour/workspace'
    assert body['history_root'] == '/naplatform/users/hdfsfour/chat_history'
    assert body['department_roots'] == ['/naplatform/departments/EHS/department_shared']
    assert body['dry_run'] is True
    assert body['provisioning_status'] == 'dry_run'
    # The plan must only reference this user's own personal + department roots.
    paths = {p['path'] for p in body['plan']}
    assert paths == {'/naplatform/users/hdfsfour/workspace', '/naplatform/users/hdfsfour/chat_history', '/naplatform/departments/EHS/department_shared'}
    assert not any('IT' in p or 'QC' in p or 'ER' in p for p in paths)


def test_workspace_hdfs_requires_active_user():
    assert client.get('/workspace/hdfs').status_code == 401


# --- audit ---------------------------------------------------------------
def test_provision_and_workspace_view_audited():
    user, token = _make_active_user('hdfs5@example.com', 'hdfsfive', ['ER'])
    t = admin_token()
    client.post(f'/admin/users/{user.id}/provision-hdfs', headers={'Authorization': f'Bearer {t}'})
    client.get('/workspace/hdfs', headers={'Authorization': f'Bearer {token}'})
    audit = client.get('/admin/audit', headers={'Authorization': f'Bearer {t}'}).json()
    actions = {e['action'] for e in audit}
    assert {'hdfs_provision', 'workspace_view'} <= actions
