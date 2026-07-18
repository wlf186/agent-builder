#!/usr/bin/env bash
# Install the pinned Python toolchain and dependencies inside this checkout.

set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 2))); then
    printf 'Agent Builder bootstrap requires Bash 4.2 or newer.\n' >&2
    exit 1
fi

BOOTSTRAP_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BOOTSTRAP_DIR" != "${BASH_SOURCE[0]}" ]] || BOOTSTRAP_DIR=.
ROOT="$(cd "$BOOTSTRAP_DIR" && pwd -P)"
unset BOOTSTRAP_DIR

# Re-exec with a reviewed environment before invoking a downloader or package
# manager. Proxy and CA settings are retained only for public dependency fetches.
if [[ "${_AGENT_BUILDER_BOOTSTRAP_CLEAN:-}" != 1 ]]; then
    bootstrap_path="$ROOT/.tools:$ROOT/.venv/bin:$ROOT/.runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    bootstrap_environment=(
        "PATH=$bootstrap_path"
        "HOME=$ROOT/.runtime/home"
        "TMPDIR=$ROOT/.runtime/tmp" "TEMP=$ROOT/.runtime/tmp" "TMP=$ROOT/.runtime/tmp"
        "_AGENT_BUILDER_BOOTSTRAP_CLEAN=1"
    )
    for bootstrap_key in LANG LANGUAGE LC_ALL LC_CTYPE TZ \
        SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE \
        HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY \
        http_proxy https_proxy all_proxy no_proxy; do
        if [[ -v "$bootstrap_key" ]]; then
            bootstrap_environment+=("$bootstrap_key=${!bootstrap_key}")
        fi
    done
    exec -c /usr/bin/env "${bootstrap_environment[@]}" \
        "$BASH" "$ROOT/bootstrap.sh" "$@"
fi
unset _AGENT_BUILDER_BOOTSTRAP_CLEAN bootstrap_path bootstrap_environment bootstrap_key

# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

UV_VERSION="0.11.7"
PYTHON_VERSION="3.11.15"
OFFLINE=false

usage() {
    cat <<'EOF'
Usage: ./bootstrap.sh [--offline]

Installs a pinned uv binary, managed Python and the frozen project environment
under .tools/, .runtime/ and .venv/. No global package location is modified.

  --offline  Forbid downloads; all required artifacts must already be cached.
EOF
}

while (($#)); do
    case "$1" in
        --offline) OFFLINE=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf '[bootstrap] ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '[bootstrap] %s\n' "$*"; }
fail() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

validate_supported_host() {
    local command_name os_name architecture glibc_version glibc_major glibc_minor
    os_name="$(uname -s)"
    architecture="$(uname -m)"
    [[ "$os_name" == Linux ]] || fail "supported deployment requires GNU/Linux"
    case "$architecture" in
        x86_64|amd64|aarch64|arm64) ;;
        *) fail "unsupported CPU architecture: $architecture" ;;
    esac
    for command_name in awk chmod dirname getconf install mkdir mv od ps rm sleep tar tr uname wc; do
        command -v "$command_name" >/dev/null 2>&1 \
            || fail "required host command is missing: $command_name"
    done
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    if [[ ! "$glibc_version" =~ ^glibc\ ([0-9]+)\.([0-9]+) ]]; then
        fail "supported deployment requires glibc 2.28 or newer"
    fi
    glibc_major="${BASH_REMATCH[1]}"
    glibc_minor="${BASH_REMATCH[2]}"
    if ((glibc_major < 2 || (glibc_major == 2 && glibc_minor < 28))); then
        fail "glibc 2.28 or newer is required; found $glibc_version"
    fi
    [[ -r /proc/self/stat ]] || fail "/proc must be mounted"
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        fail "sha256sum or shasum is required"
    fi
}

install_uv() {
    local os_name architecture target checksum archive extracted candidate installed_version
    if [[ -x "$AGENT_BUILDER_UV" ]]; then
        installed_version="$($AGENT_BUILDER_UV --version 2>/dev/null || true)"
        [[ "$installed_version" == "uv $UV_VERSION"* ]] && return
    fi
    if command -v uv >/dev/null 2>&1 && [[ "$(uv --version)" == "uv $UV_VERSION"* ]]; then
        log "copying uv $UV_VERSION into .tools"
        install -m 0755 "$(command -v uv)" "$AGENT_BUILDER_UV"
        return
    fi
    [[ "$OFFLINE" == false ]] || fail "project-local uv is missing in offline mode"
    command -v curl >/dev/null 2>&1 || fail "curl is required to download uv"
    os_name="$(uname -s)"
    architecture="$(uname -m)"
    case "$os_name/$architecture" in
        Linux/x86_64|Linux/amd64)
            target="x86_64-unknown-linux-gnu"
            checksum="6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868"
            ;;
        Linux/aarch64|Linux/arm64)
            target="aarch64-unknown-linux-gnu"
            checksum="f2ee1cde9aabb4c6e43bd3f341dadaf42189a54e001e521346dc31547310e284"
            ;;
        *) fail "unsupported platform for managed uv: $os_name/$architecture" ;;
    esac
    archive="$TMPDIR/uv-${UV_VERSION}-${target}.tar.gz"
    extracted="$TMPDIR/uv-${UV_VERSION}-${target}.extract"
    rm -rf -- "$archive" "$extracted"
    mkdir -p "$extracted"
    log "downloading pinned uv $UV_VERSION"
    curl --fail --location --silent --show-error --retry 3 \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz" \
        --output "$archive"
    [[ "$(sha256_file "$archive")" == "$checksum" ]] || fail "uv checksum mismatch"
    tar -xzf "$archive" -C "$extracted"
    candidate="$extracted/uv-${target}/uv"
    [[ -x "$candidate" ]] || fail "downloaded uv archive has an unexpected layout"
    install -m 0755 "$candidate" "$AGENT_BUILDER_UV"
    rm -rf -- "$archive" "$extracted"
}

install_python_environment() {
    local sync_arguments=(sync --frozen --managed-python --no-build --python "$PYTHON_VERSION")
    [[ -f "$ROOT/uv.lock" ]] || fail "uv.lock is missing"
    [[ "$OFFLINE" == false ]] || sync_arguments+=(--offline)
    log "syncing the frozen minimal Python environment"
    "$AGENT_BUILDER_UV" "${sync_arguments[@]}"
    [[ -x "$ROOT/.venv/bin/python" ]] || fail "uv did not create .venv"
    "$ROOT/.venv/bin/python" -c 'import fastapi, httpx, uvicorn' \
        || fail "runtime dependency import check failed"
}

validate_supported_host
install_uv
install_python_environment
log "bootstrap complete; all managed state is contained in this checkout"
