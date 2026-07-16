# Container Guide

## All-in-one
```bash
docker compose up --build
```

`ui` 서비스는 기본적으로 `https://github.com/JacobYim/core-webui.git`를 Docker build context로 사용한다. 이미 sibling directory에 core-webui를 clone해 둔 경우에는 다음처럼 로컬 context를 지정할 수 있다.

```bash
git clone https://github.com/JacobYim/core-webui.git ../core-webui
CORE_WEBUI_CONTEXT=../core-webui docker compose up --build ui
```

Windows Git Bash에서 `docker run -e BRAND_LOGO=/apptoo/...`처럼 container absolute path를 직접 넘길 때는 MSYS path conversion을 막기 위해 `MSYS_NO_PATHCONV=1`을 붙인다. Compose YAML에 선언된 값은 이 문제를 피한다.

## Infra only
```bash
docker compose up -d postgres redis qdrant neo4j hdfs-namenode hdfs-datanode-1 hdfs-datanode-2 hdfs-datanode-3
```

## Init HDFS
```bash
docker compose run --rm hdfs-init
```

## HDFS workspace provisioning (dry-run vs enabled)

API의 `HdfsProvisioner`는 RBAC HDFS root에 대한 `hdfs dfs` 명령 계획(개인 dir `chmod 700`, 부서 dir
`chmod 770`, user/group `setfacl`)을 만든다. 기본값은 **dry-run**이라 명령을 만들기만 하고 실행하지 않는다.

```bash
# Dry-run (기본): 계획만 확인, subprocess 미실행
curl -s -X POST http://localhost:8080/admin/users/<USER_ID>/provision-hdfs \
  -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .

# active user 본인 workspace 계획 조회 (항상 dry-run)
curl -s http://localhost:8080/workspace/hdfs -H "Authorization: Bearer <TOKEN>" | jq .
```

실제로 디렉토리를 만들고 ACL을 적용하려면 API 컨테이너에서 provisioning을 활성화하고, `hdfs` CLI가
NameNode에 접근 가능해야 한다.

```bash
# 활성화: subprocess로 hdfs dfs 명령을 실제 실행
docker compose run --rm -e HDFS_PROVISIONING_ENABLED=true api \
  python -c "from app.hdfs import HdfsProvisioner; from app.store import store; print(HdfsProvisioner().provision_user(store.get_user('<USER_ID>')).model_dump_json())"
```

환경변수: `HDFS_PROVISIONING_ENABLED`(기본 false), `HDFS_BIN`(기본 `hdfs`),
`HDFS_DEPARTMENT_GROUP_PREFIX`(기본 `naplatform`).

**Kerberos production note:** 계획의 `chown/chgrp`는 placeholder다. Production에서는 keytab으로 인증된
서비스 계정이 Kerberos principal/proxy-user와 HDFS ACL로 소유권·접근을 강제해야 하며, 위와 같은 ad-hoc
CLI 실행 대신 kerberized NameNode에 대해 provisioning을 수행한다.

## Separate core-webui UI
```bash
CORE_WEBUI_CONTEXT=../core-webui docker compose up -d ui
docker compose logs -f ui
```

현재 Phase에서는 core-webui HMGMA branding과 컨테이너 기동을 검증한다. 실제 agent 실행은 다음 Phase에서 NAPlatform API의 `/agents/{department}/context`와 부서별 Hermes agent container adapter를 연결한다.

## Separate agent
```bash
docker compose up -d hermes-er
docker compose logs -f hermes-er
```

## Separate docker run pattern
```bash
docker network create naplatform_net
docker run -d --name nap-redis --network naplatform_net redis:7-alpine
docker run -d --name hermes-er --network naplatform_net -e DEPARTMENT=ER -e HERMES_PROFILE=ER naplatform-hermes-agent:local
```
