#!/usr/bin/env bash
# Foreground backend launcher for development and service supervisors.

set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

[[ -x "$ROOT/.venv/bin/python" ]] || {
    echo "Missing project environment; run ./bootstrap.sh first" >&2
    exit 1
}
[[ -s "$AGENT_BUILDER_TOKEN_FILE" ]] || {
    echo "Missing local API token; run ./bootstrap.sh first" >&2
    exit 1
}

API_TOKEN="$(<"$AGENT_BUILDER_TOKEN_FILE")"
[[ ${#API_TOKEN} -ge 32 ]] || {
    echo "Invalid local API token; regenerate it with bootstrap" >&2
    exit 1
}
BUILTIN_MCP_ALLOWLIST="localhost:${MCP_SSE_PORT},127.0.0.1:${MCP_SSE_PORT},mcp.api.coingecko.com:443"
SSRF_ALLOWLIST="${BUILTIN_MCP_ALLOWLIST},${AGENT_BUILDER_SSRF_ALLOWLIST}"

export AGENT_BUILDER_API_TOKEN="$API_TOKEN"
export AGENT_BUILDER_SSRF_ALLOWLIST="$SSRF_ALLOWLIST"
unset API_TOKEN BUILTIN_MCP_ALLOWLIST SSRF_ALLOWLIST
exec "$ROOT/.venv/bin/python" "$ROOT/backend.py"
