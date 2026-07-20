# UI Integration

NAPlatform의 실제 로그인 이후 업무 UI는 이 로컬 placeholder가 아니라
`github.com/JacobYim/core-webui`를 사용한다.

`docker-compose.yml`의 `ui` 서비스는 기본적으로 원격 core-webui Git context를
직접 build한다.

```bash
docker compose build ui
```

로컬에서 core-webui를 이미 받아 둔 경우에는 네트워크 build 대신 sibling checkout을
사용할 수 있다.

```bash
git clone https://github.com/JacobYim/core-webui.git ../core-webui
CORE_WEBUI_CONTEXT=../core-webui docker compose up --build ui
```

core-webui 확인 결과:

- white-label browser UI이다.
- `BRAND_NAME`으로 표시명을 바꿀 수 있다.
- `BRAND_LOGO` 또는 `$HERMES_HOME/branding.yaml`로 로고를 바꿀 수 있다.
- 저장소에 `branding/logo.jpg`가 포함되어 있고 이미지 텍스트는 `HMG Metaplant America`이다.

NAPlatform compose에서는 다음 값으로 HMGMA branding을 적용한다.

```yaml
BRAND_NAME: HMGMA
BRAND_LOGO: /apptoo/branding/logo.jpg
```

## Phase 14 — first-run setup 화면 제거 (repo preseed)

새 볼륨에서 core-webui는 http://localhost:3000 첫 접속 시 **initial setup 화면**을 띄운다. Phase 14는
first-run 설정을 저장소(`config/core-webui/`)에서 미리 구성하고, `docker-compose.yml`의 1회성
`ui-preseed` 서비스가 `ui-hermes-home` 볼륨을 공유해 core-webui **서빙 전에** branding·settings·
setup-completed 마커를 시딩한다(`ui`는 `service_completed_successfully`로 대기). 결과적으로 첫 로드가
곧바로 워크스페이스로 열리고 **setup 화면이 없다**. 외부 repo는 vendor/수정하지 않는 non-invasive 방식이며
secret은 어디에도 넣지 않는다. 편집·기동 방법(PowerShell/Bash)은 `config/core-webui/README.md` 참고.

향후 Phase에서는 NAPlatform 로그인/승인 세션을 core-webui에 SSO/adapter로 연결해,
로그인 후 core-webui가 `/agents/{department}/context`에서 받은 허용 tool/MCP/HDFS/vector/graph scope만 사용하게 한다.

## Phase 11 — auth/session 통합 adapter (`adapter/`)

core-webui는 외부 저장소라 여기에 vendor하지 않으므로, 그 UI의 login/signup/session/부서 선택
흐름을 NAPlatform API에 연결하는 **repo 소유 adapter**를 `adapter/`에 둔다. 의존성 없는 ES module
`adapter/naplatform-adapter.js`, 계약 단일 소스 `adapter/contract.json`, 정적 데모 `adapter/index.html`,
`adapter/package.json`, `adapter/README.md`로 구성된다. 세션 토큰은 클라이언트 메모리에만 두고 어떤
파일에도 secret을 넣지 않는다.

adapter가 사용하는 API endpoint(추가만, 기존 계약 보존): `GET /auth/departments/options`,
`POST /auth/login`, `POST /auth/logout`, `GET /auth/me`(= `/core-webui/session`),
`GET /core-webui/session/status`(승인 대기/만료 UX), `POST /core-webui/session/select-department`.
자세한 흐름과 보안 주의는 `adapter/README.md`, live UI 없는 테스트는
`services/api/tests/test_webui_session.py` / `test_webui_adapter_contract.py`를 본다.
