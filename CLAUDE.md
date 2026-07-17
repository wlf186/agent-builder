# Agent Builder repository guide

Agent Builder is a local-first platform for composing and running AI agents. It
includes a FastAPI backend, a Next.js frontend, MCP and skill execution, RAG,
conversation persistence, and vendor-neutral local tracing.

This file is the concise source of truth for coding agents. `AGENTS.md` is a
symbolic link to this file so all supported agent tools receive identical rules.

## Quick commands

```bash
./bootstrap.sh                         # install pinned, project-local toolchains
./start.sh                             # bootstrap if needed, then start all services
./stop.sh                              # stop every process started by this checkout
./purge.sh cache --yes                 # remove only reproducible caches
./purge.sh all --yes                   # remove all local runtime state (destructive)
```

Lifecycle options:

- `bootstrap.sh`: `--skip-node`, `--no-build`, `--rebuild`, `--offline`
- `start.sh`: `--skip-bootstrap`, `--no-observability`, `--no-docs`
- `stop.sh`: `--force`, `--service <backend|frontend|docs|phoenix>`
- `purge.sh`: `--yes`; scopes are `cache`, `logs`, `observability`,
  `environments`, `data`, `build`, `dependencies`, or `all`

Default listeners are loopback-only:

| Component | Address |
| --- | --- |
| Frontend | `http://127.0.0.1:20815` |
| Backend API | `http://127.0.0.1:20881` |
| Built-in MCP SSE | `http://127.0.0.1:20882` |
| User guide | `http://127.0.0.1:4173` |
| Local traces | `http://127.0.0.1:6006` |

## Non-negotiable engineering rules

1. Preserve streaming semantics. Thinking, tool calls, content chunks,
   cancellation, and terminal events must remain incremental and ordered.
2. Do not install project dependencies globally. Source `env.sh` and use the
   checkout-local uv, Python, Node, npm caches, and virtual environments.
3. Do not write runtime state outside this checkout. Persistent application
   data belongs in `data/`; disposable state and managed Python belong in
   `.runtime/`; uv and Node belong in `.tools/`; the root Python environment is
   `.venv/`.
4. Never commit credentials or put them in URLs, client-visible variables,
   command arguments, logs, traces, or error responses.
5. The backend remains loopback-bound and authenticated by default. New API
   routes are protected unless they are explicitly documented health checks.
6. Treat filenames, archive entries, agent names, MCP URLs, and model endpoints
   as untrusted input. Preserve containment, upload limits, SSRF checks, and
   subprocess resource limits.
7. Do not add per-token disk writes or unbounded in-memory logs. Batch streaming
   updates and telemetry; enforce rotation, retention, sampling, and size caps.
8. Uploaded Skill execution is fail-closed. Preserve Linux Landlock filesystem
   confinement, resource limits, and default seccomp network denial; never add
   an unconfined fallback when the kernel cannot provide the sandbox.
9. Treat local-process MCP as code execution. It remains disabled unless the
   operator explicitly sets `AGENT_BUILDER_ALLOW_STDIO_MCP=1`; never enable it
   from request data or pass the control-plane API token to a child process.

## Local environment and isolation

Run development commands after loading the same containment environment used by
the lifecycle scripts:

```bash
source ./env.sh
./.tools/uv sync --frozen
./.venv/bin/python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
npm run governance:check
```

`env.sh` redirects `HOME`, `TMPDIR`/`TEMP`/`TMP`, every XDG directory,
uv/pip/npm/model/compiler caches, Playwright browsers, and Python bytecode into
`.runtime/`. It clears inherited Conda/venv and package-install destinations and
must not modify a user's shell profile. Per-skill Python environments live
under `.runtime/environments` and are created by uv.

The supported deployment baseline is glibc 2.28+ GNU/Linux on `x86_64` or
`aarch64`; [README.md](README.md) is authoritative for host packages, network
access, capacity, platform qualification, first-use model setup, and operator
troubleshooting. Do not claim another platform is supported without an
equivalent cold-checkout lifecycle validation.

Do not bypass these paths with system `pip`, global npm installs, Conda,
containers, `/tmp`-based application state, or user-home caches.
Per-agent package changes use uv with `--only-binary :all:` and the package
allowlist; do not enable source distributions or PEP 517 build hooks.

## Architecture boundaries

- `backend.py`: HTTP/SSE boundary, request validation, authentication, and
  orchestration only. Move reusable behavior into `src/`.
- `src/agent_engine.py`: agent planning and streaming execution.
- `src/observability/`: backend-neutral tracing API and OpenTelemetry exporter.
- `src/execution_engine.py`: bounded subprocess execution and cancellation.
- `src/*_manager.py`: project-local persistence and registries.
- `frontend/src/app/api/`: server-only authenticated proxy to the backend. The
  API token must never use a `NEXT_PUBLIC_` variable.
- `frontend/src/components/`: UI and bilingual `@userGuide` source annotations.
- `docs-site/`: generated component pages plus hand-written user documentation.

Conversation messages use SQLite/WAL to avoid rewriting full histories.
Knowledge-base clients and collections are reused. When adding persistence,
prefer atomic replacement or transactional append/update over repeated complete
file rewrites.

## Security and privacy

- `AGENT_BUILDER_API_TOKEN` is generated into `.runtime/secrets/api-token` with
  restrictive permissions. The frontend server injects it when proxying `/api`.
- Every non-preflight backend `/api/**` request requires the token through a
  Bearer or `X-API-Key` header. `/health` is the unauthenticated health check;
  browser code must use the same-origin frontend proxy and must never read the
  token.
- CORS origins are explicit and never wildcarded. Outbound URLs reject embedded
  credentials, metadata targets, and unsafe resolution. Every DNS hostname
  requires a narrow `AGENT_BUILDER_SSRF_ALLOWLIST` entry; custom private targets
  should use a port-scoped IP literal. Do not broadly allow private networks.
- Upload handling is streaming and bounded by compressed size, expanded size,
  entry count, and path containment.
- Skill subprocesses require Linux Landlock, have bounded resources and output,
  and cannot create network sockets unless the operator sets
  `AGENT_BUILDER_SKILL_NETWORK=allow` before startup.
- Trace and debug serializers redact credential-like fields and cap depth,
  collection count, and string size.
- Local observability uses OpenTelemetry/OpenInference and a project-local
  Phoenix SQLite store. Use `./start.sh --no-observability` for a no-op tracer
  and no dashboard; `OBSERVABILITY_ENABLED=false` applies when the backend is
  launched outside the lifecycle script.
- Report suspected vulnerabilities according to [SECURITY.md](SECURITY.md), not
  in a public issue containing exploit or credential details.

## Testing policy

Use the smallest relevant test first, then the full checks before handoff:

```bash
source ./env.sh
./.venv/bin/python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
npm run governance:check
```

Playwright Test is the reproducible CI/regression harness. Interactive browser
automation may use Playwright CLI for exploration, but it does not replace a
committed regression test for a fixed defect. Browser tests must write artifacts
under `.runtime/test-results` and must not assume a headed display.

Tests must cover negative security cases when changing file paths, uploads,
archives, URLs, authentication, subprocesses, or proxy behavior. Lifecycle
changes require start/health/stop and stale-PID validation.

## Documentation governance

Documentation ownership and workflow are defined in
[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md). The ownership summary is:

| Surface | Accountable owner | Required review trigger |
| --- | --- | --- |
| `CLAUDE.md` and linked `AGENTS.md` | repository maintainers | commands, global invariants, definition of done |
| `README.md` and lifecycle help | runtime owners | platforms, dependencies, network/capacity, paths, flags, ports, start/stop/purge behavior |
| `SECURITY.md` | security maintainers | auth, trust boundaries, allowlists, sandboxing, secret handling |
| `docs/design-docs/`, `docs/references/` | affected component owner | architecture, protocol, API, storage, or configuration changes |
| `docs/product-specs/`, `docs-site/` | feature owner | acceptance criteria or user-visible workflow changes |
| `docs/exec-plans/` | plan owner | work starts, changes direction, completes, or is abandoned |

When behavior, flags, ports, APIs, configuration, security controls, or user
flows change, update the authoritative document in the same change. Avoid
duplicating rules across files; link to the source of truth. CI validates
metadata, generated pages, Markdown, local links, documented lifecycle commands,
the `AGENTS.md` symlink, generated-page drift, documented default ports, review
freshness, dependency policy and vulnerability audits, the VitePress build,
credential patterns, and a cold-checkout full-stack lifecycle smoke test.

Maintainers review active plans when their work changes and audit maintained
design, reference, security, and operator documents at least quarterly and
before a release. The audit result updates the document or records that it was
reviewed in [the governance ledger](docs/DOCUMENTATION.md); obsolete detail is
deleted and remains available through Git history.

Run the same local gates before handoff:

```bash
source ./env.sh
npm run governance:check
```

## Definition of done

A change is complete only when:

- implementation and migration/rollback behavior are present;
- relevant unit, integration, and negative tests pass;
- frontend and documentation builds pass when affected;
- no new unbounded memory/disk growth, secret exposure, or outside-workspace
  write is introduced;
- generated artifacts and runtime state remain ignored;
- documentation matches the final behavior.

See [CONTRIBUTING.md](CONTRIBUTING.md),
[project structure](docs/references/project-structure.md),
[API reference](docs/references/api-reference.md), and
[streaming protocol](docs/design-docs/streaming-protocol.md) for details.
