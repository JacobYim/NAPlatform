"""Phase 11: core-webui auth/session UI integration endpoints.

Covers the auth/session surface the core-webui adapter drives: department
options, login success, the pending/approval-waiting UX contract, session status
vs the active-only bootstrap, /auth/me alias, logout session invalidation, and
department selection (member vs non-member). No live UI is involved — these
exercise the FastAPI app directly.
"""
from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def make_active_user(email, username, departments):
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    token = client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']
    return u, token


# --- department options ----------------------------------------------------
def test_department_options_public_and_complete():
    r = client.get('/auth/departments/options')
    assert r.status_code == 200, r.text
    opts = r.json()['departments']
    values = {o['value'] for o in opts}
    assert values == {'ER', 'IT', 'EHS', 'QC'}
    assert all(o['label'] for o in opts)  # every option carries a human label


# --- login success ---------------------------------------------------------
def test_login_success_returns_token_and_bootstrap():
    _, token = make_active_user('login.ok@example.com', 'loginok', ['IT', 'QC'])
    r = client.get('/auth/me', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b['session_status'] == 'active'
    assert b['default_department'] == 'IT'
    assert b['chat_route_template'] == '/agents/{department}/chat'
    routes = {dr['department']: dr for dr in b['department_routes']}
    assert set(routes) == {'IT', 'QC'}
    assert routes['IT']['chat_route'] == '/agents/IT/chat'
    assert routes['QC']['resources_route'] == '/resources/QC'
    assert b['approval']['can_access'] is True


def test_auth_me_matches_core_webui_session():
    _, token = make_active_user('alias@example.com', 'aliasuser', ['ER'])
    a = client.get('/auth/me', headers={'Authorization': f'Bearer {token}'})
    s = client.get('/core-webui/session', headers={'Authorization': f'Bearer {token}'})
    assert a.status_code == 200 and s.status_code == 200
    assert a.json() == s.json()  # /auth/me is a true alias


# --- pending / approval-waiting UX -----------------------------------------
def test_pending_signup_blocked_at_login():
    client.post('/auth/signup', json={'email': 'pending.block@example.com', 'username': 'pendblock',
                                      'password': 'Password123!', 'department': 'IT'})
    r = client.post('/auth/login', json={'email': 'pending.block@example.com', 'password': 'Password123!'})
    assert r.status_code == 403, r.text  # pending users cannot obtain a session


def test_session_status_reports_pending_ux_after_revocation():
    # Active at login, then revoked to pending: the existing session must show the
    # approval-waiting UX via /session/status while /core-webui/session is 403.
    u, token = make_active_user('revoke@example.com', 'revokeuser', ['QC'])
    t = admin_token()
    client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                 json={'status': 'pending'})

    boot = client.get('/core-webui/session', headers={'Authorization': f'Bearer {token}'})
    assert boot.status_code == 403, boot.text

    st = client.get('/core-webui/session/status', headers={'Authorization': f'Bearer {token}'})
    assert st.status_code == 200, st.text
    body = st.json()
    assert body['pending'] is True
    assert body['can_access'] is False
    assert body['approval']['state'] == 'pending'
    assert 'approval' in body['approval']['message'].lower()


def test_session_status_active_can_access():
    _, token = make_active_user('active.status@example.com', 'activestatus', ['EHS'])
    st = client.get('/core-webui/session/status', headers={'Authorization': f'Bearer {token}'})
    assert st.status_code == 200, st.text
    body = st.json()
    assert body['can_access'] is True
    assert body['pending'] is False
    assert body['approval']['state'] == 'active'


def test_session_status_no_bearer_is_401():
    assert client.get('/core-webui/session/status').status_code == 401


# --- logout invalidation ---------------------------------------------------
def test_logout_invalidates_session():
    _, token = make_active_user('logout@example.com', 'logoutuser', ['IT'])
    # session works before logout
    assert client.get('/core-webui/session', headers={'Authorization': f'Bearer {token}'}).status_code == 200
    out = client.post('/auth/logout', headers={'Authorization': f'Bearer {token}'})
    assert out.status_code == 200, out.text
    assert out.json()['invalidated'] is True
    # token is now dead -> 401 (expired/invalid session)
    assert client.get('/core-webui/session', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert client.get('/core-webui/session/status', headers={'Authorization': f'Bearer {token}'}).status_code == 401


def test_logout_idempotent_on_unknown_token():
    out = client.post('/auth/logout', headers={'Authorization': 'Bearer not-a-real-token'})
    assert out.status_code == 200, out.text
    assert out.json()['invalidated'] is False  # nothing to invalidate, still ok


def test_logout_requires_bearer_header():
    assert client.post('/auth/logout').status_code == 401


# --- department selection --------------------------------------------------
def test_select_department_member_returns_route():
    _, token = make_active_user('select.ok@example.com', 'selectok', ['QC'])
    r = client.post('/core-webui/session/select-department',
                    headers={'Authorization': f'Bearer {token}'}, json={'department': 'QC'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['is_member'] is True
    assert body['department'] == 'QC'
    assert body['route']['chat_route'] == '/agents/QC/chat'


def test_select_department_nonmember_denied():
    _, token = make_active_user('select.deny@example.com', 'selectdeny', ['QC'])
    r = client.post('/core-webui/session/select-department',
                    headers={'Authorization': f'Bearer {token}'}, json={'department': 'IT'})
    assert r.status_code == 403, r.text


def test_select_department_unknown_is_400():
    _, token = make_active_user('select.unknown@example.com', 'selectunknown', ['QC'])
    r = client.post('/core-webui/session/select-department',
                    headers={'Authorization': f'Bearer {token}'}, json={'department': 'NOPE'})
    assert r.status_code == 400, r.text


def test_select_department_case_insensitive():
    _, token = make_active_user('select.case@example.com', 'selectcase', ['ER'])
    r = client.post('/core-webui/session/select-department',
                    headers={'Authorization': f'Bearer {token}'}, json={'department': 'er'})
    assert r.status_code == 200, r.text
    assert r.json()['department'] == 'ER'


def test_admin_can_select_any_department():
    t = admin_token()
    r = client.post('/core-webui/session/select-department',
                    headers={'Authorization': f'Bearer {t}'}, json={'department': 'EHS'})
    assert r.status_code == 200, r.text
    assert r.json()['is_member'] is True
