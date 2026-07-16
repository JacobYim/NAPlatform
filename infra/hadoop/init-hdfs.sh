#!/usr/bin/env bash
set -euo pipefail
for i in $(seq 1 30); do hdfs dfs -ls / >/dev/null 2>&1 && break || sleep 2; done
hdfs dfs -mkdir -p /naplatform/departments/ER /naplatform/departments/IT /naplatform/departments/EHS /naplatform/departments/QC /naplatform/users
hdfs dfs -chmod 750 /naplatform/departments/*
hdfs dfs -chmod 755 /naplatform/users
hdfs dfs -ls -R /naplatform
