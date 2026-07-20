"""Neo4j graph-scope adapters (Phase 05 memory scaffold + Phase 10 real backend).

Like the vector store, a single shared Neo4j instance is separated *by metadata*
rather than by database-per-tenant: every node/relationship carries scope
properties (`owner_user_id`, `allowed_users`, `department`, `allowed_departments`)
and every read is constrained by a Cypher `WHERE` fragment built from the caller's
RBAC scope. This reuses `rbac.neo4j_filter` semantics.

Two interchangeable backends implement the same build_node_match / insert / search
contract:

- `GraphScopeAdapter` (default, `GRAPH_BACKEND=memory`) emits parameterised Cypher
  descriptors (never string-interpolated user data) and keeps a deterministic
  in-memory node/relationship store so the scope logic is testable offline.
- `Neo4jGraphBackend` (`GRAPH_BACKEND=neo4j`) drives a real `neo4j` driver when
  installed/configured (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`). It runs the
  **same** parameterised Cypher — scope values and user properties are always
  bound as `$params`, never interpolated — so the only identifiers inlined are the
  strictly-validated label / relationship type. It talks to the driver through a
  small wrapper, so tests inject a fake session/driver and no live Neo4j is
  required.

`resolve_graph_adapter()` selects the backend from the env and falls back to the
memory backend (with a logged warning) if the selection is invalid or a real
driver cannot be built — the API never fails to start because of a backend env.
"""
import logging
import os
import re
import warnings
from uuid import uuid4

from .models import User
from .rbac import AccessDenied, normalize_department, neo4j_filter

log = logging.getLogger(__name__)

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

    backend = "memory"

    def __init__(self, nodes: dict[str, list[dict]] | None = None,
                 relationships: list[dict] | None = None):
        # label -> list of node dicts; relationships as a flat insertion-ordered list.
        self._nodes: dict[str, list[dict]] = nodes if nodes is not None else {}
        self._rels: list[dict] = relationships if relationships is not None else []

    # --- introspection -----------------------------------------------------
    def status(self) -> dict:
        """Secret-free health/config summary for the admin status endpoint."""
        return {"backend": self.backend, "mode": self.backend, "configured": True,
                "url": None, "healthy": True, "detail": "in-memory (dry-run) store",
                "labels": len(self._nodes)}

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


# =========================================================================
# Phase 10 — real Neo4j backend (driver-wrapped, fake-injectable, no live DB)
# =========================================================================
VALID_GRAPH_BACKENDS = ("memory", "neo4j")

# Scope property keys that live on a node/relationship; everything else the caller
# stores is a user-facing property. Used to split a flat Neo4j record back into
# {properties, metadata} on read.
_META_KEYS = ("owner_user_id", "allowed_users", "department", "allowed_departments")


def graph_backend_mode() -> str:
    return (os.environ.get("GRAPH_BACKEND", "memory").strip().lower() or "memory")


def _redact_uri(uri: str | None) -> str | None:
    """Drop any userinfo/query so a status echo can never leak credentials."""
    if not uri:
        return None
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(uri)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class Neo4jConfig:
    """Env-derived Neo4j connection config. Never echoes the password."""

    def __init__(self, *, uri: str | None = None, user: str | None = None,
                 password: str | None = None, database: str | None = None):
        self.uri = (uri if uri is not None
                    else os.environ.get("NEO4J_URI", "")).strip() or None
        self.user = (user if user is not None
                     else os.environ.get("NEO4J_USER", "")).strip() or None
        self.password = (password if password is not None
                         else os.environ.get("NEO4J_PASSWORD", "")).strip() or None
        self.database = (database if database is not None
                         else os.environ.get("NEO4J_DATABASE", "")).strip() or None

    @property
    def configured(self) -> bool:
        return bool(self.uri)


def _split_props(flat: dict) -> tuple[dict, dict]:
    """Split a flat Neo4j property map into (user properties, scope metadata)."""
    metadata = {k: flat[k] for k in _META_KEYS if k in flat}
    properties = {k: v for k, v in flat.items()
                  if k not in _META_KEYS and k not in ("id", "scope")}
    return properties, metadata


class FakeNeo4jDriver:
    """In-memory stand-in for a Neo4j driver wrapper — for tests/dry-run status.

    It emulates the parameterised Cypher the backend runs: CREATE stores a record,
    MATCH returns the records whose scope metadata satisfies the *bound params*
    (never the query text). It records every (cypher, params) call so tests can
    assert values are passed as parameters and never interpolated into Cypher.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.nodes: list[dict] = []   # (label, props)
        self.rels: list[dict] = []    # (rel_type, props)

    def ping(self) -> bool:
        return True

    @staticmethod
    def _ident(cypher: str, pattern: str) -> str | None:
        m = re.search(pattern, cypher)
        return m.group(1) if m else None

    @staticmethod
    def _scope_ok(record: dict, params: dict) -> bool:
        return (record.get("owner_user_id") == params.get("owner_user_id")
                or params.get("user_id") in (record.get("allowed_users") or [])
                or record.get("department") == params.get("department")
                or params.get("department") in (record.get("allowed_departments") or []))

    def run(self, cypher: str, params: dict) -> list[dict]:
        params = params or {}
        self.calls.append((cypher, dict(params)))
        if "CREATE" in cypher:
            props = dict(params.get("props", {}))
            if "start_id" in params:  # relationship create
                self.rels.append({"_type": self._ident(cypher, r"\[r:(\w+)\]"),
                                  **props})
            else:  # node create
                self.nodes.append({"_label": self._ident(cypher, r"\(n:(\w+)\)"),
                                   **props})
            return [props]
        if "[r:" in cypher:  # relationship match
            rt = self._ident(cypher, r"\[r:(\w+)\]")
            return [{k: v for k, v in r.items() if k != "_type"}
                    for r in self.rels
                    if r.get("_type") == rt and self._scope_ok(r, params)]
        lbl = self._ident(cypher, r"\(n:(\w+)\)")  # node match
        return [{k: v for k, v in n.items() if k != "_label"}
                for n in self.nodes
                if n.get("_label") == lbl and self._scope_ok(n, params)]


class _RealNeo4jDriver:  # pragma: no cover - needs live neo4j
    """Wraps a live `neo4j` driver, running each parameterised query in a session."""

    def __init__(self, driver, database: str | None = None):
        self._d = driver
        self._database = database

    def ping(self) -> bool:
        try:
            self._d.verify_connectivity()
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False

    def run(self, cypher: str, params: dict) -> list[dict]:
        kwargs = {"database": self._database} if self._database else {}
        with self._d.session(**kwargs) as session:
            result = session.run(cypher, **(params or {}))
            out = []
            for record in result:
                val = record[0]
                out.append(dict(val) if hasattr(val, "items") else val)
            return out


def build_real_neo4j_driver(config: Neo4jConfig):  # pragma: no cover - needs neo4j
    """Instantiate a live Neo4j driver wrapper. Raises if the lib is missing."""
    from neo4j import GraphDatabase  # lazy: optional dependency
    auth = (config.user, config.password) if config.user else None
    driver = GraphDatabase.driver(config.uri, auth=auth)
    return _RealNeo4jDriver(driver, database=config.database)


class Neo4jGraphBackend:
    """Real-backend adapter: same scope contract as `GraphScopeAdapter`.

    Storage is delegated to an injected driver wrapper (`FakeNeo4jDriver` in tests,
    `_RealNeo4jDriver` in production). The Cypher is *identical* to what the memory
    adapter's descriptors emit — labels/relationship types are the only inlined
    identifiers (strictly validated), and every scope value or user property is
    bound as a `$param`. Switching backends never changes the access rules.
    """

    backend = "neo4j"

    def __init__(self, driver, *, config: Neo4jConfig | None = None):
        self._driver = driver
        self._config = config or Neo4jConfig()

    # --- Cypher descriptors (shared with the memory adapter) ---------------
    def build_node_match(self, user: User, active_department: str, label: str,
                         var: str = "n") -> dict:
        dep = normalize_department(active_department)
        validate_label(label)
        neo4j_filter(user, dep)  # membership gate (raises AccessDenied)
        where = SCOPE_WHERE.format(v=var)
        return {"cypher": f"MATCH ({var}:{label}) WHERE {where} RETURN {var}",
                "params": _scope_params(user, dep)}

    def build_relationship_match(self, user: User, active_department: str,
                                 rel_type: str, var: str = "r") -> dict:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        neo4j_filter(user, dep)  # membership gate
        where = SCOPE_WHERE.format(v=var)
        return {"cypher": f"MATCH ()-[{var}:{rel_type}]->() WHERE {where} RETURN {var}",
                "params": _scope_params(user, dep)}

    def _scope_metadata(self, user: User, dep: str, scope: str) -> dict:
        if scope not in VALID_SCOPES:
            raise GraphScopeError(
                f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        if scope == PERSONAL:
            return {"owner_user_id": user.id}
        return {"department": dep, "allowed_departments": [dep]}

    def _row_to_node(self, flat: dict, label: str) -> dict:
        properties, metadata = _split_props(flat)
        return {"id": flat.get("id"), "label": label,
                "scope": flat.get("scope"), "properties": properties,
                "metadata": metadata}

    # --- writes ------------------------------------------------------------
    def insert_node(self, user: User, active_department: str, label: str,
                    properties: dict, scope: str, node_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_label(label)
        self.build_node_match(user, dep, label)  # membership + label gate
        meta = self._scope_metadata(user, dep, scope)
        node_id = node_id or str(uuid4())
        props = {**dict(properties or {}), "id": node_id, "scope": scope, **meta}
        # Label is the only inlined identifier (validated); all values are bound.
        self._driver.run(f"CREATE (n:{label}) SET n += $props RETURN n",
                         {"props": props})
        return {"id": node_id, "label": label, "scope": scope,
                "properties": dict(properties or {}), "metadata": meta}

    def insert_relationship(self, user: User, active_department: str, rel_type: str,
                            start_id: str, end_id: str, properties: dict, scope: str,
                            rel_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        neo4j_filter(user, dep)  # membership gate
        meta = self._scope_metadata(user, dep, scope)
        rel_id = rel_id or str(uuid4())
        props = {**dict(properties or {}), "id": rel_id, "scope": scope, **meta}
        self._driver.run(
            "MATCH (a {id:$start_id}), (b {id:$end_id}) "
            f"CREATE (a)-[r:{rel_type}]->(b) SET r += $props RETURN r",
            {"start_id": start_id, "end_id": end_id, "props": props})
        return {"id": rel_id, "type": rel_type, "scope": scope,
                "start_id": start_id, "end_id": end_id,
                "properties": dict(properties or {}), "metadata": meta}

    # --- reads -------------------------------------------------------------
    def search_nodes(self, user: User, active_department: str, label: str,
                     query: str | None = None, limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_label(label)
        desc = self.build_node_match(user, dep, label)  # membership gate
        rows = self._driver.run(desc["cypher"], desc["params"])
        nodes = [self._row_to_node(r, label) for r in rows]
        if query:
            q = query.lower()
            nodes = [n for n in nodes if q in str(n["properties"]).lower()]
        return nodes[: max(0, limit)]

    def search_relationships(self, user: User, active_department: str, rel_type: str,
                             limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_rel_type(rel_type)
        desc = self.build_relationship_match(user, dep, rel_type)  # membership gate
        rows = self._driver.run(desc["cypher"], desc["params"])
        rels = []
        for flat in rows:
            properties, metadata = _split_props(flat)
            rels.append({"id": flat.get("id"), "type": rel_type,
                         "scope": flat.get("scope"), "properties": properties,
                         "metadata": metadata})
        return rels[: max(0, limit)]

    # --- introspection -----------------------------------------------------
    def status(self) -> dict:
        try:
            healthy = bool(self._driver.ping())
        except Exception:  # noqa: BLE001 - status probe never raises
            healthy = False
        return {"backend": self.backend, "mode": self.backend,
                "configured": self._config.configured,
                "url": _redact_uri(self._config.uri),
                "user_set": bool(self._config.user),
                "password_set": bool(self._config.password),
                "database": self._config.database,
                "healthy": healthy}


def build_graph_backend(mode: str | None = None, *, driver=None):
    """Strict factory: build the requested backend or raise for an invalid mode."""
    mode = (mode or graph_backend_mode()).strip().lower()
    if mode == "memory":
        return GraphScopeAdapter()
    if mode == "neo4j":
        config = Neo4jConfig()
        return Neo4jGraphBackend(driver or build_real_neo4j_driver(config),
                                 config=config)
    raise GraphScopeError(
        f"invalid GRAPH_BACKEND {mode!r}; expected one of {VALID_GRAPH_BACKENDS}")


def resolve_graph_adapter():
    """Select the backend from env, falling back to memory (with a warning).

    Never fails app startup: an unknown `GRAPH_BACKEND`, a missing `neo4j` driver,
    or an unconfigured Neo4j all degrade safely to the in-memory scaffold.
    """
    mode = graph_backend_mode()
    if mode not in VALID_GRAPH_BACKENDS:
        warnings.warn(f"invalid GRAPH_BACKEND {mode!r}; falling back to memory",
                      RuntimeWarning, stacklevel=2)
        log.warning("invalid GRAPH_BACKEND %r; falling back to memory", mode)
        return GraphScopeAdapter()
    if mode == "neo4j" and not Neo4jConfig().configured:
        warnings.warn("GRAPH_BACKEND=neo4j but NEO4J_URI is unset; "
                      "falling back to memory", RuntimeWarning, stacklevel=2)
        log.warning("GRAPH_BACKEND=neo4j but NEO4J_URI unset; falling back to memory")
        return GraphScopeAdapter()
    try:
        return build_graph_backend(mode)
    except Exception as e:  # noqa: BLE001 - never fail startup on a backend issue
        warnings.warn(f"could not build {mode!r} graph backend ({e}); "
                      "falling back to memory", RuntimeWarning, stacklevel=2)
        log.warning("could not build %r graph backend (%s); falling back to memory",
                    mode, e)
        return GraphScopeAdapter()


# Shared adapter instance used by the API endpoints. Default env -> memory backend,
# so the Phase 05 behavior/contract is unchanged unless GRAPH_BACKEND=neo4j.
graph_adapter = resolve_graph_adapter()
