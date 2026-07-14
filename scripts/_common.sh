#!/usr/bin/env bash
# Shared helpers for infra-up / infra-down / docker-apps-up.
# Source (not exec): . "$(dirname "$0")/_common.sh"

set -euo pipefail

# Locate project root by walking up from CWD looking for infra/docker-compose.infra.yml
find_project_root() {
  local d
  d="${1:-$PWD}"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    if [ -f "$d/infra/docker-compose.infra.yml" ]; then
      echo "$d"; return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

# Parse `name:` from a compose YAML (used to show which project we're touching).
compose_project_name() {
  local f="$1"
  awk -F: '/^name:[[:space:]]/ { gsub(/[[:space:]]/,"",$2); print $2; exit }' "$f"
}

# Confirm before a destructive action. Return 0 if user typed y|Y|yes; else exit 1.
confirm_destructive() {
  local prompt="$1"
  local answer
  printf '\n%s\nType "yes" to proceed: ' "$prompt"
  read -r answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) echo "aborted."; exit 1 ;;
  esac
}

# Print + exec (so user sees the compose invocation).
run() {
  printf '==> %s\n' "$*"
  "$@"
}
