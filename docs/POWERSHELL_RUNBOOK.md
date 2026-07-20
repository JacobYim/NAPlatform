# NAPlatform — Windows PowerShell Runbook (Phase 13)

This runbook drives NAPlatform end-to-end on **Windows using native PowerShell**
(Windows PowerShell 5.1 or PowerShell 7+). It covers clone → checkout `dev` →
a `make`-free release-check → running the default / routing / **Docker Model Runner
(`gemma4:31b`)** stacks → the adapter UI → smoke tests → the user-approval flow →
cleanup.

> **This is PowerShell, not Bash.** Every command below uses PowerShell syntax
> (`$env:NAME = "value"`, `;` to sequence, `Copy-Item`, `Invoke-RestMethod`,
> `$(...)` for substitution). **Git Bash / WSL differ** — there you would instead
> use `NAME=value cmd`, `&&` to sequence, `cp`, `curl`, `export`, and forward-slash
> idioms. A "Git Bash differs" note is called out where the difference matters. If
> you prefer Git Bash, use the `bash` snippets in `README.md` / `docs/CONTAINER_GUIDE.md`.

> **`main` is never touched by this runbook.** There is **no** `release-dev-to-main`
> step here — promotion to `main` is a separate, explicitly-approved release action
> (see `docs/FINAL_RELEASE_CHECKLIST.md`). This runbook operates on the phase branch
> and `dev` only, and leaves `main` stable.

---

## 0. Prerequisites

- **Git**, **Python 3.11+**, and **Docker Desktop 4.40+** (for the Compose stacks).
- For a *live* `gemma4:31b` reply: **Docker Model Runner** enabled in Docker Desktop
  and the model pulled — actual availability depends on your local Docker Desktop /
  model runner support:

  ```powershell
  # Enable the Docker Model Runner feature (Docker Desktop: Settings > Beta features),
  # then pull the shared model used by every department agent:
  docker model pull gemma4:31b
  docker model ls
  ```

  Without this (or without a Hermes CLI in the agent image) the model-runner stack
  still starts and validates, but agents return an execution error instead of a
  model reply. The **default** stack does not need any of this.

Check your tools:

```powershell
git --version
python --version
docker version --format '{{.Server.Version}}'
docker compose version
```

---

## 1. Clone and checkout the `dev` branch

```powershell
# Choose a workspace directory
Set-Location $HOME\source
git clone https://github.com/<org>/NAPlatform.git
Set-Location .\NAPlatform

# Integration branch (phase branches merge into dev; main stays stable)
git fetch origin
git checkout dev
git pull --ff-only origin dev
git branch --show-current      # -> dev
```

> **Git Bash differs:** use `cd ~/source` instead of `Set-Location $HOME\source`,
> and forward slashes throughout.

To work on this phase branch instead:

```powershell
git checkout phase/13-docker-model-runner-gemma4-powershell
```

---

## 2. Python environment + host-side checks (release-check equivalent, no `make`)

The `Makefile` is the canonical entrypoint, but `make` is usually absent on Windows.
These commands are the **exact equivalents** of `make release-check` — pytest +
compileall + compose config validation + the readiness gate — run directly. **None
of them touch `main`.**

```powershell
# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If activation is blocked by execution policy (per-user, reversible):
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# Install dev dependencies (pytest, httpx, fastapi, ...)
python -m pip install --upgrade pip
python -m pip install -r services\api\requirements-dev.txt

# --- release-check equivalent (host-side; main untouched) ------------------
# 1) full test suite
python -m pytest -q

# 2) byte-compile api + hermes-agent + scripts
python -m compileall services\api\app services\hermes-agent\hermes_agent scripts

# 3) validate the default (dry-run) Compose config
docker compose -f docker-compose.yml config | Out-Null

# 4) validate the enabled-routing Compose config
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml config | Out-Null

# 5) validate the shared Docker Model Runner (gemma4:31b) Compose config
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml config | Out-Null

# 6) redacted production-readiness gate (dev mode is always ready; secrets never printed)
python -c "import sys,json; sys.path.insert(0,'services/api'); from app.config import readiness_report; r=readiness_report(); print(json.dumps(r, indent=2)); sys.exit(0 if r['ready'] else 1)"
```

> **Git Bash differs:** `python -m venv .venv && source .venv/bin/activate`, then
> `pip install -r services/api/requirements-dev.txt`, and `>/dev/null` instead of
> `| Out-Null`. Env-var-prefixed forms like `PRODUCTION_MODE=true make release-check`
> are Bash-only; in PowerShell set `$env:PRODUCTION_MODE = "true"` first (below).

Enforce the strict production checks (optional; still never touches `main`):

```powershell
$env:PRODUCTION_MODE = "true"
python -c "import sys,json; sys.path.insert(0,'services/api'); from app.config import readiness_report; r=readiness_report(); print(json.dumps(r, indent=2)); sys.exit(0 if r['ready'] else 1)"
Remove-Item Env:\PRODUCTION_MODE
```

Run only the Phase 13 tests:

```powershell
python -m pytest -q services\hermes-agent\tests\test_model_runtime.py services\api\tests\test_model_runner.py services\api\tests\test_phase13_docs.py
```

---

## 3. Run the default (dry-run) stack

The default stack is **safe**: routing OFF, agents deterministic, profiles
model-less. No model runner is required.

```powershell
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps

# API health
Invoke-RestMethod http://localhost:8080/health

# Seeded admin login (local only)
$login = Invoke-RestMethod -Method Post http://localhost:8080/auth/login `
  -ContentType 'application/json' `
  -Body (@{ email = 'admin@example.com'; password = 'ChangeMe123!' } | ConvertTo-Json)
$token = $login.token

# Admin agent-routing status (dry-run; secret-free). Note model_runtime.configured=false here.
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod http://localhost:8080/admin/agents/status -Headers $headers | ConvertTo-Json -Depth 6
```

> **Git Bash differs:** use backslash-free paths and `curl -s ... | jq .` instead of
> `Invoke-RestMethod`. PowerShell uses the backtick `` ` `` for line continuation,
> not `\`.

---

## 4. Run the routing stack (API routes to agents; still no model)

```powershell
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml up -d --build

# /admin/agents/status now reports enabled=true (hermes_invoked=true at the API)
Invoke-RestMethod http://localhost:8080/admin/agents/status -Headers $headers | ConvertTo-Json -Depth 6
```

---

## 5. Run the Docker Model Runner stack (shared `gemma4:31b` for all agents)

This applies `docker-compose.model-runner.yml`, which declares the shared LLM envs
(`HERMES_LLM_PROVIDER`, `DOCKER_MODEL_RUNNER_BASE_URL`,
`DOCKER_MODEL_RUNNER_MODEL=gemma4:31b`) on the API **and every** `hermes-*` agent, so
all four departments (ER/IT/EHS/QC) share the **same** model while keeping their own
department persona. Requires a working local Docker Model Runner (Section 0).

```powershell
# Optionally override the endpoint/model (defaults shown):
$env:DOCKER_MODEL_RUNNER_BASE_URL = "http://model-runner.docker.internal/engines/v1"
$env:DOCKER_MODEL_RUNNER_MODEL    = "gemma4:31b"

docker compose -f docker-compose.yml -f docker-compose.model-runner.yml up -d --build

# Confirm the shared model runner is configured (admin-only; secret-free).
# model_runtime.model should be "gemma4:31b" and configured=true.
Invoke-RestMethod http://localhost:8080/admin/agents/status -Headers $headers |
  Select-Object -ExpandProperty model_runtime

# Each agent's own /health reports the SAME model (department persona stays isolated).
# Agents have no host port; exec into a container to read it from inside the network:
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml exec hermes-er `
  python -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8080/health'))['model_runtime'])"
```

> The base URL is redacted (userinfo/query stripped) and the API key is reported only
> as `api_key_present` — no secret leaves the process. The API key is referenced in
> the generated profile by env-var **name** only, never written to disk.

---

## 6. Adapter UI (core-webui auth/session adapter)

The repo-controlled adapter is static (`services\ui\adapter\`). Point it at a running
API and drive login/session/department-selection by hand:

```powershell
# Serve the adapter demo locally against the running API (default http://localhost:8080)
Start-Process "services\ui\adapter\index.html"
# Or serve the folder over HTTP:
python -m http.server 5500 --directory services\ui\adapter
# then browse http://localhost:5500/index.html
```

Building the external `ui` service (JacobYim/core-webui) is **not** required for the
adapter or the smoke tests.

---

## 7. Smoke tests

### 7a. Host-side smoke unit tests (no Docker)

```powershell
python -m pytest -q services\api\tests\test_smoke_routing_e2e.py services\api\tests\test_smoke_resources_e2e.py
```

### 7b. In-cluster smoke (against a running stack)

```powershell
# Dry-run smoke (default stack; expects hermes_invoked=false)
docker compose -f docker-compose.yml -f docker-compose.smoke.yml run --rm `
  -e SMOKE_EXPECT_ROUTING=false smoke

# Enabled-routing smoke (routing override; expects hermes_invoked=true)
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml run --rm `
  -e SMOKE_EXPECT_ROUTING=true smoke

# Model-runner smoke (shared gemma4:31b; needs live Docker Model Runner + a Hermes CLI
# in the agent image). Routing is ON, so hermes_invoked=true is expected; without a
# working runner the agent execution errors and the smoke fails loudly by design.
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml -f docker-compose.smoke.yml run --rm `
  -e SMOKE_EXPECT_ROUTING=true smoke

# Resource smoke (hdfs/vector/graph/audit scope; routing-agnostic)
docker compose -f docker-compose.yml -f docker-compose.smoke.yml run --rm `
  smoke python /scripts/smoke_resources_e2e.py
```

> **Git Bash differs:** the `` ` `` line continuations become `\`, and
> `-e SMOKE_EXPECT_ROUTING=true` can be replaced by an inline `SMOKE_EXPECT_ROUTING=true`
> prefix on the whole `docker compose ... run` line.

---

## 8. User-approval flow (signup → admin approval → scoped chat)

New users sign up as **pending** and must be approved by an admin before they can
access a department. Full flow via the API:

```powershell
$base = "http://localhost:8080"

# 1) A new user signs up (status: pending)
Invoke-RestMethod -Method Post "$base/auth/signup" -ContentType 'application/json' `
  -Body (@{ email='qc.user@example.com'; username='qcuser'; password='Password123!'; department='QC' } | ConvertTo-Json)

# 2) Admin logs in
$admin = Invoke-RestMethod -Method Post "$base/auth/login" -ContentType 'application/json' `
  -Body (@{ email='admin@example.com'; password='ChangeMe123!' } | ConvertTo-Json)
$adminHeaders = @{ Authorization = "Bearer $($admin.token)" }

# 3) Admin sees the pending approval and finds the user id
Invoke-RestMethod "$base/admin/approvals/pending" -Headers $adminHeaders | ConvertTo-Json -Depth 6
$uid = (Invoke-RestMethod "$base/admin/users" -Headers $adminHeaders |
        Where-Object { $_.email -eq 'qc.user@example.com' }).id

# 4) Admin approves -> active with department [QC]
Invoke-RestMethod -Method Patch "$base/admin/users/$uid" -Headers $adminHeaders `
  -ContentType 'application/json' `
  -Body (@{ status='active'; departments=@('QC') } | ConvertTo-Json)

# 5) The user logs in and chats with their department agent
$u = Invoke-RestMethod -Method Post "$base/auth/login" -ContentType 'application/json' `
  -Body (@{ email='qc.user@example.com'; password='Password123!' } | ConvertTo-Json)
$uHeaders = @{ Authorization = "Bearer $($u.token)" }
Invoke-RestMethod -Method Post "$base/agents/QC/chat" -Headers $uHeaders `
  -ContentType 'application/json' -Body (@{ message='quality trace status check' } | ConvertTo-Json) |
  ConvertTo-Json -Depth 6

# 6) Cross-department access is denied (QC user calling IT -> 403)
try {
  Invoke-RestMethod -Method Post "$base/agents/IT/chat" -Headers $uHeaders `
    -ContentType 'application/json' -Body (@{ message='cross-department probe' } | ConvertTo-Json)
} catch {
  "Denied as expected: $($_.Exception.Response.StatusCode.value__)"   # -> 403
}
```

---

## 9. Cleanup

```powershell
# Stop a stack and remove its volumes (repeat with the -f set you brought up)
docker compose -f docker-compose.yml down -v
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml down -v
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml down -v

# Clear the model-runner env overrides from this shell
Remove-Item Env:\DOCKER_MODEL_RUNNER_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:\DOCKER_MODEL_RUNNER_MODEL    -ErrorAction SilentlyContinue

# Deactivate and (optionally) remove the venv
deactivate
Remove-Item -Recurse -Force .venv
```

> **Git Bash differs:** `docker compose ... down -v` is identical, but env cleanup is
> `unset DOCKER_MODEL_RUNNER_MODEL` and venv removal is `rm -rf .venv`.

---

## Reminder — `main` stays stable

This runbook never runs `release-dev-to-main` and never checks out or pushes `main`.
Promotion to `main` happens **only** at the final, explicitly-approved release step
documented in `docs/FINAL_RELEASE_CHECKLIST.md` and `docs/ROADMAP.md`. Phase 13 work
lives on `phase/13-docker-model-runner-gemma4-powershell` and merges into `dev`.
