#!/usr/bin/env bash
# Rotate the checkout-local access token without exposing it in argv or logs.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ ! -t 0 || ! -t 1 ]]; then
    printf '[set-access-token] ERROR: an interactive terminal is required\n' >&2
    exit 2
fi

# shellcheck source=env.sh
source "$ROOT/env.sh"
"$ROOT/bootstrap.sh"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/rotate_access_token.py"
