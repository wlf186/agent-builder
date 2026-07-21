#!/usr/bin/env bash
# Quiesce the runtime and create one private checkout-local data backup.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
[[ $# -eq 1 ]] || { printf 'Usage: ./backup.sh <backup-id>\n' >&2; exit 2; }
"$ROOT/stop.sh"
# shellcheck source=env.sh
source "$ROOT/env.sh"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/backup_data.py" "$1"
