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
