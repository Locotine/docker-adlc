---
name: infra-up
description: Bring up Docker infrastructure (shared services + app services) for the current project. Use when the user says "start docker", "up docker", "khởi động dự án", "bật infra", "docker lên đi", "chạy dự án lên", "restart sau khi máy vừa mở". Wraps `docker compose -f infra/docker-compose.infra.yml [-f infra/docker-compose.apps.yml] up -d` and shows status. Auto-discovers project root by walking up for `infra/docker-compose.infra.yml`.
---

# infra-up

Bring up all Docker services for the current project.

## When to invoke

Trigger this skill when the user wants to start their project's Docker stack:
- "up docker", "start docker", "bật docker", "khởi động dự án", "chạy up"
- After a machine reboot / `colima restart` — user wants everything back
- After `git pull` bringing in compose changes — user wants to apply them

Do **not** trigger for: starting a *single* app service (use `docker-apps-up` instead), starting non-Docker processes, or CI-style non-interactive up (that's a direct `docker compose` call).

## Command

```bash
./scripts/infra-up.sh [--build] [--infra-only] [--recreate]
```

Flags:
- `--build` — rebuild images before starting (needed after Dockerfile changes)
- `--infra-only` — only postgres/redis/keycloak/… — skip app services
- `--recreate` — force recreate containers even if config unchanged (needed after env value changes)

Run from anywhere inside the project; script walks up to find `infra/docker-compose.infra.yml`.

## Preconditions

1. `infra/.env` should exist if apps compose has `${VAR:?...}` references. Script prints a warning if missing but continues (infra alone will start; apps will fail).
2. Docker daemon reachable (Colima / Docker Desktop running).

## Post-conditions

Prints `docker compose ps` with names, statuses, and port bindings so user can copy the host URL directly.

## Related

- [[infra-down]] — tear down
- [[docker-apps-up]] — up only app services (infra already running)
- [[infra-init]] — scaffold `infra/` for a new project
- [[sync-env-docker]] — verify env after start
