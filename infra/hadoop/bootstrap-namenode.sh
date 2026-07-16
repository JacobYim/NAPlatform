#!/usr/bin/env bash
set -euo pipefail
NAME_DIR=/tmp/hadoop-root/dfs/name
if [ ! -f "$NAME_DIR/current/VERSION" ]; then
  hdfs namenode -format -force -nonInteractive
fi
exec hdfs namenode
