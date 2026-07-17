#!/usr/bin/env bash
# Read the server-side credential without placing it in process arguments.

set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../env.sh
source "$ROOT/env.sh"
[[ -s "$AGENT_BUILDER_TOKEN_FILE" ]] || {
    echo "Missing local API token; run ./bootstrap.sh first" >&2
    exit 1
}
AGENT_BUILDER_API_TOKEN="$(<"$AGENT_BUILDER_TOKEN_FILE")"
[[ ${#AGENT_BUILDER_API_TOKEN} -ge 32 ]] || {
    echo "Invalid local API token" >&2
    exit 1
}
export AGENT_BUILDER_API_TOKEN

exec "$ROOT/frontend/node_modules/.bin/next" start "$ROOT/frontend" \
    -p "$FRONTEND_PORT" -H "$FRONTEND_HOST"
