#!/usr/bin/env bash
# infra-up — idempotent local provisioning, migration, and startup flow.
#
# Order:
#   postgres -> live DB/schema reconcile -> remaining infra -> Keycloak/Kafka
#   reconcile -> image build -> one-shot Prisma migrations -> apps.
#
# Flags:
#   --build       Re-evaluate app builds (Docker cache still applies)
#   --infra-only  Stop after provisioning shared infra
#   --recreate    Force recreation of long-running containers
#   -h, --help    Show this help

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
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(find_project_root)" || {
  echo "error: no infra/docker-compose.infra.yml found from $PWD upward" >&2
  exit 2
}
cd "$ROOT/infra"

[ -f .env ] || {
  echo "error: infra/.env is required; copy .env.example and fill local secrets" >&2
  exit 2
}

# DATABASE_URL must contain percent-encoded userinfo while Postgres itself needs
# the original password.  Derive companion *_URLENCODED values on every run so
# preserved credentials containing '/', '#', '@', etc. remain valid URI values.
python3 - contracts/postgres.json .env <<'PY'
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

contract_path, env_path = map(Path, sys.argv[1:])
if not contract_path.is_file():
    raise SystemExit(0)
try:
    contract = json.loads(contract_path.read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"error: cannot derive database URL secrets: {exc}")
lines = env_path.read_text().splitlines()
values = {}
for line in lines:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
    if not match:
        continue
    value = match.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    values[match.group(1)] = value
updates = {}
for database in contract.get("databases", []):
    key = database.get("password_env") if isinstance(database, dict) else None
    if key and key in values and not values[key].startswith(("GENERATE_ME_", "REPLACE_ME_")):
        updates[key + "_URLENCODED"] = urllib.parse.quote(values[key], safe="")
if not updates:
    raise SystemExit(0)
seen = set()
rendered = []
for line in lines:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if match and match.group(1) in updates:
        key = match.group(1)
        rendered.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        rendered.append(line)
for key in sorted(updates.keys() - seen):
    rendered.append(f"{key}={updates[key]}")
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=str(env_path.parent), text=True)
try:
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(rendered) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, env_path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY

INFRA=(-f docker-compose.infra.yml)
MERGED=(-f docker-compose.infra.yml -f docker-compose.apps.yml)
UP=(up -d --wait)
[ "$RECREATE" = 1 ] && UP+=(--force-recreate)

has_service() {
  local profile="$1" service="$2"
  docker compose --profile "$profile" "${INFRA[@]}" config --services \
    | grep -Fxq "$service"
}

echo "project root: $ROOT"
echo "compose project: $(compose_project_name docker-compose.infra.yml)"

echo
echo "==> phase 1/6: start Postgres and reconcile roles/databases/schemas"
if has_service provision postgres; then
  run docker compose "${INFRA[@]}" "${UP[@]}" postgres
  run docker compose --profile provision "${INFRA[@]}" run --rm postgres-provision
else
  echo "  Postgres not selected."
fi

echo
echo "==> phase 2/6: start remaining shared infrastructure"
run docker compose "${INFRA[@]}" "${UP[@]}"

echo
echo "==> phase 3/6: reconcile Keycloak and Kafka contracts"
if has_service provision keycloak-provision; then
  run docker compose --profile provision "${INFRA[@]}" run --rm keycloak-provision
fi
if has_service provision kafka-provision; then
  run docker compose --profile provision "${INFRA[@]}" run --rm kafka-provision
fi

if [ "$APPS" = 0 ] || [ ! -f docker-compose.apps.yml ]; then
  echo
  echo "==> shared infrastructure is healthy and provisioned."
  docker compose "${INFRA[@]}" ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
  exit 0
fi

mapfile -t APP_SERVICES < <(
  python3 -c 'import json; print(*json.load(open("contracts/env.json"))["services"], sep="\n")'
)
[ "${#APP_SERVICES[@]}" -gt 0 ] || {
  echo "error: contracts/env.json declares no app services" >&2
  exit 2
}

echo
echo "==> phase 4/6: build app images"
# Build is always evaluated so first-run never attempts to pull a local-only image.
# Docker's cache keeps repeated invocations cheap; --build documents explicit intent.
run docker compose "${MERGED[@]}" build "${APP_SERVICES[@]}"

echo
echo "==> phase 5/6: run one-shot Prisma migrations"
mapfile -t MIGRATIONS < <(
  docker compose --profile migrate "${MERGED[@]}" config --services \
    | grep -- '-migrate$' || true
)
for migration in "${MIGRATIONS[@]}"; do
  echo "  migrate: $migration"
  if ! docker compose --profile migrate "${MERGED[@]}" run --rm "$migration"; then
    echo "error: $migration failed." >&2
    echo "If Prisma reports P3009, resolve the failed migration explicitly with" >&2
    echo "'prisma migrate resolve'; this flow never deletes a data volume." >&2
    exit 1
  fi
done

echo
echo "==> phase 6/6: start apps and wait for readiness"
APP_UP=(up -d --wait)
[ "$RECREATE" = 1 ] && APP_UP+=(--force-recreate)
[ "$BUILD" = 1 ] && APP_UP+=(--build)
run docker compose "${MERGED[@]}" "${APP_UP[@]}" "${APP_SERVICES[@]}"

echo
echo "==> status:"
docker compose "${MERGED[@]}" ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
