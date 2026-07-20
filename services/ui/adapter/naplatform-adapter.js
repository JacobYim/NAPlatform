/*
 * NAPlatform core-webui auth/session adapter (Phase 11).
 *
 * A dependency-free ES module that connects a browser UI (core-webui, or any
 * frontend) to the NAPlatform API auth/session surface: signup, login, session
 * bootstrap, logout, the department selector, and the approval-waiting UX.
 *
 * Design:
 * - The endpoint paths mirror services/ui/adapter/contract.json exactly; the
 *   Python contract test asserts this module and the API never drift.
 * - The session token lives only in memory on the client (or a caller-provided
 *   store). NOTHING in this file is a hardcoded credential or token — the module
 *   handles tokens, it never embeds one.
 * - The API is the security boundary. This adapter mirrors the RBAC outcomes
 *   (pending -> approval UX, non-member department -> denied) but never makes an
 *   access decision itself.
 */

export const ENDPOINTS = {
  signup: "/auth/signup",
  login: "/auth/login",
  logout: "/auth/logout",
  departmentOptions: "/auth/departments/options",
  me: "/auth/me",
  session: "/core-webui/session",
  sessionStatus: "/core-webui/session/status",
  selectDepartment: "/core-webui/session/select-department",
  // chat path is per-department; build it from a session department_route.
  chatTemplate: "/agents/{department}/chat",
};

export class NAPlatformApiError extends Error {
  constructor(status, message, body) {
    super(message || `API error ${status}`);
    this.name = "NAPlatformApiError";
    this.status = status;
    this.body = body || null;
  }
}

export class NAPlatformAdapter {
  /**
   * @param {object} [opts]
   * @param {string} [opts.baseUrl]  API base URL (default: same-origin "").
   * @param {function} [opts.fetch]  fetch implementation (default: global fetch).
   */
  constructor(opts = {}) {
    this.baseUrl = (opts.baseUrl || "").replace(/\/+$/, "");
    this._fetch = opts.fetch || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : null);
    this._token = null; // in-memory only; never persisted by this module
  }

  // --- token handling (in memory) ----------------------------------------
  setToken(token) { this._token = token || null; }
  getToken() { return this._token; }
  clearToken() { this._token = null; }
  isAuthenticated() { return Boolean(this._token); }

  // --- low-level request --------------------------------------------------
  async _request(method, path, { body, auth = false } = {}) {
    if (!this._fetch) throw new Error("no fetch implementation available");
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth) {
      if (!this._token) throw new NAPlatformApiError(401, "not authenticated");
      headers["Authorization"] = `Bearer ${this._token}`;
    }
    const resp = await this._fetch(this.baseUrl + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await resp.json(); } catch (_e) { data = null; }
    if (!resp.ok) {
      const detail = (data && (data.detail || data.message)) || resp.statusText;
      throw new NAPlatformApiError(resp.status, detail, data);
    }
    return data;
  }

  // --- auth ---------------------------------------------------------------
  async departmentOptions() {
    return this._request("GET", ENDPOINTS.departmentOptions);
  }

  async signup({ email, username, password, department }) {
    return this._request("POST", ENDPOINTS.signup, {
      body: { email, username, password, department },
    });
  }

  /**
   * Log in and store the session token in memory. On a 403 (pending/disabled),
   * the error carries status 403 so the caller can render the approval UX.
   */
  async login({ email, password }) {
    const data = await this._request("POST", ENDPOINTS.login, {
      body: { email, password },
    });
    this.setToken(data && data.token);
    return data;
  }

  /** Invalidate the server session and clear the local token. Idempotent. */
  async logout() {
    try {
      if (this._token) return await this._request("POST", ENDPOINTS.logout, { auth: true });
      return { message: "logged out", invalidated: false };
    } finally {
      this.clearToken();
    }
  }

  // --- session ------------------------------------------------------------
  /** Full bootstrap for an active session (403 for pending/disabled). */
  async session() { return this._request("GET", ENDPOINTS.session, { auth: true }); }

  /** Alias of session() under /auth/me. */
  async me() { return this._request("GET", ENDPOINTS.me, { auth: true }); }

  /**
   * Lightweight status for any valid session — use this to decide whether to show
   * the workspace or the approval-waiting screen. Throws 401 when logged out.
   */
  async sessionStatus() { return this._request("GET", ENDPOINTS.sessionStatus, { auth: true }); }

  /** Validate and select a department; returns { department, is_member, route }. */
  async selectDepartment(department) {
    return this._request("POST", ENDPOINTS.selectDepartment, {
      auth: true, body: { department },
    });
  }

  // --- chat routing -------------------------------------------------------
  /** Build the chat route for a department (mirrors department_route.chat_route). */
  chatRoute(department) {
    return ENDPOINTS.chatTemplate.replace("{department}", encodeURIComponent(department));
  }

  /** Route a chat message to the department agent. */
  async chat(department, message, sessionId) {
    return this._request("POST", this.chatRoute(department), {
      auth: true, body: { message, session_id: sessionId },
    });
  }

  // --- UI helpers ---------------------------------------------------------
  /**
   * Turn a session-status (or bootstrap) payload into a UI routing decision:
   *   { screen: "workspace"|"approval-waiting"|"login", ... }
   * No secrets; pure view-model derivation.
   */
  static uxForStatus(status) {
    if (!status) return { screen: "login", canAccess: false };
    const approval = status.approval || {};
    if (status.can_access === false || approval.can_access === false) {
      return {
        screen: "approval-waiting",
        canAccess: false,
        state: approval.state || status.status,
        title: approval.title || "Approval required",
        message: approval.message || "Your account is awaiting administrator approval.",
      };
    }
    return { screen: "workspace", canAccess: true, state: "active" };
  }
}

export default NAPlatformAdapter;
