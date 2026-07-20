"""Phase 17: admin hub needs richer user-management API fields."""
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def test_admin_can_update_identity_role_departments_and_password():
    token = _admin_token()
    suffix = uuid4().hex[:8]
    old_email = f'phase17-{suffix}@example.com'
    new_email = f'phase17-renamed-{suffix}@example.com'
    old_username = f'p17{suffix}'
    new_username = f'p17r{suffix}'
    r = client.post('/auth/signup', json={
        'email': old_email, 'username': old_username,
        'password': 'OldPass123!', 'department': 'QC',
    })
    assert r.status_code == 201, r.text
    users = client.get('/admin/users', headers={'Authorization': f'Bearer {token}'}).json()
    user_id = [u['id'] for u in users if u['email'] == old_email][0]

    r = client.patch(f'/admin/users/{user_id}', headers={'Authorization': f'Bearer {token}'}, json={
        'email': new_email,
        'username': new_username,
        'status': 'active',
        'departments': ['QC', 'IT'],
        'is_admin': True,
        'password': 'NewPass123!',
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['email'] == new_email
    assert data['username'] == new_username
    assert data['status'] == 'active'
    assert sorted(data['departments']) == ['IT', 'QC']
    assert data['is_admin'] is True
    assert 'password_hash' not in data

    r = client.post('/auth/login', json={'email': new_email, 'password': 'NewPass123!'})
    assert r.status_code == 200, r.text


def test_admin_update_rejects_duplicate_email():
    token = _admin_token()
    suffix = uuid4().hex[:8]
    email_a = f'phase17-a-{suffix}@example.com'
    email_b = f'phase17-b-{suffix}@example.com'
    for email, username in ((email_a, f'p17a{suffix}'), (email_b, f'p17b{suffix}')):
        r = client.post('/auth/signup', json={'email': email, 'username': username, 'password': 'OldPass123!', 'department': 'QC'})
        assert r.status_code == 201, r.text
    users = client.get('/admin/users', headers={'Authorization': f'Bearer {token}'}).json()
    user_b = [u['id'] for u in users if u['email'] == email_b][0]
    r = client.patch(f'/admin/users/{user_b}', headers={'Authorization': f'Bearer {token}'}, json={'email': email_a})
    assert r.status_code == 409
