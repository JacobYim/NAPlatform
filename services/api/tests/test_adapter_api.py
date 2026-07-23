from fastapi.testclient import TestClient
from app.main import app,store
client=TestClient(app)
def admin_token():
    r=client.post('/auth/login',json={'email':'admin@example.com','password':'ChangeMe123!'})
    assert r.status_code==200,r.text; return r.json()['token']
def make_active_user(email,username,departments):
    client.post('/auth/signup',json={'email':email,'username':username,'password':'Password123!','department':departments[0]})
    u=store.get_user_by_email(email); t=admin_token()
    r=client.patch(f'/admin/users/{u.id}',headers={'Authorization':f'Bearer {t}'},json={'status':'active','departments':departments})
    assert r.status_code==200,r.text
    return client.post('/auth/login',json={'email':email,'password':'Password123!'}).json()['token']
def signup_pending(email,username,department):
    client.post('/auth/signup',json={'email':email,'username':username,'password':'Password123!','department':department})
    # pending users cannot log in, so bootstrap is reached via a forged-but-invalid session absence:
    return store.get_user_by_email(email)

def test_pending_user_denied_bootstrap():
    u=signup_pending('pend@example.com','penduser','IT')
    # activate temporarily to mint a session, then flip back to pending to exercise require_active
    t=admin_token(); client.patch(f'/admin/users/{u.id}',headers={'Authorization':f'Bearer {t}'},json={'status':'active','departments':['IT']})
    ut=client.post('/auth/login',json={'email':'pend@example.com','password':'Password123!'}).json()['token']
    client.patch(f'/admin/users/{u.id}',headers={'Authorization':f'Bearer {t}'},json={'status':'pending'})
    r=client.get('/core-webui/session',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_active_user_can_bootstrap():
    ut=make_active_user('boot@example.com','bootuser',['IT','QC'])
    r=client.get('/core-webui/session',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==200,r.text; b=r.json()
    assert b['departments']==['IT','QC']; assert b['default_department']=='IT'
    assert b['core_webui']['brand_name']=='HMGMA'; assert b['core_webui']['api_base_url']

def test_no_bearer_denied():
    assert client.get('/core-webui/session').status_code==401

def test_active_user_can_chat_own_department():
    ut=make_active_user('chat@example.com','chatuser',['ER'])
    r=client.post('/agents/ER/chat',headers={'Authorization':f'Bearer {ut}'},json={'message':'hello'})
    assert r.status_code==200,r.text; b=r.json()
    assert b['department']=='ER'; assert b['username']=='chatuser'; assert b['hermes_invoked'] is False
    assert '/naplatform/users/chatuser/workspace' in b['hdfs_roots']; assert 'incident_report' in b['allowed_tools']
    assert 'mcp-er-filesystem' in b['allowed_mcp_servers']; assert 'hello' in b['reply']

def test_chat_other_department_denied():
    ut=make_active_user('chat2@example.com','chatuser2',['ER'])
    r=client.post('/agents/IT/chat',headers={'Authorization':f'Bearer {ut}'},json={'message':'hi'})
    assert r.status_code==403,r.text

def test_resource_listing_returns_allowed_roots():
    ut=make_active_user('res@example.com','resuser',['QC'])
    r=client.get('/resources/QC',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==200,r.text; b=r.json()
    assert '/naplatform/departments/QC/department_shared' in b['allowed_roots']; assert '/naplatform/users/resuser/workspace' in b['allowed_roots']

def test_resource_path_allowed():
    ut=make_active_user('res2@example.com','resuser2',['QC'])
    r=client.get('/resources/QC',params={'path':'/naplatform/departments/QC/department_shared/department_shared/reports'},headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==200,r.text

def test_resource_path_forbidden_denied():
    ut=make_active_user('res3@example.com','resuser3',['QC'])
    r=client.get('/resources/QC',params={'path':'/naplatform/departments/IT/department_shared/secret'},headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_resource_other_department_denied():
    ut=make_active_user('res4@example.com','resuser4',['QC'])
    r=client.get('/resources/IT',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_admin_pending_list():
    signup_pending('adminpend@example.com','adminpenduser','EHS')
    t=admin_token()
    r=client.get('/admin/approvals/pending',headers={'Authorization':f'Bearer {t}'})
    assert r.status_code==200,r.text
    emails={u['email'] for u in r.json()}
    assert 'adminpend@example.com' in emails
    assert all(u['status']=='pending' for u in r.json())

def test_admin_pending_requires_admin():
    ut=make_active_user('notadmin@example.com','notadminuser',['ER'])
    assert client.get('/admin/approvals/pending',headers={'Authorization':f'Bearer {ut}'}).status_code==403

def test_hdfs_check_post_allowed():
    ut=make_active_user('hc1@example.com','hcuser1',['QC'])
    r=client.post('/resources/hdfs/check',headers={'Authorization':f'Bearer {ut}'},json={'path':'/naplatform/departments/QC/department_shared/department_shared/reports','department':'QC'})
    assert r.status_code==200,r.text; assert r.json()['allowed'] is True

def test_hdfs_check_post_denied():
    ut=make_active_user('hc2@example.com','hcuser2',['QC'])
    r=client.post('/resources/hdfs/check',headers={'Authorization':f'Bearer {ut}'},json={'path':'/naplatform/departments/IT/department_shared/secret','department':'QC'})
    assert r.status_code==403,r.text

def test_hdfs_check_post_unknown_department():
    ut=make_active_user('hc3@example.com','hcuser3',['QC'])
    r=client.post('/resources/hdfs/check',headers={'Authorization':f'Bearer {ut}'},json={'path':'/naplatform/departments/QC/department_shared','department':'NOPE'})
    assert r.status_code==403,r.text

def test_hdfs_check_post_traversal_denied():
    ut=make_active_user('hc4@example.com','hcuser4',['QC'])
    r=client.post('/resources/hdfs/check',headers={'Authorization':f'Bearer {ut}'},json={'path':'/naplatform/departments/QC/department_shared/../IT/secret','department':'QC'})
    assert r.status_code==403,r.text

def test_hdfs_check_get_still_works():
    ut=make_active_user('hc5@example.com','hcuser5',['QC'])
    r=client.get('/resources/hdfs/check',params={'path':'/naplatform/departments/QC/department_shared/department_shared/reports','department':'QC'},headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==200,r.text; assert r.json()['allowed'] is True

def test_resource_path_traversal_denied():
    ut=make_active_user('trav@example.com','travuser',['QC'])
    r=client.get('/resources/QC',params={'path':'/naplatform/departments/QC/department_shared/../IT/secret'},headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_agent_context_regression():
    ut=make_active_user('ctx@example.com','ctxuser',['IT'])
    r=client.get('/agents/IT/context',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==200,r.text; b=r.json()
    assert b['department']=='IT'; assert b['username']=='ctxuser'
    assert '/naplatform/departments/IT/department_shared' in b['hdfs_roots']; assert '/naplatform/users/ctxuser/workspace' in b['hdfs_roots']
    assert 'ticket_lookup' in b['allowed_tools']; assert 'mcp-it-filesystem' in b['allowed_mcp_servers']

def test_agent_context_other_department_denied():
    ut=make_active_user('ctx2@example.com','ctxuser2',['IT'])
    r=client.get('/agents/QC/context',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_agent_context_unknown_department():
    ut=make_active_user('ctx3@example.com','ctxuser3',['IT'])
    r=client.get('/agents/NOPE/context',headers={'Authorization':f'Bearer {ut}'})
    assert r.status_code==403,r.text

def test_chat_unknown_department():
    ut=make_active_user('chat3@example.com','chatuser3',['ER'])
    r=client.post('/agents/NOPE/chat',headers={'Authorization':f'Bearer {ut}'},json={'message':'hi'})
    assert r.status_code==403,r.text
