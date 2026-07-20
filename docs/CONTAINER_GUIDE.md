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

## Routing E2E smoke (dry-run vs enabled) — Phase 08

기본 `docker-compose.yml`은 항상 **dry-run**(`AGENT_ROUTING_ENABLED=false`)으로 안전하게 유지된다.
routing을 켜려면 `-f`로만 로드되는 override 파일 `docker-compose.override.routing.yml`을 얹는다(평범한
`docker compose up`에는 영향 없음). 이 override는 `api`의 flag만 `true`로 바꾸고 기존 `hermes-er/it/ehs/qc`
서비스와 내부 URL(`http://hermes-<dep>:8080`)을 그대로 재사용한다.

`scripts/smoke_routing_e2e.py`는 live 스택을 상대로 실행되는 E2E smoke이다: API와 각 Hermes agent의
`/health`를 기다리고, seeded admin으로 로그인해 QC user를 **idempotent**하게 생성·승인하고, 그 user로
`POST /agents/QC/chat`을 호출해 override가 켜졌을 땐 `hermes_invoked=true`, 기본 dry-run에선 `false`임을
확인한다(그리고 `GET /admin/agents/status`의 `enabled`와 교차 검증). QC user가 IT에서 `403`으로 거부되는
것도 확인한다. secret(비밀번호/세션 토큰)은 절대 출력하지 않으며 재실행에 안전하다.

smoke는 내부 전용 agent(`hermes-*`, host port 없음)에 서비스 이름으로 접근해야 하므로, Compose 네트워크
**안에서** 실행한다. `docker-compose.smoke.yml`이 API 이미지를 재사용하고 `./scripts`를 마운트하는 one-shot
`smoke` 서비스(profile `smoke`)를 정의한다.

```bash
# Dry-run smoke (기본 스택, routing OFF → hermes_invoked=false 기대)
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
  run --rm -e SMOKE_EXPECT_ROUTING=false smoke

# Enabled-routing smoke (override 적용, routing ON → hermes_invoked=true 기대)
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.override.routing.yml -f docker-compose.smoke.yml \
  run --rm -e SMOKE_EXPECT_ROUTING=true smoke
```

Makefile 단축키: `make smoke-dry`, `make smoke-routing`(전체 목록은 `make help`).

호스트에서 직접 스크립트를 돌릴 수도 있다(단, 내부 전용 agent에는 호스트에서 접근 불가하므로
`SMOKE_HERMES_HEALTH_URLS`를 비워 두면 agent health 체크는 건너뛴다).

```bash
python -m pip install -r services/api/requirements-dev.txt
SMOKE_API_BASE_URL=http://localhost:8080 SMOKE_EXPECT_ROUTING=false \
  python scripts/smoke_routing_e2e.py
```

Agent 자체의 실제 Hermes CLI 실행을 켜려면 agent에 `HERMES_AGENT_EXECUTION_ENABLED=true`를 추가로 준다.
override만 적용한 경우 API 레벨 HTTP 라우팅(`hermes_invoked=true`)은 켜지지만 agent는 여전히 결정적
응답(agent body의 `hermes_invoked=false`)을 반환한다. 이 불변식은 `services/api/tests/test_routing_contract.py`가
실제 hermes 앱을 상대로 증명한다.

환경변수: `SMOKE_API_BASE_URL`(기본 `http://localhost:8080`), `SMOKE_EXPECT_ROUTING`(기본 false),
`SMOKE_HERMES_HEALTH_URLS`(comma-separated), `SMOKE_ADMIN_PASSWORD`/`ADMIN_PASSWORD`,
`SMOKE_QC_EMAIL`/`SMOKE_QC_USERNAME`/`SMOKE_QC_PASSWORD`, `SMOKE_HEALTH_RETRIES`(기본 30),
`SMOKE_HEALTH_INTERVAL`(기본 2s), `SMOKE_REQUEST_TIMEOUT`(기본 30s).

**Branch policy:** Phase 08은 `phase/08-routing-e2e-compose`에서 작업하며 `main`은 건드리지 않는다
(`docs/BRANCHING.md`).

## Resource E2E smoke (hdfs/vector/graph/audit scope) — Phase 09

`scripts/smoke_resources_e2e.py`는 routing이 아니라 **리소스 격리**를 검증하는 live E2E smoke이다.
seeded admin으로 로그인해 **QC**와 **IT** user를 **idempotent**하게 생성·승인한 뒤 다음을 확인한다:

- `GET /workspace/hdfs`가 호출자 **본인**의 personal root(`/naplatform/users/<username>`)와 본인 부서
  root(`/naplatform/departments/QC`)만 반환하고 다른 부서 root는 노출하지 않는다;
- `POST /admin/users/{id}/provision-hdfs`가 **dry-run**이라 `hdfs dfs` 명령 계획(`targets[].plan[].command`)만
  반환하고 아무것도 실행하지 않는다(`dry_run=true`, `enabled=false`, 모든 `results`가 비어 있음);
- **vector** personal/부서 insert+search가 scope대로 동작하고 QC의 레코드는 IT에게 절대 보이지 않는다;
- **graph** personal/부서 insert+search도 동일하게 격리된다;
- QC user가 `/vector/IT`, `/graph/IT`, `/resources/IT`에서 모두 `403`으로 **cross-department 거부**된다;
- audit 로그(`GET /admin/audit`)에 핵심 이벤트(`vector_insert`/`vector_search`/`graph_insert`/`graph_search`/
  `hdfs_provision`/`workspace_view`/`admin_user_update`/`login`)가 남는다.

secret(비밀번호/세션 토큰)은 출력하지 않으며, vector/graph 레코드를 고정 id로 써서 재실행에 안전하다.
in-cluster 실행은 Phase 08의 `smoke` 서비스를 재사용하되 command만 리소스 스크립트로 덮어쓴다.

```bash
# Resource smoke (기본 dry-run 스택)
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
  run --rm smoke python /scripts/smoke_resources_e2e.py
```

Makefile 단축키: `make smoke-resources`, `make smoke-all-dry-run`(routing dry-run + resource),
`make smoke-all-routing`(routing enabled + resource). 호스트에서 직접 돌릴 수도 있다:

```bash
SMOKE_API_BASE_URL=http://localhost:8080 python scripts/smoke_resources_e2e.py
```

환경변수: `SMOKE_API_BASE_URL`, `SMOKE_ADMIN_PASSWORD`/`ADMIN_PASSWORD`,
`SMOKE_QC_EMAIL`/`SMOKE_QC_USERNAME`/`SMOKE_QC_PASSWORD`,
`SMOKE_IT_EMAIL`/`SMOKE_IT_USERNAME`/`SMOKE_IT_PASSWORD`,
`SMOKE_VECTOR_COLLECTION`, `SMOKE_GRAPH_LABEL`, `SMOKE_HEALTH_RETRIES`,
`SMOKE_HEALTH_INTERVAL`, `SMOKE_REQUEST_TIMEOUT`.

### Phase 업로드 / 릴리스 (main은 릴리스에서만 갱신)

git 업로드/릴리스 흐름은 `PHASE_BRANCH`(기본값: 현재 브랜치)를 사용하는 명시적 Makefile 단계로 나뉜다.
`main`은 오직 `release-dev-to-main`에서만 갱신되고 나머지 단계는 절대 건드리지 않는다.

```bash
make push-phase          # PHASE_BRANCH를 origin에 push (dev/main 미변경)
make merge-phase-to-dev  # PHASE_BRANCH를 dev에 merge 후 push (main 미변경)
make release-dev-to-main # 릴리스 전용 — main을 갱신하는 유일한 단계
```

**Branch policy:** Phase 09는 `phase/09-resource-e2e-smoke`에서 작업하며 `main`은 건드리지 않는다
(`docs/BRANCHING.md`, `docs/ROADMAP.md`).

## Real Qdrant/Neo4j/HDFS 백엔드 선택 — Phase 10

Phase 10은 vector/graph/HDFS 백엔드를 **동일한 scope/RBAC 계약을 유지한 채** env로 선택 가능하게 만든다.
기본값은 memory/dry-run이라 기본 스택과 테스트는 live 백엔드가 필요 없다. 실제 드라이버 경로는 fake로
검증한다(`services/api/tests/test_backends.py`, Docker 불필요).

백엔드 선택 env(기본 memory/dry-run):

| env | 값 | 기본 | 실백엔드 연결 env |
|-----|----|------|-------------------|
| `VECTOR_BACKEND` | `memory` \| `qdrant` | `memory` | `QDRANT_URL`, 선택적 `QDRANT_API_KEY`, `QDRANT_VECTOR_SIZE`(768), `QDRANT_DISTANCE`(Cosine) |
| `GRAPH_BACKEND` | `memory` \| `neo4j` | `memory` | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` |
| `HDFS_PROVISIONING_ENABLED` | `true` \| `false` | `false` | (Kerberos/proxy-user; 자격증명은 서비스가 보관하지 않음) |

`docker-compose.yml`의 `api`는 이 값들을 기본 memory/dry-run으로 설정해 두었고, qdrant/neo4j 서비스는 이미
Compose에 있다. 실백엔드로 전환하려면 해당 env만 바꾼다.

```bash
# 기본(memory/dry-run) — live 백엔드 불필요
docker compose -f docker-compose.yml up -d --build
# 활성 백엔드 mode 확인(admin 전용, URL redacted, secret 미노출)
TOKEN=$(curl -s localhost:8080/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"ChangeMe123!"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8080/admin/backends/status -H "Authorization: Bearer $TOKEN"

# 실백엔드로 API만 전환(공유 서비스는 이미 Compose에서 기동)
docker compose up -d qdrant neo4j hdfs-namenode hdfs-datanode-1
VECTOR_BACKEND=qdrant QDRANT_URL=http://localhost:6333 \
GRAPH_BACKEND=neo4j NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j \
NEO4J_PASSWORD=naplatform-password \
  uvicorn app.main:app --app-dir services/api --port 8080
```

동작 요약:

- **Qdrant** — collection이 없으면 `QDRANT_VECTOR_SIZE`/`QDRANT_DISTANCE`로 on-demand 생성하고, memory
  어댑터와 **동일한 metadata·Qdrant `Filter` descriptor**로 upsert/search한다.
- **Neo4j** — **parameterised Cypher만** 실행하며(값은 전부 `$param` bind, inline은 검증된 label/rel-type뿐),
  memory 어댑터와 동일한 scope 격리를 유지한다.
- **HDFS** — `HdfsProvisioner.health()`가 상수 안전 argv(`hdfs dfs -test -d /naplatform`)로 readiness를
  점검한다. 기본 dry-run(계획만), provisioning이 켜졌을 때만 실행한다.
- **안전 폴백** — 잘못된 `VECTOR_BACKEND`/`GRAPH_BACKEND`나 미구성/드라이버 미설치 시 경고 로그와 함께
  memory로 폴백하므로 API 기동이 실패하지 않는다.
- **`GET /admin/backends/status`** — 활성 mode·redacted URL·health를 반환하며 api key/password는 boolean으로만
  노출한다.

`main` branch policy는 이전 Phase와 동일하다: Phase 10은 `phase/10-real-backend-adapters`에서 작업하고
`main`은 릴리스 단계에서만 갱신되어 stable 상태로 남는다.

## core-webui auth/session UI integration — Phase 11

로그인 이후 실제 UI는 외부 `github.com/JacobYim/core-webui`이며 repo에 vendor하지 않는다.
Phase 11은 그 UI를 NAPlatform API에 연결하는 **repo 소유 adapter**(`services/ui/adapter/`)를
추가한다: 의존성 없는 ES module `naplatform-adapter.js`, 계약 단일 소스 `contract.json`, 정적
`index.html` 데모, `package.json`, `README.md`. 세션 토큰은 클라이언트 메모리에만 두며 어떤
adapter 파일에도 secret을 넣지 않는다.

API 지원 endpoint(기존 계약 보존, 추가만):

| Method | Path | Auth | 용도 |
|--------|------|------|------|
| `GET`  | `/auth/departments/options` | none | signup/부서 선택 드롭다운 옵션(세션 전 public) |
| `POST` | `/auth/login` | none | 세션 토큰 발급(pending/disabled → `403`) |
| `POST` | `/auth/logout` | session | Redis/memory 세션 무효화(idempotent) |
| `GET`  | `/auth/me` | active | 세션 부트스트랩(`/core-webui/session` alias) |
| `GET`  | `/core-webui/session/status` | session | 모든 유효 세션 상태 → 승인 대기 UX(만료 세션 `401`) |
| `POST` | `/core-webui/session/select-department` | active | 부서 membership 검증 후 route 반환(비-member `403`, unknown `400`) |

```bash
# 부서 옵션은 public (세션 불필요)
curl -s http://localhost:8080/auth/departments/options | jq .

# active 세션 부트스트랩 + 부서 route (UI는 department_routes[].chat_route로 chat 라우팅)
curl -s http://localhost:8080/auth/me -H "Authorization: Bearer <TOKEN>" | jq '.session_status, .department_routes'

# 부서 선택(비-member면 403)
curl -s -X POST http://localhost:8080/core-webui/session/select-department \
  -H "Authorization: Bearer <TOKEN>" -H 'Content-Type: application/json' \
  -d '{"department":"QC"}' | jq .

# 승인 대기/만료 처리: 세션 상태만 조회(pending이면 can_access:false, 만료면 401)
curl -s http://localhost:8080/core-webui/session/status -H "Authorization: Bearer <TOKEN>" | jq '.can_access, .approval'

# 로그아웃(세션 무효화, idempotent)
curl -s -X POST http://localhost:8080/auth/logout -H "Authorization: Bearer <TOKEN>" | jq .
```

정적 adapter 데모는 실행 중인 API를 상대로 `services/ui/adapter/index.html`을 브라우저로 열어
수동 확인한다(외부 `ui` 서비스 build 불필요). 테스트는 live UI 없이 동작한다:

```bash
pytest -q services/api/tests/test_webui_session.py services/api/tests/test_webui_adapter_contract.py
```

**Branch policy:** Phase 11은 `phase/11-core-webui-auth-session-integration`에서 작업하며 `main`은
건드리지 않는다(`docs/BRANCHING.md`, `docs/ROADMAP.md`).

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

## Phase 12 — Production hardening & release prep

기본 스택은 그대로 permissive(memory/dry-run, SQLite, in-memory 세션)하게 유지되고, 프로덕션
설정은 `.env.production.example`에 전부 문서화되어 있다(실제 secret 없음). readiness 게이트는
`PRODUCTION_MODE=true`일 때만 필수 체크를 강제한다.

```bash
# 프로덕션 env 템플릿을 복사해 실제 값으로 채운다(.env.production 은 git-ignore).
cp .env.production.example .env.production

# redacted 프로덕션 readiness 리포트(booleans + redacted URL만; secret 없음)
make readiness

# 릴리스 게이트: pytest + compile + compose config + readiness 게이트 (main 미변경)
make release-check
PRODUCTION_MODE=true make release-check     # 프로덕션 env로 필수 체크 강제

# 프로덕션 env 템플릿을 적용한 compose config 검증
make compose-config-prod
docker compose --env-file .env.production.example -f docker-compose.yml config

# 최종 스모크(도커 필요): dry-run + enabled-routing
make smoke-final

# 실행 중 API에서 admin 전용 readiness / 감사 export (secret 미노출)
curl -s http://localhost:8080/admin/release/readiness -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .
curl -s "http://localhost:8080/admin/audit/export?action=login&limit=100&format=jsonl" \
  -H "Authorization: Bearer <ADMIN_TOKEN>"
```

Phase 12 환경변수: `PRODUCTION_MODE`, `TRUSTED_ORIGINS`, `SESSION_STORE_STRICT`,
`AUDIT_RETENTION_DAYS`, `AUDIT_RETENTION_ENFORCE`. 릴리스 절차는
`docs/FINAL_RELEASE_CHECKLIST.md`, 릴리스 노트는 `docs/RELEASE_NOTES_TEMPLATE.md` 참고.

**Branch policy:** Phase 12는 `phase/12-production-hardening-release-prep`에서 작업하며 `main`은
건드리지 않는다. `main`은 명시적 승인 후 `make release-dev-to-main`에서만 갱신된다.

## Phase 13 — Docker Model Runner (공유 gemma4:31b) + PowerShell runbook

모든 부서 에이전트(ER/IT/EHS/QC)가 **하나의** 모델 `gemma4:31b`를 Docker Model Runner의
OpenAI-compatible 엔드포인트로 공유한다. 기본 스택은 여전히 dry-run(model-less)이며, 모델 러너는
`docker-compose.model-runner.yml` override를 **명시적으로 `-f`** 로 적용할 때만 켜진다.

```bash
# 기본(dry-run) — 모델 러너 미적용, profile은 model-less (안전)
docker compose -f docker-compose.yml config >/dev/null

# 공유 gemma4:31b 모델 러너 compose config 검증(모델 러너 미설치여도 검증됨)
make compose-config-model-runner
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml config

# 공유 모델 러너 스택 기동 (로컬 Docker Model Runner 필요)
#   Docker Desktop 4.40+에서 Model Runner 활성화 후:
#     docker model pull gemma4:31b
make up-model-runner
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml up -d --build

# 모델 러너 스택 routing 스모크 (라이브 DMR + 에이전트 이미지의 Hermes CLI 필요)
make smoke-model-runner

# 공유 모델 러너 설정 확인 (admin 전용; secret 미노출 — provider/redacted URL/model/boolean만)
curl -s http://localhost:8080/admin/agents/status -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .model_runtime
# 각 에이전트 /health 의 model_runtime 도 동일 모델(gemma4:31b)을 보고(부서별 persona는 격리)
```

동작 요약:
- `HERMES_AGENT_EXECUTION_ENABLED=true` **그리고** LLM provider/model 환경변수가 설정되면, 각
  에이전트의 생성 profile `config.yaml`에 공유 `llm:` 블록(provider/base_url/model=gemma4:31b)이
  추가된다. 부서별 `SOUL.md` persona는 격리되고 모델은 4개 에이전트가 동일(drift 없음).
- LLM 환경변수가 없으면 profile은 model-less → 기본 dry-run 안전.
- API 키는 env-var **이름**(`api_key_env`)으로만 참조되며 값은 디스크에 기록/노출되지 않는다.
- 실제 모델 응답은 로컬 Docker Desktop/Model Runner 지원과 에이전트 이미지의 Hermes CLI에 의존한다
  (본 단계는 스캐폴드; 모델명/엔드포인트는 요청대로 gemma4:31b로 정확히 배선).

Phase 13 환경변수: `HERMES_LLM_PROVIDER`, `DOCKER_MODEL_RUNNER_BASE_URL`,
`DOCKER_MODEL_RUNNER_MODEL`(기본 `gemma4:31b`), OpenAI-compatible fallback
`OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY`.

Windows PowerShell 실행 절차(클론 → dev 체크아웃 → release-check 대체 → 스택 실행 → 스모크 →
정리)는 `docs/POWERSHELL_RUNBOOK.md` 참고(PowerShell 문법; Git Bash와의 차이 주석 포함).

**Branch policy:** Phase 13은 `phase/13-docker-model-runner-gemma4-powershell`에서 작업하며 `main`은
건드리지 않는다(stable). `main`은 명시적 승인 후 `make release-dev-to-main`에서만 갱신된다.

## Phase 14 — core-webui first-run preseed (localhost:3000 초기 setup 화면 제거)

새 볼륨에서 외부 core-webui UI는 http://localhost:3000 첫 접속 시 **initial setup / onboarding 화면**을
띄운다. Phase 14는 **모든 first-run 설정을 이 저장소에서 미리 구성**해 Docker가 UI를 올릴 때 자동으로
적용하므로, 첫 로드가 곧바로 HMGMA 워크스페이스로 열린다 — **setup 화면 없음(no setup screen)**.
방식은 **non-invasive**하다: 외부 core-webui 이미지/entrypoint는 절대 수정하지 않고, UI가 읽는 공유
볼륨에 저장소 config를 **서빙 전에** 시딩한다.

구성 요소:

- **저장소 config (`config/core-webui/`)** — `branding.yaml`(HMGMA name/logo →
  `$HERMES_HOME/branding.yaml`), `webui-settings.json`(first-run 설정: `first_run: false` /
  `setup_completed: true` / `onboarding_completed: true`, API base URL `http://api:8080`, auth
  adapter, 기본 endpoint 값 → core-webui state dir `$HERMES_WEBUI_STATE_DIR/settings.json`),
  `README.md`. **secret 없음** — 비밀번호/토큰/API 키를 절대 기록하지 않으며, 세션 토큰은 API가 로그인
  시 발급해 adapter가 브라우저 메모리에만 보관한다.
- **preseed init (`preseed.sh` + `ui-preseed` 서비스)** — `ui-hermes-home` 볼륨을 `ui`와 공유하는
  의존성 없는 busybox one-shot 서비스로, config + setup-completed 마커(`state.json`, `.setup-complete`)를
  쓰고 종료한다. `ui`는 `depends_on`에 `condition: service_completed_successfully`로 이를 기다려
  **core-webui가 서빙하기 전에** config가 적용된다. 기본 `docker-compose.yml`에 포함되므로 평범한
  `docker compose up ui` 만으로 setup 화면이 사라진다.
- **env override 유지** — `BRAND_NAME` / `BRAND_LOGO` / `NAPLATFORM_API_BASE_URL` 가 config 파일보다
  우선하며, `ui`에는 `HERMES_WEBUI_SETUP_COMPLETED` / `HERMES_WEBUI_DISABLE_FIRST_RUN` env 플래그도
  belt-and-suspenders로 선언한다.

```bash
# 기본 스택으로 UI 기동(자동으로 preseed 적용 → setup 화면 없음)
docker compose up -d --build ui
docker compose logs ui-preseed        # 1회성 preseed 실행 로그
docker compose logs -f ui
# 브라우저에서 http://localhost:3000 → 곧바로 워크스페이스(초기 setup 화면 없음)

# 오프라인 검증(Docker 불필요)
python scripts/_phase14_check.py
pytest -q services/api/tests/test_phase14_ui_preseed.py
```

PowerShell / Bash로 config를 편집하고 Docker를 기동하는 상세 절차는
[config/core-webui/README.md](../config/core-webui/README.md) 참고. `main` policy는 이전 Phase와
동일하다: Phase 14는 `phase/14-core-webui-first-run-autoconfig`에서 작업하며 `main`은 건드리지 않고
stable로 남는다.
