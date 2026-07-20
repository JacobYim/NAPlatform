"""Qdrant vector-scope adapters (Phase 05 memory scaffold + Phase 10 real backend).

A single shared Qdrant instance is separated *by metadata*, not by physical
collection-per-tenant: every point carries scope metadata (`owner_user_id`,
`allowed_users`, `department`, `allowed_departments`) and every query is
constrained by a Qdrant filter derived from the caller's RBAC scope. This module
reuses `rbac.qdrant_filter` semantics so the same access rules the agent context
exposes are the ones enforced on insert/search.

Two interchangeable backends implement the same insert/search/build_filter
contract:

- `VectorScopeAdapter` (default, `VECTOR_BACKEND=memory`) keeps a deterministic
  in-memory store so the scope logic runs without any service. Phase 05 behavior
  is preserved verbatim.
- `QdrantVectorBackend` (`VECTOR_BACKEND=qdrant`) drives a real `qdrant_client`
  when installed/configured (`QDRANT_URL`, optional `QDRANT_API_KEY`), creating
  the collection on demand with a configurable vector size/distance and enforcing
  the *same* scope metadata + filter descriptors server-side. It talks to the
  store only through a small client wrapper, so tests inject a fake client and no
  live Qdrant is ever required.

`resolve_vector_adapter()` selects the backend from the env and falls back to the
memory backend (with a logged warning) if the selection is invalid or a real
client cannot be built — the API never fails to start because of a backend env.
"""
import logging
import os
import re
import warnings
from uuid import uuid4

from .models import User
from .rbac import AccessDenied, normalize_department, qdrant_filter

log = logging.getLogger(__name__)

# Collection names must be lowercase, start with a letter, and contain only
# `[a-z0-9_]`, 3..64 chars. Deliberately strict so a collection name can never
# carry a path/traversal/injection payload into the vector backend.
COLLECTION_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

PERSONAL = "personal"
DEPARTMENT = "department"
VALID_SCOPES = (PERSONAL, DEPARTMENT)


class VectorScopeError(ValueError):
    """Raised for invalid collection names or unknown/illegal scopes."""


def validate_collection(name: str) -> str:
    if not isinstance(name, str) or not COLLECTION_RE.match(name):
        raise VectorScopeError(f"invalid collection name: {name!r}")
    return name


def _match_metadata(meta: dict, user: User, dep: str) -> bool:
    """Mirror the `qdrant_filter` should-clauses against a point's metadata.

    A point is visible iff the caller owns it, is explicitly allowed on it, the
    point belongs to the active department, or that department is allowed on it.
    """
    return (meta.get("owner_user_id") == user.id
            or user.id in (meta.get("allowed_users") or [])
            or meta.get("department") == dep
            or dep in (meta.get("allowed_departments") or []))


class VectorScopeAdapter:
    """Builds Qdrant scope filters and enforces them over an in-memory store."""

    backend = "memory"

    def __init__(self, store: dict[str, list[dict]] | None = None):
        # collection name -> list of point dicts (insertion-ordered for determinism)
        self._collections: dict[str, list[dict]] = store if store is not None else {}

    # --- introspection -----------------------------------------------------
    def status(self) -> dict:
        """Secret-free health/config summary for the admin status endpoint."""
        return {"backend": self.backend, "mode": self.backend, "configured": True,
                "url": None, "healthy": True, "detail": "in-memory (dry-run) store",
                "collections": len(self._collections)}

    # --- filter descriptors ------------------------------------------------
    def build_filter(self, user: User, active_department: str) -> dict:
        """Return the Qdrant `Filter` payload for the caller's scope.

        Raises `AccessDenied` if the user is not a member of the department
        (and not admin), reusing the RBAC semantics verbatim.
        """
        return qdrant_filter(user, normalize_department(active_department))

    # --- writes ------------------------------------------------------------
    def insert(self, user: User, active_department: str, collection: str,
               payload: dict, scope: str, record_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_collection(collection)
        self.build_filter(user, dep)  # membership gate (raises AccessDenied)
        if scope not in VALID_SCOPES:
            raise VectorScopeError(
                f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        meta: dict = {}
        if scope == PERSONAL:
            meta["owner_user_id"] = user.id
        else:  # DEPARTMENT
            meta["department"] = dep
            meta["allowed_departments"] = [dep]
        record = {"id": record_id or str(uuid4()), "collection": collection,
                  "scope": scope, "payload": dict(payload or {}), "metadata": meta}
        self._collections.setdefault(collection, []).append(record)
        return record

    # --- reads -------------------------------------------------------------
    def search(self, user: User, active_department: str, collection: str,
               query: str | None = None, limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_collection(collection)
        self.build_filter(user, dep)  # membership gate (raises AccessDenied)
        results = [r for r in self._collections.get(collection, [])
                   if _match_metadata(r["metadata"], user, dep)]
        if query:
            q = query.lower()
            results = [r for r in results if q in str(r["payload"]).lower()]
        return results[: max(0, limit)]


# =========================================================================
# Phase 10 — real Qdrant backend (client-wrapped, fake-injectable, no live DB)
# =========================================================================
VALID_VECTOR_BACKENDS = ("memory", "qdrant")
DEFAULT_VECTOR_SIZE = 768
DEFAULT_DISTANCE = "Cosine"
# Distances mirror qdrant_client.models.Distance; validated here so a bad env
# value never reaches the driver.
VALID_DISTANCES = ("Cosine", "Dot", "Euclid", "Manhattan")


def vector_backend_mode() -> str:
    return (os.environ.get("VECTOR_BACKEND", "memory").strip().lower() or "memory")


def _redact_url(url: str | None) -> str | None:
    """Drop any userinfo/query so a status echo can never leak credentials."""
    if not url:
        return None
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class QdrantConfig:
    """Env-derived Qdrant connection/collection config. Never echoes the api key."""

    def __init__(self, *, url: str | None = None, api_key: str | None = None,
                 vector_size: int | None = None, distance: str | None = None):
        self.url = (url if url is not None
                    else os.environ.get("QDRANT_URL", "")).strip() or None
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("QDRANT_API_KEY", "")).strip() or None
        size = vector_size if vector_size is not None else os.environ.get(
            "QDRANT_VECTOR_SIZE")
        try:
            self.vector_size = int(size) if size else DEFAULT_VECTOR_SIZE
        except (TypeError, ValueError):
            self.vector_size = DEFAULT_VECTOR_SIZE
        dist = (distance if distance is not None
                else os.environ.get("QDRANT_DISTANCE", DEFAULT_DISTANCE)).strip()
        # Normalize case, fall back to the default for anything unrecognized.
        self.distance = next((d for d in VALID_DISTANCES if d.lower() == dist.lower()),
                             DEFAULT_DISTANCE)

    @property
    def configured(self) -> bool:
        return bool(self.url)


def qdrant_filter_matches(descriptor: dict, metadata: dict) -> bool:
    """Reference semantics for our Qdrant `should` filter, used by the fake client.

    A point matches if *any* should-clause matches its metadata: a `match.value`
    equals the field, or a `match.any` list intersects a list-valued field. This
    mirrors what a live Qdrant applies server-side from the same descriptor.
    """
    for clause in descriptor.get("should", []):
        key = clause.get("key")
        match = clause.get("match", {})
        value = metadata.get(key)
        if "value" in match:
            if value == match["value"]:
                return True
        elif "any" in match:
            wanted = match["any"] or []
            if isinstance(value, (list, tuple, set)):
                if any(v in wanted for v in value):
                    return True
            elif value in wanted:
                return True
    return False


class FakeQdrantClient:
    """In-memory stand-in for a Qdrant client wrapper — for tests/dry-run status.

    It records collection creation and upserts and applies the scope descriptor
    exactly like `qdrant_filter_matches`, so backend tests exercise the real
    upsert/search/filter path without a live Qdrant.
    """

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.points: dict[str, dict[str, dict]] = {}
        self.created: list[str] = []

    def ping(self) -> bool:
        return True

    def ensure_collection(self, name: str, vector_size: int, distance: str) -> bool:
        if name in self.collections:
            return False
        self.collections[name] = {"vector_size": vector_size, "distance": distance}
        self.points.setdefault(name, {})
        self.created.append(name)
        return True

    def upsert(self, collection: str, point_id: str, payload: dict) -> None:
        self.points.setdefault(collection, {})[point_id] = dict(payload)

    def search(self, collection: str, scope_filter: dict, query: str | None,
               limit: int) -> list[dict]:
        # Filter the payload ROOT (like a live Qdrant), not a nested `metadata`
        # sub-object: the scope descriptor's keys are top-level payload fields.
        rows = [p for p in self.points.get(collection, {}).values()
                if qdrant_filter_matches(scope_filter, p)]
        if query:
            q = query.lower()
            rows = [r for r in rows if q in str(r.get("payload")).lower()]
        return rows[: max(0, limit)]


class _RealQdrantClient:
    """Wraps a live `qdrant_client.QdrantClient`, translating our descriptors."""

    def __init__(self, client, models):  # pragma: no cover - needs live qdrant
        self._c = client
        self._m = models

    def ping(self) -> bool:  # pragma: no cover - needs live qdrant
        try:
            self._c.get_collections()
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False

    def ensure_collection(self, name: str, vector_size: int,
                          distance: str) -> bool:  # pragma: no cover - needs live qdrant
        existing = {c.name for c in self._c.get_collections().collections}
        if name in existing:
            return False
        self._c.create_collection(
            collection_name=name,
            vectors_config=self._m.VectorParams(
                size=vector_size, distance=self._m.Distance[distance.upper()]))
        return True

    def _to_filter(self, scope_filter: dict):  # pragma: no cover - needs live qdrant
        should = []
        for clause in scope_filter.get("should", []):
            match = clause.get("match", {})
            if "value" in match:
                cond = self._m.FieldCondition(
                    key=clause["key"], match=self._m.MatchValue(value=match["value"]))
            else:
                cond = self._m.FieldCondition(
                    key=clause["key"], match=self._m.MatchAny(any=match.get("any", [])))
            should.append(cond)
        return self._m.Filter(should=should)

    def upsert(self, collection: str, point_id: str,
               payload: dict) -> None:  # pragma: no cover - needs live qdrant
        # No embeddings in the scaffold: store a deterministic zero vector so the
        # point is addressable; scope filtering is metadata-only via scroll().
        size = self._c.get_collection(collection).config.params.vectors.size
        self._c.upsert(collection_name=collection, points=[self._m.PointStruct(
            id=point_id, vector=[0.0] * size, payload=payload)])

    def search(self, collection: str, scope_filter: dict, query: str | None,
               limit: int) -> list[dict]:  # pragma: no cover - needs live qdrant
        points, _next = self._c.scroll(
            collection_name=collection, scroll_filter=self._to_filter(scope_filter),
            limit=limit, with_payload=True)
        rows = [dict(p.payload or {}) for p in points]
        if query:
            q = query.lower()
            rows = [r for r in rows if q in str(r.get("payload")).lower()]
        return rows


def build_real_qdrant_client(config: QdrantConfig):  # pragma: no cover - needs qdrant_client
    """Instantiate a live Qdrant client wrapper. Raises if the lib is missing."""
    from qdrant_client import QdrantClient  # lazy: optional dependency
    from qdrant_client.http import models
    client = QdrantClient(url=config.url, api_key=config.api_key)
    return _RealQdrantClient(client, models)


class QdrantVectorBackend:
    """Real-backend adapter: same scope contract as `VectorScopeAdapter`.

    Storage is delegated to an injected client wrapper (`FakeQdrantClient` in
    tests, `_RealQdrantClient` in production). The scope metadata stamping and the
    Qdrant `Filter` descriptor are identical to the memory adapter, so switching
    backends never changes the access rules.
    """

    backend = "qdrant"

    def __init__(self, client, *, config: QdrantConfig | None = None):
        self._client = client
        self._config = config or QdrantConfig()

    # --- filter descriptors ------------------------------------------------
    def build_filter(self, user: User, active_department: str) -> dict:
        return qdrant_filter(user, normalize_department(active_department))

    def _scope_metadata(self, user: User, dep: str, scope: str) -> dict:
        if scope not in VALID_SCOPES:
            raise VectorScopeError(
                f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        if scope == PERSONAL:
            return {"owner_user_id": user.id}
        return {"department": dep, "allowed_departments": [dep]}

    @staticmethod
    def _point_payload(record: dict) -> dict:
        """Qdrant point payload for a record.

        The scope metadata is *flattened onto the payload root* so the filter's
        `owner_user_id`/`department`/`allowed_departments`/`allowed_users` keys
        target the very fields a live Qdrant filters on (Qdrant matches payload
        top-level keys, not a nested `metadata.*` path). The full record shape —
        `id`/`collection`/`scope`/`payload`/`metadata` — is preserved so search
        reconstructs exactly what the API returned on insert.
        """
        return {**(record.get("metadata") or {}), **record}

    # --- writes ------------------------------------------------------------
    def insert(self, user: User, active_department: str, collection: str,
               payload: dict, scope: str, record_id: str | None = None) -> dict:
        dep = normalize_department(active_department)
        validate_collection(collection)
        self.build_filter(user, dep)  # membership gate (raises AccessDenied)
        meta = self._scope_metadata(user, dep, scope)
        self._client.ensure_collection(collection, self._config.vector_size,
                                        self._config.distance)
        record = {"id": record_id or str(uuid4()), "collection": collection,
                  "scope": scope, "payload": dict(payload or {}), "metadata": meta}
        self._client.upsert(collection, record["id"], self._point_payload(record))
        return record

    # --- reads -------------------------------------------------------------
    def search(self, user: User, active_department: str, collection: str,
               query: str | None = None, limit: int = 10) -> list[dict]:
        dep = normalize_department(active_department)
        validate_collection(collection)
        filt = self.build_filter(user, dep)  # membership gate (raises AccessDenied)
        rows = self._client.search(collection, filt, query, max(0, limit))
        return [{"id": r.get("id"), "collection": r.get("collection", collection),
                 "scope": r.get("scope"), "payload": r.get("payload", {}),
                 "metadata": r.get("metadata", {})} for r in rows]

    # --- introspection -----------------------------------------------------
    def status(self) -> dict:
        try:
            healthy = bool(self._client.ping())
        except Exception:  # noqa: BLE001 - status probe never raises
            healthy = False
        return {"backend": self.backend, "mode": self.backend,
                "configured": self._config.configured,
                "url": _redact_url(self._config.url),
                "vector_size": self._config.vector_size,
                "distance": self._config.distance,
                "api_key_set": bool(self._config.api_key),
                "healthy": healthy}


def build_vector_backend(mode: str | None = None, *, client=None):
    """Strict factory: build the requested backend or raise for an invalid mode."""
    mode = (mode or vector_backend_mode()).strip().lower()
    if mode == "memory":
        return VectorScopeAdapter()
    if mode == "qdrant":
        config = QdrantConfig()
        return QdrantVectorBackend(client or build_real_qdrant_client(config),
                                   config=config)
    raise VectorScopeError(
        f"invalid VECTOR_BACKEND {mode!r}; expected one of {VALID_VECTOR_BACKENDS}")


def resolve_vector_adapter():
    """Select the backend from env, falling back to memory (with a warning).

    The API is never allowed to fail to start because of a backend env: an
    unknown `VECTOR_BACKEND`, a missing `qdrant_client`, or an unconfigured
    Qdrant all degrade safely to the in-memory scaffold.
    """
    mode = vector_backend_mode()
    if mode not in VALID_VECTOR_BACKENDS:
        warnings.warn(f"invalid VECTOR_BACKEND {mode!r}; falling back to memory",
                      RuntimeWarning, stacklevel=2)
        log.warning("invalid VECTOR_BACKEND %r; falling back to memory", mode)
        return VectorScopeAdapter()
    if mode == "qdrant" and not QdrantConfig().configured:
        warnings.warn("VECTOR_BACKEND=qdrant but QDRANT_URL is unset; "
                      "falling back to memory", RuntimeWarning, stacklevel=2)
        log.warning("VECTOR_BACKEND=qdrant but QDRANT_URL unset; falling back to memory")
        return VectorScopeAdapter()
    try:
        return build_vector_backend(mode)
    except Exception as e:  # noqa: BLE001 - never fail startup on a backend issue
        warnings.warn(f"could not build {mode!r} vector backend ({e}); "
                      "falling back to memory", RuntimeWarning, stacklevel=2)
        log.warning("could not build %r vector backend (%s); falling back to memory",
                    mode, e)
        return VectorScopeAdapter()


# Shared adapter instance used by the API endpoints. Default env -> memory backend,
# so the Phase 05 behavior/contract is unchanged unless VECTOR_BACKEND=qdrant.
vector_adapter = resolve_vector_adapter()
