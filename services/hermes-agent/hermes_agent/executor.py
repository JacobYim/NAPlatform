"""Optional real Hermes CLI execution (disabled by default).

Kept behind ``HERMES_AGENT_EXECUTION_ENABLED``; the default path never spawns a
subprocess. Command construction is a fixed argv list (``shell=False``) so the
user ``message`` and scope values are passed as data, never interpolated into a
shell. Tests exercise this path with a ``FakeHermesRunner`` — no real CLI.
"""
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def build_hermes_argv(hermes_bin: str, profile_dir: str, department: str,
                      message: str, context_file: str) -> list[str]:
    """Build a safe argv list for a Hermes chat turn — no shell involved.

    Every dynamic value is its own argv element, so a ``message`` containing
    shell metacharacters is inert. The API-issued scope travels out-of-band in a
    JSON ``context_file`` rather than on the command line.

    NOTE: the exact Hermes CLI flags are a scaffold — ``--query``/
    ``--context-file`` are placeholders pending the real CLI contract. What is
    load-bearing (and tested) is the *shape*: fixed argv, ``shell=False``, the
    message and scope passed as data.
    """
    return [
        hermes_bin,
        "--profile-dir", profile_dir,
        "--department", department,
        "chat",
        "--query", message,
        "--context-file", context_file,
    ]


def build_context_payload(*, department: str, user: dict, hdfs_roots: list,
                          allowed_tools: list, allowed_mcp_servers: list,
                          qdrant_filter: dict, neo4j_filter: dict,
                          workspace_root: str | None) -> dict:
    """Assemble the JSON scope/context handed to the Hermes CLI out-of-band."""
    return {
        "department": department,
        "user": user,
        "hdfs_roots": hdfs_roots,
        "allowed_tools": allowed_tools,
        "allowed_mcp_servers": allowed_mcp_servers,
        "qdrant_filter": qdrant_filter,
        "neo4j_filter": neo4j_filter,
        "workspace_root": workspace_root,
    }


@contextmanager
def scope_context_file(payload: dict):
    """Yield the path of a temp JSON file holding ``payload``; remove it after.

    The file exists only for the duration of the ``with`` block — i.e. across
    the runner call — and is unlinked in ``finally`` so nothing lingers on disk.
    """
    fd, path = tempfile.mkstemp(prefix="hermes-ctx-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


class HermesRunner:
    """Turn an argv list into a :class:`RunResult`."""

    def run(self, argv: list[str], timeout: float) -> RunResult:  # pragma: no cover - interface
        raise NotImplementedError


class SubprocessHermesRunner(HermesRunner):
    """Real runner: ``subprocess.run`` with a timeout, argv list, no shell."""

    def run(self, argv: list[str], timeout: float) -> RunResult:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, shell=False, check=False)
        except subprocess.TimeoutExpired as e:
            return RunResult(None, e.stdout or "", e.stderr or "", timed_out=True)
        except FileNotFoundError as e:
            return RunResult(127, "", f"hermes binary not found: {e}")
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
