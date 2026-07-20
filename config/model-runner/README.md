# Shared Docker Model Runner config (Phase 15)

Every department Hermes agent (ER / IT / EHS / QC) **and** the API share **one**
model — `gemma4:31b` — over an OpenAI-compatible endpoint. Phase 15 moves the model
+ endpoint out of the compose file and into a **repo-controlled, non-secret env
file** so you can point the stack at whichever runner you actually have, and fixes
the earlier _"Gemma4:31B connection cannot be reached"_ failure.

## The env file

[`model-runner.env`](model-runner.env) is loaded by
`docker-compose.model-runner.yml` via `env_file:` on the API and on every
`hermes-*` agent. It holds exactly two keys (no secrets):

```env
DOCKER_MODEL_RUNNER_BASE_URL=http://host.docker.internal:12434/engines/v1
DOCKER_MODEL_RUNNER_MODEL=gemma4:31b
```

`DOCKER_MODEL_RUNNER_MODEL` is `gemma4:31b` and is shared by all agents — one value,
no per-agent drift. Change `DOCKER_MODEL_RUNNER_BASE_URL` to select the endpoint.

## Two modes

### Mode 1 — External endpoint (default)

Point `DOCKER_MODEL_RUNNER_BASE_URL` at any reachable OpenAI-compatible endpoint
serving `gemma4:31b`. The compose override adds
`extra_hosts: ["host.docker.internal:host-gateway"]`, so inside the containers the
Docker **host** is always reachable as `host.docker.internal`. Docker Desktop's
Model Runner is exposed on host TCP **12434** when enabled, so the default
`http://host.docker.internal:12434/engines/v1` reaches it via the host gateway even
when the internal DNS name does not resolve. For a separate inference box:

```env
DOCKER_MODEL_RUNNER_BASE_URL=http://my-inference-host:8000/engines/v1
```

### Mode 2 — Docker Desktop local Model Runner (internal DNS)

If you run Docker Desktop 4.40+ with Model Runner enabled and prefer the in-network
DNS name (resolvable only inside the Compose network):

```env
DOCKER_MODEL_RUNNER_BASE_URL=http://model-runner.docker.internal/engines/v1
```

```bash
docker desktop enable model-runner      # or Settings > Beta features
docker model pull gemma4:31b
```

### Mode 3 — No model runner (default stack)

Just **don't** pass `docker-compose.model-runner.yml`. The default
`docker compose up` stays dry-run / model-less and safe — there is **no embedded
model-runner service**, so the stack never tries to start a model runner on its own.

## Run it

```bash
# validate (no runner needed — env file + defaults keep `config` valid)
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml config
make compose-config-model-runner

# start with the shared runner ON
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml up -d --build
make up-model-runner
```

Run-time env still wins over the file (e.g. `DOCKER_MODEL_RUNNER_BASE_URL=... docker
compose ...`), so one-off overrides need no file edit.
