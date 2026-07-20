# Architecture

```text
Browser -> NAPlatform login/admin API -> core-webui runtime UI
UI/API -> FastAPI API -> Redis sessions
                   -> Postgres auth/RBAC/audit
                   -> HDFS NameNode + 3 DataNodes
                   -> Qdrant shared vector DB (metadata-filtered per scope)
                   -> Neo4j shared graph DB (metadata-filtered per scope)
                   -> Hermes ER/IT/EHS/QC agents
```

보안 경계는 프롬프트가 아니라 API RBAC, HDFS ACL/Kerberos, DB query filter, container/user isolation이다. Production에서는 Kerberos principal/proxy-user와 HDFS ACL로 디렉토리 접근을 강제한다.

## Runtime UI

로그인 이후 사용자가 실제로 상호작용하는 UI는 `github.com/JacobYim/core-webui`이다.
core-webui는 white-label browser UI이며, `BRAND_NAME`과 `BRAND_LOGO` 환경변수로
HMGMA 브랜드를 표시할 수 있다. 확인한 저장소에는 `branding/logo.jpg`가 포함되어 있고
이미지 텍스트는 `HMG Metaplant America`이다.

NAPlatform `ui` Compose 서비스는 core-webui Git context를 build하고 다음 값을 주입한다.

```yaml
BRAND_NAME: HMGMA
BRAND_LOGO: /apptoo/branding/logo.jpg
NAPLATFORM_API_BASE_URL: http://api:8080
```

후속 Phase에서는 NAPlatform 로그인 세션/승인 상태를 core-webui entrypoint 앞단 또는 adapter API에 연결하여, core-webui agent 호출이 항상 `/agents/{department}/context`에서 발급된 허용 HDFS roots, tools, MCP, Qdrant filter, Neo4j filter만 사용하도록 한다.

## Phase 02 — core-webui adapter stub (ready)

Phase 02 adapter stub이 준비되었다. 실제 Hermes 호출은 다음 Phase에서 연결한다.
현재 in-memory store와 기존 RBAC를 그대로 사용하며, 모든 endpoint는 Bearer 세션과
active/부서 membership을 검증한다.

- `GET /core-webui/session` — Bearer 토큰을 검증하고 active user의 `user/departments/default_department`와
  core-webui launch config(`brand_name`, `brand_logo`, `api_base_url`, `webui_url`)를 반환한다.
  pending user는 `403`으로 거부된다.
- `POST /agents/{department}/chat` — active user만 허용하고 RBAC로 부서 membership을 검증한 뒤
  `AgentContext`를 구성하여 결정적(deterministic) stub 응답을 반환한다. 응답에는 `department`,
  `user`, `hdfs_roots`, `allowed_tools`, `allowed_mcp_servers`가 포함되고 `hermes_invoked=false`이다.
  실제 Hermes는 아직 호출하지 않는다(다음 Phase 예정).
- `GET /resources/{department}?path=` — 허용된 HDFS root만 반환하며, 부서 밖 경로 또는 미허가 경로는 `403`으로 거부한다.
- `GET /admin/approvals/pending` — admin이 승인 대기(pending) user 목록을 조회한다.

Pydantic request/response 모델(`SessionBootstrapResponse`, `CoreWebUILaunchConfig`, `ChatRequest`,
`ChatResponse`, `ResourceListResponse`, `PendingApproval`)과 pytest 커버리지(부트스트랩 거부/허용,
부서 격리 chat, resource 경로 enforcement, admin pending 목록)가 추가되었다.
다음 단계는 이 stub을 실제 Hermes agent 호출로 교체하는 것이다.

## Phase 03 — Persistent auth/RBAC (scaffold)

인증/RBAC 상태를 in-memory dict에서 SQLAlchemy 백엔드로 옮겼다. 기존 API 계약과 store method
surface는 그대로 유지되어 Phase 02 test/import가 계속 동작한다. Alembic은 아직 도입하지 않고
startup 시 테이블을 auto-create한다(scaffold).

- **`app/db.py`** — SQLAlchemy 테이블 `users`, `user_departments`, `audit_events`,
  `password_reset_tokens`. `DATABASE_URL`(Compose에서 `postgresql+psycopg://…`)을 사용하고,
  test/offline에서는 SQLite(in-memory `StaticPool` 또는 파일)로 fallback한다.
- **`app/store.py`** — SQLAlchemy 기반 `Store`. users/audit/reset token을 영속화하며 세션은
  주입된 session store에 위임한다. `Store(database_url=..., session_store=...)`로 격리된 test 구성이
  가능하고, `InMemoryStore`는 하위호환 alias로 남는다.
- **`app/session_store.py`** — `SessionStore` 인터페이스. `RedisSessionStore`(`REDIS_URL`,
  `SETEX` TTL)와 `MemorySessionStore` fallback. `build_session_store()`는 `REDIS_URL`이 설정되고
  연결되면 Redis를, 아니면 in-memory를 선택한다. TTL 기본값은 `SESSION_TTL_SECONDS`(3600s).
- **Password reset** — `POST /auth/password-reset/request`는 `expires_at` TTL을 가진 reset token
  row를 영속화하고, 응답에는 토큰을 노출하지 않는다.
- **Audit log** — signup, login 성공/실패, admin user update, password-reset 요청, agent chat이
  `audit_events`에 기록된다. `GET /admin/audit?limit=N`(admin 전용)으로 최신순 조회한다.

pytest 커버리지: sqlite temp DB에서 Store 인스턴스 간 영속성, 세션 TTL 만료, audit event 생성,
reset token 만료 영속성, Redis 미가용 시 in-memory fallback, 그리고 기존 30개 테스트 유지.

## Phase 04 — HDFS workspace provisioning (scaffold)

RBAC가 정의하는 HDFS root(개인 `/naplatform/users/{username}`, 부서 `/naplatform/departments/{DEP}`)에
디렉토리/ACL을 실제로 배치하기 위한 provisioning scaffold를 추가했다. 핵심 설계는 **안전한 명령 계획을
먼저 만들고(dry-run), 명시적으로 활성화됐을 때만 실행**하는 것이다.

- **`app/hdfs.py` — `HdfsProvisioner`** — 사용자/부서를 검증한 뒤 결정적인 `hdfs dfs` 명령 계획을
  만든다. 개인 디렉토리는 `mkdir -p` → `chown/chgrp`(placeholder) → `chmod 700` → `setfacl -m user:{username}:rwx`,
  부서 디렉토리는 `chmod 770` → `setfacl -m group:naplatform-{dep}:rwx` + `setfacl -m user:{username}:rwx`
  로 구성된다.
- **입력 검증** — username은 `^[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}$`(선행 dot/dash 금지)만 허용하고 `..`를
  거부한다. 부서는 `DEPARTMENTS`에 없으면 거부하며, 완성된 경로는 `normalize_hdfs_path`로 재검증하여
  traversal 없이 `BASE` 하위에 있음을 보장한다.
- **Dry-run vs enabled** — `HDFS_PROVISIONING_ENABLED=true`가 아니면 **subprocess를 전혀 실행하지 않고**
  계획만 반환한다(`dry_run=true`, `results=[]`). 활성화되면 각 명령을 argv list로 `subprocess.run`(shell 미사용)
  하여 `returncode/stdout/stderr`를 결과에 담는다.
- **Endpoints** — `POST /admin/users/{user_id}/provision-hdfs`(admin 전용)는 해당 사용자의 개인/부서
  디렉토리 provision plan(및 enabled 시 결과)을 반환한다. `GET /workspace/hdfs`(active user)는 본인의
  personal root, department roots, provisioning status와 dry-run plan만 반환한다.
- **Audit** — `hdfs_provision`, `workspace_view` 이벤트가 `audit_events`에 기록된다.

**Kerberos production note:** scaffold의 `chown/chgrp`는 placeholder다. Production에서는 개별
`hdfs dfs` 명령이 아니라 Kerberos principal/proxy-user와 HDFS ACL로 소유권·접근을 강제해야 하며,
provisioning 실행 주체는 keytab으로 인증된 서비스 계정이어야 한다.

pytest 커버리지: 개인/부서 명령 계획, traversal/invalid username 거부, admin만 provision 가능,
active user는 본인 root만 조회, 비-admin provision 거부, dry-run 기본값에서 subprocess 미실행,
그리고 audit 기록.

## Phase 05 — Qdrant/Neo4j scope adapters (metadata-separated scaffold)

핵심 설계 원칙: **Qdrant와 Neo4j는 각각 단일 공유 인스턴스**이며, 부서/사용자 격리는
물리적 collection-per-tenant나 graph-per-tenant가 아니라 **metadata filter**로 이루어진다.
모든 vector point와 graph node/relationship은 scope metadata(`owner_user_id`, `allowed_users`,
`department`, `allowed_departments`)를 갖고, 모든 read는 호출자의 RBAC scope에서 유도된
filter로 제한된다. 기존 `rbac.qdrant_filter` / `rbac.neo4j_filter` 규칙을 그대로 재사용한다.
어댑터는 live Qdrant/Neo4j 없이 동작하는 **테스트된 scaffold**다.

- **`app/vector.py` — `VectorScopeAdapter`** — active user의 개인+부서 scope에 대한 Qdrant `Filter`
  descriptor를 만들고, 부서 membership을 검증(`qdrant_filter`)하며, collection 이름을
  `^[a-z][a-z0-9_]{2,63}$`로 검증하고, 결정적인 in-memory store를 유지한다. Insert는 scope가
  `personal` 또는 `department`여야 한다: personal point는 `owner_user_id=user.id`를, department
  point는 `department=<active>`와 `allowed_departments=[<active>]`를 stamp한다. Search는
  metadata가 호출자(본인/허용 사용자, 활성/허용 부서)와 일치하는 point만 반환한다.
- **`app/graph.py` — `GraphScopeAdapter`** — `owner_user_id`/`allowed_users`/`department`/
  `allowed_departments`를 강제하는 parameterised Cypher MATCH descriptor를 만든다
  (`$owner_user_id`/`$user_id`/`$department`는 param으로 bind되며 문자열 보간을 하지 않는다).
  Cypher에 반드시 inline되어야 하는 식별자인 label은 `^[A-Za-z][A-Za-z0-9_]{0,63}$`,
  relationship type은 `^[A-Z][A-Z0-9_]{0,63}$`로 엄격히 검증한다. 결정적 in-memory
  node/relationship insert+search stub이 동일한 scope 규칙을 적용한다.
- **Endpoints** (모두 active user + 부서 membership 필요) — `POST /vector/{department}/records`,
  `GET|POST /vector/{department}/search`(응답에 생성된 Qdrant filter descriptor 포함),
  `POST /graph/{department}/nodes`, `GET|POST /graph/{department}/nodes/search`(응답에 생성된
  Cypher + params 포함).
- **Audit** — `vector_insert`, `vector_search`, `graph_insert`, `graph_search` 이벤트가 기록된다.

pytest 커버리지: 개인/부서 insert+search 허용, cross-user/cross-department 필터링, invalid
collection/label 거부, invalid scope 거부, pending/비-member 거부(401/403), audit 기록, 생성된
Qdrant/Neo4j filter/Cypher descriptor 형태, admin의 any-department scope. 실제 `qdrant_client` /
`neo4j` driver로 in-memory store를 교체하는 것은 후속 Phase이며, 어댑터가 이미 방출하는
filter/Cypher descriptor가 그 driver가 소비하는 payload다.

## Phase 06 — Department Hermes agent routing (scaffold)

Phase 02의 결정적 stub을 실제 Hermes agent routing으로 교체했다. 핵심 설계는 **부서별로
*설정된* endpoint로만 라우팅**하고, routing이 명시적으로 활성화됐을 때만 실제 HTTP 호출을 하며,
그 외에는 결정적 dry-run을 반환하는 것이다. Live Hermes 없이도 테스트가 동작한다.

- **`app/agent_router.py` — `DepartmentAgentRouter`** — `AgentContext`를 실제 agent 호출 payload로
  변환한다. 부서 endpoint는 `HERMES_ER_URL`/`HERMES_IT_URL`/`HERMES_EHS_URL`/`HERMES_QC_URL`
  (기본값은 Compose service name `http://hermes-er:8080` 등)에서 해석된다.
- **SSRF 방지** — 부서 문자열은 `normalize_department`로 검증한 뒤에만 endpoint map을 조회하고,
  **사용자가 넘긴 URL은 절대 사용하지 않는다.** 설정/기본 URL도 `http(s)://host` 형태인지 재검증하여
  `file://` 같은 scheme을 차단한다.
- **Client 추상화** — `AgentClient` 인터페이스 아래 두 구현: `HttpAgentClient`는
  `AGENT_REQUEST_TIMEOUT_SECONDS` timeout으로 `/chat`(404면 `/invoke`로 fallback)에 JSON `POST`를
  수행하고, `DryRunAgentClient`는 `AGENT_ROUTING_ENABLED`가 true가 아니거나 부서 URL이 없을 때의
  fallback으로 `request_id`, `department`, `hermes_invoked=false`와 secret-free context summary를 반환한다.
- **Invocation payload** — `message`, user identity, 전체 `AgentContext`, `allowed_tools`,
  `allowed_mcp_servers`, `hdfs_roots`, `qdrant_filter`, `neo4j_filter`, personal `workspace_root`를
  포함하여 agent가 API가 발급한 scope를 재유도 없이 그대로 준수하게 한다.
- **Endpoints** — `POST /agents/{department}/chat`는 router를 통해 라우팅한다(기본 dry-run,
  enabled+URL 설정 시에만 실제 HTTP 호출). timeout은 `504`, upstream/unreachable 오류는 `502`로
  매핑하고 성공/실패 모두 `agent_chat`으로 audit한다. `GET /admin/agents/status`(admin 전용)는
  부서별 routing 설정과 `enabled`/`dry_run` flag를 secret 없이 반환한다.
- **Audit** — `agent_chat`은 성공 시 department/`hermes_invoked`/`request_id`, 실패 시
  실패 종류(`timeout`/`upstream_error`/`routing_error`)를 기록한다.

`httpx`는 lazy import이며 주입 가능한 transport로 사용하므로, enabled HTTP 경로도 live agent 없이
`httpx.MockTransport`로 완전히 테스트된다.

pytest 커버리지: dry-run payload/context, fake transport 기반 enabled HTTP client, timeout→504 /
upstream→502 매핑, 부서 URL 검증·no-SSRF(설정된 URL만 허용, 사용자 URL 불가), admin status endpoint,
audit 이벤트, 그리고 기존 테스트 유지.

## Phase 07 — Hermes agent HTTP service (scaffold)

Phase 06까지 API `DepartmentAgentRouter`는 부서별 Hermes endpoint로 HTTP를 라우팅했지만, 실제로
받아주는 서비스가 없었다(agent 컨테이너는 `tail -f`로 대기만 함). Phase 07은 각 부서 Hermes agent
컨테이너에 실제 HTTP 서비스를 얹는다. 핵심 설계는 **API가 발급한 scope를 agent가 다시 방어적으로
검증**하고, 실제 Hermes CLI 실행은 명시적으로 활성화됐을 때만 하며, 그 외에는 결정적 응답을
반환하는 것이다. Live Hermes CLI 없이도 테스트가 동작한다.

- **`services/hermes-agent` (package `hermes_agent`)** — 작은 FastAPI 서비스. `GET /health`,
  `POST /chat`, `POST /invoke`를 노출한다. `DEPARTMENT`/`HERMES_PROFILE`/`API_BASE_URL`/`HDFS_NAMENODE`를
  로드하고, startup 시 예전 `bootstrap-agent.sh`와 동일하게 profile 파일(`SOUL.md`, `config.yaml`)을
  준비한 뒤, `DepartmentAgentRouter`가 POST하는 payload 형태(`InvokeRequest`)를 그대로 수신한다.
- **방어적 scope 재검증 (`hermes_agent/validation.py`)** — API가 보안 경계이지만, agent는 넘겨받은
  scope를 다시 확인한다: payload `department`가 컨테이너 `DEPARTMENT`와 일치해야 하고(불일치 시 `403`),
  모든 `hdfs_roots`는 traversal 없이 `/naplatform` 하위로 정규화돼야 하며, 모든
  `allowed_tools`/`allowed_mcp_servers`는 안전한 식별자(`^[a-z0-9][a-z0-9_.-]{0,63}$`)여야 한다(위반 시
  `400`). 기본 응답은 결정적이며 `hermes_invoked=false`다.
- **선택적 실행 (`hermes_agent/executor.py`)** — `HERMES_AGENT_EXECUTION_ENABLED=true`이면 `/chat`이 실제
  Hermes CLI를 `subprocess.run`(argv list, `shell=False`, `HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS` timeout)으로
  구동한다. 사용자 `message`는 단일 argv 원소로 전달되어 shell 보간이 불가능하다(안전한 명령 구성).
  기본값은 비활성이며, 테스트는 `FakeHermesRunner`로 이 경로를 검증한다(실제 CLI 없음). timeout은 `504`,
  non-zero exit는 `502`로 매핑된다.
- **Compose** — `hermes-*` 서비스는 host port 없이 내부 전용(`expose: ["8080"]`)으로 HTTP를 서빙하고
  `curl /health` healthcheck를 갖는다. API의 `AGENT_ROUTING_ENABLED`는 여전히 기본 `false`이며,
  `true`로 바꾸면(그리고 필요 시 agent에 `HERMES_AGENT_EXECUTION_ENABLED=true`) 실제 HTTP 호출로 라우팅된다.

pytest 커버리지: health, chat/invoke 결정적 기본, 부서 불일치 거부(403), invalid HDFS root 거부(400),
invalid tool/mcp 식별자 거부(400), 실행 비활성 시 runner 미호출, 실행 활성 시 fake runner 사용·timeout→504·
non-zero→502, 안전한 argv 구성. 또한 API 측 `test_agent_service_shape.py`는 `httpx.MockTransport`로
router payload가 서비스 `InvokeRequest` 계약과 일치하고 서비스 응답 형태를 router가 수용함을 증명한다.
테스트는 `services/hermes-agent/tests`에 있고 API 테스트와 함께 실행된다(`pytest.ini`가 두 경로를
`pythonpath`/`testpaths`에 등록; agent 패키지명은 API의 `app`과 충돌하지 않도록 `hermes_agent`).

## Phase 08 — Routing E2E Compose/smoke (scaffold)

Phase 06/07까지 API↔agent HTTP 경로와 그 계약을 갖췄다. Phase 08은 그 경로를 **live 스택에서 실제로
켜고 검증**하는 수단을 더하되, 안전한 기본값은 바꾸지 않는다. 핵심 설계는 **기본 `docker-compose.yml`은
언제나 dry-run**(`AGENT_ROUTING_ENABLED=false`)이고, routing을 켜는 것은 `-f`로만 로드되는 override로
분리하며, smoke는 Compose 네트워크 안에서 실제 HTTP를 구동하는 것이다.

- **`docker-compose.override.routing.yml`** — 평범한 `up`에 자동 로드되지 않는 override. `api`의
  `AGENT_ROUTING_ENABLED`만 `true`로 바꾸고 기존 `hermes-er/it/ehs/qc` 서비스와 내부
  URL(`http://hermes-<dep>:8080`)을 재사용한다. 새 agent 서비스도, agent host port도 추가하지 않는다.
- **`docker-compose.smoke.yml`** — one-shot `smoke` 서비스(profile `smoke`). API 이미지를 재사용하고
  `./scripts`를 마운트하여 네트워크 내부에서 `scripts/smoke_routing_e2e.py`를 돌린다. 내부 전용 agent에
  서비스 이름으로 접근하기 위해 in-cluster로 실행하는 것이 핵심이다.
- **`scripts/smoke_routing_e2e.py` — `RoutingSmoke`** — API/Hermes `/health` 대기 → seeded admin 로그인 →
  QC user **idempotent** 생성·승인 → QC 로그인 → `POST /agents/QC/chat`. override가 켜지면
  `hermes_invoked=true`, 기본 dry-run이면 `false`임을 확인하고 `GET /admin/agents/status`의 `enabled`와
  교차 검증한다. QC user의 IT 거부(`403`)도 검증한다. **secret 미출력 · 재실행 안전.** 주입된 HTTP
  client를 받으므로 Docker 없이 단위 테스트가 가능하다.
- **보안 경계 재확인** — smoke는 사용자가 넘긴 URL을 dial하지 않고 부서 스코프는 여전히 API RBAC로
  강제된다. override는 flag 하나만 바꿀 뿐 endpoint map(SSRF-safe)은 그대로다.
- **테스트** — `test_smoke_routing_e2e.py`(fake API `httpx.MockTransport`로 dry-run/enabled, 기대 불일치,
  signup idempotency, IT 거부, health retry, secret 미출력 검증), `test_routing_contract.py`(실제
  `hermes_agent` 앱을 상대로 `AGENT_ROUTING_ENABLED=true`+`HERMES_AGENT_EXECUTION_ENABLED=false`일 때
  API는 `hermes_invoked=true`, agent body는 `hermes_invoked=false`임을 증명).

기본 스택은 그대로 dry-run이며 `main`은 변경하지 않는다(Phase 08은 `phase/08-routing-e2e-compose`에서
작업; `docs/BRANCHING.md`). 실제 Hermes CLI 실행은 agent의 `HERMES_AGENT_EXECUTION_ENABLED=true`로만
켜지며, override는 API 레벨 HTTP 라우팅만 활성화한다.

## Phase 09 — Resource E2E smoke + 명시적 phase upload/release

Phase 08이 routing 경로를 live로 검증했다면, Phase 09는 **리소스 격리**(HDFS workspace/provisioning,
vector/graph scope, cross-department 거부, audit)를 live로 검증하고, git 업로드/릴리스 흐름을 명시적
Makefile 단계로 만들어 `main`이 실수로 갱신되지 않게 한다. 핵심 설계는 **리소스 smoke는 routing과
무관하므로 기본 dry-run 스택에서 그대로 동작**하고, `main`은 오직 명시적 릴리스 단계에서만 움직인다는
것이다. 전체 Phase 상태는 `docs/ROADMAP.md`에 있다.

- **`scripts/smoke_resources_e2e.py` — `ResourceSmoke`** — API `/health` 대기 → seeded admin 로그인 →
  **QC/IT** user **idempotent** 생성·승인 → 다음을 순서대로 검증한다:
  - `GET /workspace/hdfs`가 호출자 본인의 personal root(`/naplatform/users/<username>`)와 본인 부서
    root만 반환하고 타 부서 root는 노출하지 않음(scope 유출 방지);
  - `POST /admin/users/{id}/provision-hdfs`가 dry-run이라 `hdfs dfs` 명령 계획만 반환하고 실행은 하지
    않음(`dry_run=true`, `enabled=false`, `results` empty);
  - vector/graph의 personal·department insert+search가 scope대로 동작하며 QC 레코드가 IT에게 절대
    보이지 않음(다른 owner/부서 metadata 매칭 실패);
  - QC user가 `/vector/IT`·`/graph/IT`·`/resources/IT`에서 모두 `403`으로 cross-department 거부됨;
  - audit 로그에 핵심 이벤트(`vector_insert`/`vector_search`/`graph_insert`/`graph_search`/
    `hdfs_provision`/`workspace_view`/`admin_user_update`/`login`)가 남음.
  **secret 미출력 · 고정 id로 재실행 안전**. 주입된 HTTP client를 받아 Docker 없이 단위 테스트가 가능하다.
- **In-cluster 실행** — Phase 08의 `smoke` 서비스를 재사용하되 command만 리소스 스크립트로 덮어써
  Compose 네트워크 안에서 실행한다(`docker compose ... run --rm smoke python /scripts/smoke_resources_e2e.py`).
- **명시적 phase upload/release** — Makefile에 `push-phase`(PHASE_BRANCH를 origin에 push),
  `merge-phase-to-dev`(PHASE_BRANCH를 dev에 merge·push), `release-dev-to-main`(dev→main, **유일하게
  main을 갱신**하는 단계)을 추가했다. 모든 단계는 `PHASE_BRANCH` 변수(기본값: 현재 브랜치)를 사용하고,
  릴리스 단계 외에는 `main`을 절대 건드리지 않는다.
- **테스트** — `test_smoke_resources_e2e.py`(fake API `httpx.MockTransport`로 workspace scope,
  dry-run provisioning, vector/graph 격리 및 격리 파손 시 실패, cross-department 거부, audit 완전성,
  signup idempotency, secret 미출력 검증)와, docs/Makefile이 Phase 09 upload 옵션과 현재 phase 상태를
  담고 있는지 확인하는 `test_phase_docs.py`.

기본 스택은 그대로 dry-run이며 `main`은 변경하지 않는다(Phase 09는 `phase/09-resource-e2e-smoke`에서
작업; `docs/BRANCHING.md`, `docs/ROADMAP.md`).

## Phase 10 — Real Qdrant/Neo4j/HDFS backend adapters (기본 memory/dry-run)

Phase 05가 in-memory scaffold로 scope 규칙을 확립했다면, Phase 10은 **동일한 scope/RBAC 계약을
유지한 채** vector/graph/HDFS 백엔드를 env로 선택 가능한 **pluggable** 구조로 만든다. 각 백엔드의
기본값은 memory/dry-run이라 테스트와 기본 스택은 live Qdrant/Neo4j/HDFS 없이 동작하며, 실제 드라이버
경로는 fake client/driver/runner로 검증한다. 전체 Phase 상태는 `docs/ROADMAP.md`에 있다.

```
VECTOR_BACKEND=memory|qdrant   GRAPH_BACKEND=memory|neo4j   HDFS_PROVISIONING_ENABLED=true|false
        │                              │                              │
   VectorScopeAdapter(mem)        GraphScopeAdapter(mem)         HdfsProvisioner (dry-run)
   QdrantVectorBackend  ──▶ qdrant-client   Neo4jGraphBackend ──▶ neo4j driver   health() 준비성 probe
        (동일 Filter descriptor)        (parameterised Cypher only)     (상수 안전 argv)
```

- **백엔드 선택 env(기본 memory/dry-run)** — `VECTOR_BACKEND`, `GRAPH_BACKEND`, 기존
  `HDFS_PROVISIONING_ENABLED`. resolver는 잘못된 값·드라이버 미설치·미구성 시 **경고 로그와 함께
  memory로 안전 폴백**하므로, 백엔드 env 때문에 API 기동이 실패하지 않는다. 엄격 factory
  (`build_vector_backend`/`build_graph_backend`)는 알 수 없는 mode를 거부한다.
- **`app/vector.py` — `QdrantVectorBackend`** — 설치·구성 시 `qdrant-client` 사용(`QDRANT_URL`,
  선택적 `QDRANT_API_KEY`). collection이 없으면 `QDRANT_VECTOR_SIZE`(기본 768)/`QDRANT_DISTANCE`
  (기본 Cosine)로 **on-demand 생성**하고, memory 어댑터와 **동일한 metadata·Qdrant `Filter`
  descriptor**로 upsert/search한다. 저장은 client wrapper로 위임되어 테스트가 fake를 주입한다.
- **`app/graph.py` — `Neo4jGraphBackend`** — 설치·구성 시 `neo4j` driver 사용(`NEO4J_URI`/
  `NEO4J_USER`/`NEO4J_PASSWORD`). **parameterised Cypher만** 실행한다 — 모든 scope 값·사용자
  property는 `$param`으로 bind되고, inline되는 식별자는 엄격 검증된 label/relationship type뿐이다.
- **`app/hdfs.py` — `HdfsProvisioner.health()`** — **상수 안전 argv**(`hdfs dfs -test -d /naplatform`,
  사용자 입력·shell string 없음)로 만든 readiness probe. 기본은 dry-run(계획만, 미실행)이고,
  provisioning이 켜졌을 때만 주입/subprocess runner로 실행한다.
- **`GET /admin/backends/status`(admin 전용)** — 활성 vector/graph/hdfs mode, **redacted** 연결 URL,
  dry-run/fake health를 반환한다. api key/password는 boolean(`api_key_set`/`password_set`)으로만
  노출되어 secret이 새지 않는다.
- **테스트** — `test_backends.py`(fake 기반): memory 기본 불변, Qdrant upsert/search/filter/collection
  생성, Neo4j params/no-interpolation, backends status admin 전용·secret 미출력, HDFS health 계획,
  invalid env 거부·안전 폴백. **live Qdrant/Neo4j/HDFS 불필요.**

`/vector/{department}`·`/graph/{department}` API 계약은 불변이며, env가 선택한 백엔드를 사용하되
기본은 memory다. 기본 스택은 memory/dry-run이며 `main`은 변경하지 않는다(Phase 10은
`phase/10-real-backend-adapters`에서 작업; `docs/BRANCHING.md`, `docs/ROADMAP.md`).

## Phase 11 — core-webui auth/session UI integration adapter (테스트에 live UI 불필요)

로그인 이후 실제 UI는 외부 저장소 `github.com/JacobYim/core-webui`이며 이 repo에 vendor하지
않는다. Phase 11은 그 UI의 login/signup/session/부서 선택 흐름을 NAPlatform API에 연결하는
**repo가 소유한 adapter**를 추가하여, 브라우저나 외부 체크아웃 없이 통합을 여기서 테스트한다.
핵심 설계는 **API가 보안 경계**이고 adapter는 그 RBAC 결과(pending→승인 대기 UX, 비-member
부서→거부)를 UX로 반영만 할 뿐 접근 결정을 내리지 않는다는 것이다.

```
Browser (core-webui)
   │  services/ui/adapter/naplatform-adapter.js  (ES module, in-memory token)
   ▼
POST /auth/signup ─▶ pending          GET /auth/departments/options (public)
POST /auth/login  ─▶ token(active) / 403(pending·disabled)
GET  /auth/me  ≡  GET /core-webui/session ─▶ bootstrap + department_routes[] + approval
GET  /core-webui/session/status ─▶ any valid session: can_access + approval  (401 if expired)
POST /core-webui/session/select-department ─▶ route  (403 non-member / 400 unknown)
POST /auth/logout ─▶ Redis/memory 세션 무효화 (idempotent)
```

- **Adapter 패키지 (`services/ui/adapter/`)** — 의존성 없는 ES module
  (`naplatform-adapter.js`), 계약의 단일 소스인 `contract.json`(JS adapter와 Python
  테스트가 함께 읽어 drift 방지), 정적 `index.html` 데모, `package.json`, `README.md`.
  세션 토큰은 메모리에만 있고 어떤 adapter 파일에도 secret이 embed되지 않는다(테스트가 강제).
- **API 지원 endpoint(기존 계약 보존, 추가만)** — `GET /auth/me`(`/core-webui/session`
  alias), `GET /auth/departments/options`(public), `POST /auth/logout`(세션 무효화),
  `GET /core-webui/session/status`(모든 유효 세션의 상태 → 승인 대기 UX; 만료/무효 세션은 `401`),
  `POST /core-webui/session/select-department`(부서 membership 검증 후 chat/context/resource
  route 반환; 비-member `403`, unknown `400`).
- **세션 부트스트랩 모델 (`app/webui.py`)** — 부서 route(그래서 UI가 chat을
  `/agents/{department}/chat`로 라우팅), public 부서 옵션, 승인 대기 UX 계약을 만드는 순수
  helper. `/core-webui/session`·`/auth/me` 응답에 `session_status`, `chat_route_template`,
  `department_routes[]`, `approval`이 **추가**된다(기존 소비자는 무시).
- **pending/inactive 처리** — 신규 signup은 `pending`이라 로그인 불가(`403`); 로그인 후 권한이
  회수되면 `/core-webui/session`은 `403`이지만 `/core-webui/session/status`는 `can_access:false`
  승인 계약을 반환; 만료/무효 세션은 어디서나 `401`.

pytest 커버리지: `test_webui_session.py`(login 성공, pending 로그인 차단 + 승인 대기 status,
부서 옵션, logout 무효화, member/비-member 부서 선택, `/auth/me` alias, 만료 세션 401),
`test_webui_adapter_contract.py`(`contract.json`의 모든 endpoint가 올바른 method/응답 형태로 앱에
존재, JS adapter가 모든 contract path를 참조, adapter 파일에 token/secret 미유출). 기본 스택과
`main`은 불변이다(Phase 11은 `phase/11-core-webui-auth-session-integration`에서 작업).
