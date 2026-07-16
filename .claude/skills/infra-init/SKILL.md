---
name: infra-init
description: Audit a multi-service repository and generate local Docker Compose, contract-driven app env, Prisma-aware Node images, Postgres schema reconciliation, and Keycloak/Kafka provisioning. Use for new/fresh projects without infra/.
---

# infra-init

## Command

```bash
./scripts/infra-init.py [--root <path>] [--force]
                        [--yes | --detect-json | --config <json>]
```

- `--detect-json`: read-only facts, suggested reviewed config, and uncertainties.
- `--config`: generate non-interactively from the full detect report or its
  `suggested_config`.
- `--yes`: use only safe defaults; missing required values/start scripts or
  ambiguous Keycloak ownership still fail and require reviewed config.
- `--force`: move existing `infra/` to a timestamped backup, regenerate, then
  preserve old `.env` values and append newly required keys.

## Audit sources

For every service, the env contract is the union of:

- `.env.example` values and `# expected KEY=<regex>` declarations;
- production-source `getOrThrow`, `get`, `process.env`, and Joi/Zod-style schemas;
- Prisma `env("KEY")` declarations.

Tests, mocks, fixtures, generated output and comments are excluded from source
topic scanning. Versioned Kafka topic literals and `KAFKA_*TOPIC*` values are
unioned. Env/example drift is reported; the generator never edits app source or
`.env.example` automatically.

## Generated behavior

- Env keys come from the audited contract, not from dependency guesses.
- Local infra endpoints are rewritten by key semantics; app-to-app URLs use the
  discovered service graph or explicit `service_refs`, never port-only guessing.
- Secrets become required `${VAR:?...}` references in ignored `infra/.env`.
  The reviewed per-service `secret_keys` list is the explicit classifier override.
  App secrets are external by default; only keys explicitly listed in
  `generated_secret_keys` may be minted locally.
- External endpoints remain visible; required unavailable services fail strict
  verification instead of silently receiving stubs.
- Generated Node images use the lowest major satisfying `engines.node` (Node 24
  LTS fallback), Debian slim in every stage, `/usr/bin/tini`, and the reviewed npm
  production script.
- Prisma images copy `prisma/` before install, install OpenSSL/CA certificates and
  run `prisma generate` explicitly. Migration is a separate one-shot service.
- Health paths are statically detected with readiness preferred. Config is
  authoritative; unknown health means no fabricated `/health` check.
- PostgreSQL provisioning reconciles roles, DBs and every Prisma multiSchema on
  both fresh and existing volumes. It never appends `?schema=` to `DATABASE_URL`;
  DB passwords are percent-encoded into a derived URL-only env value.
- Temporal sets `SKIP_DB_CREATE=true` only for generated internal PostgreSQL.
- Generated Keycloak reconciliation is local-dev only and uses Admin API updates.
- Kafka topics are explicit; strict auto-create is disabled only when a complete
  non-empty topic contract exists. Existing partition/replication/config drift is
  reconciled when safe or fails explicitly.

## Files

```text
infra/
  docker-compose.infra.yml
  docker-compose.apps.yml
  contracts/{env,postgres,keycloak,kafka}.json
  provision/{keycloak.py,kafka.sh}
  pg-init/00-multi-db.sh
  dockerfiles/Dockerfile.<service>
  .env.example
  .gitignore
  README.md
```

## Safety and review

Invalid names, image/secret collisions, occupied ports, fixed UID/GID Dockerfiles,
unsupported missing Dockerfiles, missing required env values, absent npm start
scripts and ambiguous realm/client/role ownership are reported before writing.
Do not convert a generated local realm into a production realm by assumption.

## Related

- [[docker-bootstrap]]: full reviewed flow
- [[infra-up]]: provision/migrate/start existing generated infra
- [[sync-env-docker]]: verify generator output against running containers
