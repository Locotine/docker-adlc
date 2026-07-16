---
name: docker-bootstrap
description: One-shot end-to-end setup + start for the project. Chains infra-init (if `infra/` missing) → env check → infra-up → sync-env-docker verify (per app) → print host URLs. Use when the user says "bootstrap", "chạy toàn bộ dự án", "up hết", "onboarding project mới", "one-shot start", "one-click up", "khởi động cả stack", "clone xong rồi làm gì", or is a new developer picking up the repo for the first time. NEVER tears anything down — use [[infra-down]] for that.
---

# docker-bootstrap

Chains all the "up" skills into a single command so a new dev (or the same dev after a reboot) can go from clean checkout to running stack in one step.

## When to invoke

Trigger this skill whenever the user wants the full happy-path start:
- "bootstrap dự án", "chạy hết lên", "one-click start", "clone xong up thế nào"
- New machine / fresh checkout / after `colima restart`
- Onboarding a new dev — this replaces a README walkthrough

Do **not** trigger for: single-service restart (use [[docker-apps-up]]), tear down ([[infra-down]]), or just checking env ([[sync-env-docker]]).

## Command

```bash
./scripts/bootstrap.sh [--build] [--recreate] [--skip-init] [--skip-verify] [--yes] [--init-config <json>]
```

Flags:
- `--build` — forwarded to infra-up; rebuild images
- `--recreate` — forwarded to infra-up; force recreate containers
- `--skip-init` — do NOT scaffold even if `infra/` missing (fail loudly instead)
- `--skip-verify` — skip the per-service env verify step
- `--yes` / `-y` — run end-to-end without prompts. Includes every supported detected service, uses detected/default ports and project name, enables auto-detected infra modules, confirms file generation, and materializes random local secrets from `.env.example` when needed. Fails early if a selected Python/Go service has no Dockerfile.
- `--init-config <json>` — use choices already reviewed by the user for the nested infra-init step; implies `--yes` for the rest of bootstrap.

## Flow (5 steps)

1. **scaffold** — if `infra/docker-compose.infra.yml` is missing, run [[infra-init]]. With `--yes`, it uses detected defaults without prompting. Skip if already present.
2. **env check** — if `infra/.env` missing but `.env.example` exists → offer to copy. Warn that secrets still need real values.
3. **up** — call [[infra-up]] with forwarded `--build` / `--recreate`.
4. **verify** — for each service in `docker-compose.apps.yml`, call [[sync-env-docker]] `verify`, aggregate report. Non-fatal (env issues warn, not fail).
5. **urls** — parse `docker compose ps`, print `http://localhost:<host-port>` per service.

## Behaviour rules

1. **Never destructive** — docker-bootstrap only creates and starts. Data volumes untouched. For teardown use [[infra-down]].
2. **Idempotent** — safe to re-run: init step skips if already exists, up step is a no-op if containers already healthy.
3. **Halts on hard errors** — if `infra-init` or `infra-up` fails, docker-bootstrap exits with their status. Verify problems are reported but not fatal.
4. **Interactive by default at the shell** — direct use without flags allows manual customization. Use `--yes` for a fully non-interactive run.

## Agent execution policy

The goal is **autonomous execution, not autonomous guessing**. Never hand the command back to the user just because a script was historically interactive.

If `infra/docker-compose.infra.yml` already exists, run and monitor this yourself:

```bash
./scripts/bootstrap.sh --yes
```

If infra is missing, first run the read-only preflight yourself:

```bash
./scripts/infra-init.py --root <project-root> --detect-json
```

The report contains `suggested_config` and `uncertainties`.

1. Treat direct evidence as settled: detected service metadata, unique declared ports, unambiguous dependency-to-module matches, and the root-derived project/network name.
2. Treat every reported uncertainty as requiring review. Also add an uncertainty when repository evidence conflicts or when proceeding would require guessing a secret, realm/client identity, external port, or product-specific module.
3. If uncertainties exist, invoke **Grill Me** when available. Otherwise use `AskUserQuestion` with the same pattern: consolidate related questions into one short round, give a recommended answer first, and state the impact of each choice. Do not ask about facts already established by the scan.
4. Apply the answers to `suggested_config`, save the reviewed JSON to a temporary file, then run and monitor:

```bash
./scripts/bootstrap.sh --init-config <reviewed-json>
```

5. If the report has no uncertainties, or the user explicitly said to accept all detected defaults, run `./scripts/bootstrap.sh --yes` directly.

Do not use bare `--yes` to silently resolve reported uncertainties. `--yes` is the execution mechanism after defaults are known to be acceptable; it is not permission to invent product decisions.

Continue diagnosing safe, in-scope failures yourself. Ask the user only when setup needs a missing secret/value that cannot be derived safely, an external permission, or a material configuration choice not covered by defaults.

## Typical output shape

```
===[ 1/4 scaffold infra/ (skipped — already exists) ]===
===[ 2/4 check infra/.env ]===
  infra/.env exists (1 lines)
===[ 3/4 docker compose up ]===
  ==> docker compose ... up -d
  ==> status: (table)
===[ 4/4 sync-env-docker verify ]===
  --- d-bff-auth-client ---
    schema keys: 26  actual keys: 28  problems: 1
    [MISSING] OTEL_EXPORTER_OTLP_ENDPOINT: ...
  --- d-identity-trust ---
    ...
  --- d-taxonomy ---
    ...
  ! verify reported problems (see above).
===[ urls ]===
  d-bff-auth-client          http://localhost:4000
  d-identity-trust           http://localhost:23000
  d-taxonomy                 http://localhost:3300
  dp-keycloak                http://localhost:8080
  ...
done.
```

## Related

- [[infra-init]] — step 1
- [[infra-up]] — step 3
- [[sync-env-docker]] — step 4
- [[docker-apps-up]] — for single-service restarts after docker-bootstrap
- [[infra-down]] — teardown (deliberately NOT chained here)
