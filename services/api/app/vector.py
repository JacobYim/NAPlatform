"""Qdrant vector-scope adapter (Phase 05).

A single shared Qdrant instance is separated *by metadata*, not by physical
collection-per-tenant: every point carries scope metadata (`owner_user_id`,
`allowed_users`, `department`, `allowed_departments`) and every query is
constrained by a Qdrant filter derived from the caller's RBAC scope. This module
reuses `rbac.qdrant_filter` semantics so the same access rules the agent context
exposes are the ones enforced on insert/search.

Nothing here talks to a live Qdrant. `VectorScopeAdapter` builds
Qdrant-compatible filter descriptors and keeps a deterministic in-memory store so
the scope logic can be tested without the service running. Swapping the in-memory
store for a real `qdrant_client` is a later phase; the filter descriptors this
adapter emits are already Qdrant `Filter` payloads.
"""
import re
from uuid import uuid4

from .models import User
from .rbac import AccessDenied, normalize_department, qdrant_filter

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

    def __init__(self, store: dict[str, list[dict]] | None = None):
        # collection name -> list of point dicts (insertion-ordered for determinism)
        self._collections: dict[str, list[dict]] = store if store is not None else {}

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


# Shared adapter instance used by the API endpoints.
vector_adapter = VectorScopeAdapter()
