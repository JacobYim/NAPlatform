# Architecture

```text
UI -> FastAPI API -> Redis sessions
                -> Postgres auth/RBAC/audit
                -> HDFS NameNode + 3 DataNodes
                -> Qdrant shared vector DB
                -> Neo4j shared graph DB
                -> Hermes ER/IT/EHS/QC agents
```

보안 경계는 프롬프트가 아니라 API RBAC, HDFS ACL/Kerberos, DB query filter, container/user isolation이다. Production에서는 Kerberos principal/proxy-user와 HDFS ACL로 디렉토리 접근을 강제한다.
