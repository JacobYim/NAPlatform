"""Neo4j graph-scope adapter (Phase 05).

Like the vector store, a single shared Neo4j instance is separated *by metadata*
rather than by database-per-tenant: every node/relationship carries scope
properties (`owner_user_id`, `allowed_users`, `department`, `allowed_departments`)
and every read is constrained by a Cypher `WHERE` fragment built from the caller's
RBAC scope. This reuses `rbac.neo4j_filter` semantics.

Nothing here talks to a live Neo4j. `GraphScopeAdapter` emits parameterised Cypher
descriptors (never string-interpolated user data) and keeps a deterministic
in-memory node/relationship store so the scope logic is testable offline. Labels
and relationship types are the only identifiers that must be inlined into Cypher,
so they are validated against strict regexes before use.
"""
import re
from uuid import uuid4

from .models import User
from .rbac import AccessDenied, normalize_department, neo4j_filter

# Neo4j labels are conventionally PascalCase; relationship types SCREAMING_SNAKE.
# Both are inlined into Cypher (they cannot be parameterised), so they are locked
# to `[A-Za-z][A-Za-z0-9_]{0,63}` / `[A-Z][A-Z0-9_]{0,63}` — no quotes, spaces,
# backticks, or anything that could break out of the identifier.
LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
REL_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

PERSONAL = "personal"
DEPARTMENT = "department"
VALID_SCOPES = (PERSONAL, DEPARTMENT)

# Parameterised scope predicate shared by node and relationship reads. `$var`
# placeholders are bound from `_scope_params`, never string-formatted.
SCOPE_WHERE = ("({v}.owner_user_id = $owner_user_id "
               "OR $user_id IN coalesce({v}.allowed_users, []) "
               "OR {v}.department = $department "
               "OR $department IN coalesce({v}.allowed_departments, []))")


class GraphScopeError(ValueError):
    """Raised for invalid labels/relationship types or unknown/illegal scopes."""


def validate_label(label: str) -> str:
    if not isinstance(label, str) or not LABEL_RE.match(label):
        raise GraphScopeError(f"invalid node label: {label!r}")
    return label


def validate_rel_type(rel_type: str) -> str:
    if not isinstance(rel_type, str) or not REL_TYPE_RE.match(rel_type):
        raise GraphScopeError(f"invalid relationship type: {rel_type!r}")
    return rel_type


def _scope_params(user: User, dep: str) -> dict:
    return {"owner_user_id": user.id, "user_id": user.id, "department": dep}


def _match_metadata(meta: dict, user: User, dep: str) -> bool:
    """Mirror the scope `WHERE` fragment against a node/relationship's metadata."""
    return (meta.get("owner_user_id") == user.id
            or user.id in (meta.get("allowed_users") or [])
            or meta.get("department") == dep
            or dep in (meta.get("allowed_departments") or []))


class GraphScopeAdapter:
    """Builds Neo4j Cypher scope descriptors and enforces them in-memory."""

    def __init__(self, nodes: dict[str, list[dict]] | None = None,
                 relationships: list[dict] | None = None):
        # label -> list of node dicts; relationships as a flat insertion-ordered list.
        self._nodes: dict[str, list[dict]] = nodes if nodes is not None else {}
        self._rels: list[dict] = relationships if relationships is not None else []

    # --- Cypher descriptors ------------------------------------------------
    def build_node_match(self, user: User, active_department: str, label: str,
                         var: str = "n") -> dict:
        """Return a parameterised Cypher MATCH descriptor for the caller's scope."""
        dep = normalize_department(active_department)
        validate_label(label)
        neo4j_filter(user, dep)  # membership gate (raises AccessDenied)
        where = SCOPE_WHERE.format(v=var)
        cypher = f"MATCH ({var}:{label}) WHERE {where} RETURN {var}"
        return {"cypher": cypher, "params": _scope_params(user, dep)}

    def build_relationship_match(self, user: User, active_department: str,
                                 rel_type: str, var: str = "r") -> dict:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        neo4j_filter(user, dep)  # membership gate (raises AccessDenied)
        where = SCOPE_WHERE.format(v=var)
        cypher = f"MATCH ()-[{var}:{rel_type}]->() WHERE {where} RETURN {var}"
        return {"cypher": cypher, "params": _scope_params(user, dep)}

    def _scope_metadata(self, user: User, dep: str, scope: str) -> dict:
        if scope not in VALID_SCOPES:
            raise GraphScopeError(
                f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        if scope == PERSONAL:
            return {"owner_user_id": user.id}
        return {"department": dep, "allowed_departments": [dep]}

    # --- writes ------------------------------------------------------------
    def insert_node(self, user: User, active_department: str, label: str,
                    properties: dict, scope: str, node_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_label(label)
        self.build_node_match(user, dep, label)  # membership + label gate
        meta = self._scope_metadata(user, dep, scope)
        node = {"id": node_id or str(uuid4()), "label": label, "scope": scope,
                "properties": dict(properties or {}), "metadata": meta}
        self._nodes.setdefault(label, []).append(node)
        return node

    def insert_relationship(self, user: User, active_department: str, rel_type: str,
                            start_id: str, end_id: str, properties: dict, scope: str,
                            rel_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        neo4j_filter(user, dep)  # membership gate
        meta = self._scope_metadata(user, dep, scope)
        rel = {"id": rel_id or str(uuid4()), "type": rel_type, "scope": scope,
               "start_id": start_id, "end_id": end_id,
               "properties": dict(properties or {}), "metadata": meta}
        self._rels.append(rel)
        return rel

    # --- reads -------------------------------------------------------------
    def search_nodes(self, user: User, active_department: str, label: str,
                     query: str | None = None, limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_label(label)
        neo4j_filter(user, dep)  # membership gate
        results = [n for n in self._nodes.get(label, [])
                   if _match_metadata(n["metadata"], user, dep)]
        if query:
            q = query.lower()
            results = [n for n in results if q in str(n["properties"]).lower()]
        return results[: max(0, limit)]

    def search_relationships(self, user: User, active_department: str, rel_type: str,
                             limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        neo4j_filter(user, dep)  # membership gate
        results = [r for r in self._rels
                   if r["type"] == rel_type and _match_metadata(r["metadata"], user, dep)]
        return results[: max(0, limit)]


# Shared adapter instance used by the API endpoints.
graph_adapter = GraphScopeAdapter()
