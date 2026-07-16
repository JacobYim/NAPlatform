# Container Guide

## All-in-one
```bash
docker compose up --build
```

## Infra only
```bash
docker compose up -d postgres redis qdrant neo4j hdfs-namenode hdfs-datanode-1 hdfs-datanode-2 hdfs-datanode-3
```

## Init HDFS
```bash
docker compose run --rm hdfs-init
```

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
