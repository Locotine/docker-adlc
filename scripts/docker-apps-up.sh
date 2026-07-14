#!/usr/bin/env bash
# docker-apps-up — bring up ONLY the app services (assumes shared infra already healthy).
#
# Wrapper around: `docker compose -f infra/docker-compose.infra.yml -f infra/docker-compose.apps.yml up -d <svc...>`
# with `--no-deps` so we don't try to touch infra containers (postgres/redis/keycloak/…).
#
# Usage:
#   docker-apps-up.sh                          # up all app services
#   docker-apps-up.sh d-bff-auth-client        # up one service
#   docker-apps-up.sh d-taxonomy d-identity-trust  # up several
#
# Flags:
#   --build            Rebuild images before starting
#   --recreate         Force recreate even if config unchanged
#   -h, --help         Show this help

# shellcheck source=./_common.sh
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

BUILD=0
RECREATE=0
SERVICES=()
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --recreate) RECREATE=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) SERVICES+=("$arg") ;;
  esac
done

ROOT="$(find_project_root)" || { echo "error: no infra/docker-compose.infra.yml found from $PWD upward" >&2; exit 2; }
cd "$ROOT/infra"

if [ ! -f docker-compose.apps.yml ]; then
  echo "error: no infra/docker-compose.apps.yml — nothing to up." >&2
  exit 2
fi

# Pre-check infra healthy (any project container matching label + running)
PROJ="$(compose_project_name docker-compose.infra.yml)"
INFRA_UP="$(docker ps --filter "label=com.docker.compose.project=${PROJ}" --filter "label=com.docker.compose.service=postgres" --format '{{.Names}}' | head -1)"
if [ -z "$INFRA_UP" ]; then
  echo "warn: shared infra (postgres) not detected as running for project '$PROJ'." >&2
  echo "      run ./scripts/infra-up.sh --infra-only  first, or use infra-up.sh (full)." >&2
fi

if [ ! -f .env ]; then
  echo "warn: infra/.env missing — apps referencing \${VAR:?...} will fail."
fi

UP_ARGS=(up -d --no-deps)
[ "$BUILD" = 1 ]    && UP_ARGS+=(--build)
[ "$RECREATE" = 1 ] && UP_ARGS+=(--force-recreate)

run docker compose -f docker-compose.infra.yml -f docker-compose.apps.yml \
  "${UP_ARGS[@]}" "${SERVICES[@]}"

echo
echo "==> app status:"
docker compose -f docker-compose.infra.yml -f docker-compose.apps.yml \
  ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}' \
  | awk 'NR==1 || /^d-/ {print}'
