# core-webui first-run preseed + login gate (Phase 14 / 15)

The runtime UI is the external `github.com/JacobYim/core-webui` (white-label
Hermes WebUI). On a fresh volume it shows an **initial setup / onboarding
screen** at http://localhost:3000. Phase 14 suppresses that by **preconfiguring
all required first-run settings from this repo** and applying them automatically
when Docker brings the UI up — so the very first load **skips the setup screen**.

**Phase 15 — login required.** Skipping the setup screen must **not** drop the
user straight into chat. After the setup skip the UI **requires login**: `/`
redirects to `/login`. This is repo/compose-controlled — no code change and **no
secret in any preseed file**:

- `webui-settings.json` declares `login_required: true`, `require_auth: true`,
  `start_page: "login"` (and `defaults.landing: "login"`), so a core-webui version
  that reads its gate from settings shows the login page first.
- The `ui` service in `docker-compose.yml` also sets
  `HERMES_WEBUI_LOGIN_REQUIRED=true` / `HERMES_WEBUI_REQUIRE_AUTH=true` /
  `HERMES_WEBUI_START_PAGE=login` as a belt-and-suspenders signal for versions that
  read the gate from the environment.
- `ui-preseed` carries `CORE_WEBUI_LOGIN_REQUIRED=true`; `preseed.sh` uses it to
  flip the `login_required`/`require_auth` booleans in the copied settings at boot
  (still **never** writing a password).

## Login and registration (email/password + admin approval)

The UI login page uses the NAPlatform account flow, not a password-only WebUI
gate:

1. **Register** tab posts to the API `POST /auth/signup`, creating a `pending`
   user.
2. Pending users cannot enter chat; login returns an approval-waiting message
   until an admin approves the account.
3. An admin approves/revokes users through the API (`PATCH /admin/users/{id}`).
4. Once active, the user's **email + password** login posts to `POST /auth/login`,
   the WebUI mints its local cookie, and the browser redirects to chat/workspace.

The `ui` service sets:

```yaml
HERMES_WEBUI_AUTH_MODE: naplatform
```

It intentionally does **not** set `HERMES_WEBUI_PASSWORD`, so current
NAPlatform-auth WebUI builds cannot fall back to the old password-only gate.

Seeded local admin for approvals: `admin@example.com` / `ChangeMe123!`.

## Admin Hub

When `POST /auth/login` reports `is_admin: true`, the WebUI redirects to
`/admin` instead of opening chat directly. The admin hub provides:

- user list with status, departments, and role;
- pending/active/admin summary metrics;
- editable email, username, status, departments, password, and admin role;
- effective HDFS workspace roots for the selected user.

Workspace roots are derived from NAPlatform RBAC: the personal root is
`/naplatform/users/<username>` and shared roots are
`/naplatform/departments/<DEPARTMENT>` for the user's departments.

## How it works (non-invasive preseed)

We do **not** vendor or edit the external core-webui repo. Instead:

1. This directory (`config/core-webui/`) holds the repo-controlled config:
   - **`branding.yaml`** — copied to `$HERMES_HOME/branding.yaml` (the branding
     path core-webui reads). Sets the HMGMA name/logo.
   - **`webui-settings.json`** — copied to the core-webui state dir
     (`$HERMES_WEBUI_STATE_DIR`, default `/home/hermeswebui/.hermes/webui/settings.json`).
     Declares **first-run disabled** semantics (`first_run: false`,
     `setup_completed: true`, `onboarding_completed: true`,
     `initial_setup_completed: true`), the API base URL, the auth adapter config,
     and the default endpoint values.
   - **`preseed.sh`** — a dependency-free POSIX/busybox init script.
2. `docker-compose.yml` runs a one-shot **`ui-preseed`** service (busybox) that
   shares the `ui-hermes-home` volume with `ui`, runs `preseed.sh` to write the
   files + setup-completed markers into the shared volume, then exits. The `ui`
   service `depends_on` it with `condition: service_completed_successfully`, so
   the config is in place **before core-webui serves**.
3. Belt-and-suspenders: the `ui` service also sets
   `HERMES_WEBUI_SETUP_COMPLETED` / `HERMES_WEBUI_DISABLE_FIRST_RUN` /
   `HERMES_WEBUI_SKIP_ONBOARDING` env flags for core-webui versions that read the
   flag from the environment.

**No secrets** live in any file here. Passwords/tokens/API keys are never
written; the session token is minted by the API at login and kept in browser
memory by `services/ui/adapter/naplatform-adapter.js`.

> core-webui's exact settings schema is owned by the external repo and may vary
> between versions. `webui-settings.json` therefore carries the first-run-disabled
> flag under several plausible key names, and `preseed.sh` writes both a
> `state.json` and sentinel marker files. If a core-webui version uses a
> different key, edit `webui-settings.json` — it is pure config, no code change.

## Edit the config

### PowerShell (Windows)

```powershell
# Edit branding / settings in your editor
notepad .\config\core-webui\branding.yaml
notepad .\config\core-webui\webui-settings.json

# Override a value for one run without editing files (env wins over the config):
$env:BRAND_NAME = "HMGMA"
$env:BRAND_LOGO = "/apptoo/branding/logo.jpg"
$env:NAPLATFORM_API_BASE_URL = "http://api:8080"
```

### Bash / Git Bash

```bash
# Edit branding / settings in your editor
${EDITOR:-nano} config/core-webui/branding.yaml
${EDITOR:-nano} config/core-webui/webui-settings.json

# Override a value for one run without editing files (env wins over the config):
export BRAND_NAME=HMGMA
export BRAND_LOGO=/apptoo/branding/logo.jpg
export NAPLATFORM_API_BASE_URL=http://api:8080
```

## Start Docker with the preseeded UI

The preseed is part of the **default** `docker-compose.yml`, so a plain bring-up
applies it automatically — **no first-run setup screen** at http://localhost:3000,
which then **redirects to `/login`** (Phase 15: login required).

### PowerShell (Windows)

```powershell
# Bring up the UI (and its API dependency + one-shot preseed) from the repo root
docker compose up -d --build ui

# Watch the preseed run once, then core-webui start
docker compose logs ui-preseed
docker compose logs -f ui

# Open the UI — no setup wizard; it shows the login page (login required)
Start-Process "http://localhost:3000"
```

### Bash / Git Bash

```bash
# Bring up the UI (and its API dependency + one-shot preseed) from the repo root
docker compose up -d --build ui

# Watch the preseed run once, then core-webui start
docker compose logs ui-preseed
docker compose logs -f ui

# Open http://localhost:3000 — no setup wizard; it shows the login page (login required)
```

To re-seed from a clean state (e.g. after changing the config), reset the volume:

```bash
docker compose down -v         # removes ui-hermes-home so the preseed re-applies
docker compose up -d --build ui
```

## Verify offline (no Docker)

```bash
python scripts/_phase14_check.py     # parses compose + config, asserts the wiring
pytest -q services/api/tests/test_phase14_ui_preseed.py
```

### UI port conflict: host port 3000 already in use

The UI container listens on port `8787` internally and publishes to host port `3000` by default. If Docker reports `address already in use` for `0.0.0.0:3000`, keep the same stack and choose another host port:

```bash
UI_HOST_PORT=3001 CORE_WEBUI_CONTEXT=../core-webui docker compose up -d --build ui
```

Then open:

```text
http://localhost:3001
```

PowerShell equivalent:

```powershell
$env:UI_HOST_PORT = "3001"
$env:CORE_WEBUI_CONTEXT = "..\core-webui"
docker compose up -d --build ui
```

To discover what owns port 3000 on Windows:

```powershell
netstat -ano | findstr :3000
```

The first-run preseed still runs exactly the same; only the host URL changes.

