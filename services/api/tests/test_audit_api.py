"""Phase 03: audit-event creation and the admin audit listing endpoint."""
import time

from fastapi.testclient import TestClient
from app.main import app, store
client = TestClient(app)


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def test_login_success_and_failure_audited():
    admin_token()  # a successful admin login
    client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'wrong'})
    t = admin_token()
    r = client.get('/admin/audit', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    logins = [e for e in r.json() if e['action'] == 'login']
    assert any(e['success'] is True for e in logins)
    assert any(e['success'] is False for e in logins)


def test_signup_and_chat_audited():
    client.post('/auth/signup', json={'email': 'aud@example.com', 'username': 'auduser', 'password': 'Password123!', 'department': 'ER'})
    u = store.get_user_by_email('aud@example.com')
    t = admin_token()
    client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'}, json={'status': 'active', 'departments': ['ER']})
    ut = client.post('/auth/login', json={'email': 'aud@example.com', 'password': 'Password123!'}).json()['token']
    client.post('/agents/ER/chat', headers={'Authorization': f'Bearer {ut}'}, json={'message': 'hi'})
    r = client.get('/admin/audit', headers={'Authorization': f'Bearer {t}'})
    actions = {e['action'] for e in r.json()}
    assert {'signup', 'admin_user_update', 'agent_chat'} <= actions


def test_audit_requires_admin():
    client.post('/auth/signup', json={'email': 'na@example.com', 'username': 'nauser', 'password': 'Password123!', 'department': 'IT'})
    u = store.get_user_by_email('na@example.com')
    t = admin_token()
    client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'}, json={'status': 'active', 'departments': ['IT']})
    ut = client.post('/auth/login', json={'email': 'na@example.com', 'password': 'Password123!'}).json()['token']
    assert client.get('/admin/audit', headers={'Authorization': f'Bearer {ut}'}).status_code == 403


def test_password_reset_request_persists_token_but_not_returned():
    r = client.post('/auth/password-reset/request', params={'email': 'admin@example.com'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert 'token' not in body
    assert body['message']
    # The request must be audited even though the token is never returned.
    t = admin_token()
    audit = client.get('/admin/audit', headers={'Authorization': f'Bearer {t}'}).json()
    assert any(e['action'] == 'password_reset_request' for e in audit)


def test_password_reset_unknown_email_does_not_leak():
    r = client.post('/auth/password-reset/request', params={'email': 'nobody@example.com'})
    assert r.status_code == 200
    assert 'token' not in r.json()


def test_expired_session_token_yields_401():
    # Mint a session for the admin user with a 1s TTL, let it expire, and prove
    # the auth dependency rejects the stale token on a protected endpoint.
    admin = store.get_user_by_email('admin@example.com')
    token = store.create_session(admin.id, ttl=1)
    fresh = client.get('/core-webui/session',
                       headers={'Authorization': f'Bearer {token}'})
    assert fresh.status_code == 200, fresh.text
    time.sleep(1.1)
    expired = client.get('/core-webui/session',
                         headers={'Authorization': f'Bearer {token}'})
    assert expired.status_code == 401, expired.text
    assert expired.json()['detail'] == 'invalid session'
