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
