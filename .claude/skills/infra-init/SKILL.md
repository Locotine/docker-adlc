---
name: infra-init
description: Scaffold `infra/` directory (shared Docker compose + Dockerfiles + env template + README) for a multi-service project. Interactive or fully automatic with `--yes` — scans sibling folders for candidate services (Node/Python/Go), reads their tech stack (deps in package.json, requirements.txt, go.mod), and selects the needed infra modules (postgres, redis, kafka/redpanda, keycloak, temporal, minio, elasticsearch). Use when starting a new multi-service project, or when the user says "khởi tạo infra", "scaffold docker cho dự án", "tạo compose cho project mới", "generate infra folder".
---

# infra-init

Interactive or non-interactive scaffold for shared Docker infrastructure across multiple app services in a repo.

## When to invoke

Trigger this skill when the user wants to bootstrap a Docker setup for a multi-service repo:
- "khởi tạo infra", "scaffold docker cho dự án mới", "tạo compose"
- "generate infra folder", "init docker stack", "bootstrap monorepo docker"
- New folder cloned/created, sibling services exist, but no `infra/` yet

Do **not** trigger for: adding a single service to an existing infra (edit `docker-compose.apps.yml` directly), or when the user is on a single-service project (use their own `Dockerfile` + `docker-compose.yml`).

## Command

```bash
./scripts/infra-init.py [--root <path>] [--force] [--yes | --detect-json | --config <json>]
```

Flags:
- `--root <path>` — project root to scan (default: parent of `scripts/`)
- `--force` — overwrite existing `infra/` (backs up to `infra.backup.<timestamp>/`)
- `--yes` / `-y` — include all supported detected services, use detected/default ports and root-derived project/network names, enable detected infra modules, and write without prompting. Postgres is also enabled when required by Keycloak or Temporal. Fails early when a selected Python/Go service has no Dockerfile instead of generating a broken compose plan.
- `--detect-json` — read-only preflight. Prints candidates, suggested choices, and uncertainty records without prompting or writing.
- `--config <json>` — generate without prompts from a reviewed config. Accepts either the full `--detect-json` report or its `suggested_config` object.

## Workflow

1. **Scan**: walks project root, finds candidate services (folders with `package.json` / `pyproject.toml` / `requirements.txt` / `go.mod`).
2. **Detect tech stack** per service from deps:
   - `prisma|pg|typeorm` → postgres
   - `redis|ioredis|bullmq` → redis
   - `kafkajs|@confluentinc/kafka-javascript` → redpanda (kafka)
   - `openid-client|jose|passport-jwt|keycloak-*` → keycloak
   - `@temporalio/*` → temporal
   - `@aws-sdk/client-s3|minio` → minio
   - `@elastic/elasticsearch` → elasticsearch
3. **Plan** interactively, or automatically with `--yes`:
   - Compose project name + docker network name
   - Include/exclude each detected service, set host port + container port
   - Enable/disable each detected infra module (allowing user to override auto-detect)
4. **Preview plan** then confirm.
5. **Write** to `infra/`:
   - `docker-compose.infra.yml` — only enabled infra modules
   - `docker-compose.apps.yml` — app services with env wiring (DATABASE_URL, KAFKA_BROKERS, etc.) matching their detected tech
   - `dockerfiles/Dockerfile.<service>` — Node template (only for services without existing Dockerfile)
   - `.env.example` — with cheatsheet for minting Keycloak client secrets
   - `.gitignore` — excludes `.env`
   - `README.md` — port map + up/down commands
   - `pg-init/00-multi-db.sh` — creates DB + role per service (if postgres enabled)
   - `keycloak-realms/README.md` — placeholder for realm exports (if keycloak enabled)

## Behaviour rules

1. **Non-destructive**: refuses to write if `infra/` exists. `--force` backs up first, never deletes silently.
2. **Automation-safe**: `--yes` and `--config` never read stdin. Agents should use `--detect-json`, ask the user only about reported or evidence-based uncertainties, then use `--config`. Bare `--yes` is for CI or explicit acceptance of all detected defaults. Without these flags, every choice remains interactive.
3. **Node-first**: only generates Dockerfile for Node services. Python/Go services detected but Dockerfile is user's responsibility.
4. **Env wiring per service**: apps compose only wires env vars for infra modules the service actually needs (based on deps). Avoids polluting service env with unused KAFKA_BROKERS etc.

## After running

User should:
1. `cd infra && cp .env.example .env` and mint real secrets
2. Drop Keycloak realm exports into `infra/keycloak-realms/` if keycloak enabled
3. `../scripts/infra-up.sh` to bring everything up
4. `../scripts/sync-env-docker.py verify <service>` to check env

## Related

- [[infra-up]] / [[infra-down]] / [[docker-apps-up]] — lifecycle
- [[sync-env-docker]] — env verification after scaffolding
