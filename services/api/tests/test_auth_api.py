from fastapi.testclient import TestClient
from app.main import app,store
client=TestClient(app)
def admin_token():
    r=client.post('/auth/login',json={'email':'admin@example.com','password':'ChangeMe123!'})
    assert r.status_code==200,r.text; return r.json()['token']
def test_signup_requires_approval():
    r=client.post('/auth/signup',json={'email':'new@example.com','username':'newuser','password':'Password123!','department':'IT'}); assert r.status_code==201
    assert client.post('/auth/login',json={'email':'new@example.com','password':'Password123!'}).status_code==403
def test_admin_approve_and_context():
    email='approved@example.com'; client.post('/auth/signup',json={'email':email,'username':'approved','password':'Password123!','department':'QC'}); u=store.get_user_by_email(email); t=admin_token()
    assert client.patch(f'/admin/users/{u.id}',headers={'Authorization':f'Bearer {t}'},json={'status':'active','departments':['QC']}).status_code==200
    ut=client.post('/auth/login',json={'email':email,'password':'Password123!'}).json()['token']; ctx=client.get('/agents/QC/context',headers={'Authorization':f'Bearer {ut}'})
    assert ctx.status_code==200,ctx.text; b=ctx.json(); assert '/naplatform/users/approved' in b['hdfs_roots']; assert 'quality_trace' in b['allowed_tools']
def test_denies_other_department_context():
    email='denied@example.com'; client.post('/auth/signup',json={'email':email,'username':'denied','password':'Password123!','department':'EHS'}); u=store.get_user_by_email(email); t=admin_token(); client.patch(f'/admin/users/{u.id}',headers={'Authorization':f'Bearer {t}'},json={'status':'active','departments':['EHS']})
    ut=client.post('/auth/login',json={'email':email,'password':'Password123!'}).json()['token']; assert client.get('/agents/IT/context',headers={'Authorization':f'Bearer {ut}'}).status_code==403
