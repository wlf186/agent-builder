#!/usr/bin/env bash
# Start the Agent Builder gateway and its managed runtime.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"
umask 077

RUNTIME_ROOT="$AGENT_BUILDER_RUNTIME_DIR/control-plane"
PID_FILE="$RUNTIME_ROOT/gateway.pid"
LOG_FILE="$RUNTIME_ROOT/gateway.log"
LOCK_FILE="$RUNTIME_ROOT/lifecycle.lock"
LOG_SUPERVISOR="$ROOT/scripts/log_supervisor.py"
IDENTITY_HELPER="$ROOT/scripts/process_identity.sh"
if [[ ! -f "$IDENTITY_HELPER" || -L "$IDENTITY_HELPER" \
    || "$(stat -c '%u' -- "$IDENTITY_HELPER" 2>/dev/null || true)" != "$EUID" \
    || "$(stat -c '%h' -- "$IDENTITY_HELPER" 2>/dev/null || true)" != 1 ]]; then
    printf '[agent-builder start] ERROR: process identity helper is missing or unsafe\n' >&2
    exit 1
fi
# shellcheck source=scripts/process_identity.sh
source "$IDENTITY_HELPER"
SYNC_COUNTER_TOOL="$ROOT/scripts/sync_counter_tool.py"
SYNC_COUNTER_LIBRARY="$ROOT/.runtime/qualification-sync/libagent_builder_sync_counter.so"
SYNC_COUNTER_FILE="$ROOT/.runtime/qualification-sync/sync-counter.bin"
SOURCE_ROOT="$ROOT/src"
PYTHON="$ROOT/.venv/bin/python"
STARTING_PID=""
STARTING_PGID=""
STARTING_MARKER=""
LAUNCH_ATTEMPTED=false
QUALIFICATION_SYNC_COUNT=false

export HARNESS_V2_HOST=0.0.0.0
export HARNESS_V2_PORT=20815
HEALTH_URL="http://127.0.0.1:${HARNESS_V2_PORT}/health"
export PYTHONPATH="$SOURCE_ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

usage() {
    cat <<'EOF'
Usage: ./start.sh [--qualification-sync-count]

Starts Agent Builder on 0.0.0.0:20815 using only checkout-local state.

--qualification-sync-count  Explicitly instrument supervisor, Gateway and
                            Workers for bounded libc sync-call qualification.
                            This is not a normal operating mode.
EOF
}

if (($#)); then
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --qualification-sync-count) QUALIFICATION_SYNC_COUNT=true ;;
        *) printf '[agent-builder start] ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    (($# == 1)) || { printf '[agent-builder start] ERROR: too many arguments\n' >&2; usage >&2; exit 2; }
fi

log() { printf '[agent-builder start] %s\n' "$*"; }
fail() { printf '[agent-builder start] ERROR: %s\n' "$*" >&2; return 1; }

process_marker() {
    agent_builder_process_marker "$1"
}

record_value() {
    local key="$1"
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$PID_FILE"
}

safe_pid_record() {
    agent_builder_private_pid_record "$PID_FILE" 4096 \
        && agent_builder_gateway_record_shape_valid "$PID_FILE"
}

GATEWAY_PID=""
GATEWAY_PGID=""
GATEWAY_MARKER=""
WEB_PID=""
WEB_MARKER=""
GATEWAY_SYNC_COUNTER=""
gateway_identity_valid() {
    agent_builder_gateway_chain_valid \
        "$ROOT" "$GATEWAY_PID" "$GATEWAY_PGID" "$GATEWAY_MARKER" \
        "$WEB_PID" "$WEB_MARKER" "$GATEWAY_SYNC_COUNTER"
}

load_managed_gateway() {
    local schema role recorded_root attempt
    safe_pid_record || return 1
    schema="$(record_value schema)"
    role="$(record_value role)"
    GATEWAY_PID="$(record_value pid)"
    GATEWAY_PGID="$(record_value pgid)"
    GATEWAY_MARKER="$(record_value marker)"
    WEB_PID="$(record_value web_pid)"
    WEB_MARKER="$(record_value web_marker)"
    GATEWAY_SYNC_COUNTER="$(record_value sync_counter)"
    recorded_root="$(record_value root)"
    [[ "$schema" == 1 && "$role" == gateway ]] || return 1
    [[ "$GATEWAY_PID" =~ ^[0-9]+$ && "$GATEWAY_PID" -gt 1 ]] || return 1
    [[ "$GATEWAY_PGID" == "$GATEWAY_PID" && -n "$GATEWAY_MARKER" ]] || return 1
    [[ "$WEB_PID" =~ ^[0-9]+$ && "$WEB_PID" -gt 1 \
        && "$WEB_PID" != "$GATEWAY_PID" && -n "$WEB_MARKER" ]] || return 1
    [[ -z "$GATEWAY_SYNC_COUNTER" \
        || "$GATEWAY_SYNC_COUNTER" == libc-sync-calls-v1 ]] || return 1
    [[ "$recorded_root" == "$ROOT" ]] || return 1
    kill -0 "$GATEWAY_PID" 2>/dev/null || return 1

    for ((attempt = 0; attempt < 3; attempt++)); do
        gateway_identity_valid && return 0
        sleep 0.1
    done
    return 1
}

tcp_open() {
    (exec 9<>"/dev/tcp/$1/$2") >/dev/null 2>&1
}

health_ok() {
    curl --noproxy '*' --fail --silent --show-error --max-time 2 "$HEALTH_URL" \
        >/dev/null 2>&1
}

group_alive() {
    [[ "$1" =~ ^[0-9]+$ ]] && kill -0 -- "-$1" 2>/dev/null
}

starting_identity_valid() {
    local expected_sync=""
    [[ "$STARTING_PID" =~ ^[0-9]+$ && "$STARTING_PID" -gt 1 ]] || return 1
    [[ "$STARTING_PGID" == "$STARTING_PID" && -n "$STARTING_MARKER" ]] || return 1
    if [[ "$QUALIFICATION_SYNC_COUNT" == true ]]; then
        expected_sync=libc-sync-calls-v1
    fi
    agent_builder_supervisor_identity_valid \
        "$ROOT" "$STARTING_PID" "$STARTING_PGID" "$STARTING_MARKER" \
        "$expected_sync"
}

starting_direct_child_valid() {
    local current_marker
    [[ "$STARTING_PID" =~ ^[0-9]+$ && "$STARTING_PID" -gt 1 \
        && -n "$STARTING_MARKER" ]] || return 1
    kill -0 "$STARTING_PID" 2>/dev/null || return 1
    current_marker="$(process_marker "$STARTING_PID" 2>/dev/null || true)"
    [[ "$current_marker" == "$STARTING_MARKER" ]]
}

starting_child_reapable() {
    local current_marker process_state
    kill -0 "$STARTING_PID" 2>/dev/null || return 0
    [[ -n "$STARTING_MARKER" ]] || return 1
    current_marker="$(process_marker "$STARTING_PID" 2>/dev/null || true)"
    [[ "$current_marker" == "$STARTING_MARKER" ]] || return 1
    process_state="$(ps -o stat= -p "$STARTING_PID" 2>/dev/null | tr -d '[:space:]')"
    [[ "$process_state" == Z* ]]
}

adopt_starting_record() {
    if load_managed_gateway && [[ "$GATEWAY_PID" == "$STARTING_PID" ]]; then
        STARTING_PGID="$GATEWAY_PGID"
        STARTING_MARKER="$GATEWAY_MARKER"
        return 0
    fi
    return 1
}

remove_own_pid_record() {
    local recorded_pid recorded_pgid recorded_marker recorded_root
    safe_pid_record || return 0
    recorded_pid="$(record_value pid)"
    recorded_pgid="$(record_value pgid)"
    recorded_marker="$(record_value marker)"
    recorded_root="$(record_value root)"
    if [[ "$recorded_pid" == "$STARTING_PID" \
        && "$recorded_pgid" == "$STARTING_PGID" \
        && "$recorded_marker" == "$STARTING_MARKER" \
        && "$recorded_root" == "$ROOT" ]]; then
        rm -f -- "$PID_FILE"
    fi
}

rollback() {
    local status="$1" tick
    trap - ERR INT TERM
    if [[ "$STARTING_PID" =~ ^[0-9]+$ ]]; then
        if ! starting_identity_valid; then
            for ((tick = 0; tick < 20; tick++)); do
                adopt_starting_record && break
                starting_direct_child_valid || break
                sleep 0.05
            done
        fi
        if starting_identity_valid; then
            kill -TERM -- "-$STARTING_PGID" 2>/dev/null || true
        elif starting_direct_child_valid; then
            kill -TERM -- "$STARTING_PID" 2>/dev/null || true
        elif group_alive "$STARTING_PGID"; then
            printf '[agent-builder start] WARNING: startup process identity changed; refusing cached PGID signal\n' >&2
        fi
        for ((tick = 0; tick < 30; tick++)); do
            starting_child_reapable && break
            adopt_starting_record || true
            sleep 0.1
        done
        if starting_identity_valid; then
            kill -KILL -- "-$STARTING_PGID" 2>/dev/null || true
        elif starting_direct_child_valid; then
            kill -KILL -- "$STARTING_PID" 2>/dev/null || true
        fi
        for ((tick = 0; tick < 20; tick++)); do
            starting_child_reapable && break
            sleep 0.05
        done
        if starting_child_reapable; then
            wait "$STARTING_PID" 2>/dev/null || true
        fi
        if [[ -n "$STARTING_PGID" ]] && ! group_alive "$STARTING_PGID"; then
            remove_own_pid_record
        fi
    fi
    if [[ "$LAUNCH_ATTEMPTED" == true ]]; then
        flock -u 8 2>/dev/null || true
        exec 8>&-
        "$ROOT/stop.sh" --force \
            || printf '[agent-builder start] WARNING: residual Worker cleanup was incomplete\n' >&2
    fi
    exit "$status"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT TERM

wait_for_health() {
    local tick
    for ((tick = 0; tick < 150; tick++)); do
        load_managed_gateway || fail "gateway exited before becoming healthy; see $LOG_FILE"
        health_ok && return 0
        sleep 0.2
    done
    fail "gateway did not become healthy within 30 seconds; see $LOG_FILE"
}

[[ -f "$SOURCE_ROOT/agent_builder_v2/web.py" && ! -L "$SOURCE_ROOT/agent_builder_v2/web.py" ]] \
    || fail "gateway module is missing: $SOURCE_ROOT/agent_builder_v2/web.py"
[[ -f "$LOG_SUPERVISOR" && ! -L "$LOG_SUPERVISOR" ]] \
    || fail "log supervisor is missing or unsafe: $LOG_SUPERVISOR"
command -v ps >/dev/null 2>&1 || fail "ps is required for process validation"
command -v flock >/dev/null 2>&1 || fail "flock is required for lifecycle serialization"
command -v curl >/dev/null 2>&1 || fail "curl is required for the health check"
[[ -x "$ROOT/bootstrap.sh" ]] || fail "bootstrap.sh is missing or not executable"

agent_builder_reject_symlink_path "$RUNTIME_ROOT" || fail "control-plane runtime path is unsafe"
agent_builder_ensure_directory "$RUNTIME_ROOT" || fail "cannot create control-plane runtime root"
agent_builder_reject_symlink_path "$PID_FILE" || fail "gateway PID path is unsafe"
agent_builder_reject_symlink_path "$LOG_FILE" || fail "gateway log path is unsafe"
agent_builder_reject_symlink_path "$LOCK_FILE" || fail "lifecycle lock path is unsafe"
if [[ -e "$LOCK_FILE" ]]; then
    [[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" \
        && "$(stat -c '%h' -- "$LOCK_FILE" 2>/dev/null || true)" == 1 ]] \
        || fail "lifecycle lock is not a private regular file"
fi
exec 8>>"$LOCK_FILE"
chmod 0600 -- "$LOCK_FILE"
[[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" \
    && "$(stat -c '%h' -- "$LOCK_FILE" 2>/dev/null || true)" == 1 ]] \
    || fail "lifecycle lock became unsafe while opening"
flock -w 30 8 || fail "timed out waiting for another V2 lifecycle command"

if load_managed_gateway; then
    health_ok || fail "gateway is running but unhealthy; see $LOG_FILE"
    if [[ "$QUALIFICATION_SYNC_COUNT" == true \
        && "$(record_value sync_counter)" != "libc-sync-calls-v1" ]]; then
        fail "the running Gateway was not started in sync-counter qualification mode"
    fi
    log "gateway is already healthy (pid $GATEWAY_PID)"
    trap - ERR INT TERM
    exit 0
fi

log "checking the frozen checkout-local Python environment"
"$ROOT/bootstrap.sh"
# Bootstrap may have created the managed interpreter and environment paths.
source "$ROOT/env.sh"
export PYTHONPATH="$SOURCE_ROOT"
[[ -x "$PYTHON" ]] || fail "checkout-local Python is missing after bootstrap"

if [[ -e "$PID_FILE" || -L "$PID_FILE" ]]; then
    safe_pid_record || fail "refusing unsafe gateway PID record: $PID_FILE"
    stale_pid="$(record_value pid)"
    if [[ "$stale_pid" =~ ^[0-9]+$ ]] && kill -0 "$stale_pid" 2>/dev/null; then
        fail "PID record names a live process that failed ownership validation; inspect $PID_FILE"
    fi
    log "removing stale gateway PID record"
    rm -f -- "$PID_FILE"
fi

if tcp_open 127.0.0.1 "$HARNESS_V2_PORT"; then
    fail "port 0.0.0.0:$HARNESS_V2_PORT conflicts with an unmanaged listener"
fi

SUPERVISOR_QUALIFICATION_ARGS=()
if [[ "$QUALIFICATION_SYNC_COUNT" == true ]]; then
    [[ -f "$SYNC_COUNTER_TOOL" && ! -L "$SYNC_COUNTER_TOOL" ]] \
        || fail "sync counter qualification tool is missing or unsafe"
    log "preparing and self-testing the bounded libc sync-call counter"
    "$PYTHON" "$SYNC_COUNTER_TOOL" prepare \
        || fail "sync counter qualification preparation failed"
    [[ -f "$SYNC_COUNTER_LIBRARY" && ! -L "$SYNC_COUNTER_LIBRARY" \
        && "$(stat -c '%h' -- "$SYNC_COUNTER_LIBRARY" 2>/dev/null || true)" == 1 \
        && "$(stat -c '%a' -- "$SYNC_COUNTER_LIBRARY" 2>/dev/null || true)" == 500 ]] \
        || fail "prepared sync counter library is unsafe"
    [[ -f "$SYNC_COUNTER_FILE" && ! -L "$SYNC_COUNTER_FILE" \
        && "$(stat -c '%h' -- "$SYNC_COUNTER_FILE" 2>/dev/null || true)" == 1 \
        && "$(stat -c '%a' -- "$SYNC_COUNTER_FILE" 2>/dev/null || true)" == 600 \
        && "$(stat -c '%s' -- "$SYNC_COUNTER_FILE" 2>/dev/null || true)" == 4096 ]] \
        || fail "prepared sync counter page is unsafe"
    SUPERVISOR_QUALIFICATION_ARGS+=(--qualification-sync-counter)
    LD_PRELOAD="$SYNC_COUNTER_LIBRARY" \
    _AGENT_BUILDER_QUALIFICATION_SYNC_COUNTER=1 \
    _AGENT_BUILDER_SYNC_COUNTER_FILE="$SYNC_COUNTER_FILE" \
    _AGENT_BUILDER_SYNC_COUNTER_ROLE=supervisor \
    _AGENT_BUILDER_SYNC_COUNTER_REQUIRED=1 \
    "$PYTHON" "$LOG_SUPERVISOR" \
        --new-session \
        --clean-env \
        "${SUPERVISOR_QUALIFICATION_ARGS[@]}" \
        --runtime-root "$RUNTIME_ROOT" \
        --log-file "$LOG_FILE" \
        --pid-file "$PID_FILE" \
        --max-bytes 5242880 \
        --backups 3 \
        -- \
        "$PYTHON" -m agent_builder_v2.web \
        </dev/null >/dev/null 2>&1 8>&- &
else
    "$PYTHON" "$LOG_SUPERVISOR" \
        --new-session \
        --clean-env \
        --runtime-root "$RUNTIME_ROOT" \
        --log-file "$LOG_FILE" \
        --pid-file "$PID_FILE" \
        --max-bytes 5242880 \
        --backups 3 \
        -- \
        "$PYTHON" -m agent_builder_v2.web \
        </dev/null >/dev/null 2>&1 8>&- &
fi
STARTING_PID=$!
LAUNCH_ATTEMPTED=true
STARTING_MARKER="$(process_marker "$STARTING_PID" 2>/dev/null || true)"
record_ready=false
for ((record_tick = 0; record_tick < 100; record_tick++)); do
    kill -0 "$STARTING_PID" 2>/dev/null \
        || fail "gateway failed to launch; see $LOG_FILE"
    if adopt_starting_record; then
        record_ready=true
        break
    fi
    sleep 0.02
done
[[ "$record_ready" == true ]] \
    || fail "gateway supervisor did not publish its identity; see $LOG_FILE"
starting_identity_valid || fail "gateway identity changed during startup"
wait_for_health

log "gateway is healthy at http://0.0.0.0:$HARNESS_V2_PORT (pid $STARTING_PID)"
STARTING_PID=""
STARTING_PGID=""
trap - ERR INT TERM
