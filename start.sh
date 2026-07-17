#!/usr/bin/env bash
# Start the complete Agent Builder stack using only project-managed processes.

set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

SKIP_BOOTSTRAP=false
ENABLE_OBSERVABILITY=true
ENABLE_DOCS=true
STARTED_SERVICES=()
ROLLING_BACK=false
STARTING_PID=""
STARTING_PGID=""

usage() {
    cat <<'EOF'
Usage: ./start.sh [--skip-bootstrap] [--no-observability] [--no-docs]

The default starts Phoenix, backend, production frontend, and documentation.
Any failed health check rolls back every service started by this invocation.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --skip-bootstrap) SKIP_BOOTSTRAP=true ;;
        --no-observability) ENABLE_OBSERVABILITY=false ;;
        --no-docs) ENABLE_DOCS=false ;;
        --help|-h) usage; exit 0 ;;
        --force)
            echo "Unsafe port-based force start was removed; run ./stop.sh --force first." >&2
            exit 2
            ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '[start] %s\n' "$*"; }
fail() { printf '[start] ERROR: %s\n' "$*" >&2; return 1; }

pid_file() { printf '%s/%s.pid' "$AGENT_BUILDER_RUNTIME_DIR/pids" "$1"; }

process_marker() {
    local stat
    if [[ -r "/proc/$1/stat" ]]; then
        stat="$(<"/proc/$1/stat")"
        stat="${stat##*) }"
        printf 'linux:%s\n' "$(awk '{print $20}' <<<"$stat")"
    else
        printf 'ps:%s\n' "$(ps -o lstart= -p "$1" 2>/dev/null | tr -d '[:space:]')"
    fi
}

managed_running() {
    local name="$1" file pid pgid recorded_marker recorded_root current_marker current_pgid command_line attempt
    file="$(pid_file "$name")"
    [[ -r "$file" && ! -L "$file" ]] || return 1
    pid="$(awk -F= '$1 == "pid" {print $2}' "$file")"
    pgid="$(awk -F= '$1 == "pgid" {print $2}' "$file")"
    recorded_marker="$(awk -F= '$1 == "marker" {print $2}' "$file")"
    recorded_root="$(awk -F= '$1 == "root" {sub(/^root=/, ""); print}' "$file")"
    [[ "$pid" =~ ^[0-9]+$ && "$pgid" =~ ^[0-9]+$ && -n "$recorded_marker" ]] || return 1
    [[ "$recorded_root" == "$ROOT" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    for ((attempt = 0; attempt < 3; attempt++)); do
        current_marker="$(process_marker "$pid")"
        current_pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
        command_line="$(ps -o command= -p "$pid" 2>/dev/null)"
        if [[ "$current_marker" == "$recorded_marker" && "$current_pgid" == "$pgid" \
            && "$command_line" == *"$ROOT/scripts/run_with_rotating_log.py"* ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

tcp_open() {
    local host="$1" port="$2"
    (exec 9<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

health_ok() {
    local url="$1"
    curl --noproxy '*' --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1
}

wait_for_health() {
    local name="$1" url="$2" timeout="$3" waited=0
    while (( waited < timeout )); do
        if health_ok "$url"; then
            return 0
        fi
        if ! managed_running "$name"; then
            fail "$name exited before becoming healthy; see .runtime/logs/$name.log"
            return 1
        fi
        sleep 1
        ((waited += 1))
    done
    fail "$name did not become healthy within ${timeout}s; see .runtime/logs/$name.log"
}

check_observability_storage() {
    local total=0 file size threshold="$OBSERVABILITY_STORAGE_WARN_BYTES"
    local maximum="$OBSERVABILITY_STORAGE_MAX_BYTES"
    [[ "$threshold" =~ ^[0-9]+$ ]] || fail "OBSERVABILITY_STORAGE_WARN_BYTES must be an integer"
    [[ "$maximum" =~ ^[0-9]+$ && "$maximum" -gt 0 ]] || fail "OBSERVABILITY_STORAGE_MAX_BYTES must be a positive integer"
    if [[ -n "$(find "$PHOENIX_WORKING_DIR" -type l -print -quit 2>/dev/null)" ]]; then
        fail "Phoenix storage contains a symlink; refusing an unsafe working directory"
        return 1
    fi
    while IFS= read -r -d '' file; do
        size="$(wc -c < "$file")"
        total=$((total + size))
    done < <(find "$PHOENIX_WORKING_DIR" -type f -print0 2>/dev/null)
    if ((total >= threshold)); then
        log "WARNING: Phoenix storage is ${total} bytes (threshold ${threshold}); run ./purge.sh observability when retention data is no longer needed"
    fi
    if ((total >= maximum)); then
        fail "Phoenix storage is ${total} bytes, at or above the hard startup limit ${maximum}; run ./purge.sh observability --yes"
    fi
}

stop_started_service() {
    local name="$1" file pid pgid i
    file="$(pid_file "$name")"
    if ! managed_running "$name"; then
        rm -f -- "$file"
        return 0
    fi
    pid="$(awk -F= '$1 == "pid" {print $2}' "$file")"
    pgid="$(awk -F= '$1 == "pgid" {print $2}' "$file")"
    kill -TERM -- "-$pgid" 2>/dev/null || true
    for ((i = 0; i < 30; i++)); do
        kill -0 -- "-$pgid" 2>/dev/null || break
        sleep 0.2
    done
    if kill -0 -- "-$pgid" 2>/dev/null; then
        kill -KILL -- "-$pgid" 2>/dev/null || true
        for ((i = 0; i < 25; i++)); do
            kill -0 -- "-$pgid" 2>/dev/null || break
            sleep 0.2
        done
    fi
    wait "$pid" 2>/dev/null || true
    if kill -0 -- "-$pgid" 2>/dev/null; then
        log "WARNING: rollback could not stop $name process group $pgid; retaining its PID record"
        return 1
    fi
    rm -f -- "$file"
}

rollback() {
    local status="$1" index attempt
    [[ "$ROLLING_BACK" == false ]] || exit "$status"
    ROLLING_BACK=true
    trap - ERR INT TERM
    if [[ "$STARTING_PID" =~ ^[0-9]+$ ]]; then
        if [[ ! "$STARTING_PGID" =~ ^[0-9]+$ ]]; then
            STARTING_PGID="$(ps -o pgid= -p "$STARTING_PID" 2>/dev/null | tr -d '[:space:]')"
        fi
        if [[ "$STARTING_PGID" =~ ^[0-9]+$ && "$STARTING_PGID" == "$STARTING_PID" ]]; then
            kill -TERM -- "-$STARTING_PGID" 2>/dev/null || true
            for ((attempt = 0; attempt < 30; attempt++)); do
                kill -0 -- "-$STARTING_PGID" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 -- "-$STARTING_PGID" 2>/dev/null; then
                kill -KILL -- "-$STARTING_PGID" 2>/dev/null || true
            fi
        else
            kill -TERM "$STARTING_PID" 2>/dev/null || true
            for ((attempt = 0; attempt < 30; attempt++)); do
                kill -0 "$STARTING_PID" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$STARTING_PID" 2>/dev/null; then
                kill -KILL "$STARTING_PID" 2>/dev/null || true
            fi
        fi
        wait "$STARTING_PID" 2>/dev/null || true
        STARTING_PID=""
        STARTING_PGID=""
    fi
    if ((${#STARTED_SERVICES[@]} > 0)); then
        log "startup failed; rolling back managed services"
        for ((index=${#STARTED_SERVICES[@]} - 1; index >= 0; index--)); do
            stop_started_service "${STARTED_SERVICES[$index]}" || true
        done
    fi
    exit "$status"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT TERM

start_managed() {
    local name="$1" host="$2" port="$3" health_url="$4" timeout="$5"
    shift 5
    local file pid pgid marker log_file temporary_file
    file="$(pid_file "$name")"
    log_file="$AGENT_BUILDER_RUNTIME_DIR/logs/$name.log"

    if managed_running "$name"; then
        health_ok "$health_url" || fail "$name is running but unhealthy"
        log "$name is already running"
        return 0
    fi
    rm -f -- "$file"
    if tcp_open "$host" "$port"; then
        fail "port $host:$port is occupied by a process not managed by this checkout"
        return 1
    fi

    "$ROOT/.venv/bin/python" "$ROOT/scripts/run_with_rotating_log.py" \
        --new-session --clean-env "$log_file" -- "$@" </dev/null >/dev/null 2>&1 &
    pid=$!
    STARTING_PID="$pid"
    sleep 0.2
    kill -0 "$pid" 2>/dev/null || fail "$name failed to launch; see $log_file"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    STARTING_PGID="$pgid"
    marker="$(process_marker "$pid")"
    [[ "$pgid" == "$pid" && -n "$marker" ]] || {
        kill -TERM "$pid" 2>/dev/null || true
        fail "$name did not enter its own process group"
    }
    temporary_file="${file}.new.$$"
    rm -f -- "$temporary_file"
    {
        printf 'pid=%s\n' "$pid"
        printf 'pgid=%s\n' "$pgid"
        printf 'marker=%s\n' "$marker"
        printf 'root=%s\n' "$ROOT"
    } > "$temporary_file"
    chmod 0600 "$temporary_file"
    mv -f -- "$temporary_file" "$file"
    STARTED_SERVICES+=("$name")
    STARTING_PID=""
    STARTING_PGID=""
    wait_for_health "$name" "$health_url" "$timeout"
    log "$name is healthy (pid $pid)"
}

if [[ "$SKIP_BOOTSTRAP" == false ]]; then
    "$ROOT/bootstrap.sh"
fi
# bootstrap may have generated the token after this script's first source.
source "$ROOT/env.sh"
[[ -x "$ROOT/.venv/bin/python" ]] || fail ".venv is missing; run ./bootstrap.sh"
[[ -s "$AGENT_BUILDER_TOKEN_FILE" ]] || fail "local API token is missing; run ./bootstrap.sh"
API_TOKEN="$(<"$AGENT_BUILDER_TOKEN_FILE")"
[[ ${#API_TOKEN} -ge 32 ]] || fail "local API token is invalid; run ./purge.sh dependencies --yes and bootstrap again"

# Complete preflight before creating the first long-running process.
command -v curl >/dev/null 2>&1 || fail "curl is required for health checks"
for managed_path in \
    "$ROOT/frontend/node_modules" "$ROOT/frontend/.next" \
    "$ROOT/docs-site/node_modules" "$ROOT/docs-site/.vitepress/dist"; do
    agent_builder_reject_symlink_path "$managed_path" \
        || fail "managed dependency/build path contains a symlink"
done
unset managed_path
[[ -x "$ROOT/frontend/node_modules/.bin/next" ]] || fail "frontend dependencies are missing"
[[ -f "$ROOT/frontend/.next/BUILD_ID" ]] || fail "production frontend build is missing"
if [[ "$ENABLE_DOCS" == true ]]; then
    [[ -x "$ROOT/docs-site/node_modules/.bin/vitepress" ]] || fail "docs dependencies are missing"
    [[ -f "$ROOT/docs-site/.vitepress/dist/index.html" ]] || fail "docs build is missing"
fi
if [[ "$ENABLE_OBSERVABILITY" == true ]]; then
    [[ -x "$ROOT/.venv/bin/phoenix" ]] || fail "Phoenix CLI is missing from .venv"
    check_observability_storage
fi

if [[ "$ENABLE_OBSERVABILITY" == true ]]; then
    export OBSERVABILITY_ENABLED=true
    export OBSERVABILITY_BACKEND=otlp
    start_managed phoenix "$PHOENIX_HOST" "$PHOENIX_PORT" \
        "http://${PHOENIX_HOST}:${PHOENIX_PORT}/healthz" 120 \
        "$ROOT/.venv/bin/phoenix" serve
else
    export OBSERVABILITY_ENABLED=false
    export OBSERVABILITY_BACKEND=noop
    if managed_running phoenix; then
        fail "Phoenix is already running; stop the existing stack before using --no-observability"
    fi
    log "observability explicitly disabled"
fi

start_managed backend "$BACKEND_HOST" "$BACKEND_PORT" \
    "http://${BACKEND_HOST}:${BACKEND_PORT}/health" 180 \
    "$ROOT/start_backend.sh"

start_managed frontend "$FRONTEND_HOST" "$FRONTEND_PORT" \
    "http://${FRONTEND_HOST}:${FRONTEND_PORT}/" 120 \
    "$ROOT/scripts/start_frontend.sh"

if [[ "$ENABLE_DOCS" == true ]]; then
    start_managed docs "$DOCS_HOST" "$DOCS_PORT" \
        "http://${DOCS_HOST}:${DOCS_PORT}/docs/" 60 \
        "$ROOT/docs-site/node_modules/.bin/vitepress" preview "$ROOT/docs-site" \
        --host "$DOCS_HOST" --port "$DOCS_PORT"
fi

trap - ERR INT TERM
printf '\nAgent Builder started successfully:\n'
printf '  frontend:      http://%s:%s\n' "$FRONTEND_HOST" "$FRONTEND_PORT"
printf '  backend:       http://%s:%s\n' "$BACKEND_HOST" "$BACKEND_PORT"
if [[ "$ENABLE_DOCS" == true ]]; then
    printf '  documentation: http://%s:%s/docs/\n' "$DOCS_HOST" "$DOCS_PORT"
fi
if [[ "$ENABLE_OBSERVABILITY" == true ]]; then
    printf '  observability: http://%s:%s\n' "$PHOENIX_HOST" "$PHOENIX_PORT"
fi
printf '  logs:          %s/logs\n' "$AGENT_BUILDER_RUNTIME_DIR"
