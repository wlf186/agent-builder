#!/usr/bin/env bash
# Linux process-identity primitives shared by the lifecycle entrypoints.
# This file is sourced; it intentionally performs no work at load time.

agent_builder_process_marker() {
    local pid="$1" process_stat marker
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
    process_stat="$(<"/proc/$pid/stat")" || return 1
    process_stat="${process_stat##*) }"
    marker="$(awk '{print $20}' <<<"$process_stat")"
    [[ "$marker" =~ ^[0-9]+$ ]] || return 1
    printf 'linux:%s\n' "$marker"
}

agent_builder_process_relationship() {
    local pid="$1" process_stat state parent group
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
    process_stat="$(<"/proc/$pid/stat")" || return 1
    process_stat="${process_stat##*) }"
    read -r state parent group _ <<<"$process_stat"
    [[ -n "$state" && "$parent" =~ ^[0-9]+$ && "$group" =~ ^[0-9]+$ ]] \
        || return 1
    printf '%s %s\n' "$parent" "$group"
}

# Return 0 for an exact argv match, 1 for a readable mismatch, and 2 when
# procfs did not permit a conclusive read.  Callers must never flatten argv:
# doing so makes spaces and injected trailing arguments ambiguous.
agent_builder_process_argv_matches() {
    local pid="$1" index
    shift
    local -a expected=("$@") actual=()
    [[ "$pid" =~ ^[0-9]+$ && -e "/proc/$pid/cmdline" ]] || return 2
    if ! mapfile -d '' -t actual 2>/dev/null < "/proc/$pid/cmdline"; then
        return 2
    fi
    ((${#actual[@]} == ${#expected[@]})) || return 1
    for ((index = 0; index < ${#expected[@]}; index++)); do
        [[ "${actual[$index]}" == "${expected[$index]}" ]] || return 1
    done
}

# Return 0 and print cwd, or 2 when procfs intentionally hides it (as it does
# for a non-dumpable Worker from a sibling lifecycle process).
agent_builder_process_cwd() {
    local pid="$1" cwd
    [[ "$pid" =~ ^[0-9]+$ && -e "/proc/$pid" ]] || return 2
    cwd="$(readlink -- "/proc/$pid/cwd" 2>/dev/null)" || return 2
    [[ "$cwd" == /* ]] || return 2
    printf '%s\n' "$cwd"
}

agent_builder_process_status_value() {
    local pid="$1" key="$2"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/status" ]] || return 1
    awk -F: -v key="$key" '$1 == key {
        value=$2
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        print value
        found=1
        exit
    } END {if (!found) exit 1}' "/proc/$pid/status" 2>/dev/null
}

agent_builder_private_pid_record() {
    local file="$1" maximum_size="${2:-4096}" owner mode links size
    [[ -f "$file" && ! -L "$file" ]] || return 1
    read -r owner mode links size < <(
        stat -c '%u %a %h %s' -- "$file" 2>/dev/null
    ) || return 1
    [[ "$owner" == "$EUID" && "$mode" == 600 && "$links" == 1 \
        && "$size" =~ ^[0-9]+$ && "$size" -gt 0 \
        && "$maximum_size" =~ ^[0-9]+$ && "$size" -le "$maximum_size" ]]
}

agent_builder_gateway_record_shape_valid() {
    local file="$1"
    awk -F= '
        BEGIN {
            required["schema"]=1; required["role"]=1; required["pid"]=1
            required["pgid"]=1; required["marker"]=1; required["root"]=1
            required["web_pid"]=1; required["web_marker"]=1
            allowed["sync_counter"]=1
        }
        {
            key=$1
            if (NF < 2 || key == "" || seen[key]++ || !(key in required || key in allowed)) {
                bad=1
            }
        }
        END {
            for (key in required) if (!(key in seen)) bad=1
            if (bad || (NR != 8 && NR != 9) || (NR == 9 && !("sync_counter" in seen))) {
                exit 1
            }
        }
    ' "$file" 2>/dev/null
}

agent_builder_worker_record_shape_valid() {
    local file="$1"
    awk -F= '
        BEGIN {
            required["schema"]=1; required["role"]=1; required["pid"]=1
            required["pgid"]=1; required["marker"]=1; required["root"]=1
            required["agent_id"]=1; required["run"]=1; required["run_root"]=1
            required["module"]=1; required["interpreter"]=1; required["cwd"]=1
            required["command"]=1
        }
        {
            key=$1
            if (NF < 2 || key == "" || seen[key]++ || !(key in required)) bad=1
        }
        END {
            for (key in required) if (!(key in seen)) bad=1
            if (bad || NR != 13) exit 1
        }
    ' "$file" 2>/dev/null
}

agent_builder_supervisor_identity_valid() {
    local root="$1" pid="$2" pgid="$3" marker="$4" sync_counter="${5:-}"
    local current_marker current_parent current_group current_cwd argv_status
    local python="$root/.venv/bin/python"
    local supervisor="$root/scripts/log_supervisor.py"
    local runtime="$root/.runtime/control-plane"
    local -a expected=(
        "$python" "$supervisor" --new-session --clean-env
    )
    if [[ -n "$sync_counter" ]]; then
        [[ "$sync_counter" == libc-sync-calls-v1 ]] || return 1
        expected+=(--qualification-sync-counter)
    fi
    expected+=(
        --runtime-root "$runtime"
        --log-file "$runtime/gateway.log"
        --pid-file "$runtime/gateway.pid"
        --max-bytes 5242880
        --backups 3
        --
        "$python" -m agent_builder_v2.web
    )

    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 && "$pgid" == "$pid" \
        && "$marker" =~ ^linux:[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    current_marker="$(agent_builder_process_marker "$pid" 2>/dev/null)" || return 1
    read -r current_parent current_group < <(
        agent_builder_process_relationship "$pid"
    ) || return 1
    current_cwd="$(agent_builder_process_cwd "$pid" 2>/dev/null)" || return 1
    [[ "$current_marker" == "$marker" && "$current_parent" -ge 1 \
        && "$current_group" == "$pgid" && "$current_cwd" == "$root" ]] \
        || return 1
    if agent_builder_process_argv_matches "$pid" "${expected[@]}"; then
        argv_status=0
    else
        argv_status=$?
    fi
    [[ "$argv_status" == 0 ]]
}

agent_builder_gateway_chain_valid() {
    local root="$1" supervisor_pid="$2" supervisor_pgid="$3"
    local supervisor_marker="$4" web_pid="$5" web_marker="$6"
    local sync_counter="${7:-}"
    local current_marker web_parent web_group web_cwd
    local python="$root/.venv/bin/python"

    agent_builder_supervisor_identity_valid \
        "$root" "$supervisor_pid" "$supervisor_pgid" \
        "$supervisor_marker" "$sync_counter" || return 1
    [[ "$web_pid" =~ ^[0-9]+$ && "$web_pid" -gt 1 \
        && "$web_pid" != "$supervisor_pid" \
        && "$web_marker" =~ ^linux:[0-9]+$ ]] || return 1
    kill -0 "$web_pid" 2>/dev/null || return 1
    current_marker="$(agent_builder_process_marker "$web_pid" 2>/dev/null)" \
        || return 1
    read -r web_parent web_group < <(
        agent_builder_process_relationship "$web_pid"
    ) || return 1
    web_cwd="$(agent_builder_process_cwd "$web_pid" 2>/dev/null)" || return 1
    [[ "$current_marker" == "$web_marker" \
        && "$web_parent" == "$supervisor_pid" \
        && "$web_group" == "$supervisor_pgid" \
        && "$web_cwd" == "$root" ]] || return 1
    agent_builder_process_argv_matches \
        "$web_pid" "$python" -m agent_builder_v2.web
}

agent_builder_worker_sandbox_identity_valid() {
    local pid="$1" pgid="$2" expected_parent="${3:-}"
    local status_pid status_parent status_pgid tracer no_new_privileges
    local seccomp seccomp_filters
    status_pid="$(agent_builder_process_status_value "$pid" Pid 2>/dev/null)" \
        || return 1
    status_parent="$(agent_builder_process_status_value "$pid" PPid 2>/dev/null)" \
        || return 1
    status_pgid="$(agent_builder_process_status_value "$pid" NSpgid 2>/dev/null)" \
        || return 1
    tracer="$(agent_builder_process_status_value "$pid" TracerPid 2>/dev/null)" \
        || return 1
    no_new_privileges="$(
        agent_builder_process_status_value "$pid" NoNewPrivs 2>/dev/null
    )" || return 1
    seccomp="$(agent_builder_process_status_value "$pid" Seccomp 2>/dev/null)" \
        || return 1
    seccomp_filters="$(
        agent_builder_process_status_value "$pid" Seccomp_filters 2>/dev/null
    )" || return 1
    [[ "$status_pid" == "$pid" && "$status_pgid" == "$pgid" \
        && "$tracer" == 0 && "$no_new_privileges" == 1 && "$seccomp" == 2 \
        && "$seccomp_filters" =~ ^[0-9]+$ && "$seccomp_filters" -ge 1 ]] \
        || return 1
    [[ -z "$expected_parent" || "$status_parent" == "$expected_parent" ]]
}

# A live Worker is admitted only with all independent properties present:
# immutable PID/start marker, own process group, sandbox status, exact argv and
# cwd.  A non-dumpable Worker may hide argv/cwd from this sibling; that narrow
# case is accepted only after its sandbox and verified Web parent have matched.
# During shutdown, allow_reparented applies solely to an identity already
# cached under that Web PID.  A readable mismatch is always rejected.
agent_builder_worker_identity_valid() {
    local pid="$1" pgid="$2" marker="$3" verified_web_pid="$4"
    local interpreter="$5" expected_cwd="$6"
    local allow_reparented="${7:-false}" required_live_parent="$verified_web_pid"
    local current_marker current_parent current_group current_cwd
    local argv_status cwd_status

    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 && "$pgid" == "$pid" \
        && "$marker" =~ ^linux:[0-9]+$ && -n "$interpreter" \
        && "$verified_web_pid" =~ ^[0-9]+$ && "$verified_web_pid" -gt 1 \
        && "$expected_cwd" == /* \
        && "$allow_reparented" =~ ^(true|false)$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    current_marker="$(agent_builder_process_marker "$pid" 2>/dev/null)" || return 1
    read -r current_parent current_group < <(
        agent_builder_process_relationship "$pid"
    ) || return 1
    [[ "$current_marker" == "$marker" && "$current_group" == "$pgid" ]] \
        || return 1
    if [[ "$allow_reparented" != true \
        && "$current_parent" != "$verified_web_pid" ]]; then
        return 1
    fi
    if [[ "$allow_reparented" == true ]]; then
        # The process was admitted while this PID/start marker was a child of
        # verified_web_pid.  Web may now have exited during ordered shutdown.
        required_live_parent=""
    fi
    agent_builder_worker_sandbox_identity_valid \
        "$pid" "$pgid" "$required_live_parent" || return 1

    if agent_builder_process_argv_matches \
        "$pid" "$interpreter" -m agent_builder_v2.worker; then
        argv_status=0
    else
        argv_status=$?
    fi
    [[ "$argv_status" == 0 || "$argv_status" == 2 ]] || return 1

    if current_cwd="$(agent_builder_process_cwd "$pid" 2>/dev/null)"; then
        cwd_status=0
    else
        cwd_status=$?
    fi
    if [[ "$cwd_status" == 0 ]]; then
        [[ "$current_cwd" == "$expected_cwd" ]] || return 1
    elif [[ "$cwd_status" != 2 ]]; then
        return 1
    fi
    kill -0 "$pid" 2>/dev/null
}
