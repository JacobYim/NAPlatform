"""Minimal WebHDFS-backed file browser for NAPlatform workspaces.

This module exposes HDFS directory/file access behind the same RBAC roots used
for agent context. It uses WebHDFS on the NameNode HTTP port so the API
container does not need a Hadoop CLI binary or Docker socket.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .models import User
from .rbac import assert_hdfs_path_allowed, normalize_hdfs_path

WEBHDFS_URL = os.environ.get("HDFS_WEBHDFS_URL", "http://hdfs-namenode:9870/webhdfs/v1").rstrip("/")
WEBHDFS_USER = os.environ.get("HDFS_WEBHDFS_USER", "root")
WEBHDFS_TIMEOUT_SECONDS = float(os.environ.get("HDFS_WEBHDFS_TIMEOUT_SECONDS", "10"))
MAX_READ_BYTES = int(os.environ.get("HDFS_WEBHDFS_MAX_READ_BYTES", str(2 * 1024 * 1024)))


@dataclass
class HdfsBrowserError(Exception):
    message: str
    status_code: int = 502

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _join(root: str, rel: str = ".") -> str:
    root = normalize_hdfs_path(root)
    rel = (rel or ".").strip()
    if rel in ("", ".", "/"):
        return root
    if rel.startswith("/"):
        path = rel
    else:
        path = posixpath.normpath(posixpath.join(root, rel))
    return normalize_hdfs_path(path)


def _url(path: str, op: str, **params: str) -> str:
    path = normalize_hdfs_path(path)
    query = {"op": op, "user.name": WEBHDFS_USER}
    query.update({k: v for k, v in params.items() if v is not None})
    return f"{WEBHDFS_URL}{urllib.parse.quote(path)}?{urllib.parse.urlencode(query)}"


def _request_json(path: str, op: str, *, method: str = "GET", **params: str) -> dict:
    req = urllib.request.Request(_url(path, op, **params), headers={"Accept": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=WEBHDFS_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HdfsBrowserError(f"WebHDFS {op} failed for {path}: HTTP {e.code} {detail}", e.code) from e
    except Exception as e:  # noqa: BLE001
        raise HdfsBrowserError(f"WebHDFS {op} failed for {path}: {e}") from e


def ensure_dir(user: User, root: str) -> None:
    path = normalize_hdfs_path(root)
    assert_hdfs_path_allowed(user, path)
    _request_json(path, "MKDIRS", method="PUT")


def list_dir(user: User, root: str, rel: str = ".") -> dict:
    path = _join(root, rel)
    assert_hdfs_path_allowed(user, path)
    try:
        data = _request_json(path, "LISTSTATUS")
    except HdfsBrowserError as e:
        if e.status_code != 404 or rel not in ("", ".", "/"):
            raise
        ensure_dir(user, root)
        data = _request_json(path, "LISTSTATUS")
    statuses = ((data.get("FileStatuses") or {}).get("FileStatus") or [])
    entries = []
    base_rel = "" if rel in ("", ".", "/") else rel.strip("/")
    for item in statuses[:200]:
        name = item.get("pathSuffix") or ""
        child_rel = name if not base_rel else f"{base_rel}/{name}"
        is_dir = item.get("type") == "DIRECTORY"
        entries.append({
            "name": name,
            "path": child_rel,
            "type": "dir" if is_dir else "file",
            "size": None if is_dir else item.get("length"),
            "mtime_ns": int(item.get("modificationTime", 0)) * 1_000_000 if item.get("modificationTime") else None,
            "hdfs_path": posixpath.join(path, name),
        })
    signature = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"entries": entries, "signature": signature, "path": rel or ".", "hdfs_root": normalize_hdfs_path(root)}


def read_file(user: User, root: str, rel: str) -> dict:
    path = _join(root, rel)
    assert_hdfs_path_allowed(user, path)
    status = _request_json(path, "GETFILESTATUS").get("FileStatus") or {}
    if status.get("type") != "FILE":
        raise HdfsBrowserError(f"Not a file: {rel}", 404)
    size = int(status.get("length") or 0)
    if size > MAX_READ_BYTES:
        raise HdfsBrowserError(f"File too large ({size} bytes, max {MAX_READ_BYTES})", 413)
    try:
        with urllib.request.urlopen(_url(path, "OPEN"), timeout=WEBHDFS_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_READ_BYTES + 1)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HdfsBrowserError(f"WebHDFS OPEN failed for {path}: HTTP {e.code} {detail}", e.code) from e
    content = raw.decode("utf-8", errors="replace")
    return {"path": rel, "content": content, "size": len(raw), "lines": content.count("\n") + 1, "hdfs_path": path}


def write_file(user: User, root: str, rel: str, content: str) -> dict:
    path = _join(root, rel)
    assert_hdfs_path_allowed(user, path)
    parent = posixpath.dirname(path)
    assert_hdfs_path_allowed(user, parent)
    _request_json(parent, "MKDIRS", method="PUT")
    data = (content or "").encode("utf-8")
    first = _url(path, "CREATE", overwrite="true")
    req = urllib.request.Request(first, headers={"Content-Type": "application/octet-stream"}, method="PUT")
    try:
        urllib.request.urlopen(req, timeout=WEBHDFS_TIMEOUT_SECONDS).read()
        location = first
    except urllib.error.HTTPError as e:
        if e.code != 307:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise HdfsBrowserError(f"WebHDFS CREATE failed for {path}: HTTP {e.code} {detail}", e.code) from e
        location = e.headers.get("Location") or first
    req2 = urllib.request.Request(location, data=data, headers={"Content-Type": "application/octet-stream"}, method="PUT")
    try:
        with urllib.request.urlopen(req2, timeout=WEBHDFS_TIMEOUT_SECONDS) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HdfsBrowserError(f"WebHDFS CREATE write failed for {path}: HTTP {e.code} {detail}", e.code) from e
    return {"ok": True, "path": rel, "size": len(data), "hdfs_path": path}


def make_dir(user: User, root: str, rel: str) -> dict:
    path = _join(root, rel)
    assert_hdfs_path_allowed(user, path)
    _request_json(path, "MKDIRS", method="PUT")
    return {"ok": True, "path": rel, "hdfs_path": path}


def delete_path(user: User, root: str, rel: str, recursive: bool = False) -> dict:
    path = _join(root, rel)
    assert_hdfs_path_allowed(user, path)
    _request_json(path, "DELETE", method="DELETE", recursive="true" if recursive else "false")
    return {"ok": True, "path": rel, "hdfs_path": path}


def rename_path(user: User, root: str, rel: str, new_name: str) -> dict:
    src = _join(root, rel)
    safe_name = str(new_name or "").strip()
    if not safe_name or "/" in safe_name or ".." in safe_name:
        raise ValueError("Invalid file name")
    dst = posixpath.join(posixpath.dirname(src), safe_name)
    assert_hdfs_path_allowed(user, src)
    assert_hdfs_path_allowed(user, dst)
    req = urllib.request.Request(_url(src, "RENAME", destination=dst), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=WEBHDFS_TIMEOUT_SECONDS) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HdfsBrowserError(f"WebHDFS RENAME failed for {src}: HTTP {e.code} {detail}", e.code) from e
    new_rel = posixpath.join(posixpath.dirname(rel.strip("/")), safe_name).strip("/")
    return {"ok": True, "old_path": rel, "new_path": new_rel, "hdfs_path": dst}
