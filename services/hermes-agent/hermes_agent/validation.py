"""Defensive validation of the API-issued scope carried in each request.

The API is the security boundary, but the agent re-validates the scope it is
handed so a misconfigured or spoofed caller can never widen it:

- every ``hdfs_root`` must resolve (no traversal) to a path under ``/naplatform``;
- every ``allowed_tools`` / ``allowed_mcp_servers`` entry must be a safe
  identifier (no shell metacharacters, path separators, or whitespace).
"""
import re

BASE = "/naplatform"
# tools like ``hdfs_list``/``incident_report`` and MCP servers like
# ``mcp-er-filesystem`` — lowercase alnum with ``_``/``-``/``.`` separators only.
SAFE_IDENT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class ScopeValidationError(ValueError):
    """A request carried an out-of-policy scope value."""


def normalize_hdfs_path(path: str) -> str:
    """Collapse ``//``/``.`` and reject ``..`` traversal; require absolute."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ScopeValidationError(f"path must be absolute: {path!r}")
    segs: list[str] = []
    for s in path.split("/"):
        if s == "" or s == ".":
            continue
        if s == "..":
            raise ScopeValidationError(f"path traversal not allowed: {path!r}")
        segs.append(s)
    return "/" + "/".join(segs)


def assert_hdfs_roots(roots: object) -> None:
    if not isinstance(roots, list):
        raise ScopeValidationError("hdfs_roots must be a list")
    for r in roots:
        p = normalize_hdfs_path(r)
        if not (p == BASE or p.startswith(BASE + "/")):
            raise ScopeValidationError(f"hdfs root outside {BASE}: {r!r}")


def assert_safe_identifiers(values: object, kind: str) -> None:
    if not isinstance(values, list):
        raise ScopeValidationError(f"{kind} must be a list")
    for v in values:
        if not isinstance(v, str) or not SAFE_IDENT.match(v):
            raise ScopeValidationError(f"unsafe {kind} identifier: {v!r}")


def assert_workspace_root(workspace_root: object) -> None:
    """A ``workspace_root``, when present, obeys the same HDFS path rules.

    It must be an absolute path under ``/naplatform`` with no ``..`` traversal —
    the identical safety contract enforced for every ``hdfs_root``.
    """
    if workspace_root is None:
        return
    if not isinstance(workspace_root, str):
        raise ScopeValidationError("workspace_root must be a string")
    p = normalize_hdfs_path(workspace_root)
    if not (p == BASE or p.startswith(BASE + "/")):
        raise ScopeValidationError(f"workspace_root outside {BASE}: {workspace_root!r}")
