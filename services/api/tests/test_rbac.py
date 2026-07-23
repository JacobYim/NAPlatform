import pytest
from app.models import User,UserStatus
from app.rbac import *
from app.security import hash_password
@pytest.fixture
def er_user(): return User(id="u1",email="a@example.com",username="alice",password_hash=hash_password("Password123!"),status=UserStatus.active,departments=["ER"])
def test_hdfs_roots(er_user): assert allowed_hdfs_roots(er_user,"ER")==["/naplatform/departments/ER/department_shared","/naplatform/users/alice/chat_history","/naplatform/users/alice/workspace"]
def test_hdfs_denies_other_department(er_user):
    with pytest.raises(AccessDenied): assert_hdfs_path_allowed(er_user,"/naplatform/departments/IT/department_shared/a","ER")
def test_hdfs_denies_user_parent(er_user):
    with pytest.raises(AccessDenied): assert_hdfs_path_allowed(er_user,"/naplatform/users/alice","ER")
def test_qdrant_filter(er_user): assert {p['key'] for p in qdrant_filter(er_user,'ER')['should']}=={'owner_user_id','allowed_users','department','allowed_departments'}
def test_neo4j_filter(er_user): assert neo4j_filter(er_user,'ER')['department']=='ER'
def test_tools_department_scoped(er_user): assert 'incident_report' in allowed_tools(er_user,'ER') and 'ticket_lookup' not in allowed_tools(er_user,'ER')
def test_mcp_servers(er_user): assert allowed_mcp_servers(er_user,'ER')==['mcp-er-filesystem','mcp-er-knowledge']
