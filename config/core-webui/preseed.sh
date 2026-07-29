#!/bin/sh
# NAPlatform core-webui first-run preseed (Phase 14).
#
# A tiny, dependency-free POSIX/busybox shell script. It runs as a one-shot init
# step (the `ui-preseed` service in docker-compose.yml) that shares the
# `ui-hermes-home` volume with the `ui` (core-webui) service. It seeds the
# core-webui state dir with repo-controlled config BEFORE core-webui starts, so
# the white-label UI at http://localhost:3000 opens straight into the workspace
# with NO initial setup / onboarding wizard.
#
# NON-INVASIVE: this never edits or replaces the external core-webui image or its
# entrypoint. It only writes config files into the shared home/state volume the UI
# reads. If core-webui's real start command is untouched, the setup screen is
# suppressed purely by the presence of the preseeded settings + marker below.
#
# NO SECRETS: this script handles branding / endpoints / first-run + login flags
# only. It NEVER writes a password, token, or API key. The login password is
# supplied to the ui container via HERMES_WEBUI_PASSWORD / CORE_WEBUI_PASSWORD env
# (see docker-compose.yml), never seeded into settings.json here. The session token
# is minted by the API at login and kept in browser memory by the adapter.
#
# Phase 15: after the first-run wizard is skipped the UI must land on a LOGIN page,
# not directly in chat. webui-settings.json carries login_required/require_auth/
# start_page=login (and landing=login); this script can flip login_required at boot
# via CORE_WEBUI_LOGIN_REQUIRED for repo/compose control.
#
# Env overrides (all optional; env wins over the repo config files):
#   HERMES_HOME              default /home/hermeswebui/.hermes
#   HERMES_WEBUI_STATE_DIR   default $HERMES_HOME/webui
#   BRAND_NAME               overrides branding name in both files
#   BRAND_LOGO               overrides branding logo path in both files
#   NAPLATFORM_API_BASE_URL  overrides the API base URL in settings.json
#   CORE_WEBUI_LOGIN_REQUIRED  overrides login_required/require_auth in settings.json (true/false)
#   CORE_WEBUI_PRESEED_SRC   default /preseed/src   (mounted config/core-webui)
#   CORE_WEBUI_PRESEED_ENABLED  default true   (set false to skip seeding entirely)
#   CORE_WEBUI_PRESEED_FORCE    default true   (false = don't overwrite existing settings.json)
set -eu

HERMES_HOME="${HERMES_HOME:-/home/hermeswebui/.hermes}"
STATE_DIR="${HERMES_WEBUI_STATE_DIR:-$HERMES_HOME/webui}"
SRC="${CORE_WEBUI_PRESEED_SRC:-/preseed/src}"
ENABLED="${CORE_WEBUI_PRESEED_ENABLED:-true}"
FORCE="${CORE_WEBUI_PRESEED_FORCE:-true}"

if [ "$ENABLED" != "true" ]; then
  echo "[core-webui-preseed] disabled (CORE_WEBUI_PRESEED_ENABLED=$ENABLED); leaving state dir untouched"
  exit 0
fi

echo "[core-webui-preseed] HERMES_HOME=$HERMES_HOME STATE_DIR=$STATE_DIR SRC=$SRC FORCE=$FORCE"
mkdir -p "$HERMES_HOME" "$STATE_DIR"

# --- branding.yaml -> $HERMES_HOME/branding.yaml ---------------------------
cp "$SRC/branding.yaml" "$HERMES_HOME/branding.yaml"

# --- settings.json -> $STATE_DIR/settings.json -----------------------------
SETTINGS="$STATE_DIR/settings.json"
if [ -f "$SETTINGS" ] && [ "$FORCE" != "true" ]; then
  echo "[core-webui-preseed] $SETTINGS exists and FORCE=false; keeping existing settings"
else
  cp "$SRC/webui-settings.json" "$SETTINGS"
fi

# --- env overrides (env wins over the repo config) -------------------------
# Only rewrite values that were explicitly provided via env. sed on the copied
# files only, never on the read-only repo source.
if [ -n "${BRAND_NAME:-}" ]; then
  sed -i "s#\"name\": \"[^\"]*\"#\"name\": \"$BRAND_NAME\"#" "$SETTINGS" || true
  sed -i "s#^name: .*#name: $BRAND_NAME#" "$HERMES_HOME/branding.yaml" || true
fi
if [ -n "${BRAND_LOGO:-}" ]; then
  sed -i "s#\"logo\": \"[^\"]*\"#\"logo\": \"$BRAND_LOGO\"#" "$SETTINGS" || true
  sed -i "s#^logo: .*#logo: $BRAND_LOGO#" "$HERMES_HOME/branding.yaml" || true
fi
if [ -n "${NAPLATFORM_API_BASE_URL:-}" ]; then
  # Replace both api.base_url and endpoints.api_base_url occurrences.
  sed -i "s#http://api:8080#$NAPLATFORM_API_BASE_URL#g" "$SETTINGS" || true
fi
# Phase 15: allow repo/compose to flip the login-required gate at boot. This only
# rewrites the boolean flags — NO password is ever written into settings.json.
if [ -n "${CORE_WEBUI_LOGIN_REQUIRED:-}" ]; then
  sed -i "s#\"login_required\": [a-z]*#\"login_required\": $CORE_WEBUI_LOGIN_REQUIRED#g" "$SETTINGS" || true
  sed -i "s#\"require_auth\": [a-z]*#\"require_auth\": $CORE_WEBUI_LOGIN_REQUIRED#g" "$SETTINGS" || true
fi

# --- Hermes Agent config for WebUI chat -------------------------------------
# The browser chat path imports run_agent.AIAgent inside the WebUI container. When
# the model-runner override supplies DOCKER_MODEL_RUNNER_BASE_URL/MODEL, seed a
# minimal Hermes config into the shared HERMES_HOME so AIAgent has a provider and
# does not fail with "No LLM provider configured". No real secret is written; the
# Docker Model Runner/OpenAI-compatible local endpoint accepts a placeholder key.
resolve_model_runner_selection() {
  SELECTED_INDEX="${DOCKER_MODEL_RUNNER_DEFAULT_INDEX:-0}"
  eval SELECTED_BASE="\${DOCKER_MODEL_RUNNER_${SELECTED_INDEX}_BASE_URL:-}"
  eval SELECTED_MODEL="\${DOCKER_MODEL_RUNNER_${SELECTED_INDEX}_MODEL:-}"
  eval SELECTED_WEBUI_MODEL="\${DOCKER_MODEL_RUNNER_${SELECTED_INDEX}_WEBUI_MODEL:-}"
  if [ -z "$SELECTED_BASE" ] || [ -z "$SELECTED_MODEL" ]; then
    for idx in 0 1 2 3 4 5 6 7 8 9; do
      eval CANDIDATE_BASE="\${DOCKER_MODEL_RUNNER_${idx}_BASE_URL:-}"
      eval CANDIDATE_MODEL="\${DOCKER_MODEL_RUNNER_${idx}_MODEL:-}"
      if [ -n "$CANDIDATE_BASE" ] && [ -n "$CANDIDATE_MODEL" ]; then
        SELECTED_INDEX="$idx"
        SELECTED_BASE="$CANDIDATE_BASE"
        SELECTED_MODEL="$CANDIDATE_MODEL"
        eval SELECTED_WEBUI_MODEL="\${DOCKER_MODEL_RUNNER_${idx}_WEBUI_MODEL:-}"
        break
      fi
    done
  fi
  DOCKER_MODEL_RUNNER_RESOLVED_BASE_URL="${DOCKER_MODEL_RUNNER_BASE_URL:-$SELECTED_BASE}"
  DOCKER_MODEL_RUNNER_RESOLVED_MODEL="${DOCKER_MODEL_RUNNER_MODEL:-$SELECTED_MODEL}"
  DOCKER_MODEL_RUNNER_RESOLVED_WEBUI_MODEL="${HERMES_WEBUI_DEFAULT_MODEL:-${SELECTED_WEBUI_MODEL:-$DOCKER_MODEL_RUNNER_RESOLVED_MODEL}}"
}
resolve_model_runner_selection
if [ -n "${DOCKER_MODEL_RUNNER_RESOLVED_BASE_URL:-}" ]; then
  WEBUI_MODEL_ID="${DOCKER_MODEL_RUNNER_RESOLVED_WEBUI_MODEL:-gemma4:31b}"

  # fully-qualified id. Hermes department agents normalize gemma4:31b
  # internally, but WebUI chat calls AIAgent directly, so seed the exact id the
  # runner accepts to avoid provider errors like get model '"gemma4:31b"'.
  if [ "$WEBUI_MODEL_ID" = "gemma4:31b" ]; then
    WEBUI_MODEL_ID="docker.io/ai/gemma4:31B"
  fi
  cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: custom
  default: $WEBUI_MODEL_ID
  base_url: ${DOCKER_MODEL_RUNNER_RESOLVED_BASE_URL}
agent:
  max_turns: 20
  disabled_toolsets: []
approvals:
  mode: off
EOF
  echo "[core-webui-preseed] Hermes Agent chat config -> $HERMES_HOME/config.yaml ($WEBUI_MODEL_ID @ $DOCKER_MODEL_RUNNER_RESOLVED_BASE_URL; runner index ${SELECTED_INDEX:-0})"
fi

# --- setup-completed markers ----------------------------------------------
# Multiple markers so the first-run wizard stays suppressed regardless of which
# signal a given core-webui version checks (a state file and/or a sentinel file).
printf '%s\n' '{"first_run": false, "setup_completed": true, "onboarding_completed": true, "initial_setup_completed": true}' \
  > "$STATE_DIR/state.json"
: > "$STATE_DIR/.setup-complete"
: > "$STATE_DIR/.first-run-done"

echo "[core-webui-preseed] preseed complete:"
echo "  branding -> $HERMES_HOME/branding.yaml"
echo "  settings -> $SETTINGS"
echo "  markers  -> $STATE_DIR/state.json, $STATE_DIR/.setup-complete, $STATE_DIR/.first-run-done"
echo "[core-webui-preseed] localhost:3000 skips the first-run setup screen and lands on the LOGIN page (login required)"
