# Security policy

## Reporting a vulnerability

Do not open a public issue containing exploit details, tokens, private data, or
credentials. Contact the repository maintainers through a private GitHub
security advisory for `wlf186/agent-builder`. Include affected versions,
reproduction steps, impact, and a suggested mitigation when available.

Do not access data that is not yours, persist on systems, disrupt services, or
publish a vulnerability before maintainers have had a reasonable opportunity to
respond.

## Supported configuration

The supported security baseline is the current `main` branch on glibc 2.28 or
newer GNU/Linux (`x86_64` or `aarch64`) with Landlock ABI 1 or newer, a
seccomp-supported architecture, and `/proc` mounted, running with:

- loopback-only listeners;
- generated backend API authentication enabled;
- the server-side frontend proxy as the browser's API boundary;
- explicit CORS origins and an allowlist for trusted outbound DNS names and
  private targets;
- project-local toolchains and runtime paths;
- upload, archive, path, log, and subprocess resource limits;
- Skill filesystem confinement and network denial enabled;
- local-process MCP disabled unless explicitly approved by the operator;
- local OpenTelemetry export containing bounded, redacted attributes.
- disabled framework analytics, including Chroma, Hugging Face, Next.js, and
  Phoenix telemetry, in the managed service environment.

The managed lifecycle refuses non-loopback host values. Exposing ports through
an untrusted or unauthenticated proxy, disabling authentication, broadening SSRF
allowlists, or running unreviewed uploaded skills changes the threat model and
is not a supported secure deployment.

## API and browser boundary

Bootstrap creates `.runtime/secrets/api-token` with mode `0600`. The backend
fails closed if `AGENT_BUILDER_API_TOKEN` is absent or shorter than 32
characters. Every non-preflight backend request under `/api` requires either a
Bearer token or `X-API-Key`; comparisons are constant-time. `/health` is the
documented unauthenticated liveness endpoint.

Browser code must call the same-origin frontend `/api` routes. Only the Next.js
server reads and injects the backend token. Never place the token in a
`NEXT_PUBLIC_` variable, response body, URL, trace, log, child environment, or
client-side storage.

The default CORS list contains only the local frontend origins. Wildcards are
rejected. Adding an origin or binding a listener beyond loopback requires a
deployment-specific threat review and an authenticated reverse proxy.

## Outbound connections and MCP

Outbound HTTP(S) validation rejects URL credentials, malformed targets, cloud
metadata endpoints, and destinations that resolve to non-global addresses.
Every DNS hostname must be explicitly trusted through
`AGENT_BUILDER_SSRF_ALLOWLIST`; a global IP literal does not need an entry.
Private services should use an allowlisted, port-scoped IP literal; only the
allowlisted `localhost` name may resolve to loopback. Entries can be an exact
hostname, an exact `host:port`, an IP literal, or an IP network. Hostname
wildcards are deliberately rejected; prefer the narrowest stable entry and
never allow a metadata address. The lifecycle script preloads only the
built-in model/MCP hostnames and local
service ports. DNS is resolved before each managed use and every resolved
address must pass validation.

An MCP `stdio` service launches a local process with operator-selected arguments
and environment variables. It is disabled by default. Set
`AGENT_BUILDER_ALLOW_STDIO_MCP=1` only after reviewing the executable and its
configuration; request data must not toggle this setting. The backend token is
explicitly forbidden in MCP child environments.

## Skill execution

Uploaded Skills execute in per-agent uv environments under
`.runtime/environments`. On Linux, Landlock restricts readable paths and confines
writes to the execution workspace. CPU time, per-process address space,
process-group aggregate RSS/count, file size, descriptors, captured output,
wall time, and workspace growth are bounded. Descendants cannot leave the
managed process group, and termination verifies the entire group instead of
only its leader. If Landlock or `/proc` process-group monitoring is unavailable,
execution is refused rather than silently running without confinement.

The child seccomp filter denies network socket creation by default. Setting
`AGENT_BUILDER_SKILL_NETWORK=allow` changes the trust boundary for every Skill
process started with that environment and should be used only for reviewed
workloads. Package installation is limited by validated package syntax and
`AGENT_BUILDER_PACKAGE_ALLOWLIST`. uv additionally enforces
`--only-binary :all:` so a package without a wheel fails closed instead of
executing source-distribution or PEP 517 build hooks.

## Observability and local data

Traces use OpenTelemetry/OpenInference and OTLP/HTTP. The default local viewer
writes SQLite data beneath `.runtime/phoenix`; normal successful traces are
sampled and export is batched. Attribute depth, collection size, and string
length are capped, and credential-like values are redacted before export.

Use `./start.sh --no-observability` when traces must not be collected. Treat a
custom `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and its headers as a data egress
boundary: use HTTPS where appropriate, keep credentials in runtime environment
variables, and verify the collector's access and retention policies.

## Credential handling

Secrets belong in runtime environment variables or `.runtime/secrets/`, never
in source, Git URLs, client-visible `NEXT_PUBLIC_` variables, logs, traces,
screenshots, test fixtures, or issue reports. Revoke and rotate a credential
immediately if it may have been exposed.

Managed service supervisors and bootstrap use explicit environment allowlists.
Bootstrap retains only project paths, locale/CA settings, and explicitly
configured network proxies; clone tokens, cloud credentials, activated
Conda/venv state, and unrelated package-manager variables are not forwarded to
uv, npm, curl, or long-running service children.
