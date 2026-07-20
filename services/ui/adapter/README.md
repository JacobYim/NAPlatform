# NAPlatform core-webui auth/session adapter (Phase 11)

The real post-login runtime UI is external — [`github.com/JacobYim/core-webui`](https://github.com/JacobYim/core-webui)
— so it is **not** vendored into this repo. This directory is the repo-controlled
**adapter** that connects that UI's login/signup/session/department-selector
flows to the NAPlatform API. It is a small, dependency-free ES module plus a
machine-readable contract, so the integration can be tested here (Python contract
tests, no live UI or browser required) even though the UI itself lives elsewhere.

## Files

| File | Purpose |
|------|---------|
| `naplatform-adapter.js` | Dependency-free ES module (`NAPlatformAdapter`) wrapping the API auth/session endpoints. |
| `contract.json` | Single source of truth for endpoint shapes. The JS adapter and the Python test both read it, so frontend and API cannot drift. |
| `index.html` | Static demo page driving the whole flow against a running API. No credentials embedded. |
| `package.json` | Package metadata (no runtime deps). |

## API surface (Phase 11)

All paths are under `NAPLATFORM_API_BASE_URL`. Auth is `Authorization: Bearer <session-token>`.

| Method | Path | Auth | Use |
|--------|------|------|-----|
| `POST` | `/auth/signup` | none | Create a pending user (admin approval required). |
| `POST` | `/auth/login` | none | Get a session token. Pending/disabled → `403`. |
| `POST` | `/auth/logout` | session | Invalidate the Redis/memory session (idempotent). |
| `GET`  | `/auth/departments/options` | none | Option list for the signup / selector dropdown. |
| `GET`  | `/auth/me` | active | Session bootstrap (alias of `/core-webui/session`). |
| `GET`  | `/core-webui/session` | active | Full bootstrap; pending/disabled → `403`. |
| `GET`  | `/core-webui/session/status` | session | Status for any valid session → drives the approval-waiting UX. `401` when logged out/expired. |
| `POST` | `/core-webui/session/select-department` | active | Validate membership, return chat/context/resource routes. Non-member → `403`, unknown → `400`. |
| `POST` | `/agents/{department}/chat` | active | Route chat to the department agent. |

## Session / approval-waiting flow

1. **Signup** → user is `pending`. Login is refused (`403`) until an admin approves.
2. **Login** (active) → token stored in memory by the adapter.
3. **Session bootstrap** (`/auth/me` or `/core-webui/session`) → `department_routes[]`
   gives each department's `chat_route` (`/agents/{department}/chat`), so the UI
   routes chat without hardcoding paths.
4. **Approval-waiting UX** → if an account is revoked to `pending`/`disabled` after
   login, `/core-webui/session` returns `403`; the UI instead calls
   `/core-webui/session/status`, which returns `can_access:false` plus an `approval`
   contract (`state`, `title`, `message`) to render the waiting screen.
   `NAPlatformAdapter.uxForStatus(status)` maps this to
   `{ screen: "workspace" | "approval-waiting" | "login" }`.
5. **Inactive / expired session** → `/core-webui/session/status` returns `401`; the
   UI treats it as logged out.
6. **Department selection** → `selectDepartment(dep)` denies a non-member (`403`).

## Usage

```js
import { NAPlatformAdapter } from "./naplatform-adapter.js";

const api = new NAPlatformAdapter({ baseUrl: "http://localhost:8080" });

const { departments } = await api.departmentOptions();     // populate dropdown
await api.login({ email, password });                       // token kept in memory
const status = await api.sessionStatus();
if (NAPlatformAdapter.uxForStatus(status).screen === "workspace") {
  const boot = await api.me();
  const dep = boot.default_department;
  await api.selectDepartment(dep);
  const answer = await api.chat(dep, "quality trace status?");
}
await api.logout();
```

## Security notes

- **No secrets in this package.** The adapter *handles* a session token but never
  embeds one; there are no passwords or tokens in any file here. The Python test
  `services/api/tests/test_webui_adapter_contract.py` enforces this (it fails if a
  known credential literal or a hardcoded `Bearer <token>` appears in these files).
- **The token lives only in memory** (`this._token`); persistence is the caller's
  choice, not this module's.
- **The API is the security boundary.** RBAC, session TTL, and department
  membership are enforced server-side. The adapter mirrors the outcomes for UX but
  makes no access decision itself.

## Testing (no live UI)

```bash
python -m pip install -r services/api/requirements-dev.txt
pytest -q services/api/tests/test_webui_session.py services/api/tests/test_webui_adapter_contract.py
```

These prove the endpoints exist with the documented shapes, that `contract.json`
and `naplatform-adapter.js` stay in sync with the API, and that no token leaks
into the adapter files.
