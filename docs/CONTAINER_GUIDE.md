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

## Qdrant/Neo4j scope adapters (single shared instance, metadata-filtered)

Qdrant와 Neo4j는 각각 **단일 공유 서비스**로 뜨고, 부서/사용자 격리는 collection/graph를 나누는 것이
아니라 **metadata filter**로 강제한다. API의 `VectorScopeAdapter`/`GraphScopeAdapter`는 호출자의 RBAC
scope에서 Qdrant `Filter`와 parameterised Cypher를 만들어 personal/부서 데이터를 분리한다. 현재 Phase는
live Qdrant/Neo4j 없이 동작하는 결정적 in-memory scaffold이며, 모든 endpoint는 active user와 부서
membership을 검증한다.

```bash
# vector: 개인 point insert (owner_user_id stamp) 후 scoped search
curl -s -X POST http://localhost:8080/vector/ER/records -H "Authorization: Bearer <TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"collection":"notes","scope":"personal","payload":{"text":"hello"}}' | jq .
curl -s -X POST http://localhost:8080/vector/ER/search -H "Authorization: Bearer <TOKEN>" \
  -H 'Content-Type: application/json' -d '{"collection":"notes"}' | jq '.filter, .count'

# graph: 부서 node insert 후 scoped search (생성된 Cypher/params 확인)
curl -s -X POST http://localhost:8080/graph/ER/nodes -H "Authorization: Bearer <TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"label":"Incident","scope":"department","properties":{"title":"t"}}' | jq .
curl -s "http://localhost:8080/graph/ER/nodes/search?label=Incident" \
  -H "Authorization: Bearer <TOKEN>" | jq '.cypher, .params'
```

Search 응답에는 실제 Qdrant filter descriptor(`filter`)와 Neo4j Cypher/params(`cypher`,`params`)가 그대로
포함되어, 후속 Phase에서 in-memory store를 실제 `qdrant_client`/`neo4j` driver로 교체할 때 동일한
payload를 그대로 사용할 수 있다. 부서 밖 데이터나 다른 사용자의 personal record는 절대 반환되지 않는다.

## Department Hermes agent routing (dry-run vs enabled)

API의 `DepartmentAgentRouter`는 `/agents/{department}/chat`을 부서별로 *설정된* Hermes endpoint로
라우팅한다. 부서 endpoint는 `HERMES_ER_URL`/`HERMES_IT_URL`/`HERMES_EHS_URL`/`HERMES_QC_URL`
(기본값은 Compose service name `http://hermes-er:8080` 등)에서 온다. 기본값은 **dry-run**이라 네트워크를
전혀 건드리지 않고 결정적 응답(`hermes_invoked=false`, `request_id`, context summary)을 반환한다.

```bash
# Dry-run (기본): 실제 Hermes 호출 없이 결정적 응답
curl -s -X POST http://localhost:8080/agents/ER/chat -H "Authorization: Bearer <TOKEN>" \
  -H 'Content-Type: application/json' -d '{"message":"hello"}' | jq '.hermes_invoked, .request_id, .reply'

# admin: 부서별 routing 설정과 enabled/dry_run flag 조회 (secret 없음)
curl -s http://localhost:8080/admin/agents/status -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .
```

실제 Hermes agent를 호출하려면 부서 agent가 HTTP(`/chat` 또는 `/invoke`)를 서빙해야 하고, API에서
routing을 활성화한다. timeout은 `504`, upstream/도달 불가 오류는 `502`로 매핑된다.

```bash
docker compose run --rm \
  -e AGENT_ROUTING_ENABLED=true \
  -e AGENT_REQUEST_TIMEOUT_SECONDS=30 \
  -e HERMES_ER_URL=http://hermes-er:8080 \
  api uvicorn app.main:app --host 0.0.0.0 --port 8080
```

환경변수: `AGENT_ROUTING_ENABLED`(기본 false), `AGENT_REQUEST_TIMEOUT_SECONDS`(기본 30),
`AGENT_INVOKE_PATH`(기본 `/chat`), `HERMES_{ER,IT,EHS,QC}_URL`. 부서 문자열은 검증 후에만 endpoint
map을 조회하며, 사용자가 넘긴 URL은 절대 사용하지 않는다(SSRF 방지).

## Hermes agent HTTP service (deterministic vs execution)

각 부서 Hermes agent 컨테이너(`services/hermes-agent`, package `hermes_agent`)는 이제 `tail -f`로
대기만 하지 않고 작은 FastAPI 서비스를 띄운다. `GET /health`, `POST /chat`, `POST /invoke`를 내부
전용(host port 없이 `expose: ["8080"]`)으로 서빙하며, API의 `DepartmentAgentRouter`가 이 서비스로
라우팅한다. 기본값은 **결정적 응답**(`hermes_invoked=false`)이라 실제 Hermes CLI가 없어도 동작한다.

```bash
# agent 단독 기동 후 내부 health 확인 (host port 미노출 → 컨테이너 내부에서 확인)
docker compose up -d hermes-er
docker compose exec hermes-er curl -fsS http://localhost:8080/health

# API를 통해 라우팅 (기본 dry-run). 실제 HTTP 라우팅을 켜려면 API에 AGENT_ROUTING_ENABLED=true.
docker compose run --rm -e AGENT_ROUTING_ENABLED=true api \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

agent는 API가 발급한 scope를 방어적으로 재검증한다: payload `department`가 컨테이너 `DEPARTMENT`와
다르면 `403`, `hdfs_roots`가 `/naplatform` 밖이거나 traversal이면 `400`,
`allowed_tools`/`allowed_mcp_servers`에 안전하지 않은 식별자가 있으면 `400`.

실제 Hermes CLI를 구동하려면 agent에서 실행을 활성화한다(기본 비활성). 활성화되면 `/chat`이
`subprocess.run`(argv list, shell 미사용, timeout)으로 CLI를 호출하며 timeout은 `504`, non-zero exit는
`502`로 매핑된다. 사용자 `message`는 단일 argv 원소로 전달되어 shell 보간이 불가능하다.

```bash
docker compose run --rm \
  -e HERMES_AGENT_EXECUTION_ENABLED=true \
  -e HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS=60 \
  hermes-er
```

환경변수: `HERMES_AGENT_EXECUTION_ENABLED`(기본 false), `HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS`(기본 60),
`HERMES_BIN`(기본 `hermes`), 그리고 기존 `DEPARTMENT`/`HERMES_PROFILE`/`API_BASE_URL`/`HDFS_NAMENODE`.

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
