# Docker Claude Toolkit

This repository is both a Claude Code marketplace and the `docker-claude` plugin source. Its purpose is to install reusable Docker infrastructure skills and scripts into other projects without replacing their existing `.claude/` or `scripts/` directories.

## Repository layout

- `.claude-plugin/marketplace.json` defines the `driverplus-tools` marketplace.
- `.claude-plugin/plugin.json` defines the `docker-claude` plugin and its version.
- `skills/install-project/SKILL.md` is the plugin-facing installer skill.
- `scripts/install-project.py` merges payload files into a target project without overwriting existing files.
- `.claude/skills/` contains the six standalone project skills copied by the installer.
- `scripts/` contains the operational Docker scripts copied by the installer. `install-project.py` itself is excluded from the payload.
- `tests/test_install_project.py` verifies installer safety and idempotency.

Never publish or copy `.claude/settings.local.json`; it is machine-local and ignored by Git.

## Skill routing

Use the repository's actual skills and scripts for these requests:

- Project onboarding, one-shot setup, or “run everything” → use `bootstrap` and `scripts/bootstrap.sh`.
- Scaffold a new shared Docker stack → use `infra-init` and `scripts/infra-init.py`.
- Start the complete Docker stack or infrastructure only → use `infra-up` and `scripts/infra-up.sh`.
- Start or rebuild selected application services without restarting infrastructure → use `docker-apps-up` and `scripts/docker-apps-up.sh`.
- Stop Docker services, optionally removing volumes/images → use `infra-down` and `scripts/infra-down.sh`; treat destructive flags as explicit-confirmation operations.
- Verify container environment variables or generate local environment files → use `sync-env-docker` and `scripts/sync-env-docker.py`.
- Install this toolkit into another project → use `docker-claude:install-project`, which runs `scripts/install-project.py` from the plugin cache.

Do not invent a generic Docker workflow when one of these existing skills covers the request. Read the matching `SKILL.md` before running its script.

## Installer invariants

Changes to `scripts/install-project.py` must preserve all of these rules:

1. Merge files individually; never delete or replace an entire destination directory.
2. Copy only files that do not exist at the destination.
3. Leave identical files unchanged.
4. Report same-path/different-content files as conflicts and preserve the project-owned version.
5. Reject symlink escapes and invalid destination parents before copying anything.
6. Preserve executable modes on copied scripts.
7. Exclude `scripts/install-project.py`, `.claude/settings.local.json`, caches, and test artifacts from the project payload.

## Verification

Run these checks after changing manifests, installer behavior, skills, or scripts:

```bash
claude plugin validate . --strict
env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
bash -n scripts/_common.sh scripts/bootstrap.sh scripts/docker-apps-up.sh scripts/infra-down.sh scripts/infra-up.sh
```

For an end-to-end local marketplace check, use a temporary Claude configuration so personal plugin settings are not modified:

```bash
export CLAUDE_CONFIG_DIR="$(mktemp -d)"
claude plugin marketplace add ./ --scope user
claude plugin install docker-claude@driverplus-tools --scope user
claude plugin details docker-claude@driverplus-tools
```

## Release rules

- Bump `version` in `.claude-plugin/plugin.json` for every published update; pinned plugin versions are not refreshed until this value changes.
- Keep `README.md` installation commands aligned with the marketplace and plugin names, and always pass `--scope user` explicitly. Do not document a global/system-wide install.
- Run the full verification block before pushing.
- Review installer changes specifically for unintended overwrites or path traversal.
