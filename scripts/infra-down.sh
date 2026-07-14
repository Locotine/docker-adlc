#!/usr/bin/env bash
# infra-down — stop and remove ALL containers of the current project's compose.
#
# Default: keeps volumes (data preserved). Non-destructive.
#
# Flags:
#   --volumes          ALSO remove named volumes (postgres data, keycloak-data, etc.) — DESTRUCTIVE
#   --rmi              ALSO remove images built by compose — frees ~1GB per boundary
#   --apps-only        Only tear down apps compose (leave infra running)
#   --infra-only       Only tear down infra compose
#   -h, --help         Show this help
#
# Any destructive flag prompts for confirmation.

# shellcheck source=./_common.sh
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

VOLUMES=0
RMI=0
APPS=1
INFRA=1
for arg in "$@"; do
  case "$arg" in
    --volumes) VOLUMES=1 ;;
    --rmi) RMI=1 ;;
    --apps-only) INFRA=0 ;;
    --infra-only) APPS=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(find_project_root)" || { echo "error: no infra/docker-compose.infra.yml found from $PWD upward" >&2; exit 2; }
cd "$ROOT/infra"

PROJ="$(compose_project_name docker-compose.infra.yml)"
echo "project root: $ROOT"
echo "compose project: ${PROJ:-<unnamed>}"

if [ "$APPS" = 1 ] && [ ! -f docker-compose.apps.yml ]; then
  APPS=0
fi

COMPOSE=()
[ "$INFRA" = 1 ] && COMPOSE+=(-f docker-compose.infra.yml)
[ "$APPS" = 1 ]  && COMPOSE+=(-f docker-compose.apps.yml)

DOWN_ARGS=(down)
if [ "$VOLUMES" = 1 ]; then
  confirm_destructive "!! --volumes will DELETE named volumes (postgres/keycloak/redis/redpanda DATA) for project '$PROJ'."
  DOWN_ARGS+=(-v)
fi
if [ "$RMI" = 1 ]; then
  confirm_destructive "!! --rmi will delete images built by compose."
  DOWN_ARGS+=(--rmi local)
fi

run docker compose "${COMPOSE[@]}" "${DOWN_ARGS[@]}"

echo
echo "==> remaining containers matching project:"
docker ps -a --filter "label=com.docker.compose.project=${PROJ}" --format 'table {{.Names}}\t{{.Status}}' || true
