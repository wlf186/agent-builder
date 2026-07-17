#!/usr/bin/env bash
# Shared, project-local runtime environment. Source this file from lifecycle
# scripts; it deliberately does not modify a user's shell profile.

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    AGENT_BUILDER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
else
    AGENT_BUILDER_ROOT="$(pwd -P)"
fi
export AGENT_BUILDER_ROOT

export AGENT_BUILDER_RUNTIME_DIR="$AGENT_BUILDER_ROOT/.runtime"
export AGENT_BUILDER_TOOLS_DIR="$AGENT_BUILDER_ROOT/.tools"
export AGENT_BUILDER_UV="$AGENT_BUILDER_TOOLS_DIR/uv"
export AGENT_BUILDER_NODE_HOME="$AGENT_BUILDER_TOOLS_DIR/node"
export AGENT_BUILDER_ENVIRONMENTS_DIR="$AGENT_BUILDER_RUNTIME_DIR/environments"
export AGENT_BUILDER_ORIGINAL_HOME="${AGENT_BUILDER_ORIGINAL_HOME:-${HOME:-}}"

# An activated Conda/venv, pip install target, npm prefix, or uv workspace from
# the invoking shell must not redirect this checkout's managed dependencies.
for agent_builder_prefix in CONDA_ PIP_ UV_ npm_config_ NPM_CONFIG_; do
    while IFS= read -r agent_builder_variable; do
        unset "$agent_builder_variable"
    done < <(compgen -A variable "$agent_builder_prefix" || true)
done
unset agent_builder_prefix agent_builder_variable
unset _CE_CONDA _CE_M VIRTUAL_ENV VIRTUAL_ENV_PROMPT
unset PYTHONHOME PYTHONUSERBASE NODE_PATH NODE_OPTIONS COREPACK_HOME
unset LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH
unset PYTHONINSPECT PYTHONSTARTUP BASH_ENV ENV CDPATH
unset GHTK_AB AGENT_BUILDER_API_TOKEN
unset TEMP TMP XDG_RUNTIME_DIR

agent_builder_env_fail() {
    printf 'Agent Builder environment error: %s\n' "$*" >&2
    return 1
}

# Refuse symlinks at every existing component of a managed path. Checking only
# .runtime itself is insufficient: mkdir/chmod and downstream libraries follow
# a pre-created .runtime/cache (or logs, tmp, state, ...) symlink.
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
export PIP_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/pip"
export npm_config_cache="$AGENT_BUILDER_RUNTIME_DIR/cache/npm"
export NPM_CONFIG_CACHE="$npm_config_cache"
export npm_config_userconfig="$XDG_CONFIG_HOME/npmrc"
export NPM_CONFIG_USERCONFIG="$npm_config_userconfig"
export npm_config_globalconfig="$XDG_CONFIG_HOME/npm-globalrc"
export NPM_CONFIG_GLOBALCONFIG="$npm_config_globalconfig"
export npm_config_prefix="$AGENT_BUILDER_RUNTIME_DIR/npm-prefix"
export NPM_CONFIG_PREFIX="$npm_config_prefix"
export npm_config_update_notifier=false
export npm_config_audit=false
export npm_config_fund=false
export NEXT_TELEMETRY_DISABLED=1
export HF_HOME="$AGENT_BUILDER_RUNTIME_DIR/cache/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export SENTENCE_TRANSFORMERS_HOME="$HF_HOME/sentence-transformers"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$AGENT_BUILDER_RUNTIME_DIR/cache/torch"
export TORCH_EXTENSIONS_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/torch-extensions"
export TORCHINDUCTOR_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/torchinductor"
export TRITON_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/triton"
export NUMBA_CACHE_DIR="$AGENT_BUILDER_RUNTIME_DIR/cache/numba"
export PLAYWRIGHT_BROWSERS_PATH="$AGENT_BUILDER_RUNTIME_DIR/cache/playwright"
export MPLCONFIGDIR="$AGENT_BUILDER_RUNTIME_DIR/config/matplotlib"
export PYTHONPYCACHEPREFIX="$AGENT_BUILDER_RUNTIME_DIR/cache/pycache"
export PYTHONNOUSERSITE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export HF_HUB_DISABLE_TELEMETRY=1
export ANONYMIZED_TELEMETRY=False
export GRADIO_ANALYTICS_ENABLED=False
export DO_NOT_TRACK=1

export BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
export BACKEND_PORT="${BACKEND_PORT:-20881}"
export FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
export FRONTEND_PORT="${FRONTEND_PORT:-20815}"
export DOCS_HOST="${DOCS_HOST:-127.0.0.1}"
export DOCS_PORT="${DOCS_PORT:-4173}"
export MCP_SSE_HOST="${MCP_SSE_HOST:-127.0.0.1}"
export MCP_SSE_PORT="${MCP_SSE_PORT:-20882}"
export AGENT_BUILDER_PACKAGE_ALLOWLIST="${AGENT_BUILDER_PACKAGE_ALLOWLIST:-aiofiles,chromadb,cryptography,httpx,langchain,langchain-core,langchain-ollama,langchain-openai,langchain-text-splitters,langgraph,mcp,openpyxl,pillow,pdfplumber,pypdf2,pypdfium2,python-docx,reportlab,lxml}"
export AGENT_BUILDER_SSRF_ALLOWLIST="${AGENT_BUILDER_SSRF_ALLOWLIST:-open.bigmodel.cn:443,dashscope.aliyuncs.com:443,localhost:11434,127.0.0.1:11434}"
export AGENT_BUILDER_EXECUTION_OUTPUT_LIMIT="${AGENT_BUILDER_EXECUTION_OUTPUT_LIMIT:-1048576}"
export AGENT_BUILDER_EXECUTION_MEMORY_LIMIT="${AGENT_BUILDER_EXECUTION_MEMORY_LIMIT:-4294967296}"
export AGENT_BUILDER_EXECUTION_FILE_LIMIT="${AGENT_BUILDER_EXECUTION_FILE_LIMIT:-104857600}"
export AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT="${AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT:-536870912}"
export AGENT_BUILDER_EXECUTION_PROCESS_LIMIT="${AGENT_BUILDER_EXECUTION_PROCESS_LIMIT:-64}"
export AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT="${AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT:-4294967296}"

# The supported lifecycle is local-only. Constraining host syntax also makes
# the derived proxy and browser-origin URLs immune to ambient injection.
for agent_builder_host in \
    "$BACKEND_HOST" "$FRONTEND_HOST" "$DOCS_HOST" "$MCP_SSE_HOST"; do
    case "$agent_builder_host" in
        127.0.0.1|localhost) ;;
        *)
            agent_builder_env_fail "managed service hosts must be 127.0.0.1 or localhost" \
                || return 1 2>/dev/null || exit 1
            ;;
    esac
done
unset agent_builder_host

for agent_builder_port in \
    "$BACKEND_PORT" "$FRONTEND_PORT" "$DOCS_PORT" "$MCP_SSE_PORT"; do
    if [[ ! "$agent_builder_port" =~ ^[1-9][0-9]*$ ]] \
        || ((agent_builder_port < 1 || agent_builder_port > 65535)); then
        agent_builder_env_fail "service ports must be integers from 1 through 65535" \
            || return 1 2>/dev/null || exit 1
    fi
done
unset agent_builder_port

agent_builder_validate_limit() {
    local name="$1" minimum="$2" maximum="$3" value="${!1}"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]] \
        || ((value < minimum || value > maximum)); then
        agent_builder_env_fail "$name must be an integer from $minimum through $maximum"
        return 1
    fi
}
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_OUTPUT_LIMIT 65536 16777216 \
    || return 1 2>/dev/null || exit 1
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_MEMORY_LIMIT 67108864 17179869184 \
    || return 1 2>/dev/null || exit 1
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_FILE_LIMIT 1048576 1073741824 \
    || return 1 2>/dev/null || exit 1
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT 1048576 10737418240 \
    || return 1 2>/dev/null || exit 1
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_PROCESS_LIMIT 1 64 \
    || return 1 2>/dev/null || exit 1
agent_builder_validate_limit AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT 67108864 17179869184 \
    || return 1 2>/dev/null || exit 1
unset -f agent_builder_validate_limit

# The frontend proxy attaches the private backend token. Never accept an
# inherited target or origin list; derive them from validated local settings,
# including when custom ports are selected.
export AGENT_BUILDER_BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
export AGENT_BUILDER_CORS_ORIGINS="http://127.0.0.1:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}"
export AGENT_BUILDER_FRONTEND_ORIGINS="$AGENT_BUILDER_CORS_ORIGINS"

# The managed observability service is intentionally local-only and SQLite-only.
# Drop database selectors inherited from an invoking shell before starting any
# child process; remote export can be added outside the managed lifecycle if a
# deployment explicitly needs it.
unset PHOENIX_SQL_DATABASE_URL PHOENIX_SQL_DATABASE_READ_REPLICA_URL
unset PHOENIX_SQL_DATABASE_SCHEMA
unset PHOENIX_POSTGRES_HOST PHOENIX_POSTGRES_PORT PHOENIX_POSTGRES_USER
unset PHOENIX_POSTGRES_PASSWORD PHOENIX_POSTGRES_DB
unset PHOENIX_POSTGRES_USE_AWS_IAM_AUTH PHOENIX_POSTGRES_USE_AZURE_MANAGED_IDENTITY
unset PHOENIX_POSTGRES_AZURE_SCOPE PHOENIX_POSTGRES_AWS_IAM_TOKEN_LIFETIME_SECONDS
export PHOENIX_HOST=127.0.0.1
export PHOENIX_PORT="${PHOENIX_PORT:-6006}"
if [[ ! "$PHOENIX_PORT" =~ ^[1-9][0-9]*$ ]] \
    || ((PHOENIX_PORT < 1 || PHOENIX_PORT > 65535)); then
    agent_builder_env_fail "PHOENIX_PORT must be an integer from 1 through 65535" \
        || return 1 2>/dev/null || exit 1
fi
export PHOENIX_WORKING_DIR="$AGENT_BUILDER_RUNTIME_DIR/phoenix"
export PHOENIX_DEFAULT_RETENTION_POLICY_DAYS="${PHOENIX_DEFAULT_RETENTION_POLICY_DAYS:-7}"
if [[ ! "$PHOENIX_DEFAULT_RETENTION_POLICY_DAYS" =~ ^[1-9][0-9]*$ ]] \
    || ((PHOENIX_DEFAULT_RETENTION_POLICY_DAYS < 1 \
        || PHOENIX_DEFAULT_RETENTION_POLICY_DAYS > 30)); then
    agent_builder_env_fail "Phoenix retention must be between 1 and 30 days" \
        || return 1 2>/dev/null || exit 1
fi
export PHOENIX_TELEMETRY_ENABLED=false
export PHOENIX_ALLOW_EXTERNAL_RESOURCES=false
export PHOENIX_ALLOWED_SANDBOX_PROVIDERS=NONE
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:${PHOENIX_PORT}/v1/traces"
export OBSERVABILITY_ENABLED="${OBSERVABILITY_ENABLED:-true}"
export OBSERVABILITY_BACKEND="${OBSERVABILITY_BACKEND:-otlp}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-agent-builder}"
export OBSERVABILITY_SUCCESS_SAMPLE_RATE="${OBSERVABILITY_SUCCESS_SAMPLE_RATE:-0.2}"
export OBSERVABILITY_SLOW_REQUEST_MS="${OBSERVABILITY_SLOW_REQUEST_MS:-5000}"
export OBSERVABILITY_KEEP_ERRORS="${OBSERVABILITY_KEEP_ERRORS:-true}"
export OBSERVABILITY_KEEP_SLOW="${OBSERVABILITY_KEEP_SLOW:-true}"
export OBSERVABILITY_BATCH_DELAY_MS="${OBSERVABILITY_BATCH_DELAY_MS:-2000}"
export OBSERVABILITY_BATCH_SIZE="${OBSERVABILITY_BATCH_SIZE:-256}"
export OBSERVABILITY_BATCH_QUEUE_SIZE="${OBSERVABILITY_BATCH_QUEUE_SIZE:-2048}"
export OBSERVABILITY_EXPORT_TIMEOUT_MS="${OBSERVABILITY_EXPORT_TIMEOUT_MS:-10000}"
export OBSERVABILITY_MAX_PENDING_TRACES="${OBSERVABILITY_MAX_PENDING_TRACES:-2048}"
export OBSERVABILITY_MAX_SPANS_PER_TRACE="${OBSERVABILITY_MAX_SPANS_PER_TRACE:-512}"
export OBSERVABILITY_MAX_TRACE_BYTES="${OBSERVABILITY_MAX_TRACE_BYTES:-1048576}"
export OBSERVABILITY_PRIORITY_QUEUE_TRACES="${OBSERVABILITY_PRIORITY_QUEUE_TRACES:-64}"
export OBSERVABILITY_PRIORITY_BATCH_DELAY_MS="${OBSERVABILITY_PRIORITY_BATCH_DELAY_MS:-100}"
export OBSERVABILITY_MAX_ATTRIBUTE_LENGTH="${OBSERVABILITY_MAX_ATTRIBUTE_LENGTH:-4096}"
export OBSERVABILITY_MAX_COLLECTION_ITEMS="${OBSERVABILITY_MAX_COLLECTION_ITEMS:-50}"
export OBSERVABILITY_MAX_ATTRIBUTE_DEPTH="${OBSERVABILITY_MAX_ATTRIBUTE_DEPTH:-8}"
export OBSERVABILITY_STORAGE_WARN_BYTES="${OBSERVABILITY_STORAGE_WARN_BYTES:-1073741824}"
export OBSERVABILITY_STORAGE_MAX_BYTES="${OBSERVABILITY_STORAGE_MAX_BYTES:-5368709120}"

if [[ ! "$OBSERVABILITY_STORAGE_WARN_BYTES" =~ ^(0|[1-9][0-9]*)$ ]]; then
    agent_builder_env_fail "OBSERVABILITY_STORAGE_WARN_BYTES must be an integer" || return 1 2>/dev/null || exit 1
fi
if [[ ! "$OBSERVABILITY_STORAGE_MAX_BYTES" =~ ^[1-9][0-9]*$ ]] \
    || ((OBSERVABILITY_STORAGE_MAX_BYTES <= 0)); then
    agent_builder_env_fail "OBSERVABILITY_STORAGE_MAX_BYTES must be a positive integer" || return 1 2>/dev/null || exit 1
fi
if ((OBSERVABILITY_STORAGE_WARN_BYTES > OBSERVABILITY_STORAGE_MAX_BYTES)); then
    agent_builder_env_fail "observability warning threshold exceeds its hard limit" || return 1 2>/dev/null || exit 1
fi
# Do not allow caller-provided Phoenix capacity variables to weaken the project
# hard limit. Phoenix's own disk monitor blocks inserts at 90% while the startup
# preflight below uses the exact byte limit.
export PHOENIX_DATABASE_ALLOCATED_STORAGE_CAPACITY_GIBIBYTES
agent_builder_capacity_whole=$((OBSERVABILITY_STORAGE_MAX_BYTES / 1073741824))
agent_builder_capacity_remainder=$((OBSERVABILITY_STORAGE_MAX_BYTES % 1073741824))
agent_builder_capacity_fraction=$((agent_builder_capacity_remainder * 1000000000 / 1073741824))
printf -v PHOENIX_DATABASE_ALLOCATED_STORAGE_CAPACITY_GIBIBYTES \
    '%d.%09d' "$agent_builder_capacity_whole" "$agent_builder_capacity_fraction"
unset agent_builder_capacity_whole agent_builder_capacity_remainder agent_builder_capacity_fraction
export PHOENIX_DATABASE_USAGE_INSERTION_BLOCKING_THRESHOLD_PERCENTAGE=90

export AGENT_BUILDER_TOKEN_FILE="$AGENT_BUILDER_RUNTIME_DIR/secrets/api-token"

export PATH="$AGENT_BUILDER_NODE_HOME/bin:$AGENT_BUILDER_TOOLS_DIR:$AGENT_BUILDER_ROOT/.venv/bin:$UV_TOOL_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONPATH="$AGENT_BUILDER_ROOT"

umask 077
for agent_builder_path in \
    "$AGENT_BUILDER_RUNTIME_DIR" "$AGENT_BUILDER_TOOLS_DIR" \
    "$HOME" "$TMPDIR" "$XDG_RUNTIME_DIR" \
    "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" \
    "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
    "$PIP_CACHE_DIR" "$npm_config_cache" "$npm_config_prefix" "$HF_HOME" \
    "$HUGGINGFACE_HUB_CACHE" "$SENTENCE_TRANSFORMERS_HOME" \
    "$TRANSFORMERS_CACHE" "$TORCH_HOME" "$TORCH_EXTENSIONS_DIR" \
    "$TORCHINDUCTOR_CACHE_DIR" "$TRITON_CACHE_DIR" "$NUMBA_CACHE_DIR" \
    "$PLAYWRIGHT_BROWSERS_PATH" "$MPLCONFIGDIR" "$PYTHONPYCACHEPREFIX" \
    "$AGENT_BUILDER_RUNTIME_DIR/logs" "$AGENT_BUILDER_RUNTIME_DIR/pids" \
    "$AGENT_BUILDER_ENVIRONMENTS_DIR" "$AGENT_BUILDER_RUNTIME_DIR/secrets" \
    "$AGENT_BUILDER_RUNTIME_DIR/state" "$PHOENIX_WORKING_DIR"; do
    agent_builder_ensure_directory "$agent_builder_path" \
        || return 1 2>/dev/null || exit 1
done
unset agent_builder_path

for agent_builder_path in \
    "$AGENT_BUILDER_ROOT/.venv" "$AGENT_BUILDER_ROOT/data" \
    "$AGENT_BUILDER_NODE_HOME" "$AGENT_BUILDER_UV" \
    "$AGENT_BUILDER_TOKEN_FILE" "$npm_config_userconfig" \
    "$npm_config_globalconfig"; do
    agent_builder_reject_symlink_path "$agent_builder_path" \
        || return 1 2>/dev/null || exit 1
done
unset agent_builder_path
