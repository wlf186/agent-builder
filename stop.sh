#!/usr/bin/env bash
# Stop only processes that were started and recorded by this checkout.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=env.sh
source "$ROOT/env.sh"
cd "$ROOT"

FORCE=false
ONLY_SERVICE=""
FAILURES=0

usage() {
    cat <<'EOF'
Usage: ./stop.sh [--force] [--service phoenix|backend|frontend|docs]

The normal path waits up to 15 seconds before escalating to SIGKILL. --force
uses a one-second grace period. Processes not owned by this checkout are never
killed; an occupied unmanaged port makes the command fail.
EOF
}

while (($#)); do
    case "$1" in
        --force|-f) FORCE=true; shift ;;
        --service)
            (($# >= 2)) || { usage >&2; exit 2; }
            ONLY_SERVICE="$2"; shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$ONLY_SERVICE" in
    ""|phoenix|backend|frontend|docs) ;;
    *) echo "Unknown service: $ONLY_SERVICE" >&2; exit 2 ;;
esac

log() { printf '[stop] %s\n' "$*"; }
warn() { printf '[stop] WARNING: %s\n' "$*" >&2; }
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

group_alive() {
    kill -0 -- "-$1" 2>/dev/null
}

stop_service() {
    local name="$1" file pid pgid ticks max_ticks
    file="$(pid_file "$name")"
    if ! managed_running "$name"; then
        if [[ -e "$file" || -L "$file" ]]; then
            warn "removing stale or invalid PID record for $name"
            rm -f -- "$file"
        else
            log "$name is already stopped"
        fi
        return 0
    fi

    pid="$(awk -F= '$1 == "pid" {print $2}' "$file")"
    pgid="$(awk -F= '$1 == "pgid" {print $2}' "$file")"
    log "stopping $name process group $pgid"
    kill -TERM -- "-$pgid" 2>/dev/null || true
    if [[ "$FORCE" == true ]]; then max_ticks=5; else max_ticks=75; fi
    for ((ticks = 0; ticks < max_ticks; ticks++)); do
        group_alive "$pgid" || break
        sleep 0.2
    done
    if group_alive "$pgid"; then
        warn "$name exceeded its grace period; sending SIGKILL"
        kill -KILL -- "-$pgid" 2>/dev/null || true
        for ((ticks = 0; ticks < 25; ticks++)); do
            group_alive "$pgid" || break
            sleep 0.2
        done
    fi
    if group_alive "$pgid"; then
        warn "$name process group $pgid is still alive"
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    rm -f -- "$file"
    log "$name stopped"
}

tcp_open() {
    local host="$1" port="$2"
    (exec 9<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

verify_port_closed() {
    local name="$1" host="$2" port="$3"
    if tcp_open "$host" "$port"; then
        warn "$name port $host:$port is still occupied by an unmanaged process"
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    return 0
}

services=(docs frontend backend phoenix)
if [[ -n "$ONLY_SERVICE" ]]; then
    services=("$ONLY_SERVICE")
fi

for service in "${services[@]}"; do
    stop_service "$service" || true
done

for service in "${services[@]}"; do
    case "$service" in
        docs) verify_port_closed docs "$DOCS_HOST" "$DOCS_PORT" || true ;;
        frontend) verify_port_closed frontend "$FRONTEND_HOST" "$FRONTEND_PORT" || true ;;
        backend)
            verify_port_closed backend "$BACKEND_HOST" "$BACKEND_PORT" || true
            verify_port_closed builtin-mcp "$MCP_SSE_HOST" "$MCP_SSE_PORT" || true
            ;;
        phoenix) verify_port_closed phoenix "$PHOENIX_HOST" "$PHOENIX_PORT" || true ;;
    esac
done

if ((FAILURES > 0)); then
    warn "shutdown incomplete ($FAILURES verification failure(s))"
    exit 1
fi
log "all requested services are stopped"
