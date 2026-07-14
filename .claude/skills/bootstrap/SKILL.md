---
name: bootstrap
description: One-shot end-to-end setup + start for the project. Chains infra-init (if `infra/` missing) → env check → infra-up → sync-env-docker verify (per app) → print host URLs. Use when the user says "bootstrap", "chạy toàn bộ dự án", "up hết", "onboarding project mới", "one-shot start", "one-click up", "khởi động cả stack", "clone xong rồi làm gì", or is a new developer picking up the repo for the first time. NEVER tears anything down — use [[infra-down]] for that.
---

# bootstrap

Chains all the "up" skills into a single command so a new dev (or the same dev after a reboot) can go from clean checkout to running stack in one step.

## When to invoke

Trigger this skill whenever the user wants the full happy-path start:
- "bootstrap dự án", "chạy hết lên", "one-click start", "clone xong up thế nào"
- New machine / fresh checkout / after `colima restart`
- Onboarding a new dev — this replaces a README walkthrough

Do **not** trigger for: single-service restart (use [[docker-apps-up]]), tear down ([[infra-down]]), or just checking env ([[sync-env-docker]]).

## Command

```bash
./scripts/bootstrap.sh [--build] [--recreate] [--skip-init] [--skip-verify] [--yes]
```

Flags:
- `--build` — forwarded to infra-up; rebuild images
- `--recreate` — forwarded to infra-up; force recreate containers
- `--skip-init` — do NOT scaffold even if `infra/` missing (fail loudly instead)
- `--skip-verify` — skip the per-service env verify step
- `--yes` / `-y` — assume yes to bootstrap's own prompts (init trigger, env copy). Does NOT affect the inner interactive infra-init prompts.

## Flow (5 steps)

1. **scaffold** — if `infra/docker-compose.infra.yml` missing → prompt to run [[infra-init]] (interactive). Skip if already present.
2. **env check** — if `infra/.env` missing but `.env.example` exists → offer to copy. Warn that secrets still need real values.
3. **up** — call [[infra-up]] with forwarded `--build` / `--recreate`.
4. **verify** — for each service in `docker-compose.apps.yml`, call [[sync-env-docker]] `verify`, aggregate report. Non-fatal (env issues warn, not fail).
5. **urls** — parse `docker compose ps`, print `http://localhost:<host-port>` per service.

## Behaviour rules

1. **Never destructive** — bootstrap only creates and starts. Data volumes untouched. For teardown use [[infra-down]].
2. **Idempotent** — safe to re-run: init step skips if already exists, up step is a no-op if containers already healthy.
3. **Halts on hard errors** — if `infra-init` or `infra-up` fails, bootstrap exits with their status. Verify problems are reported but not fatal.
4. **Interactive by default** — will prompt at init + env-copy junctions. Use `--yes` to auto-accept both.

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
- [[docker-apps-up]] — for single-service restarts after bootstrap
- [[infra-down]] — teardown (deliberately NOT chained here)
