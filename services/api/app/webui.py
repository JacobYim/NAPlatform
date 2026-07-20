"""Phase 11: core-webui auth/session UI integration model.

Pure helpers that turn a NAPlatform user + RBAC scope into the *session
bootstrap* and *department-routing* model the core-webui adapter consumes.

These functions build only route strings and public department metadata — no
session tokens, passwords, or other secrets ever pass through here — so the
adapter can render the login/signup/department-selector/approval-waiting UX
without re-deriving RBAC or handling secrets. Department membership is validated
with the existing ``rbac`` semantics (``normalize_department`` + membership),
keeping the Phase 02+ contracts intact.
"""
from .models import DEPARTMENTS, DepartmentOption, DepartmentRoute, User, UserStatus
from .rbac import AccessDenied, normalize_department

# Stable, human-facing labels for the signup / department selector. Values are
# the canonical department keys used everywhere else in the API.
DEPARTMENT_LABELS = {
    "ER": "Emergency Response",
    "IT": "Information Technology",
    "EHS": "Environment, Health & Safety",
    "QC": "Quality Control",
}

# The UI routes the API exposes per department. These mirror the real endpoints
# so core-webui can route chat to /agents/{department}/chat without guessing.
CHAT_ROUTE_TEMPLATE = "/agents/{department}/chat"
CONTEXT_ROUTE_TEMPLATE = "/agents/{department}/context"
RESOURCES_ROUTE_TEMPLATE = "/resources/{department}"


def department_label(dep: str) -> str:
    return DEPARTMENT_LABELS.get(dep, dep)


def department_options() -> list[DepartmentOption]:
    """Public department options for the signup / selector dropdown."""
    return [DepartmentOption(value=d, label=department_label(d)) for d in sorted(DEPARTMENTS)]


def department_route(dep: str) -> DepartmentRoute:
    """Build the per-department UI route model. Validates the department key."""
    d = normalize_department(dep)
    return DepartmentRoute(
        department=d,
        label=department_label(d),
        chat_route=CHAT_ROUTE_TEMPLATE.format(department=d),
        context_route=CONTEXT_ROUTE_TEMPLATE.format(department=d),
        resources_route=RESOURCES_ROUTE_TEMPLATE.format(department=d),
    )


def user_department_routes(user: User) -> list[DepartmentRoute]:
    """Routes for every department the user belongs to (normalized, deduped)."""
    seen: list[str] = []
    for raw in user.departments:
        d = normalize_department(raw)
        if d not in seen:
            seen.append(d)
    return [department_route(d) for d in seen]


def assert_department_selectable(user: User, department: str) -> DepartmentRoute:
    """Return the route for ``department`` iff the user may route chat there.

    Raises ``ValueError`` for an unknown department and ``AccessDenied`` when the
    user is not a member (admins may select any department), matching the RBAC
    rule the chat/context endpoints already enforce.
    """
    d = normalize_department(department)  # ValueError on unknown department
    members = [normalize_department(x) for x in user.departments]
    if d not in members and not user.is_admin:
        raise AccessDenied("user is not a member of requested department")
    return department_route(d)


def approval_ux(user: User) -> dict:
    """The approval-waiting UX contract the adapter renders for a session.

    A pending signup shows the "awaiting approval" screen; a disabled account
    shows a blocked screen; an active account can proceed. ``can_access`` is the
    single boolean the adapter branches on. No secrets are included.
    """
    if user.status == UserStatus.pending:
        return {"state": "pending", "can_access": False,
                "title": "Approval required",
                "message": "Your account is awaiting administrator approval."}
    if user.status == UserStatus.disabled:
        return {"state": "disabled", "can_access": False,
                "title": "Account disabled",
                "message": "Your account has been disabled. Contact an administrator."}
    return {"state": "active", "can_access": True,
            "title": "Active", "message": "Your account is active."}
