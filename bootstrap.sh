#!/usr/bin/env bash
# Reproducibly install all project dependencies inside this checkout.

set -Eeuo pipefail

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 2))); then
    printf 'Agent Builder bootstrap requires Bash 4.2 or newer.\n' >&2
    exit 1
fi
if [[ ! -x /usr/bin/env ]]; then
    printf 'Agent Builder bootstrap requires /usr/bin/env.\n' >&2
    exit 1
fi
BOOTSTRAP_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BOOTSTRAP_DIR" != "${BASH_SOURCE[0]}" ]] || BOOTSTRAP_DIR=.
ROOT="$(cd "$BOOTSTRAP_DIR" && pwd -P)"
unset BOOTSTRAP_DIR

# Empty the environment before env.sh creates directories or invokes any
# package/download tool. Bash's `exec -c` applies the empty environment at
# execve time; /usr/bin/env then adds only the reviewed locale, CA, and proxy
# settings needed to bootstrap public dependencies.
if [[ "${_AGENT_BUILDER_BOOTSTRAP_CLEAN:-}" != 1 ]]; then
    bootstrap_path="$ROOT/.tools/node/bin:$ROOT/.tools:$ROOT/.venv/bin:$ROOT/.runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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
NODE_VERSION="22.17.0"
PYTHON_VERSION="3.11.15"
SKIP_NODE=false
BUILD_ASSETS=true
REBUILD=false
OFFLINE=false

usage() {
    cat <<'EOF'
Usage: ./bootstrap.sh [--skip-node] [--no-build] [--rebuild] [--offline]

  --skip-node  Skip the pinned project-local Node.js runtime and Node packages.
  --no-build   Install Node dependencies without building frontend/docs assets.
  --rebuild    Rebuild frontend and docs even when outputs already exist.
  --offline    Forbid dependency network access; requires populated local caches.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --skip-node) SKIP_NODE=true ;;
        --no-build) BUILD_ASSETS=false ;;
        --rebuild) REBUILD=true ;;
        --offline) OFFLINE=true ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '[bootstrap] %s\n' "$*"; }
fail() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

validate_supported_host() {
    local command_name os arch glibc_version glibc_major glibc_minor
    os="$(uname -s)"
    arch="$(uname -m)"
    [[ "$os" == Linux ]] || fail "supported deployment requires GNU/Linux"
    case "$arch" in
        x86_64|amd64|aarch64|arm64) ;;
        *) fail "unsupported CPU architecture: $arch" ;;
    esac
    for command_name in \
        awk chmod curl dirname find getconf install mkdir mv od ps rm sleep \
        tar tr uname wc; do
        command -v "$command_name" >/dev/null 2>&1 \
            || fail "required host command is missing: $command_name"
    done
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    if [[ ! "$glibc_version" =~ ^glibc\ ([0-9]+)\.([0-9]+) ]]; then
        fail "supported deployment requires glibc 2.28 or newer (musl is unsupported)"
    fi
    glibc_major="${BASH_REMATCH[1]}"
    glibc_minor="${BASH_REMATCH[2]}"
    if ((glibc_major < 2 || (glibc_major == 2 && glibc_minor < 28))); then
        fail "glibc 2.28 or newer is required; found $glibc_version"
    fi
    [[ -r /dev/urandom ]] || fail "/dev/urandom is required for local secrets"
    [[ -r /proc/self/stat ]] || fail "/proc must be mounted for process isolation"
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        fail "sha256sum or shasum is required to verify uv"
    fi
}

install_uv() {
    local os arch target checksum archive extracted candidate installed_version
    mkdir -p "$AGENT_BUILDER_TOOLS_DIR"
    if [[ -x "$AGENT_BUILDER_UV" ]]; then
        installed_version="$($AGENT_BUILDER_UV --version 2>/dev/null || true)"
        if [[ "$installed_version" == "uv $UV_VERSION"* ]]; then
            return
        fi
    fi

    if command -v uv >/dev/null 2>&1 && [[ "$(uv --version)" == "uv $UV_VERSION"* ]]; then
        log "copying uv $UV_VERSION into .tools"
        install -m 0755 "$(command -v uv)" "$AGENT_BUILDER_UV"
        return
    fi
    [[ "$OFFLINE" == false ]] || fail "project-local uv is missing in offline mode"
    command -v curl >/dev/null 2>&1 || fail "curl is required to download uv"
    command -v tar >/dev/null 2>&1 || fail "tar is required to unpack uv"

    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os/$arch" in
        Linux/x86_64|Linux/amd64)
            target="x86_64-unknown-linux-gnu"
            checksum="6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868"
            ;;
        Linux/aarch64|Linux/arm64)
            target="aarch64-unknown-linux-gnu"
            checksum="f2ee1cde9aabb4c6e43bd3f341dadaf42189a54e001e521346dc31547310e284"
            ;;
        *) fail "unsupported platform for managed uv: $os/$arch" ;;
    esac

    archive="$TMPDIR/uv-${UV_VERSION}-${target}.tar.gz"
    extracted="$TMPDIR/uv-${UV_VERSION}-${target}"
    rm -rf -- "$extracted"
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

install_node_runtime() {
    local os arch platform checksum archive extracted candidate
    if [[ -x "$AGENT_BUILDER_NODE_HOME/bin/node" ]] \
        && [[ "$($AGENT_BUILDER_NODE_HOME/bin/node --version)" == "v$NODE_VERSION" ]]; then
        return
    fi
    [[ "$OFFLINE" == false ]] || fail "project-local Node.js is missing in offline mode"
    command -v curl >/dev/null 2>&1 || fail "curl is required to download Node.js"
    command -v tar >/dev/null 2>&1 || fail "tar is required to unpack Node.js"

    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os/$arch" in
        Linux/x86_64|Linux/amd64)
            platform="linux-x64"
            checksum="0fa01328a0f3d10800623f7107fbcd654a60ec178fab1ef5b9779e94e0419e1a"
            ;;
        Linux/aarch64|Linux/arm64)
            platform="linux-arm64"
            checksum="3e99df8b01b27dc8b334a2a30d1cd500442b3b0877d217b308fd61a9ccfc33d4"
            ;;
        *) fail "unsupported platform for managed Node.js: $os/$arch" ;;
    esac

    archive="$TMPDIR/node-v${NODE_VERSION}-${platform}.tar.gz"
    extracted="$TMPDIR/node-v${NODE_VERSION}-${platform}.extract"
    rm -rf -- "$archive" "$extracted"
    mkdir -p "$extracted"
    log "downloading pinned Node.js $NODE_VERSION"
    curl --fail --location --silent --show-error --retry 3 \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-${platform}.tar.gz" \
        --output "$archive"
    [[ "$(sha256_file "$archive")" == "$checksum" ]] || fail "Node.js checksum mismatch"
    tar -xzf "$archive" -C "$extracted" --strip-components=1
    candidate="$AGENT_BUILDER_TOOLS_DIR/.node-${NODE_VERSION}.new"
    rm -rf -- "$candidate"
    mv "$extracted" "$candidate"
    rm -rf -- "$AGENT_BUILDER_NODE_HOME"
    mv "$candidate" "$AGENT_BUILDER_NODE_HOME"
    rm -f -- "$archive"
    [[ "$($AGENT_BUILDER_NODE_HOME/bin/node --version)" == "v$NODE_VERSION" ]] \
        || fail "project-local Node.js verification failed"
}

ensure_token() {
    agent_builder_reject_symlink_path "$AGENT_BUILDER_TOKEN_FILE" \
        || fail "local API token path contains a symlink"
    if [[ -s "$AGENT_BUILDER_TOKEN_FILE" ]]; then
        chmod 0600 -- "$AGENT_BUILDER_TOKEN_FILE"
        return
    fi
    local token temporary_file
    token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    [[ ${#token} -eq 64 ]] || fail "failed to generate local API token"
    temporary_file="${AGENT_BUILDER_TOKEN_FILE}.new.$$"
    rm -f -- "$temporary_file"
    printf '%s\n' "$token" > "$temporary_file"
    chmod 0600 -- "$temporary_file"
    mv -f -- "$temporary_file" "$AGENT_BUILDER_TOKEN_FILE"
    log "generated .runtime/secrets/api-token"
}

install_python_dependencies() {
    [[ -f "$ROOT/uv.lock" ]] || fail "uv.lock is missing"
    local args=(sync --frozen --managed-python --no-build --python "$PYTHON_VERSION")
    [[ "$OFFLINE" == false ]] || args+=(--offline)
    log "syncing locked Python environment"
    "$AGENT_BUILDER_UV" "${args[@]}"
    [[ -x "$ROOT/.venv/bin/python" ]] || fail "uv did not create .venv"
}

lock_digest() {
    local package_dir="$1"
    {
        sha256_file "$package_dir/package.json"
        sha256_file "$package_dir/package-lock.json"
    } | tr -d '\n' | {
        if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'
        else shasum -a 256 | awk '{print $1}'; fi
    }
}

write_state_file() {
    local destination="$1" value="$2" temporary_file
    agent_builder_reject_symlink_path "$destination" \
        || fail "managed state path contains a symlink: $destination"
    temporary_file="${destination}.new.$$"
    rm -f -- "$temporary_file"
    printf '%s\n' "$value" > "$temporary_file"
    chmod 0600 -- "$temporary_file"
    mv -f -- "$temporary_file" "$destination"
}

install_node_project() {
    local label="$1" dir="$2" state_file expected actual
    state_file="$AGENT_BUILDER_RUNTIME_DIR/state/npm-${label}.sha256"
    agent_builder_reject_symlink_path "$dir/node_modules" \
        || fail "managed Node dependency path contains a symlink: $dir/node_modules"
    agent_builder_reject_symlink_path "$state_file" \
        || fail "managed state path contains a symlink: $state_file"
    expected="$(lock_digest "$dir")"
    if [[ -r "$state_file" ]]; then
        actual="$(<"$state_file")"
    else
        actual=""
    fi
    if [[ ! -d "$dir/node_modules" || "$actual" != "$expected" ]]; then
        log "installing locked Node dependencies for $label"
        (cd "$dir" && npm ci --no-audit --no-fund)
        write_state_file "$state_file" "$expected"
    else
        log "Node dependencies for $label are current"
    fi
}

build_node_assets() {
    local frontend_hash docs_hash frontend_state docs_state frontend_recorded="" docs_recorded=""
    frontend_hash="$(source_digest "$ROOT/frontend")"
    docs_hash="$(source_digest "$ROOT/docs-site")"
    frontend_state="$AGENT_BUILDER_RUNTIME_DIR/state/build-frontend.sha256"
    docs_state="$AGENT_BUILDER_RUNTIME_DIR/state/build-docs.sha256"
    for managed_path in \
        "$ROOT/frontend/.next" "$ROOT/docs-site/.vitepress/dist" \
        "$frontend_state" "$docs_state"; do
        agent_builder_reject_symlink_path "$managed_path" \
            || fail "managed build path contains a symlink: $managed_path"
    done
    [[ ! -r "$frontend_state" ]] || frontend_recorded="$(<"$frontend_state")"
    [[ ! -r "$docs_state" ]] || docs_recorded="$(<"$docs_state")"

    if [[ "$REBUILD" == true || ! -f "$ROOT/frontend/.next/BUILD_ID" \
        || "$frontend_recorded" != "$frontend_hash" ]]; then
        log "building production frontend"
        (cd "$ROOT/frontend" && npm run build)
        write_state_file "$frontend_state" "$frontend_hash"
    else
        log "frontend build already exists"
    fi
    if [[ "$REBUILD" == true || ! -f "$ROOT/docs-site/.vitepress/dist/index.html" \
        || "$docs_recorded" != "$docs_hash" ]]; then
        log "building documentation site"
        (cd "$ROOT/docs-site" && npm run build)
        write_state_file "$docs_state" "$docs_hash"
    else
        log "documentation build already exists"
    fi
}

source_digest() {
    "$ROOT/.venv/bin/python" - "$1" <<'PY'
from hashlib import sha256
import os
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
excluded_directories = {
    "node_modules", ".next", "dist", "cache", "__pycache__", "test-results",
    "tests", ".playwright-cli",
}
digest = sha256()
for current_raw, directories, filenames in os.walk(root, followlinks=False):
    current = Path(current_raw)
    retained = []
    for name in sorted(directories):
        path = current / name
        if name in excluded_directories:
            continue
        if path.is_symlink():
            raise SystemExit(f"refusing source symlink during build hashing: {path}")
        retained.append(name)
    directories[:] = retained
    for name in sorted(filenames):
        path = current / name
        if path.is_symlink():
            raise SystemExit(f"refusing source symlink during build hashing: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
print(digest.hexdigest())
PY
}

validate_supported_host
install_uv
ensure_token
install_python_dependencies

if [[ "$SKIP_NODE" == false ]]; then
    install_node_runtime
    [[ "$(command -v node)" == "$AGENT_BUILDER_NODE_HOME/bin/node" ]] \
        || fail "project-local Node.js is not first on PATH"
    [[ "$(command -v npm)" == "$AGENT_BUILDER_NODE_HOME/bin/npm" ]] \
        || fail "project-local npm is not first on PATH"
    install_node_project root "$ROOT"
    install_node_project frontend "$ROOT/frontend"
    install_node_project docs "$ROOT/docs-site"
    if [[ "$BUILD_ASSETS" == true ]]; then
        build_node_assets
    fi
fi

log "bootstrap complete; all managed state is under this checkout"
