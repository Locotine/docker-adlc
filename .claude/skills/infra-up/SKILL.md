---
name: infra-up
description: Idempotently provision and start generated local Docker infrastructure and apps. Reconciles PostgreSQL, Keycloak and Kafka, runs Prisma migrations once, waits for apps, and never deletes data.
---

# infra-up

## Command

```bash
./scripts/infra-up.sh [--build] [--infra-only] [--recreate]
```

Run from anywhere below the project root.

- `--build`: explicitly re-evaluate builds; Docker cache still applies.
- `--infra-only`: stop after base infra and Postgres/Keycloak/Kafka provisioning.
- `--recreate`: force recreate long-running containers.

`infra/.env` is mandatory. The flow is:

1. Derive percent-encoded DB URL secrets without changing raw PostgreSQL passwords.
2. Start PostgreSQL and wait healthy.
3. Run the live idempotent role/database/schema reconciler.
4. Start remaining infrastructure and wait healthy.
5. Run Keycloak Admin API and Kafka topic reconcilers.
6. Build each app image.
7. Run every generated Prisma migration service once; failure is fatal.
8. Start apps and wait for health/readiness.

Keycloak and Kafka provisioning use one-shot Compose profile services, so the host
needs Docker Compose and Python only; it does not need `rpk` or `kcadm` installed.
If Prisma returns `P3009`, the script prints repair guidance and never wipes the
PostgreSQL volume.

## Related

- [[docker-bootstrap]]: includes strict post-start verification
- [[infra-init]]: creates contracts and provisioners
- [[sync-env-docker]]: verify running state
- [[infra-down]]: explicit teardown
