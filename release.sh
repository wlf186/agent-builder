#!/usr/bin/env bash
# Run the supported release gate and build checkout-local release evidence.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RR_ID="${1:-}"
[[ "$RR_ID" =~ ^RR-QUA-[0-9]{8}-[0-9]{2}$ ]] || {
    printf 'Usage: ./release.sh RR-QUA-YYYYMMDD-NN\n' >&2
    exit 2
}
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
    printf '[release] ERROR: this release is qualified only on GNU/Linux x86_64\n' >&2
    exit 1
}

"$ROOT/bootstrap.sh"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"
umask 077
LIFECYCLE_TOUCHED=false

restore_service_on_exit() {
    local status="$1"
    trap - EXIT
    if [[ "$LIFECYCLE_TOUCHED" == true ]]; then
        "$ROOT/start.sh" >/dev/null 2>&1 \
            || printf '[release] WARNING: automatic service recovery failed; run ./start.sh\n' >&2
    fi
    exit "$status"
}
trap 'restore_service_on_exit $?' EXIT

quarantine_preexisting_pytest_temp() {
    local username source parent destination
    username="$(id -un)"
    [[ "$username" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || {
        printf '[release] ERROR: unsafe local username for pytest temp containment\n' >&2
        return 1
    }
    source="$ROOT/.runtime/tmp/pytest-of-$username"
    [[ -e "$source" || -L "$source" ]] || return 0
    [[ -d "$source" && ! -L "$source" \
        && "$(stat -c '%u' -- "$source" 2>/dev/null || true)" == "$EUID" ]] || {
        printf '[release] ERROR: pre-existing pytest temp root is unsafe\n' >&2
        return 1
    }
    parent="$ROOT/.runtime/test-results/release-quarantine"
    destination="$parent/$RR_ID-preexisting-pytest"
    [[ ! -e "$destination" && ! -L "$destination" ]] || {
        printf '[release] ERROR: pytest quarantine destination already exists\n' >&2
        return 1
    }
    mkdir -p "$parent"
    chmod 0700 "$parent"
    mv -T -- "$source" "$destination"
    printf '[release] quarantined reproducible pytest temp: %s\n' \
        "${destination#"$ROOT/"}"
}

VERSION="$(tr -d '\n' < VERSION)"
OUTPUT="$ROOT/.runtime/release/$VERSION/$RR_ID"
TEST_TEMP="$ROOT/.runtime/test-results/release-pytest/$RR_ID"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ \
    && ! -e "$OUTPUT" && ! -L "$OUTPUT" \
    && ! -e "$TEST_TEMP" && ! -L "$TEST_TEMP" ]] || {
    printf '[release] ERROR: invalid version or existing release evidence path\n' >&2
    exit 1
}
mkdir -p "$OUTPUT"
chmod 0700 "$OUTPUT"
mkdir -p "$(dirname "$TEST_TEMP")"
chmod 0700 "$(dirname "$TEST_TEMP")"
quarantine_preexisting_pytest_temp

printf '[release] running complete tests and governance\n'
"$ROOT/.venv/bin/python" -m pytest --basetemp "$TEST_TEMP"
"$ROOT/governance.sh"

printf '[release] auditing dependencies and generating CycloneDX SBOM\n'
"$ROOT/.tools/uv" tool run --from pip-audit==2.10.1 pip-audit \
    --path "$ROOT/.venv/lib/python3.11/site-packages" \
    --progress-spinner off --format cyclonedx-json --output "$OUTPUT/sbom.cdx.json"
chmod 0600 "$OUTPUT/sbom.cdx.json"

printf '[release] running managed lifecycle and real-model bounded soak\n'
LIFECYCLE_TOUCHED=true
"$ROOT/stop.sh"
"$ROOT/start.sh"
"$ROOT/.venv/bin/python" "$ROOT/scripts/qualify_runtime.py" \
    --rr-id "$RR_ID" --implementation-ref worktree --turns 16
"$ROOT/stop.sh"

"$ROOT/.venv/bin/python" "$ROOT/scripts/build_release_artifact.py" \
    --rr-id "$RR_ID" --output "$OUTPUT"

printf '[release] restarting qualified service on 0.0.0.0:20815\n'
"$ROOT/start.sh"
LIFECYCLE_TOUCHED=false
printf '[release] PASS: %s\n' "$OUTPUT"
