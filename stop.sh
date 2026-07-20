#!/usr/bin/env bash
# Stop the validated Agent Builder gateway and every managed Worker process.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=env.sh
if ! source "$ROOT/env.sh"; then
    printf '[agent-builder stop] ERROR: could not load the contained root environment\n' >&2
    exit 1
fi
cd "$ROOT"
umask 077

export HARNESS_V2_HOST=0.0.0.0
export HARNESS_V2_PORT=20815

RUNTIME_ROOT="$AGENT_BUILDER_RUNTIME_DIR/control-plane"
AGENTS_ROOT="$AGENT_BUILDER_RUNTIME_DIR/agents"
PID_FILE="$RUNTIME_ROOT/gateway.pid"
LOCK_FILE="$RUNTIME_ROOT/lifecycle.lock"
LOG_SUPERVISOR="$ROOT/scripts/log_supervisor.py"
IDENTITY_HELPER="$ROOT/scripts/process_identity.sh"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -f "$IDENTITY_HELPER" || -L "$IDENTITY_HELPER" \
    || "$(stat -c '%u' -- "$IDENTITY_HELPER" 2>/dev/null || true)" != "$EUID" \
    || "$(stat -c '%h' -- "$IDENTITY_HELPER" 2>/dev/null || true)" != 1 ]]; then
    printf '[agent-builder stop] ERROR: process identity helper is missing or unsafe\n' >&2
    exit 1
fi
# shellcheck source=scripts/process_identity.sh
source "$IDENTITY_HELPER"
FORCE=false
FAILURES=0
GATEWAY_VALID=false
GATEWAY_PID=""
GATEWAY_PGID=""
GATEWAY_MARKER=""
WEB_PID=""
WEB_MARKER=""
GATEWAY_SYNC_COUNTER=""
WORKER_PIDS=()
WORKER_PGIDS=()
WORKER_FILES=()
WORKER_MARKERS=()
WORKER_INTERPRETERS=()
WORKER_CWDS=()
SCAN_DIR=""

usage() {
    cat <<'EOF'
Usage: ./stop.sh [--force]

Stops the validated gateway and every validated per-Run Worker.
Normal shutdown allows 15 seconds before SIGKILL; --force allows one second.
An occupied port is never used as authority to kill a process.
EOF
}

while (($#)); do
    case "$1" in
        --force|-f) FORCE=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf '[agent-builder stop] ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '[agent-builder stop] %s\n' "$*"; }
warn() { printf '[agent-builder stop] WARNING: %s\n' "$*" >&2; }

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

gateway_identity_valid() {
    agent_builder_gateway_chain_valid \
        "$ROOT" "$GATEWAY_PID" "$GATEWAY_PGID" "$GATEWAY_MARKER" \
        "$WEB_PID" "$WEB_MARKER" "$GATEWAY_SYNC_COUNTER"
}

gateway_supervisor_identity_valid() {
    agent_builder_supervisor_identity_valid \
        "$ROOT" "$GATEWAY_PID" "$GATEWAY_PGID" "$GATEWAY_MARKER" \
        "$GATEWAY_SYNC_COUNTER"
}

gateway_record_matches() {
    safe_pid_record \
        && [[ "$(record_value pid)" == "$GATEWAY_PID" ]] \
        && [[ "$(record_value pgid)" == "$GATEWAY_PGID" ]] \
        && [[ "$(record_value marker)" == "$GATEWAY_MARKER" ]] \
        && [[ "$(record_value root)" == "$ROOT" ]] \
        && [[ "$(record_value web_pid)" == "$WEB_PID" ]] \
        && [[ "$(record_value web_marker)" == "$WEB_MARKER" ]] \
        && [[ "$(record_value sync_counter)" == "$GATEWAY_SYNC_COUNTER" ]]
}

group_alive() {
    [[ "$1" =~ ^[0-9]+$ ]] && kill -0 -- "-$1" 2>/dev/null
}

tcp_open() {
    (exec 9<>"/dev/tcp/$1/$2") >/dev/null 2>&1
}

handle_gateway_record() {
    local candidate_pid candidate_pgid
    if load_managed_gateway; then
        GATEWAY_VALID=true
        return
    fi
    if [[ ! -e "$PID_FILE" && ! -L "$PID_FILE" ]]; then
        log "gateway is already stopped"
        return
    fi
    if ! safe_pid_record; then
        warn "refusing unsafe gateway PID record: $PID_FILE"
        FAILURES=$((FAILURES + 1))
        return
    fi
    candidate_pid="$(record_value pid)"
    candidate_pgid="$(record_value pgid)"
    if [[ "$candidate_pid" =~ ^[0-9]+$ ]] && kill -0 "$candidate_pid" 2>/dev/null; then
        warn "gateway PID record names a live process that failed ownership validation; not killing it"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if [[ "$candidate_pgid" =~ ^[0-9]+$ && "$candidate_pgid" -gt 1 ]] \
        && group_alive "$candidate_pgid"; then
        warn "gateway leader is gone but its recorded process group remains; retaining the PID record"
        FAILURES=$((FAILURES + 1))
        return
    fi
    warn "removing stale gateway PID record"
    rm -f -- "$PID_FILE"
}

WORKER_PID=""
WORKER_PGID=""
WORKER_MARKER=""
WORKER_AGENT_ID=""
WORKER_RUN_ID=""
WORKER_INTERPRETER=""
WORKER_CWD=""
worker_record_value() {
    local file="$1" key="$2"
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

safe_worker_pid_file() {
    local file="$1" relative runs leaf remainder
    agent_builder_private_pid_record "$file" 4096 || return 1
    agent_builder_worker_record_shape_valid "$file" || return 1
    agent_builder_reject_symlink_path "$file" >/dev/null 2>&1 || return 1
    relative="${file#"$AGENTS_ROOT"/}"
    [[ "$relative" != "$file" ]] || return 1
    IFS='/' read -r WORKER_AGENT_ID runs WORKER_RUN_ID leaf remainder <<<"$relative"
    [[ -z "${remainder:-}" && "$runs" == runs && "$leaf" == worker.pid ]] || return 1
    [[ "$WORKER_AGENT_ID" =~ ^[a-f0-9-]{32,36}$ ]] || return 1
    [[ "$WORKER_RUN_ID" =~ ^[a-f0-9-]{32,36}$ ]] || return 1
}

# Return 0 for a validated live Worker, 3 for a stale record, 4 for an
# incomplete record, and 2 when identity cannot be proven.
load_managed_worker() {
    local file="$1" schema role recorded_root recorded_agent recorded_run
    local recorded_run_root module command_line
    local expected_run_root expected_command
    safe_worker_pid_file "$file" || return 2
    [[ -s "$file" ]] || return 4
    schema="$(worker_record_value "$file" schema)"
    role="$(worker_record_value "$file" role)"
    WORKER_PID="$(worker_record_value "$file" pid)"
    WORKER_PGID="$(worker_record_value "$file" pgid)"
    WORKER_MARKER="$(worker_record_value "$file" marker)"
    recorded_root="$(worker_record_value "$file" root)"
    recorded_agent="$(worker_record_value "$file" agent_id)"
    recorded_run="$(worker_record_value "$file" run)"
    recorded_run_root="$(worker_record_value "$file" run_root)"
    module="$(worker_record_value "$file" module)"
    WORKER_INTERPRETER="$(worker_record_value "$file" interpreter)"
    WORKER_CWD="$(worker_record_value "$file" cwd)"
    expected_run_root="$AGENTS_ROOT/$WORKER_AGENT_ID/runs/$WORKER_RUN_ID"
    expected_command="$AGENTS_ROOT/$WORKER_AGENT_ID/worker-env/bin/python -m agent_builder_v2.worker"
    if [[ -z "$schema" || -z "$role" || -z "$WORKER_PID" || -z "$WORKER_PGID" \
        || -z "$WORKER_MARKER" || -z "$recorded_root" || -z "$recorded_agent" \
        || -z "$recorded_run" || -z "$recorded_run_root" || -z "$module" \
        || -z "$WORKER_INTERPRETER" || -z "$WORKER_CWD" ]]; then
        return 4
    fi
    [[ "$schema" == 1 && "$role" == worker ]] || return 2
    [[ "$WORKER_PID" =~ ^[0-9]+$ && "$WORKER_PID" -gt 1 ]] || return 2
    [[ "$WORKER_PGID" == "$WORKER_PID" ]] || return 2
    [[ "$WORKER_MARKER" =~ ^linux:[0-9]+$ ]] || return 2
    [[ "$recorded_root" == "$ROOT" && "$recorded_agent" == "$WORKER_AGENT_ID" \
        && "$recorded_run" == "$WORKER_RUN_ID" ]] || return 2
    [[ "$recorded_run_root" == "$expected_run_root" \
        && "$module" == agent_builder_v2.worker \
        && "$WORKER_INTERPRETER" == "$AGENTS_ROOT/$WORKER_AGENT_ID/worker-env/bin/python" \
        && "$WORKER_CWD" == "$expected_run_root/work" ]] || return 2
    command_line="$(worker_record_value "$file" command)"
    [[ "$command_line" == "$expected_command" ]] || return 2
    kill -0 "$WORKER_PID" 2>/dev/null || return 3
    # A live Worker is killable only while it is still a direct child of the
    # fully validated Web process.  The private record alone is not authority.
    [[ "$GATEWAY_VALID" == true ]] || return 2
    gateway_identity_valid || return 2
    if agent_builder_worker_identity_valid \
        "$WORKER_PID" "$WORKER_PGID" "$WORKER_MARKER" "$WEB_PID" \
        "$WORKER_INTERPRETER" "$WORKER_CWD"; then
        return 0
    fi
    kill -0 "$WORKER_PID" 2>/dev/null || return 3
    return 2
}

remove_worker_run_root() {
    local file="$1" expected_pid="$2" expected_marker="$3"
    local relative agent_id runs run_id leaf remainder run_root
    safe_worker_pid_file "$file" || return 1
    [[ "$(worker_record_value "$file" pid)" == "$expected_pid" \
        && "$(worker_record_value "$file" marker)" == "$expected_marker" ]] || return 1
    relative="${file#"$AGENTS_ROOT"/}"
    IFS='/' read -r agent_id runs run_id leaf remainder <<<"$relative"
    [[ -z "${remainder:-}" && "$runs" == runs && "$leaf" == worker.pid ]] || return 1
    [[ "$agent_id" =~ ^[a-f0-9-]{32,36}$ \
        && "$run_id" =~ ^[a-f0-9-]{32,36}$ ]] || return 1
    run_root="$AGENTS_ROOT/$agent_id/runs/$run_id"
    [[ "$file" == "$run_root/worker.pid" && -d "$run_root" && ! -L "$run_root" ]] \
        || return 1
    agent_builder_reject_symlink_path "$run_root" >/dev/null 2>&1 || return 1
    rm -rf --one-file-system -- "$run_root" || return 1
    [[ ! -e "$run_root" && ! -L "$run_root" ]]
}

collect_workers() {
    local file status count=0 unsafe_link worker_scan link_scan
    WORKER_PIDS=()
    WORKER_PGIDS=()
    WORKER_FILES=()
    WORKER_MARKERS=()
    WORKER_INTERPRETERS=()
    WORKER_CWDS=()
    [[ -d "$AGENTS_ROOT" ]] || return
    if ! agent_builder_reject_symlink_path "$AGENTS_ROOT" >/dev/null 2>&1; then
        warn "refusing unsafe Agent runtime root: $AGENTS_ROOT"
        FAILURES=$((FAILURES + 1))
        return
    fi
    worker_scan="$SCAN_DIR/worker-pids"
    link_scan="$SCAN_DIR/unsafe-links"
    : > "$link_scan"
    if ! find -P "$AGENTS_ROOT" -mindepth 1 -maxdepth 1 -type l -print \
        > "$link_scan"; then
        warn "could not completely scan Agent runtime entries"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if ! find -P "$AGENTS_ROOT" -mindepth 2 -maxdepth 4 \
        \( -path "$AGENTS_ROOT/*/runs" -o -path "$AGENTS_ROOT/*/runs/*" \) \
        -type l -print >> "$link_scan"; then
        warn "could not completely scan Run path symlinks"
        FAILURES=$((FAILURES + 1))
        return
    fi
    unsafe_link="$(head -n 1 "$link_scan")"
    if [[ -n "$unsafe_link" ]]; then
        warn "Agent runtime contains a symlink; affected Workers will not be followed: $unsafe_link"
        FAILURES=$((FAILURES + 1))
    fi
    if ! find -P "$AGENTS_ROOT" -mindepth 4 -maxdepth 4 -name worker.pid -print0 \
        > "$worker_scan"; then
        warn "could not completely scan Worker PID records"
        FAILURES=$((FAILURES + 1))
        return
    fi
    while IFS= read -r -d '' file; do
        count=$((count + 1))
        if ((count > 256)); then
            warn "more than 256 Worker PID records exist; refusing an unbounded shutdown scan"
            FAILURES=$((FAILURES + 1))
            break
        fi
        load_managed_worker "$file"
        status=$?
        case "$status" in
            0)
                WORKER_PIDS+=("$WORKER_PID")
                WORKER_PGIDS+=("$WORKER_PGID")
                WORKER_FILES+=("$file")
                WORKER_MARKERS+=("$WORKER_MARKER")
                WORKER_INTERPRETERS+=("$WORKER_INTERPRETER")
                WORKER_CWDS+=("$WORKER_CWD")
                ;;
            3)
                if [[ "$WORKER_PGID" =~ ^[0-9]+$ && "$WORKER_PGID" -gt 1 ]] \
                    && group_alive "$WORKER_PGID"; then
                    warn "Worker leader is gone but its process group remains; retaining: $file"
                    FAILURES=$((FAILURES + 1))
                else
                    warn "removing stale Worker Run root: ${file%/worker.pid}"
                    if ! remove_worker_run_root "$file" "$WORKER_PID" "$WORKER_MARKER"; then
                        warn "could not safely remove stale Worker Run root: $file"
                        FAILURES=$((FAILURES + 1))
                    fi
                fi
                ;;
            4)
                warn "Worker PID record is incomplete; retaining it for a later scan: $file"
                FAILURES=$((FAILURES + 1))
                ;;
            *)
                if [[ -e "$file" || -L "$file" ]]; then
                    warn "Worker PID record failed ownership validation; not killing its process: $file"
                    FAILURES=$((FAILURES + 1))
                fi
                ;;
        esac
    done < "$worker_scan"
}

worker_identity_valid() {
    local index="$1" require_parent="${2:-false}" pid allow_reparented=true
    pid="${WORKER_PIDS[$index]}"
    if [[ "$require_parent" == true ]]; then
        gateway_identity_valid || return 1
        allow_reparented=false
    fi
    agent_builder_worker_identity_valid \
        "$pid" "${WORKER_PGIDS[$index]}" "${WORKER_MARKERS[$index]}" \
        "$WEB_PID" "${WORKER_INTERPRETERS[$index]}" "${WORKER_CWDS[$index]}" \
        "$allow_reparented"
}

worker_record_matches() {
    local index="$1" file="${WORKER_FILES[$1]}"
    safe_worker_pid_file "$file" \
        && [[ "$(worker_record_value "$file" pid)" == "${WORKER_PIDS[$index]}" ]] \
        && [[ "$(worker_record_value "$file" marker)" == "${WORKER_MARKERS[$index]}" ]]
}

signal_workers() {
    local signal_name="$1" require_parent="${2:-false}" index
    for ((index = 0; index < ${#WORKER_PGIDS[@]}; index++)); do
        if worker_identity_valid "$index" "$require_parent"; then
            kill -"$signal_name" -- "-${WORKER_PGIDS[$index]}" 2>/dev/null || true
        elif group_alive "${WORKER_PGIDS[$index]}"; then
            warn "Worker ${WORKER_PIDS[$index]} identity changed; refusing cached PGID signal"
            FAILURES=$((FAILURES + 1))
        fi
    done
}

managed_processes_alive() {
    local index
    if [[ "$GATEWAY_VALID" == true ]] && gateway_supervisor_identity_valid; then
        return 0
    fi
    for ((index = 0; index < ${#WORKER_PGIDS[@]}; index++)); do
        worker_identity_valid "$index" && return 0
    done
    return 1
}

wait_for_managed_processes() {
    local max_ticks="$1" tick
    for ((tick = 0; tick < max_ticks; tick++)); do
        managed_processes_alive || return 0
        sleep 0.2
    done
    ! managed_processes_alive
}

cleanup_worker_records() {
    local index file
    for ((index = 0; index < ${#WORKER_FILES[@]}; index++)); do
        file="${WORKER_FILES[$index]}"
        if ! group_alive "${WORKER_PGIDS[$index]}" && [[ -e "$file" ]]; then
            if worker_record_matches "$index"; then
                if ! remove_worker_run_root \
                    "$file" "${WORKER_PIDS[$index]}" "${WORKER_MARKERS[$index]}"; then
                    warn "could not safely remove stopped Worker Run root: $file"
                    FAILURES=$((FAILURES + 1))
                fi
            fi
        fi
    done
}

stop_residual_workers() {
    local index
    collect_workers
    ((${#WORKER_PGIDS[@]} > 0)) || return
    log "stopping ${#WORKER_PGIDS[@]} residual Worker process(es)"
    signal_workers TERM true
    wait_for_managed_processes 10 || true
    for ((index = 0; index < ${#WORKER_PGIDS[@]}; index++)); do
        if worker_identity_valid "$index"; then
            warn "Worker ${WORKER_PIDS[$index]} ignored SIGTERM; sending SIGKILL"
            kill -KILL -- "-${WORKER_PGIDS[$index]}" 2>/dev/null || true
        elif group_alive "${WORKER_PGIDS[$index]}"; then
            warn "Worker ${WORKER_PIDS[$index]} identity changed; refusing cached PGID SIGKILL"
            FAILURES=$((FAILURES + 1))
        fi
    done
    wait_for_managed_processes 25 || true
    cleanup_worker_records
    for ((index = 0; index < ${#WORKER_PGIDS[@]}; index++)); do
        if worker_identity_valid "$index" || group_alive "${WORKER_PGIDS[$index]}"; then
            warn "Worker process group ${WORKER_PGIDS[$index]} is still alive"
            FAILURES=$((FAILURES + 1))
        fi
    done
}

signal_gateway() {
    local signal_name="$1" require_chain="${2:-false}"
    local identity_valid=false
    if [[ "$require_chain" == true ]] && gateway_identity_valid; then
        identity_valid=true
    elif [[ "$require_chain" != true ]] && gateway_supervisor_identity_valid; then
        identity_valid=true
    fi
    if [[ "$identity_valid" == true ]]; then
        kill -"$signal_name" -- "-$GATEWAY_PGID" 2>/dev/null || true
    elif group_alive "$GATEWAY_PGID"; then
        warn "gateway identity changed; refusing cached PGID signal"
        FAILURES=$((FAILURES + 1))
    fi
}

cleanup_scan() {
    [[ -n "$SCAN_DIR" && "$SCAN_DIR" == "$RUNTIME_ROOT"/.stop-scan.* ]] || return
    rm -f -- "$SCAN_DIR/worker-pids" "$SCAN_DIR/unsafe-links"
    rmdir -- "$SCAN_DIR" 2>/dev/null || true
}

if ! agent_builder_reject_symlink_path "$RUNTIME_ROOT" >/dev/null 2>&1; then
    warn "control-plane runtime path is unsafe: $RUNTIME_ROOT"
    exit 1
fi
if ! agent_builder_ensure_directory "$RUNTIME_ROOT"; then
    warn "cannot create the control-plane runtime root"
    exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
    warn "flock is required for lifecycle serialization"
    exit 1
fi
if ! agent_builder_reject_symlink_path "$LOCK_FILE" >/dev/null 2>&1; then
    warn "lifecycle lock path is unsafe: $LOCK_FILE"
    exit 1
fi
if [[ -e "$LOCK_FILE" ]] && [[ ! -f "$LOCK_FILE" || -L "$LOCK_FILE" \
    || "$(stat -c '%h' -- "$LOCK_FILE" 2>/dev/null || true)" != 1 ]]; then
    warn "lifecycle lock is not a private regular file"
    exit 1
fi
if ! exec 8>>"$LOCK_FILE"; then
    warn "could not open the lifecycle lock"
    exit 1
fi
chmod 0600 -- "$LOCK_FILE"
if [[ ! -f "$LOCK_FILE" || -L "$LOCK_FILE" \
    || "$(stat -c '%h' -- "$LOCK_FILE" 2>/dev/null || true)" != 1 ]]; then
    warn "lifecycle lock became unsafe while opening"
    exit 1
fi
if ! flock -w 30 8; then
    warn "timed out waiting for another V2 lifecycle command"
    exit 1
fi
SCAN_DIR="$(mktemp -d "$RUNTIME_ROOT/.stop-scan.XXXXXX")" || {
    warn "could not create the bounded Worker scan directory"
    exit 1
}
chmod 0700 -- "$SCAN_DIR"
trap cleanup_scan EXIT
trap 'cleanup_scan; exit 130' INT TERM

handle_gateway_record
collect_workers

if ((${#WORKER_PGIDS[@]} > 0)); then
    log "stopping ${#WORKER_PGIDS[@]} validated Worker process(es)"
    # Preserve the verified Web -> Worker relationship until every initial
    # Worker has received its shutdown signal.
    signal_workers TERM true
fi
if [[ "$GATEWAY_VALID" == true ]]; then
    log "stopping gateway process group $GATEWAY_PGID"
    signal_gateway TERM true
fi

if [[ "$FORCE" == true ]]; then
    grace_ticks=5
else
    grace_ticks=75
fi
wait_for_managed_processes "$grace_ticks" || true

# KILL follows the same Worker-first order.  Cached Worker ownership remains
# bound to the PID/start marker that was accepted while Web was its parent.
signal_workers KILL
if [[ "$GATEWAY_VALID" == true ]] && gateway_supervisor_identity_valid; then
    warn "gateway exceeded its grace period; sending SIGKILL"
    signal_gateway KILL false
elif [[ "$GATEWAY_VALID" == true ]] && group_alive "$GATEWAY_PGID"; then
    warn "gateway group remains but its leader identity cannot be revalidated"
    FAILURES=$((FAILURES + 1))
fi
wait_for_managed_processes 25 || true
cleanup_worker_records

if [[ "$GATEWAY_VALID" == true ]]; then
    if gateway_supervisor_identity_valid || group_alive "$GATEWAY_PGID"; then
        warn "gateway process group $GATEWAY_PGID is still alive"
        FAILURES=$((FAILURES + 1))
    else
        if gateway_record_matches; then
            rm -f -- "$PID_FILE"
            log "gateway stopped"
        elif [[ -e "$PID_FILE" || -L "$PID_FILE" ]]; then
            warn "gateway PID record changed during shutdown; refusing to delete it"
            FAILURES=$((FAILURES + 1))
        fi
    fi
fi

# A gateway can race with the first scan while shutting down. Scan once more
# after it is gone and terminate only Workers that pass the same validation.
stop_residual_workers

if tcp_open 127.0.0.1 "$HARNESS_V2_PORT"; then
    warn "port 0.0.0.0:$HARNESS_V2_PORT remains occupied by an unmanaged or unvalidated process"
    FAILURES=$((FAILURES + 1))
fi

if ((FAILURES > 0)); then
    warn "shutdown incomplete ($FAILURES validation or termination failure(s))"
    exit 1
fi
log "gateway and Workers are stopped"
