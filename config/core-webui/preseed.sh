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
# NO SECRETS: this script handles branding / endpoints / first-run flags only. It
# never writes a password, token, or API key. The session token is minted by the
# API at login and kept in browser memory by the adapter.
#
# Env overrides (all optional; env wins over the repo config files):
#   HERMES_HOME              default /home/hermeswebui/.hermes
#   HERMES_WEBUI_STATE_DIR   default $HERMES_HOME/webui
#   BRAND_NAME               overrides branding name in both files
#   BRAND_LOGO               overrides branding logo path in both files
#   NAPLATFORM_API_BASE_URL  overrides the API base URL in settings.json
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
echo "[core-webui-preseed] localhost:3000 will open the workspace directly (no first-run setup screen)"
