# Project structure

## Repository map

```text
agent-builder/
├── backend.py                 FastAPI/SSE boundary and orchestration
├── bootstrap.sh               pinned checkout-local toolchain/dependency setup
├── start.sh                   transactional complete-stack start
├── stop.sh                    identity-checked process shutdown
├── purge.sh                   scoped cache/data/runtime cleanup
├── env.sh                     project-contained HOME/TMP/XDG/cache environment
├── pyproject.toml             Python dependencies and test configuration
├── uv.lock                    reproducible Python resolution
├── src/
│   ├── agent_engine.py        planning, LLM/tool execution, streaming
│   ├── observability/         vendor-neutral API and OpenTelemetry exporter
│   ├── security.py            auth, path, URL, upload, and execution validation
│   ├── execution_engine.py    bounded subprocess execution
│   ├── environment_manager.py per-agent uv environments
│   ├── conversation_manager.py SQLite/WAL conversation persistence
│   ├── knowledge_base_manager.py document metadata and Chroma collection cache
│   └── *_manager.py           application registries and persistence
├── frontend/
│   ├── src/app/               Next.js application and server routes
│   ├── src/app/api/           authenticated server-only backend proxy
│   ├── src/components/        UI plus bilingual user-guide annotations
│   └── tests/                 Playwright regression tests
├── docs-site/                 VitePress end-user guide
├── docs/                      design, product, reference, and plan documents
├── scripts/                   governance and bounded log helpers
├── tests/                     Python tests
├── data/                      non-reproducible user/application data
├── .runtime/                  ignored runtime state and disposable caches
├── .tools/                    ignored downloaded toolchains
└── .venv/                     ignored root Python environment
```

## Runtime ownership

| Owner | Writes | Must not write |
| --- | --- | --- |
| lifecycle scripts | `.runtime/pids`, `.runtime/logs`, `.runtime/secrets` | global service manager or user profile |
| uv/bootstrap | `.tools`, `.venv`, `.runtime/python`, `.runtime/cache/uv` | system Python/site-packages |
| frontend/docs | local `node_modules`, `.runtime/cache/npm`, build directories | global npm prefix or user npm cache |
| backend persistence | `data/` | absolute/user-controlled paths |
| skill execution | `.runtime/environments`, `.runtime/tmp` | user home or system temporary state |
| observability | `.runtime/phoenix` | external databases by default |

`env.sh` supplies absolute paths derived from the checkout root, so lifecycle
commands work from any current directory. Runtime files are never tracked.

The managed roots `.runtime/`, `.tools/`, `.venv/`, and `data/` must be real
directories, not symlinks. Startup refuses those roots when implemented as
symlinks so path containment cannot be redirected outside the checkout.

## Persistence formats

- Agents, model/MCP/skill registries, and knowledge-base configuration are
  project data under `data/` and use contained, validated paths.
- Conversations are transactional SQLite rows. The manager imports the former
  per-conversation JSON layout once without deleting recoverable legacy data.
- Knowledge-base documents have sidecar metadata; list/read operations do not
  recompute and rewrite statistics. Chroma clients and collections are reused.
- Host logs rotate by size/count. Debug logs and trace attributes are bounded
  and retained for limited periods.

## Dependency boundaries

Python dependencies are declared once in `pyproject.toml` and locked in
`uv.lock`; `requirements.txt` is a compatibility export, not a second source of
truth. Frontend, root documentation tooling, and `docs-site` each use their own
committed npm lockfile.

The default observability stack is OpenTelemetry/OpenInference exported over
OTLP/HTTP to local Phoenix. Application code imports only `src.observability`,
so another compatible collector can replace the local viewer without changing
agent execution.

## Execution and network boundaries

- Uploaded Skill processes can read their managed environment and required
  system runtime files, but Landlock confines writes to their execution
  workspace. Unsupported Linux kernels fail closed.
- Skill network sockets are denied by seccomp unless the operator explicitly
  selects `AGENT_BUILDER_SKILL_NETWORK=allow`.
- `stdio` MCP is disabled until `AGENT_BUILDER_ALLOW_STDIO_MCP=1` is set for a
  reviewed local command.
- Remote HTTP(S) endpoints cannot contain credentials or resolve to metadata or
  unapproved non-global addresses. Trusted private endpoints are listed
  narrowly in `AGENT_BUILDER_SSRF_ALLOWLIST`.
- The generated API token remains in `.runtime/secrets` and is injected only by
  server-side processes; child processes and browser bundles do not receive it.
