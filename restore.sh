#!/usr/bin/env bash
# Quiesce the runtime and restore one validated checkout-local data backup.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
[[ $# -eq 2 && "$2" == --yes ]] || {
    printf 'Usage: ./restore.sh backups/<backup-id>.tar --yes\n' >&2
    exit 2
}
"$ROOT/stop.sh"
# shellcheck source=env.sh
source "$ROOT/env.sh"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/restore_data.py" "$1" --yes
