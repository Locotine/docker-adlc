---
name: docker-apps-up
description: Bring up ONLY the app services (assumes shared infra already healthy). Use when the user says "restart app", "up service X", "bật lại app", "chỉ up boundary", "khởi động lại d-taxonomy", or wants to rebuild/recreate one or more app containers without touching postgres/redis/keycloak/kafka. Uses `--no-deps` so infra containers are not restarted. Accepts a list of service names to restrict scope.
---

# docker-apps-up

Bring up (or recreate) one or more app services without touching shared infra.

## When to invoke

Trigger this skill when the user wants to:
- Restart just an app after code change: "up lại d-taxonomy", "restart d-bff", "rebuild service X"
- Bring up all apps after infra is already healthy (e.g. after `infra-up --infra-only`)
- Apply env changes to a single app without disturbing others

Do **not** trigger for: starting infra (use [[infra-up]]), or when infra is down (script warns but continues; app will fail to connect).

## Command

```bash
./scripts/docker-apps-up.sh                              # all apps
./scripts/docker-apps-up.sh <service> [<service>...]     # named apps
./scripts/docker-apps-up.sh --build d-taxonomy           # rebuild before start
./scripts/docker-apps-up.sh --recreate d-bff-auth-client # force recreate (apply new env)
```

Flags:
- `--build` — rebuild image before starting
- `--recreate` — force recreate container (needed to pick up new env values from compose or `.env`)

Positional args are service names as declared in `docker-compose.apps.yml`.

## Preconditions

- Shared infra containers are running (script warns if postgres missing).
- `infra/.env` exists if any app references `${VAR:?...}`.

## Related

- [[infra-up]] — full up (infra + apps)
- [[infra-down]] — full teardown
- [[sync-env-docker]] — verify env after recreate
