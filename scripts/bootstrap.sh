#!/usr/bin/env bash
# bootstrap — one-shot end-to-end flow for the project.
#
# Chain:
#   1. [init]    if infra/ missing → run infra-init.py (interactive, or defaults with --yes)
#   2. [env]     if infra/.env missing → warn + offer to copy .env.example (won't auto-fill secrets)
#   3. [up]      run infra-up.sh (pass through --build, --recreate)
#   4. [verify]  run sync-env-docker.py verify <svc> for every app service, aggregate report
#   5. [urls]    print host URLs for each app (host-port from `docker compose ps`)
#
# Flags forwarded to infra-up.sh:
#   --build         Rebuild images before starting
#   --recreate      Force recreate containers
#
# Flags local to bootstrap:
#   --skip-init     Do not scaffold even if infra/ missing (fail instead)
#   --skip-verify   Skip step 4
#   --yes           Run end-to-end without prompts; infra-init uses detected defaults
#   --init-config PATH  Use a reviewed infra-init JSON config (implies --yes)
#   -h, --help      Show this help

# shellcheck source=./_common.sh
. "$(cd "$(dirname "$0")" && pwd)/_common.sh"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

BUILD=""
RECREATE=""
SKIP_INIT=0
SKIP_VERIFY=0
ASSUME_YES=0
INIT_CONFIG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --build) BUILD="--build" ;;
    --recreate) RECREATE="--recreate" ;;
    --skip-init) SKIP_INIT=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --init-config)
      shift
      [ "$#" -gt 0 ] || { echo "--init-config requires a JSON path" >&2; exit 2; }
      INIT_CONFIG="$1"
      ASSUME_YES=1
      ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

ask_confirm() {
  local prompt="$1"
  if [ "$ASSUME_YES" = 1 ]; then return 0; fi
  local ans
  printf '%s [y/N]: ' "$prompt"
  read -r ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

materialize_placeholder_secrets() {
  local env_file="$1"
  local tmp line key secret generated
  generated=0
  tmp="$(mktemp "${env_file}.tmp.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *=REPLACE_ME_*)
        key="${line%%=*}"
        if ! secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; then
          rm -f "$tmp"
          return 1
        fi
        printf '%s=%s\n' "$key" "$secret" >>"$tmp"
        generated=$((generated + 1))
        ;;
      *) printf '%s\n' "$line" >>"$tmp" ;;
    esac
  done <"$env_file"
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
  echo "  generated $generated random local secret(s) in infra/.env (values hidden)."
}

banner() {
  printf '\n\033[1m===[ %s ]===\033[0m\n' "$1"
}

# ---------- 1. init ----------

ROOT="$(find_project_root 2>/dev/null || true)"
if [ -z "${ROOT:-}" ]; then
  # No infra yet — need init
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  banner "1/4 scaffold infra/"
  echo "  no infra/docker-compose.infra.yml found under $ROOT"
  if [ "$SKIP_INIT" = 1 ]; then
    echo "  --skip-init set → abort. Run scripts/infra-init.py manually." >&2
    exit 2
  fi
  if ask_confirm "  run scripts/infra-init.py now?"; then
    INIT_ARGS=(--root "$ROOT")
    if [ -n "$INIT_CONFIG" ]; then
      INIT_ARGS+=(--config "$INIT_CONFIG")
    elif [ "$ASSUME_YES" = 1 ]; then
      INIT_ARGS+=(--yes)
    fi
    "$SCRIPT_DIR/infra-init.py" "${INIT_ARGS[@]}"
  else
    echo "  aborted."; exit 1
  fi
else
  banner "1/4 scaffold infra/ (skipped — already exists)"
fi

# Re-locate root (may have been just created)
ROOT="$(find_project_root)"
cd "$ROOT/infra"

# ---------- 2. env ----------

banner "2/4 check infra/.env"
if [ -f .env ]; then
  echo "  infra/.env exists ($(wc -l <.env) lines)"
elif [ -f .env.example ]; then
  echo "  infra/.env missing but .env.example present."
  if ask_confirm "  copy .env.example → .env (you MUST fill in real secrets after)?"; then
    cp .env.example .env
    if [ "$ASSUME_YES" = 1 ]; then
      echo "  copied; automatic mode will replace placeholder secrets."
    else
      echo "  copied. Edit infra/.env now if apps reference \${VAR:?...}."
      if ! ask_confirm "  continue to up-step now (apps may fail if secrets not filled)?"; then
        echo "  paused. Re-run bootstrap when .env is ready."; exit 0
      fi
    fi
  else
    echo "  skipped. apps may fail to start."
  fi
else
  echo "  no .env / .env.example found — assuming apps don't need secret vars."
fi

if [ "$ASSUME_YES" = 1 ] && [ -f .env ] && grep -q '=REPLACE_ME_' .env; then
  materialize_placeholder_secrets .env
fi

# ---------- 3. up ----------

banner "3/4 docker compose up"
"$SCRIPT_DIR/infra-up.sh" $BUILD $RECREATE

# ---------- 4. verify ----------

if [ "$SKIP_VERIFY" = 1 ]; then
  banner "4/4 verify (skipped by --skip-verify)"
else
  banner "4/4 sync-env-docker verify"
  # Discover app service names from docker-compose.apps.yml (services under `services:` root)
  APPS=""
  if [ -f "$ROOT/infra/docker-compose.apps.yml" ]; then
    APPS="$(awk '
      /^services:/ { in_services=1; next }
      in_services && /^[a-zA-Z]/ { in_services=0 }
      in_services && /^  [a-zA-Z0-9_-]+:/ { gsub(":",""); gsub(" ",""); print }
    ' "$ROOT/infra/docker-compose.apps.yml")"
  fi
  if [ -z "$APPS" ]; then
    echo "  no app services declared in docker-compose.apps.yml — nothing to verify."
  else
    FAIL=0
    for svc in $APPS; do
      echo "  --- $svc ---"
      "$SCRIPT_DIR/sync-env-docker.py" verify "$svc" 2>&1 | sed 's/^/    /' || FAIL=1
    done
    if [ "$FAIL" = 1 ]; then
      echo
      echo "  ! verify reported problems in one or more services (see above)."
      echo "    Common fixes:"
      echo "      - PLACEHOLDER: mint real value into infra/.env then ./scripts/docker-apps-up.sh <svc> --recreate"
      echo "      - MISMATCH: edit infra/docker-compose.apps.yml env, then --recreate"
      echo "      - MISSING: add key to infra/docker-compose.apps.yml env block"
    fi
  fi
fi

# ---------- 5. urls ----------

banner "urls"
if [ -f "$ROOT/infra/docker-compose.apps.yml" ]; then
  docker compose -f "$ROOT/infra/docker-compose.infra.yml" -f "$ROOT/infra/docker-compose.apps.yml" \
    ps --format '{{.Name}}\t{{.Ports}}' 2>/dev/null \
    | awk -F'\t' '
      $2 ~ /0\.0\.0\.0:[0-9]+->/ {
        match($2, /0\.0\.0\.0:[0-9]+/)
        port = substr($2, RSTART+8, RLENGTH-8)
        printf "  %-25s  http://localhost:%s\n", $1, port
      }
    ' | sort -u
fi

echo
echo "done."
