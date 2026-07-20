"""HDFS workspace provisioning / ACL scaffold (Phase 04).

`HdfsProvisioner` builds *safe*, deterministic `hdfs dfs` command plans that
carve out a personal directory per user and a shared directory per department,
mirroring the RBAC roots exposed by `app.rbac`.

Safety model:
- Usernames and departments are validated before they ever touch a command.
  Path traversal (`..`), slashes, and unknown departments are rejected, and the
  resulting HDFS paths are re-checked to stay under `BASE`.
- Nothing executes unless `HDFS_PROVISIONING_ENABLED=true`. The default is a
  dry run that only returns the planned commands — no subprocess is spawned.
- When enabled, execution shells out via `subprocess.run` with an argv list
  (never a shell string), so there is no shell-injection surface.

`chown`/`chgrp` are placeholders in the scaffold: real ownership in production
is enforced by Kerberos principals / proxy-users, not by these commands.
"""
import os
import re
import subprocess

from .models import (HdfsCommandPlan, HdfsCommandResult, HdfsDirProvision,
                     HdfsHealthReport, HdfsProvisionReport, WorkspaceHdfsResponse)
from .models import User
from .rbac import BASE, normalize_department, normalize_hdfs_path

# First char must be alphanumeric/underscore so a name can never start with a
# dot or dash; 3..64 chars total. This is stricter than the signup pattern and
# deliberately forbids anything that could form a traversal segment.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}$")

DEFAULT_TIMEOUT_SECONDS = 30
HDFS_BIN = os.environ.get("HDFS_BIN", "hdfs")
DEPARTMENT_GROUP_PREFIX = os.environ.get("HDFS_DEPARTMENT_GROUP_PREFIX", "naplatform")
PERSONAL_MODE = "700"
DEPARTMENT_MODE = "770"


class ProvisioningError(ValueError):
    """Raised for invalid usernames/departments or unsafe paths."""


def provisioning_enabled() -> bool:
    return os.environ.get("HDFS_PROVISIONING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def department_group(department: str) -> str:
    return f"{DEPARTMENT_GROUP_PREFIX}-{normalize_department(department).lower()}"


def validate_username(username: str) -> str:
    if not isinstance(username, str) or ".." in username or not USERNAME_RE.match(username):
        raise ProvisioningError(f"invalid username: {username!r}")
    return username


def _safe_path(path: str) -> str:
    """Re-check a built path: absolute, no traversal, and under BASE."""
    try:
        normalized = normalize_hdfs_path(path)
    except Exception as e:  # AccessDenied on '..' or non-absolute
        raise ProvisioningError(f"unsafe path: {path}") from e
    if normalized != path or not normalized.startswith(BASE + "/"):
        raise ProvisioningError(f"unsafe path: {path}")
    return normalized


def _subprocess_runner(argv: list[str], timeout: int):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


class HdfsProvisioner:
    """Builds (and, when enabled, executes) HDFS provisioning command plans."""

    def __init__(self, *, enabled: bool | None = None, hdfs_bin: str = HDFS_BIN,
                 timeout: int = DEFAULT_TIMEOUT_SECONDS, runner=None):
        self.enabled = provisioning_enabled() if enabled is None else enabled
        self.hdfs_bin = hdfs_bin
        self.timeout = timeout
        self._runner = runner or _subprocess_runner

    # --- plan construction -------------------------------------------------
    def _cmd(self, argv: list[str], description: str) -> tuple[list[str], HdfsCommandPlan]:
        return argv, HdfsCommandPlan(command=" ".join(argv), description=description,
                                     argv=argv)

    def _dir_plan(self, *, path: str, kind: str, owner: str, group: str,
                  mode: str, acl_entries: list[str]) -> tuple[HdfsDirProvision, list[list[str]]]:
        path = _safe_path(path)
        specs = [
            self._cmd([self.hdfs_bin, "dfs", "-mkdir", "-p", path],
                      "create directory (idempotent)"),
            self._cmd([self.hdfs_bin, "dfs", "-chown", f"{owner}:{group}", path],
                      "set owner:group (placeholder; Kerberos principal in prod)"),
            self._cmd([self.hdfs_bin, "dfs", "-chgrp", group, path],
                      "set group (placeholder; Kerberos-mapped group in prod)"),
            self._cmd([self.hdfs_bin, "dfs", "-chmod", mode, path],
                      f"restrict permissions to {mode}"),
        ]
        for entry in acl_entries:
            specs.append(self._cmd(
                [self.hdfs_bin, "dfs", "-setfacl", "-m", entry, path],
                f"grant ACL {entry}"))
        provision = HdfsDirProvision(
            path=path, kind=kind, owner=owner, group=group, mode=mode,
            plan=[plan for _argv, plan in specs], results=[])
        return provision, [argv for argv, _plan in specs]

    def personal_target(self, username: str) -> tuple[HdfsDirProvision, list[list[str]]]:
        username = validate_username(username)
        path = f"{BASE}/users/{username}"
        return self._dir_plan(path=path, kind="personal", owner=username,
                              group=username, mode=PERSONAL_MODE,
                              acl_entries=[f"user:{username}:rwx"])

    def department_target(self, username: str, department: str
                          ) -> tuple[HdfsDirProvision, list[list[str]]]:
        username = validate_username(username)
        dep = normalize_department(department)
        group = department_group(dep)
        path = f"{BASE}/departments/{dep}"
        # The department dir grants the group rwx and adds a per-user ACL so the
        # member reaches the shared tree; mode 770 keeps outsiders out.
        return self._dir_plan(path=path, kind="department", owner=group,
                              group=group, mode=DEPARTMENT_MODE,
                              acl_entries=[f"group:{group}:rwx",
                                           f"user:{username}:rwx"])

    def build_targets(self, username: str, departments: list[str]
                      ) -> list[tuple[HdfsDirProvision, list[list[str]]]]:
        seen: set[str] = set()
        targets = [self.personal_target(username)]
        for d in departments:
            dep = normalize_department(d)
            if dep in seen:
                continue
            seen.add(dep)
            targets.append(self.department_target(username, dep))
        return targets

    # --- execution ---------------------------------------------------------
    def _execute(self, argv: list[str], plan: HdfsCommandPlan) -> HdfsCommandResult:
        try:
            code, out, err = self._runner(argv, self.timeout)
        except Exception as e:  # noqa: BLE001 - surface any runner failure as a result
            return HdfsCommandResult(command=plan.command, description=plan.description,
                                     executed=True, returncode=None, stderr=str(e))
        return HdfsCommandResult(command=plan.command, description=plan.description,
                                 executed=True, returncode=code, stdout=out, stderr=err)

    def provision(self, username: str, departments: list[str],
                  user_id: str | None = None) -> HdfsProvisionReport:
        """Build the plan and, only when enabled, execute it.

        Default (disabled) is a dry run: `results` stays empty and no subprocess
        is spawned. When enabled, each command runs and its result is recorded.
        """
        targets = self.build_targets(username, departments)
        dry_run = not self.enabled
        for provision, argvs in targets:
            if self.enabled:
                provision.results = [self._execute(argv, plan)
                                     for argv, plan in zip(argvs, provision.plan)]
        return HdfsProvisionReport(
            user_id=user_id, username=username, enabled=self.enabled,
            dry_run=dry_run, targets=[p for p, _argvs in targets])

    def plan(self, username: str, departments: list[str]) -> list[HdfsDirProvision]:
        """Dry-run only: return the planned dirs without ever executing."""
        return [p for p, _argvs in self.build_targets(username, departments)]

    # --- health / readiness ------------------------------------------------
    def health_plan(self) -> tuple[list[str], HdfsCommandPlan]:
        """Build the readiness command: `hdfs dfs -test -d <BASE>`.

        The argv is fully constant — the only inputs are the trusted `HDFS_BIN`
        and the fixed `BASE` root — so there is no user-controlled/injection
        surface. `-test -d` returns 0 iff the base workspace directory exists,
        which is a good proxy for "namenode reachable and initialized".
        """
        return self._cmd([self.hdfs_bin, "dfs", "-test", "-d", BASE],
                         "check base workspace dir exists (readiness probe)")

    def health(self) -> HdfsHealthReport:
        """Plan (and, only when enabled, execute) the HDFS readiness probe.

        Default (disabled) is a dry run: the command is planned but never spawned,
        `checked=False`, `healthy=None`. When enabled, the probe runs via the
        injected/subprocess runner and `healthy` reflects a 0 exit code.
        """
        argv, plan = self.health_plan()
        if not self.enabled:
            return HdfsHealthReport(enabled=False, dry_run=True, checked=False,
                                    healthy=None, plan=plan)
        result = self._execute(argv, plan)
        healthy = result.returncode == 0
        return HdfsHealthReport(enabled=True, dry_run=False, checked=True,
                                healthy=healthy, plan=plan, result=result)

    # --- User-facing convenience ------------------------------------------
    def provision_user(self, user: User) -> HdfsProvisionReport:
        return self.provision(user.username,
                              [normalize_department(d) for d in user.departments],
                              user_id=user.id)

    def workspace(self, user: User) -> WorkspaceHdfsResponse:
        deps = [normalize_department(d) for d in user.departments]
        plan = self.plan(user.username, deps)
        return WorkspaceHdfsResponse(
            user_id=user.id, username=user.username,
            personal_root=f"{BASE}/users/{user.username}",
            department_roots=[f"{BASE}/departments/{d}" for d in deps],
            enabled=self.enabled,
            dry_run=True,
            provisioning_status="enabled" if self.enabled else "dry_run",
            plan=plan)


def backend_status() -> dict:
    """Secret-free HDFS provisioning status for the admin backends endpoint.

    Reports the provisioning mode and the *planned* (never executed) readiness
    command. Nothing here spawns a subprocess or exposes credentials — HDFS auth
    is Kerberos/proxy-user based and not held by this service.
    """
    prov = HdfsProvisioner()
    _argv, plan = prov.health_plan()
    return {"backend": "hdfs", "mode": "hdfs",
            "provisioning_enabled": prov.enabled,
            "dry_run": not prov.enabled,
            "hdfs_bin": prov.hdfs_bin, "base": BASE,
            "healthy": None,  # dry-run status never executes the probe
            "health_command": plan.command,
            "detail": ("provisioning enabled" if prov.enabled
                       else "dry-run (no subprocess)")}
