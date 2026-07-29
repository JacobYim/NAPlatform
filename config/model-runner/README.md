# Shared Docker Model Runner config

Every department Hermes agent (ER / IT / EHS / QC), the API, and WebUI chat share
one selected Docker Model Runner/OpenAI-compatible endpoint at a time. The
selection lives in [`model-runner.env`](model-runner.env), a repo-controlled,
non-secret env file loaded by `docker-compose.model-runner.yml` via `env_file:`.

## Candidate list

Use numbered candidates and choose the default by index:

```env
DOCKER_MODEL_RUNNER_DEFAULT_INDEX=0

DOCKER_MODEL_RUNNER_0_NAME=local-docker-desktop
DOCKER_MODEL_RUNNER_0_BASE_URL=http://host.docker.internal:12434/engines/v1
DOCKER_MODEL_RUNNER_0_MODEL=gemma4:31b
DOCKER_MODEL_RUNNER_0_WEBUI_MODEL=docker.io/ai/gemma4:31B

DOCKER_MODEL_RUNNER_1_NAME=workstation-192-168-100-10
DOCKER_MODEL_RUNNER_1_BASE_URL=http://192.168.100.10:12434/engines/v1
DOCKER_MODEL_RUNNER_1_MODEL=gemma4:31b
DOCKER_MODEL_RUNNER_1_WEBUI_MODEL=docker.io/ai/gemma4:31B
```

Selection rule:

1. `DOCKER_MODEL_RUNNER_DEFAULT_INDEX` chooses `DOCKER_MODEL_RUNNER_<N>_*`.
2. If that index is incomplete, the first complete candidate by index order is used.
3. One-off runtime env vars still override the file:
   - `DOCKER_MODEL_RUNNER_BASE_URL`
   - `DOCKER_MODEL_RUNNER_MODEL`
   - `HERMES_WEBUI_DEFAULT_MODEL`
4. No passwords, tokens, or API keys belong in this file.

`DOCKER_MODEL_RUNNER_<N>_WEBUI_MODEL` exists because Docker Model Runner may
accept the repo contract model as `gemma4:31b` in department-agent normalization,
while WebUI's direct Hermes runtime often needs the exact served ID such as
`docker.io/ai/gemma4:31B`.

## Built-in examples

- **Index 0** — current workstation / Docker Desktop Model Runner reachable from
  containers through `host.docker.internal:12434`.
- **Index 1** — another workstation on the lab network:
  `http://192.168.100.10:12434/engines/v1`.

To use the network workstation by default:

```env
DOCKER_MODEL_RUNNER_DEFAULT_INDEX=1
```

## Run it

```bash
# validate config (no runner call required)
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml config
make compose-config-model-runner

# start with the selected shared runner ON
docker compose -f docker-compose.yml -f docker-compose.model-runner.yml up -d --build
make up-model-runner
```

To disable model runner entirely, do not pass `docker-compose.model-runner.yml`.
The base stack remains dry-run/model-less and does not start a model-runner
service of its own.
