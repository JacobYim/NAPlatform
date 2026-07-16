from .models import DEPARTMENTS, User, UserStatus
BASE="/naplatform"
class AccessDenied(Exception): pass
def normalize_department(department:str)->str:
    dep=department.upper()
    if dep not in DEPARTMENTS: raise ValueError(f"unknown department: {department}")
    return dep
def user_personal_root(user:User)->str: return f"{BASE}/users/{user.username}"
def department_root(department:str)->str: return f"{BASE}/departments/{normalize_department(department)}"
def allowed_hdfs_roots(user:User, active_department:str|None=None)->list[str]:
    if user.status!=UserStatus.active: return []
    roots=[user_personal_root(user)]; deps=[normalize_department(d) for d in user.departments]
    if active_department:
        dep=normalize_department(active_department)
        if dep not in deps and not user.is_admin: raise AccessDenied("user is not a member of requested department")
        roots.append(department_root(dep))
    else: roots += [department_root(d) for d in deps]
    return sorted(set(roots))
def assert_hdfs_path_allowed(user:User,path:str,active_department:str|None=None)->None:
    p=path.rstrip('/') or '/'
    if any(p==r or p.startswith(r+'/') for r in allowed_hdfs_roots(user,active_department)): return
    raise AccessDenied(f"path not allowed: {path}")
def qdrant_filter(user:User, active_department:str)->dict:
    dep=normalize_department(active_department)
    if dep not in user.departments and not user.is_admin: raise AccessDenied("department vector scope denied")
    return {"should":[{"key":"owner_user_id","match":{"value":user.id}},{"key":"allowed_users","match":{"any":[user.id]}},{"key":"department","match":{"value":dep}},{"key":"allowed_departments","match":{"any":[dep]}}]}
def neo4j_filter(user:User, active_department:str)->dict:
    dep=normalize_department(active_department)
    if dep not in user.departments and not user.is_admin: raise AccessDenied("department graph scope denied")
    return {"owner_user_id":user.id,"department":dep,"allowed_departments":[dep],"allowed_users":[user.id]}
DEPARTMENT_TOOLS={"ER":["hdfs_list","hdfs_read","qdrant_search","neo4j_query","incident_report"],"IT":["hdfs_list","hdfs_read","qdrant_search","neo4j_query","ticket_lookup","openshell"],"EHS":["hdfs_list","hdfs_read","qdrant_search","neo4j_query","safety_checklist"],"QC":["hdfs_list","hdfs_read","qdrant_search","neo4j_query","quality_trace"]}
def allowed_tools(user:User, active_department:str)->list[str]:
    dep=normalize_department(active_department)
    if user.status!=UserStatus.active: return []
    if dep not in user.departments and not user.is_admin: raise AccessDenied("department tool scope denied")
    tools=DEPARTMENT_TOOLS[dep]
    return sorted(set(tools+["admin_users","admin_audit","nemoclaw","openshell"])) if user.is_admin else tools
def allowed_mcp_servers(user:User, active_department:str)->list[str]:
    dep=normalize_department(active_department)
    if dep not in user.departments and not user.is_admin: raise AccessDenied("department MCP scope denied")
    return [f"mcp-{dep.lower()}-filesystem", f"mcp-{dep.lower()}-knowledge"]
