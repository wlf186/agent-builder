#!/usr/bin/env bash
# Checkout-contained environment shared by lifecycle, tests and maintenance.
# Source this file; it never edits a shell profile.

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    AGENT_BUILDER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
else
    AGENT_BUILDER_ROOT="$(pwd -P)"
fi
export AGENT_BUILDER_ROOT

export AGENT_BUILDER_RUNTIME_DIR="$AGENT_BUILDER_ROOT/.runtime"
export AGENT_BUILDER_TOOLS_DIR="$AGENT_BUILDER_ROOT/.tools"
export AGENT_BUILDER_UV="$AGENT_BUILDER_TOOLS_DIR/uv"

# An inherited package environment must never redirect this checkout.
for agent_builder_prefix in CONDA_ PIP_ UV_ npm_config_ NPM_CONFIG_; do
    while IFS= read -r agent_builder_variable; do
        unset "$agent_builder_variable"
    done < <(compgen -A variable "$agent_builder_prefix" || true)
done
unset agent_builder_prefix agent_builder_variable
unset _CE_CONDA _CE_M VIRTUAL_ENV VIRTUAL_ENV_PROMPT
unset PYTHONHOME PYTHONUSERBASE PYTHONINSPECT PYTHONSTARTUP PYTHONPATH
unset LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH
unset BASH_ENV ENV CDPATH

agent_builder_env_fail() {
    printf 'Agent Builder environment error: %s\n' "$*" >&2
    return 1
}

# Refuse a symlink at every existing component before a managed path is used.
agent_builder_reject_symlink_path() {
    local requested="$1" relative current component
    case "$requested" in
        "$AGENT_BUILDER_ROOT") return 0 ;;
        "$AGENT_BUILDER_ROOT"/*) ;;
        *) agent_builder_env_fail "managed path is outside the checkout: $requested"; return 1 ;;
    esac
    relative="${requested#"$AGENT_BUILDER_ROOT"/}"
    current="$AGENT_BUILDER_ROOT"
    while [[ -n "$relative" ]]; do
        if [[ "$relative" == */* ]]; then
            component="${relative%%/*}"
            relative="${relative#*/}"
        else
            component="$relative"
            relative=""
        fi
        [[ -n "$component" ]] || continue
        current="$current/$component"
        if [[ -L "$current" ]]; then
            agent_builder_env_fail "refusing managed symlink path: $current"
            return 1
        fi
    done
}

agent_builder_ensure_directory() {
    local directory="$1"
    agent_builder_reject_symlink_path "$directory" || return 1
    mkdir -p -- "$directory" || return 1
    chmod 0700 -- "$directory" || return 1
}

export HOME="$AGENT_BUILDER_RUNTIME_DIR/home"
export TMPDIR="$AGENT_BUILDER_RUNTIME_DIR/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export XDG_CACHE_HOME="$AGENT_BUILDER_RUNTIME_DIR/cache"
export XDG_CONFIG_HOME="$AGENT_BUILDER_RUNTIME_DIR/config"
export XDG_DATA_HOME="$AGENT_BUILDER_RUNTIME_DIR/share"
export XDG_STATE_HOME="$AGENT_BUILDER_RUNTIME_DIR/state"
export XDG_RUNTIME_DIR="$AGENT_BUILDER_RUNTIME_DIR/xdg-runtime"

export UV_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/uv"
export UV_PYTHON_INSTALL_DIR="$AGENT_BUILDER_RUNTIME_DIR/python"
export UV_PROJECT_ENVIRONMENT="$AGENT_BUILDER_ROOT/.venv"
export UV_TOOL_DIR="$AGENT_BUILDER_RUNTIME_DIR/tools"
export UV_TOOL_BIN_DIR="$AGENT_BUILDER_RUNTIME_DIR/bin"
export UV_LINK_MODE=copy
export UV_NO_PROGRESS=1
export PIP_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PYTHONPYCACHEPREFIX="$AGENT_BUILDER_RUNTIME_DIR/cache/pycache"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export DO_NOT_TRACK=1

# These are product invariants, not caller-selectable deployment settings.
export HARNESS_V2_HOST=0.0.0.0
export HARNESS_V2_PORT=20815
export PYTHONPATH="$AGENT_BUILDER_ROOT/src"
export PATH="$AGENT_BUILDER_TOOLS_DIR:$AGENT_BUILDER_ROOT/.venv/bin:$UV_TOOL_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

umask 077
for agent_builder_path in \
    "$AGENT_BUILDER_RUNTIME_DIR" "$AGENT_BUILDER_TOOLS_DIR" \
    "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" \
    "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR" \
    "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$UV_TOOL_DIR" \
    "$UV_TOOL_BIN_DIR" "$PIP_CACHE_DIR" \
    "$AGENT_BUILDER_RUNTIME_DIR/control-plane" \
    "$AGENT_BUILDER_RUNTIME_DIR/secrets" \
    "$AGENT_BUILDER_RUNTIME_DIR/agents" \
    "$AGENT_BUILDER_ROOT/data" "$AGENT_BUILDER_ROOT/data/agents"; do
    agent_builder_ensure_directory "$agent_builder_path" \
        || return 1 2>/dev/null || exit 1
done
unset agent_builder_path
