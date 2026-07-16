---
name: sync-env-docker
description: Strictly verify any generated app service against its audited env contract and running Compose dependencies, or generate a local-host env file. Use for env drift, login/auth failure, connection issues, or bootstrap readiness.
---

# sync-env-docker

## Verify

```bash
./scripts/sync-env-docker.py verify <service> [--skip-infra]
```

The service name is dynamic; it is not limited to hardcoded boundaries. All scaled
replicas, including stopped/crashed ones, are resolved through
`com.docker.compose.service` and project labels; transient Compose one-off
containers are excluded.

The verifier reads `infra/contracts/env.json`, generated from the union of app env
example, production config reads, validation schema and Prisma. It checks:

- `MISSING`, `PLACEHOLDER`, expected regex `MISMATCH`;
- strict `EXTRA` outside a small platform allowlist;
- `SECRET_DRIFT` against `infra/.env` without printing either value;
- container health/readiness for every replica;
- Compose aliases and internal listener ports (including ports declared in command
  arguments such as Redpanda `--kafka-addr`, not only Docker `EXPOSE`);
- PostgreSQL role/database provisioning from `contracts/postgres.json`, with a
  backwards-compatible pg-init parser for hyphenated identifiers and psql vars;
- Keycloak realm, client, role and service-account token probes;
- Kafka topic existence, partition/replication/config shape, and strict
  `auto_create_topics_enabled=false` state.

Every reported problem is a non-zero exit. `--skip-infra` keeps the strict app env,
secret and container-health checks but skips sibling dependency probes.

## Generate local env

```bash
./scripts/sync-env-docker.py gen-local <service> [--out <path>]
```

This substitutes generated internal service endpoints with actual published
`localhost` ports for running the app outside Docker. It never prints secrets.

## Rules

1. Verify is read-only and never patches app contracts or running containers.
2. Secret output is always redacted; mismatch messages show names only.
3. App container names must not be forced with `container_name`; label lookup keeps
   Compose project isolation and scaling valid.
4. A generated contract is authoritative. Fallback source audit is used only for
   older installations that do not yet have `infra/contracts/env.json`.

## Related

- [[docker-bootstrap]]: invokes verify as the fatal final gate
- [[infra-init]]: produces the shared contract
- [[infra-up]]: reconciles and starts dependencies before verify
