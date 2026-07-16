---
name: docker-bootstrap
description: One-shot local-dev setup from source audit through provisioning, migration, app readiness, strict verification, and URLs. Use for "bootstrap", "chạy toàn bộ dự án", "one-click up", onboarding, or a fresh clone. Never deletes data.
---

# docker-bootstrap

Runs the complete local-development path:

```text
audit/review -> generate -> base infra -> Postgres reconcile
             -> Keycloak/Kafka reconcile -> Prisma migrate once
             -> apps ready -> strict verify/smoke -> URLs
```

## Command

```bash
./scripts/bootstrap.sh [--build] [--recreate] [--skip-init] [--skip-verify]
                       [--yes] [--init-config <json>]
```

- `--build`: re-evaluate app builds; Docker cache still applies.
- `--recreate`: recreate long-running containers.
- `--skip-init`: fail if generated infra does not already exist.
- `--skip-verify`: explicit escape hatch; skips the final readiness contract gate.
- `--yes`: no shell prompts, materialize only reviewed local-owned secrets, and use safe detected defaults.
- `--init-config`: use reviewed audit choices and continue without shell prompts.

## Agent execution policy

The goal is autonomous execution, not autonomous guessing.

When `infra/` is missing, first run the read-only audit:

```bash
./scripts/infra-init.py --root <project-root> --detect-json
```

Use facts supported by source directly. Consolidate every item in `uncertainties`
into one Grill Me round, recommend an answer and explain its impact. Also ask when
a required external service/value, Keycloak ownership/permission, or shared DB
ownership cannot be derived. Do not ask about information already proven by the
audit. Apply answers to `suggested_config`, then run:

```bash
./scripts/bootstrap.sh --init-config <reviewed.json>
```

If the user explicitly accepts all recommendations and the audit has no unresolved
required value/ownership, continue automatically. `--yes` never grants permission
to invent a realm, client, secret, external endpoint, or production policy.

## Guarantees

1. No data deletion. Existing PostgreSQL volumes are reconciled live; a Prisma
   `P3009` remains fatal with `migrate resolve` guidance.
2. Provisioning is idempotent. A second run reconciles instead of silently relying
   on first-volume-only init/import behavior.
3. Prisma migrations run in one-shot services before apps, never in every replica.
4. Required env, provisioning, migration, container readiness, strict verification,
   Keycloak client-credential smoke and Kafka topic checks are fatal.
5. Generated Keycloak realms grant broad local admin rights and wildcard browser
   origins only for local dev. They are not staging/production manifests.
6. External app credentials remain `REPLACE_ME_*` and stop bootstrap until supplied;
   only `GENERATE_ME_*` values owned by the local stack are randomized.

## Related

- [[infra-init]]: audit and generator
- [[infra-up]]: provisioning/start orchestrator
- [[sync-env-docker]]: strict post-start verifier
- [[infra-down]]: explicit teardown
