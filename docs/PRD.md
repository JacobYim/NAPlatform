# NAPlatform PRD

## 목표
ER, IT, EHS, QC 부서별 Hermes Agent를 Docker Compose로 운영하고 로그인 사용자별 권한에 따라 HDFS, tools, MCP, Qdrant, Neo4j 리소스를 분리한다. 로그인 이후 실제 업무 UI는 `github.com/JacobYim/core-webui`를 사용하고, 기존 HMGMA branding/logo를 유지한다.

## 요구사항
- 회원가입/로그인/로그아웃
- 비밀번호 분실 이메일 reset flow
- 회원가입 후 admin 승인 전 사용 불가
- 승인/로그인 이후 실제 업무 UI는 `github.com/JacobYim/core-webui` 사용
- core-webui의 HMGMA logo/white-label 설정 유지
- admin: 사용자 승인, 비활성화, 부서 변경/추가, 비밀번호 변경
- 부서 권한: `/naplatform/departments/{ER|IT|EHS|QC}` 접근
- 개인 권한: `/naplatform/users/{username}` 접근
- HDFS NameNode 1개, DataNode 3개
- Redis 세션 유지
- 단일 Qdrant/Neo4j를 metadata/namespace/ACL로 분리
- 부서별 Hermes agent container: ER/IT/EHS/QC
- 각 agent에 NemoClaw/OpenShell bootstrap hook 제공

## Acceptance Criteria
1. pending 사용자는 로그인/agent 사용 불가
2. active 사용자는 본인 개인 디렉토리 접근 가능
3. 부서 멤버는 부서 공동 디렉토리 접근 가능
4. 타 부서 HDFS/vector/graph/tool/MCP 접근 차단
5. admin은 사용자 상태/부서/비밀번호 변경 가능
6. 로그인 후 core-webui가 HMGMA 브랜드로 표시됨
7. core-webui agent runtime은 NAPlatform API가 발급한 허용 AgentContext만 사용
8. `pytest`와 `docker compose config` 통과
