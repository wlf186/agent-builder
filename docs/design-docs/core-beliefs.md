# Core engineering beliefs

This document explains the durable reasoning behind the concise rules in
[`CLAUDE.md`](../../CLAUDE.md). When the two disagree, fix them together and
treat `CLAUDE.md` as the agent-facing rule source.

## Local first means contained by default

The supported stack must be reproducible from one checkout without global
language environments or containers. Pinned uv, Python, and Node toolchains
belong in `.tools/`, the application environment belongs in `.venv/`,
disposable state belongs in `.runtime/`, and non-reproducible application data
belongs in `data/`.

Source `env.sh` before development commands. It redirects home, temporary, XDG,
package, model, and browser caches into the checkout. A feature is incomplete if
its normal or error path writes state elsewhere.

## Streaming is a protocol

Thinking updates, content chunks, tool calls, tool results, cancellation, and
terminal events are ordered protocol elements, not cosmetic animation. Code
must preserve incremental delivery, exactly-once reconstruction, bounded
buffering, and cancellation propagation. See
[the streaming protocol](streaming-protocol.md).

## Trust boundaries fail closed

The backend authenticates every `/api/**` request and remains loopback-bound by
default. File paths, archives, URLs, headers, package specifications, MCP
configuration, and subprocess arguments are untrusted inputs.

Uploaded Skill execution requires Linux Landlock confinement and bounded
resources. Network sockets are denied by default. A missing sandbox is an error,
not permission to run unconfined. Local-process MCP is disabled until an
operator explicitly opts in after reviewing the command.

## Persistence should minimize write amplification

Append or update transactional records instead of rewriting complete histories.
Reuse expensive clients and collections. Batch streaming UI updates and trace
exports. Logs, traces, caches, and temporary artifacts require explicit bounds,
rotation, retention, and purge behavior.

These rules protect latency and SSD lifetime as well as correctness.

## Tests prove boundaries and failures

Use the smallest focused regression first, then the complete relevant suite.
Security-sensitive changes require negative and boundary cases. Lifecycle
changes require clean/repeated start, rollback after failed health checks,
graceful and forced stop, stale PID rejection, and port-ownership checks.

Canonical checks are listed in [the testing guide](../references/testing-guide.md).
Do not replace a committed regression with an interactive browser transcript.

## Documentation is part of the change

Behavior, configuration, API, lifecycle, security, storage, and user-flow
changes update their authoritative documents in the same review. Obsolete
instructions are deleted and remain available through Git history. Ownership,
review cadence, and automated gates are defined in
[documentation governance](../DOCUMENTATION.md).

## Dependency changes are locked

Python dependencies are changed through the checkout-local uv and committed in
both `pyproject.toml` and `uv.lock`:

```bash
source ./env.sh
./.tools/uv add package-name
./.tools/uv sync --frozen
```

Do not use system Python, system `pip`, global npm installation, a user package
cache, or direct edits that leave the lockfile inconsistent.
