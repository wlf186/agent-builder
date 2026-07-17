# Agent Builder frontend

This directory contains the Next.js 16 browser interface and its server-only
proxy boundary. The complete deployment contract and supported hosts are
documented in the repository [README](../README.md); do not deploy this folder
as an independent static application.

## Managed runtime

From the repository root:

```bash
./bootstrap.sh
./start.sh
```

The managed frontend listens on `http://127.0.0.1:20815`. It proxies browser
requests under `/api` and streaming requests under `/stream` to the
authenticated backend at `http://127.0.0.1:20881`; the private API token is read
only by the Next.js server. `/docs` is proxied to the project-local user guide
on port `4173`.

`FRONTEND_PORT`, `BACKEND_PORT`, and `DOCS_PORT` may be set before bootstrap and
start. The lifecycle scripts validate loopback hosts and keep the proxy targets
aligned with those values.

## Development checks

Load the checkout-local environment before invoking Node tools:

```bash
source ./env.sh
npm --prefix frontend run lint
npm --prefix frontend run build
(cd frontend && npm exec playwright test)
```

The relevant boundaries are:

- `src/app/api/[...path]/route.ts`: authenticated server-side API proxy;
- `src/app/stream/agents/[name]/chat/route.ts`: ordered SSE streaming proxy;
- `src/lib/serverOrigin.ts`: Host, Origin, and Fetch Metadata validation;
- `src/components/`: UI components and bilingual `@userGuide` annotations;
- `tests/`: reproducible Playwright regression tests.

Preserve incremental thinking, tool-call, content, cancellation, and terminal
events when changing the streaming route or chat UI. Never expose the backend
token through a `NEXT_PUBLIC_` variable, browser response, log, or trace.

See the [testing guide](../docs/references/testing-guide.md),
[API reference](../docs/references/api-reference.md), and
[streaming protocol](../docs/design-docs/streaming-protocol.md).
