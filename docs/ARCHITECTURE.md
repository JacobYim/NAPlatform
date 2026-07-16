# Architecture

```text
Browser -> NAPlatform login/admin API -> core-webui runtime UI
UI/API -> FastAPI API -> Redis sessions
                   -> Postgres auth/RBAC/audit
                   -> HDFS NameNode + 3 DataNodes
                   -> Qdrant shared vector DB
                   -> Neo4j shared graph DB
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
