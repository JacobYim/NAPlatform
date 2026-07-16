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

향후 Phase에서는 NAPlatform 로그인/승인 세션을 core-webui에 SSO/adapter로 연결해,
로그인 후 core-webui가 `/agents/{department}/context`에서 받은 허용 tool/MCP/HDFS/vector/graph scope만 사용하게 한다.
