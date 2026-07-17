#!/usr/bin/env bash
# Deliberately remove selected project-local runtime state.

set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

SCOPE=""
YES=false

usage() {
    cat <<'EOF'
Usage: ./purge.sh <scope> [--yes]

Scopes:
  cache           Download caches and temporary execution directories
  logs            Rotated service logs
  observability   Phoenix SQLite data only
  environments    Per-agent uv environments and their metadata
  data            Application/user data (destructive)
  build           Frontend and documentation build outputs
  dependencies    .venv, local uv/Python, and Node dependencies
  all             Every generated file above, including local secrets

The stack must be fully stopped. --yes is required in non-interactive use.
EOF
}

while (($#)); do
    case "$1" in
        --yes|-y) YES=true; shift ;;
        --help|-h) usage; exit 0 ;;
        cache|logs|observability|environments|data|build|dependencies|all)
            [[ -z "$SCOPE" ]] || { echo "Only one scope may be selected" >&2; exit 2; }
            SCOPE="$1"; shift
            ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ -n "$SCOPE" ]] || { usage >&2; exit 2; }

if [[ "$YES" == false ]]; then
    if [[ ! -t 0 ]]; then
        echo "Refusing non-interactive purge without --yes" >&2
        exit 2
    fi
    printf "Permanently purge '%s' state from %s? Type '%s' to continue: " \
        "$SCOPE" "$ROOT" "$SCOPE"
    read -r confirmation
    [[ "$confirmation" == "$SCOPE" ]] || { echo "Purge cancelled"; exit 1; }
fi

if ! "$ROOT/stop.sh" --force; then
    echo "Refusing to purge while managed or conflicting services remain" >&2
    exit 1
fi

safe_remove() {
    local requested="$1" parent resolved_parent
    case "$requested" in
        "$ROOT"/*) ;;
        *) echo "Refusing to remove path outside project: $requested" >&2; exit 1 ;;
    esac
    parent="$(dirname -- "$requested")"
    [[ -d "$parent" ]] || {
        echo "Refusing to remove path with a missing parent: $requested" >&2
        exit 1
    }
    resolved_parent="$(cd "$parent" && pwd -P)"
    case "$resolved_parent" in
        "$ROOT"|"$ROOT"/*) rm -rf -- "$requested" ;;
        *) echo "Refusing to remove path through an external parent: $requested" >&2; exit 1 ;;
    esac
}

purge_cache() {
    safe_remove "$AGENT_BUILDER_RUNTIME_DIR/cache"
    safe_remove "$AGENT_BUILDER_RUNTIME_DIR/tmp"
}

purge_logs() { safe_remove "$AGENT_BUILDER_RUNTIME_DIR/logs"; }
purge_observability() { safe_remove "$AGENT_BUILDER_RUNTIME_DIR/phoenix"; }

purge_environments() {
    safe_remove "$AGENT_BUILDER_ENVIRONMENTS_DIR"
    safe_remove "$ROOT/data/environments"
}

purge_data() {
    safe_remove "$ROOT/data"
}

purge_build() {
    safe_remove "$ROOT/frontend/.next"
    safe_remove "$ROOT/docs-site/.vitepress/dist"
}

purge_dependencies() {
    safe_remove "$ROOT/.venv"
    safe_remove "$ROOT/.tools"
    safe_remove "$AGENT_BUILDER_RUNTIME_DIR/python"
    safe_remove "$AGENT_BUILDER_RUNTIME_DIR/tools"
    safe_remove "$AGENT_BUILDER_RUNTIME_DIR/bin"
    safe_remove "$ROOT/node_modules"
    safe_remove "$ROOT/frontend/node_modules"
    safe_remove "$ROOT/docs-site/node_modules"
}

case "$SCOPE" in
    cache) purge_cache ;;
    logs) purge_logs ;;
    observability) purge_observability ;;
    environments) purge_environments ;;
    data) purge_data ;;
    build) purge_build ;;
    dependencies) purge_dependencies ;;
    all)
        purge_cache
        purge_logs
        purge_observability
        purge_environments
        purge_data
        purge_build
        purge_dependencies
        safe_remove "$AGENT_BUILDER_RUNTIME_DIR"
        ;;
esac

printf '[purge] removed %s state\n' "$SCOPE"
