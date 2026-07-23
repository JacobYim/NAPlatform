from fastapi.testclient import TestClient

from app import hdfs_web
from app.main import app
from app.models import User, UserStatus
from app.security import hash_password
from app.store import store


def _login_active(username="hdfsuser", department="QC"):
    user = User(
        id=f"u-{username}",
        email=f"{username}@example.com",
        username=username,
        password_hash=hash_password("pw"),
        status=UserStatus.active,
        departments=[department],
    )
    try:
        store.add_user(user)
    except ValueError:
        pass
    token = store.create_session(user.id)
    return {"Authorization": f"Bearer {token}"}, user


def test_workspace_hdfs_list_enforces_user_workspace_root(monkeypatch):
    headers, user = _login_active("alicehdfs")

    def fake_request_json(path, op, **kwargs):
        assert path == f"/naplatform/users/{user.username}/workspace"
        assert op == "LISTSTATUS"
        return {"FileStatuses": {"FileStatus": [
            {"pathSuffix": "readme.txt", "type": "FILE", "length": 12, "modificationTime": 123},
            {"pathSuffix": "docs", "type": "DIRECTORY", "modificationTime": 124},
        ]}}

    monkeypatch.setattr(hdfs_web, "_request_json", fake_request_json)
    with TestClient(app) as client:
        r = client.get(f"/workspace/hdfs/list?root=/naplatform/users/{user.username}/workspace&path=.", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["hdfs_root"] == f"/naplatform/users/{user.username}/workspace"
    assert [e["name"] for e in data["entries"]] == ["readme.txt", "docs"]


def test_workspace_hdfs_list_denies_user_parent_root():
    headers, user = _login_active("bobhdfs")
    with TestClient(app) as client:
        r = client.get(f"/workspace/hdfs/list?root=/naplatform/users/{user.username}&path=.", headers=headers)
    assert r.status_code == 403


def test_workspace_hdfs_list_denies_other_personal_root():
    headers, _ = _login_active("otherhdfs")
    with TestClient(app) as client:
        r = client.get("/workspace/hdfs/list?root=/naplatform/users/other/workspace&path=.", headers=headers)
    assert r.status_code == 403


def test_workspace_hdfs_department_shared_allowed_and_department_parent_denied(monkeypatch):
    headers, _ = _login_active("dephdfs", "QC")

    def fake_request_json(path, op, **kwargs):
        assert path == "/naplatform/departments/QC/department_shared"
        assert op == "LISTSTATUS"
        return {"FileStatuses": {"FileStatus": []}}

    monkeypatch.setattr(hdfs_web, "_request_json", fake_request_json)
    with TestClient(app) as client:
        ok = client.get("/workspace/hdfs/list?root=/naplatform/departments/QC/department_shared&path=.", headers=headers)
        denied = client.get("/workspace/hdfs/list?root=/naplatform/departments/QC&path=.", headers=headers)
    assert ok.status_code == 200, ok.text
    assert denied.status_code == 403


def test_workspace_hdfs_file_reads_via_webhdfs(monkeypatch):
    headers, user = _login_active("carolhdfs")

    def fake_request_json(path, op, **kwargs):
        assert path == f"/naplatform/users/{user.username}/workspace/note.txt"
        assert op == "GETFILESTATUS"
        return {"FileStatus": {"type": "FILE", "length": 5}}

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, *_): return b"hello"

    monkeypatch.setattr(hdfs_web, "_request_json", fake_request_json)
    monkeypatch.setattr(hdfs_web.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with TestClient(app) as client:
        r = client.get(f"/workspace/hdfs/file?root=/naplatform/users/{user.username}/workspace&path=note.txt", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "hello"


def test_agent_context_uses_only_workspace_department_shared_and_history_roots():
    headers, user = _login_active("ctxhdfs", "QC")
    with TestClient(app) as client:
        r = client.get("/agents/QC/context", headers=headers)
    assert r.status_code == 200, r.text
    roots = r.json()["hdfs_roots"]
    assert f"/naplatform/users/{user.username}/workspace" in roots
    assert f"/naplatform/users/{user.username}/chat_history" in roots
    assert "/naplatform/departments/QC/department_shared" in roots
    assert f"/naplatform/users/{user.username}" not in roots
    assert "/naplatform/departments/QC" not in roots
