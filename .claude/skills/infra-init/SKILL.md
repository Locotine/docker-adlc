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
- `--yes` / `-y` — include all supported detected services, allocate currently available host ports, use normalized root-derived project/network names, enable detected infra modules, and write without prompting. Postgres is also enabled when required by Keycloak or Temporal. Fails early when a selected Python/Go service has no Dockerfile instead of generating a broken compose plan.
- `--detect-json` — read-only preflight. Prints candidates, available app/infra port suggestions, Dockerfile strategy, and uncertainty records without prompting or writing.
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
   - Choose `service` or `generated` Dockerfile per Node service
   - Enable/disable each detected infra module (allowing user to override auto-detect)
   - Set host ports per infra endpoint through `infra_ports`; container ports stay stable
4. **Preview plan** then confirm.
5. **Write** to `infra/`:
   - `docker-compose.infra.yml` — only enabled infra modules
   - `docker-compose.apps.yml` — app services with env wiring (DATABASE_URL, KAFKA_BROKERS, etc.) matching their detected tech
   - `dockerfiles/Dockerfile.<service>` — Node template when the reviewed service choice is `generated`
   - `.env.example` — with cheatsheet for minting Keycloak client secrets
   - `.gitignore` — excludes `.env`
   - `README.md` — port map + up/down commands
   - `pg-init/00-multi-db.sh` — creates DB + role per service (if postgres enabled)
   - `keycloak-realms/README.md` — placeholder for realm exports (if keycloak enabled)

## Behaviour rules

1. **Non-destructive**: refuses to write if `infra/` exists. `--force` backs up first, never deletes silently.
2. **Automation-safe**: `--yes` and `--config` never read stdin. Agents should use `--detect-json`, ask the user only about reported or evidence-based uncertainties, then use `--config`. Bare `--yes` is for CI or explicit acceptance of all detected defaults. Without these flags, every choice remains interactive. Conflicting app/infra service names and normalized Postgres secret-key collisions are reported for Grill Me and blocked until the reviewed plan renames, excludes, or disables the conflict.
3. **Host-aware ports**: app and infra host ports share one allocation set and are probed against current TCP listeners. Occupied preferred ports are moved to the next available port and reported as `host_port_unavailable` for review. Internal Compose ports do not change.
4. **Docker-safe names**: Compose project/network names and generated image repository components are normalized; image paths are always lowercase even when service folders are uppercase. App services do not set global `container_name`, so Compose scopes their container names to the project.
5. **Node-first**: only generates Dockerfile for Node services. Missing Node Dockerfiles and existing Dockerfiles that create fixed numeric UID/GID identities are reported as uncertainties. The reviewed config selects `services.<name>.dockerfile` as `service` or `generated`; Python/Go Dockerfiles remain the user's responsibility.
6. **Env wiring per service**: apps compose only wires env vars for infra modules the service actually needs (based on deps). Avoids polluting service env with unused KAFKA_BROKERS etc.

## After running

User should:
1. `cd infra && cp .env.example .env` and mint real secrets
2. Drop Keycloak realm exports into `infra/keycloak-realms/` if keycloak enabled
3. `../scripts/infra-up.sh` to bring everything up
4. `../scripts/sync-env-docker.py verify <service>` to check env

## Related

- [[infra-up]] / [[infra-down]] / [[docker-apps-up]] — lifecycle
- [[sync-env-docker]] — env verification after scaffolding
