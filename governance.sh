#!/usr/bin/env bash
# Run the dependency-free governance gate without Node or global installs.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CHECKER="$ROOT/scripts/check_governance.py"

export HOME="$ROOT/.runtime/home"
export TMPDIR="$ROOT/.runtime/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export XDG_CACHE_HOME="$ROOT/.runtime/xdg/cache"
export XDG_CONFIG_HOME="$ROOT/.runtime/xdg/config"
export XDG_DATA_HOME="$ROOT/.runtime/xdg/data"
export XDG_STATE_HOME="$ROOT/.runtime/xdg/state"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    printf '[governance] ERROR: Python 3 is required; run ./bootstrap.sh first\n' >&2
    exit 1
fi

exec "$PYTHON" -B "$CHECKER"
