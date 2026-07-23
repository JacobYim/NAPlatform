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


def test_workspace_hdfs_list_enforces_user_roots(monkeypatch):
    headers, user = _login_active("alicehdfs")

    def fake_request_json(path, op):
        assert path == f"/naplatform/users/{user.username}"
        assert op == "LISTSTATUS"
        return {"FileStatuses": {"FileStatus": [
            {"pathSuffix": "readme.txt", "type": "FILE", "length": 12, "modificationTime": 123},
            {"pathSuffix": "docs", "type": "DIRECTORY", "modificationTime": 124},
        ]}}

    monkeypatch.setattr(hdfs_web, "_request_json", fake_request_json)
    with TestClient(app) as client:
        r = client.get(f"/workspace/hdfs/list?root=/naplatform/users/{user.username}&path=.", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["hdfs_root"] == f"/naplatform/users/{user.username}"
    assert [e["name"] for e in data["entries"]] == ["readme.txt", "docs"]


def test_workspace_hdfs_list_denies_other_personal_root():
    headers, _ = _login_active("bobhdfs")
    with TestClient(app) as client:
        r = client.get("/workspace/hdfs/list?root=/naplatform/users/other&path=.", headers=headers)
    assert r.status_code == 403


def test_workspace_hdfs_file_reads_via_webhdfs(monkeypatch):
    headers, user = _login_active("carolhdfs")

    def fake_request_json(path, op):
        assert path == f"/naplatform/users/{user.username}/note.txt"
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
        r = client.get(f"/workspace/hdfs/file?root=/naplatform/users/{user.username}&path=note.txt", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "hello"
