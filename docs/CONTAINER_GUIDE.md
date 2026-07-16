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
