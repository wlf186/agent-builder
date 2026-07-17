---
title: Local Observability
---

# Local Observability

Agent Builder emits vendor-neutral OpenTelemetry traces with OpenInference
attributes. The default lifecycle starts a project-local Phoenix dashboard and
stores its SQLite data under `.runtime/phoenix`.

## Open the dashboard

Use **Local Traces** in the application sidebar, or open
`http://127.0.0.1:6006` on the host running Agent Builder. The dashboard shows
agent, model, tool, retrieval, and sub-agent spans correlated by conversation.

## Privacy and disk controls

- Credentials and authorization values are redacted before export.
- Attribute strings and collections have hard size limits.
- Each in-flight trace also has span-count and memory limits. Oversized traces
  fail-keep a bounded partial trace instead of growing without limit.
- Successful spans are exported in batches rather than written per token.
  Errors and slow traces use a priority export path before the bounded success
  queue; successful traces are sampled at 20% by default.
- Phoenix data has a 7-day default retention policy. Phoenix continuously
  blocks new inserts at 90% of its managed 5 GiB allocation, and startup also
  refuses a database already at the hard limit.

All paths, sample rates, batch sizes, and retention settings are declared in
`env.sh`. The managed lifecycle forces loopback-only Phoenix, local SQLite, and
a local OTLP endpoint; inherited PostgreSQL selectors are removed. Startup
warns when the complete Phoenix working directory reaches the configured
warning threshold. Remove trace data only after stopping the stack:

```bash
./stop.sh
./purge.sh observability --yes
```

To disable both trace export and the local dashboard, start with:

```bash
./start.sh --no-observability
```

When launching the backend outside the lifecycle script, setting
`OBSERVABILITY_ENABLED=false` selects the no-op tracer.

## Export elsewhere

Application code does not depend on the dashboard implementation. For an
independently managed backend process (outside `start.sh`), set
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to a compatible OTLP/HTTP collector. The
official lifecycle intentionally overrides it with local Phoenix. A remote
collector is a data-egress boundary: protect header credentials in runtime
environment variables and review its transport security, access, and retention.
