#!/usr/bin/env bash
# infra-up — bring up SHARED INFRA + APPS for the current project.
#
# Runs `docker compose -f infra/docker-compose.infra.yml [-f infra/docker-compose.apps.yml] up -d`
# from wherever you invoke it (walks up to find infra/).
#
# Flags:
#   --build            Rebuild images before starting (slower first time)
#   --infra-only       Only bring up shared infra (postgres/redis/keycloak/…)
#   --recreate         Force recreate containers even if config unchanged
#   -h, --help         Show this help
#
# For "apps only" (infra already healthy) use ./docker-apps-up.sh instead.
#
# Requires `infra/.env` if apps compose references ${VAR:?...} placeholders.
# Auto-warns if .env missing.

# shellcheck source=./_common.sh
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

BUILD=0
APPS=1
RECREATE=0
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --infra-only) APPS=0 ;;
    --recreate) RECREATE=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(find_project_root)" || { echo "error: no infra/docker-compose.infra.yml found from $PWD upward" >&2; exit 2; }
cd "$ROOT/infra"

PROJ="$(compose_project_name docker-compose.infra.yml)"
echo "project root: $ROOT"
echo "compose project: ${PROJ:-<unnamed>}"

if [ "$APPS" = 1 ] && [ ! -f docker-compose.apps.yml ]; then
  echo "warn: docker-compose.apps.yml not found — bringing up infra only." >&2
  APPS=0
fi

if [ "$APPS" = 1 ] && [ ! -f .env ]; then
  echo "warn: infra/.env missing — apps referencing \${VAR:?...} will fail to start."
  echo "hint: cp .env.example .env && edit real secrets (see .env.example header for kcadm.sh recipe)"
fi

COMPOSE=(-f docker-compose.infra.yml)
[ "$APPS" = 1 ] && COMPOSE+=(-f docker-compose.apps.yml)

UP_ARGS=(up -d)
[ "$BUILD" = 1 ]    && UP_ARGS+=(--build)
[ "$RECREATE" = 1 ] && UP_ARGS+=(--force-recreate)

run docker compose "${COMPOSE[@]}" "${UP_ARGS[@]}"

echo
echo "==> status:"
docker compose "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
