---
name: infra-down
description: Stop and remove Docker containers for the current project. Use when the user says "down docker", "tắt docker", "dừng dự án", "stop services", "tắt hết đi", or wants to reclaim resources. Non-destructive by default (volumes preserved). Supports `--volumes` (wipe data — prompts for confirmation) and `--rmi` (remove built images).
---

# infra-down

Stop and remove Docker containers for the current project.

## When to invoke

Trigger this skill when the user wants to stop their project's Docker stack:
- "down", "stop", "tắt docker", "tắt dự án", "dừng lại", "clear"
- Before shutting down machine — user wants clean state
- Freeing memory when switching to another project

Do **not** trigger for: killing a single container (use `docker kill` directly), or when the user wants to *restart* (use [[infra-up]] with `--recreate`).

## Command

```bash
./scripts/infra-down.sh [--volumes] [--rmi] [--apps-only] [--infra-only]
```

Flags:
- `--volumes` — **DESTRUCTIVE** — also remove named volumes (postgres/keycloak/redis data). Script prompts for `yes` confirmation.
- `--rmi` — also remove images built by compose (frees ~1GB per boundary). Also confirms.
- `--apps-only` — leave shared infra running, only stop apps
- `--infra-only` — only stop infra (apps must be down first, else orphan)

## Behaviour rules

1. Default is **safe**: containers stopped and removed, but volumes stay. Data survives.
2. `--volumes` requires explicit `yes` typed — data loss is unrecoverable.
3. Script prints remaining containers with the project label after tearing down, so user can see if anything leaked.

## Related

- [[infra-up]] — bring up
- [[docker-apps-up]] — up only apps
