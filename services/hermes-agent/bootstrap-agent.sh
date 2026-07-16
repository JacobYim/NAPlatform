#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HERMES_HOME/profiles/$HERMES_PROFILE"
printf "You are the %s department Hermes agent for NAPlatform. Only use API-supplied AgentContext allow-lists.\n" "$DEPARTMENT" > "$HERMES_HOME/profiles/$HERMES_PROFILE/SOUL.md"
cat > "$HERMES_HOME/profiles/$HERMES_PROFILE/config.yaml" <<EOF
security:
  redact_secrets: true
naplatform:
  department: "$DEPARTMENT"
  api_base_url: "$API_BASE_URL"
  hdfs_namenode: "$HDFS_NAMENODE"
  optional_tools:
    nemoclaw: true
    openshell: true
EOF
echo "Hermes profile prepared: $HERMES_PROFILE/$DEPARTMENT; NemoClaw/OpenShell hooks enabled pending concrete package sources."
tail -f /dev/null
