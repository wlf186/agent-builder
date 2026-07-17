# Testing guide

## Test layers

| Layer | Command | Purpose |
| --- | --- | --- |
| Python unit/integration | `./.venv/bin/python -m pytest` | backend, storage, security, tracing, execution |
| Frontend static checks | `npm --prefix frontend run lint` | React/TypeScript/Next rules |
| Frontend production build | `npm --prefix frontend run build` | type checking and server/client boundary |
| Browser regression | `npm --prefix frontend exec playwright test` | reproducible end-to-end behavior |
| Documentation governance | `npm run governance:check` | generated docs, links, build, credential patterns |

Always load the contained environment first:

```bash
source ./env.sh
```

This redirects test caches, temporary files, browser downloads, screenshots,
traces, and Python bytecode into `.runtime/`.

## Browser automation

Playwright Test is the committed CI/regression harness. Tests are headless by
default and write artifacts to `.runtime/test-results/playwright`.

```bash
npm --prefix frontend exec playwright test
npm --prefix frontend exec playwright test tests/runtime-check.spec.ts
PLAYWRIGHT_HEADED=true npm --prefix frontend exec playwright test
```

Set `PLAYWRIGHT_BASE_URL` when testing a non-default frontend address.

Playwright CLI may be used for interactive exploration and demos. An
interactive transcript is not a regression test: when exploration finds a
defect, add a focused Playwright Test case that reproduces it without manual
judgment.

## Service-aware tests

Start the stack with `./start.sh` before browser tests. Browser code must call
the backend through the frontend `/api` proxy; direct backend requests require
the server-only token and should be limited to backend integration tests.

Lifecycle changes require these scenarios:

1. clean bootstrap and start;
2. repeated start without duplicate processes;
3. one component health-check failure and transactional rollback;
4. graceful full stop, then forced stop;
5. stale/reused PID metadata rejection;
6. selective stop and subsequent full recovery.

## Security and resource tests

Changes to trust boundaries must include both successful and rejected cases:

- missing, malformed, and incorrect authentication;
- traversal, symlink escape, absolute paths, and hostile archive entries;
- oversized, highly compressed, excessive-entry, and interrupted uploads;
- loopback/private/link-local/DNS-rebinding outbound destinations;
- subprocess timeout, output cap, process-tree cancellation, and invalid package
  specifications;
- very large log/trace attributes and credential redaction.

Use bounded fixtures. Do not put test artifacts in `/tmp`, a user home, or the
repository root.

## Streaming acceptance criteria

For streaming chat, assert the protocol rather than animation timing:

- events arrive in order and content can be reconstructed exactly once;
- thinking and tool-call events are not delayed until completion;
- cancellation terminates the backend task and child process tree;
- an error produces one bounded terminal error event;
- rapid chunks are rendered in batches without losing final content;
- conversation persistence contains one copy of each message.
