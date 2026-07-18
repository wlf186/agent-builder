#!/usr/bin/env bash
# Remove explicitly selected checkout-local state after a verified shutdown.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$SCRIPT_DIR"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

usage() {
    cat <<'EOF'
Usage: ./purge.sh <scope> --yes

Scopes:
  cache         Reproducible package and Python caches
  logs          Rotated gateway logs
  environments  Agent runtime roots, including reusable Worker environments
  data          Persistent Agent data and event journals (destructive)
  dependencies  .venv, .tools, managed Python and caches
  runtime       All disposable runtime state, including the login token
  all           Runtime, dependencies and persistent Agent data (destructive)

The private _legacy-reference archive is never a purge target.
EOF
}

[[ $# -eq 2 ]] || { usage >&2; exit 2; }
PURGE_SCOPE="$1"
[[ "$2" == --yes ]] || { printf '[purge] ERROR: --yes is required\n' >&2; exit 2; }
case "$PURGE_SCOPE" in
    cache|logs|environments|data|dependencies|runtime|all) ;;
    *) printf '[purge] ERROR: unknown scope: %s\n' "$PURGE_SCOPE" >&2; usage >&2; exit 2 ;;
esac

[[ "$ROOT" != / && -n "$ROOT" && -d "$ROOT/.git" ]] \
    || { printf '[purge] ERROR: invalid checkout root\n' >&2; exit 1; }

"$ROOT/stop.sh" --force

safe_remove_tree() {
    local target="$1"
    case "$target" in
        "$ROOT/.runtime"|"$ROOT/.runtime/cache"|"$ROOT/.runtime/python"|\
        "$ROOT/.runtime/agents"|"$ROOT/.venv"|"$ROOT/.tools"|\
        "$ROOT/data/agents") ;;
        *) printf '[purge] ERROR: refused unapproved target: %s\n' "$target" >&2; return 1 ;;
    esac
    agent_builder_reject_symlink_path "$target" || return 1
    if [[ -L "$target" ]]; then
        printf '[purge] ERROR: refused symlink target: %s\n' "$target" >&2
        return 1
    fi
    if [[ -e "$target" ]]; then
        rm -rf --one-file-system -- "$target"
        printf '[purge] removed %s\n' "${target#"$ROOT"/}"
    fi
}

safe_remove_log() {
    local target="$1"
    case "$target" in
        "$ROOT/.runtime/control-plane/gateway.log"|\
        "$ROOT/.runtime/control-plane/gateway.log.1"|\
        "$ROOT/.runtime/control-plane/gateway.log.2"|\
        "$ROOT/.runtime/control-plane/gateway.log.3") ;;
        *) printf '[purge] ERROR: refused unapproved log target\n' >&2; return 1 ;;
    esac
    agent_builder_reject_symlink_path "$target" || return 1
    [[ ! -L "$target" ]] || { printf '[purge] ERROR: refused log symlink\n' >&2; return 1; }
    rm -f -- "$target"
}

case "$PURGE_SCOPE" in
    cache)
        safe_remove_tree "$ROOT/.runtime/cache"
        ;;
    logs)
        for log_path in \
            "$ROOT/.runtime/control-plane/gateway.log" \
            "$ROOT/.runtime/control-plane/gateway.log.1" \
            "$ROOT/.runtime/control-plane/gateway.log.2" \
            "$ROOT/.runtime/control-plane/gateway.log.3"; do
            safe_remove_log "$log_path"
        done
        printf '[purge] removed managed gateway logs\n'
        ;;
    environments)
        safe_remove_tree "$ROOT/.runtime/agents"
        ;;
    data)
        safe_remove_tree "$ROOT/data/agents"
        ;;
    dependencies)
        safe_remove_tree "$ROOT/.venv"
        safe_remove_tree "$ROOT/.tools"
        safe_remove_tree "$ROOT/.runtime/python"
        safe_remove_tree "$ROOT/.runtime/cache"
        ;;
    runtime)
        safe_remove_tree "$ROOT/.runtime"
        ;;
    all)
        safe_remove_tree "$ROOT/.runtime"
        safe_remove_tree "$ROOT/.venv"
        safe_remove_tree "$ROOT/.tools"
        safe_remove_tree "$ROOT/data/agents"
        ;;
esac

printf '[purge] scope %s complete\n' "$PURGE_SCOPE"
