# Agent Builder

Agent Builder is a local-first web application for creating and running AI
agents with model services, MCP tools, skills, streaming chat, conversation
history, RAG knowledge bases, and local OpenTelemetry traces.

## Supported deployment

The secure, fully supported target is a writable GNU/Linux checkout on a local
filesystem. The lifecycle scripts fail early outside this baseline.

| Platform | Support |
| --- | --- |
| glibc 2.28+ Linux, x86_64 | supported and exercised by hosted CI |
| glibc 2.28+ Linux, ARM64 | dependency artifacts are available; release validation on ARM hardware is still required |
| recent WSL2 | conditional on glibc 2.28+, `/proc`, Landlock, and seccomp support |
| Alpine or another musl distribution | unsupported |
| native Windows or macOS | unsupported for the complete, sandboxed stack |

Uploaded Skills additionally require Landlock ABI 1 or newer, a supported
seccomp architecture, and a mounted Linux `/proc`. If those kernel controls are
unavailable, Skill execution fails closed instead of running without a sandbox.

The checkout must be on a filesystem that is writable, permits executable
files, supports Unix permissions and symbolic links, and provides normal SQLite
file locking. A `noexec` mount, FAT/NTFS share, or some network filesystems are
not supported deployment locations.

## Host prerequisites

Install Git, CA certificates, Bash 4.2 or newer, `curl`, `tar`, a SHA-256
utility, and standard Linux command-line tools (`awk`, `find`, `getconf`,
`install`, `od`, `ps`, `tr`, and `wc`). For Ubuntu 22.04/24.04:

```bash
sudo apt-get update
sudo apt-get install -y \
  bash ca-certificates coreutils curl findutils gawk git libc-bin procps tar
```

For a RHEL-compatible distribution:

```bash
sudo dnf install -y \
  bash ca-certificates coreutils curl findutils gawk git glibc-common procps-ng tar
```

No global Python, Node.js, npm, Conda, Docker, or Podman installation is
required. After host packages are installed, bootstrap does not require root
access and never modifies a shell profile.

Check the two most important compatibility values with:

```bash
getconf GNU_LIBC_VERSION   # must report glibc 2.28 or newer
uname -m                   # x86_64 or aarch64/arm64
```

## Network and capacity

A cold bootstrap requires HTTPS access, directly or through the standard
`HTTP_PROXY`/`HTTPS_PROXY` and CA environment variables, to:

- `github.com` and GitHub release-asset hosts for uv and managed Python;
- `nodejs.org` for the pinned Node.js archive;
- `pypi.org` and `files.pythonhosted.org` for Python wheels;
- `download.pytorch.org` and `download-r2.pytorch.org` for CPU-only PyTorch;
- `registry.npmjs.org` for the three locked Node dependency trees.

The first installation currently occupies roughly 3.3 GiB before user data;
downloads and build caches temporarily require more. Reserve 8–10 GiB of free
disk space. Use at least 4 GiB of RAM, with 8 GiB recommended when building the
frontend or running resource-intensive Skills. The five default ports listed
below must be free.

`./bootstrap.sh --offline` is only for a checkout whose project-local caches and
toolchains have already been populated; it cannot initialize a fresh clone.

## Start the complete stack

Clone the repository onto a supported host, then run the documented lifecycle:

```bash
git clone https://github.com/wlf186/agent-builder.git
cd agent-builder
./bootstrap.sh
./start.sh
```

Bootstrap pins uv 0.11.7, Python 3.11.15, and Node.js 22.17.0 inside the
checkout. Downloaded uv and Node archives are SHA-256 verified, Python
dependencies come from `uv.lock`, and Node dependencies come from npm lockfiles.
Source distributions are disabled for the root Python environment.

`start.sh` also runs bootstrap when needed, so subsequent starts need only that
one command. Open `http://127.0.0.1:20815` after it reports success. It starts
and health-checks Phoenix, FastAPI, Next.js, the built-in MCP service, and the
user guide as one transaction; if any required service fails, it rolls back the
processes it started.

Stop the complete stack with:

```bash
./stop.sh
```

The scripts are location-independent and only manage processes whose identity
and checkout root match their PID metadata. Run `./start.sh --help`,
`./stop.sh --help`, or `./purge.sh --help` for options.

Basic health checks after startup are:

```bash
curl --noproxy '*' --fail http://127.0.0.1:20815/
curl --noproxy '*' --fail http://127.0.0.1:20881/health
curl --noproxy '*' --fail http://127.0.0.1:20882/health
curl --noproxy '*' --fail http://127.0.0.1:4173/docs/
curl --noproxy '*' --fail http://127.0.0.1:6006/healthz
```

## Local-only state

| Path | Contents | Reproducible? |
| --- | --- | --- |
| `.tools/` | downloaded uv and Node launchers/toolchains | yes |
| `.venv/` | root Python environment | yes |
| `.runtime/` | managed Python, PIDs, logs, caches, temporary files, Phoenix data, skill envs | mostly |
| `frontend/node_modules/` | frontend dependencies | yes |
| `docs-site/node_modules/` | documentation dependencies | yes |
| `data/` | agents, registries, knowledge bases, conversations, uploads | no |

No bootstrap step modifies a shell profile or installs a global package.
Application processes receive project-local `HOME`, all temporary/XDG paths,
package/model/compiler caches, npm configuration, and browser cache locations
from `env.sh`. Inherited Conda/venv, pip/npm install destinations, `NODE_PATH`,
and uv workspace selectors are cleared. Bootstrap also re-executes with an
exact environment allowlist, so unrelated caller credentials are not exposed to
uv, npm, or download subprocesses.

Dynamic packages for per-agent environments must be allowlisted and available
as wheels. uv runs with `--only-binary :all:`; source distributions and their
PEP 517 build hooks are refused.

Use the explicit purge interface instead of deleting ad hoc paths:

```bash
./purge.sh cache --yes
./purge.sh logs --yes
./purge.sh observability --yes
./purge.sh all --yes       # includes user data; destructive
```

## Services and security

The managed lifecycle restricts every listener to `127.0.0.1` or `localhost`:

| Service | Port |
| --- | --- |
| Web application | 20815 |
| Authenticated backend | 20881 |
| Built-in MCP SSE | 20882 |
| User guide | 4173 |
| Phoenix trace dashboard | 6006 |

Bootstrap generates a high-entropy API token at
`.runtime/secrets/api-token`. It is used only by the frontend server and backend;
it is never exposed as a browser-visible environment variable. To expose the
application, keep managed services on loopback and place an authenticated,
deployment-reviewed reverse proxy in front of them.

Every non-preflight backend `/api/**` request is authenticated. Browser code
uses the same-origin Next.js proxy, which injects the token server-side. Direct
backend clients must use `Authorization: Bearer ...` or `X-API-Key`; `/health`
is the documented unauthenticated health endpoint.

Uploaded Skill processes run fail-closed on Linux: Landlock confines reads to
explicit runtime paths and writes to the execution workspace, resource limits
cap CPU, per-process and aggregate memory, files, process-group size,
descriptors, and output, and a seccomp filter blocks network socket creation by
default. Process-group accounting polls Linux's virtual `/proc` filesystem and
does not write to disk. If Landlock is unavailable, Skill execution is refused.
Network access is an explicit operator decision via
`AGENT_BUILDER_SKILL_NETWORK=allow`.

Local-process MCP (`stdio`) can execute a trusted command and is therefore
disabled by default. Enable it only for reviewed commands with
`AGENT_BUILDER_ALLOW_STDIO_MCP=1`. Remote model and MCP URLs reject credentials,
cloud metadata targets, and unsafe resolution. Every DNS hostname must be
listed in `AGENT_BUILDER_SSRF_ALLOWLIST`; startup supplies only the exact
built-in model/MCP hostnames and local service ports. Add custom names
explicitly—hostname wildcards are rejected—and express private destinations as
narrowly scoped, port-specific IP literals rather than broad networks.

Tracing is vendor-neutral OpenTelemetry/OpenInference over OTLP/HTTP. Phoenix is
the default local viewer and keeps SQLite data under `.runtime/phoenix`. Disable
both the viewer and trace export with:

```bash
./start.sh --no-observability
```

The supported lifecycle deliberately forces that endpoint to local Phoenix.
An independently managed backend can set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
to another compatible collector because application code does not depend on
the local viewer. Trace values are redacted and bounded, normal successful
traces are sampled, critical traces use an independent bounded batch queue, and
Phoenix enforces retention plus a managed storage ceiling.

## First use

Bootstrap installs the Agent Builder application, not an LLM or a cloud-model
credential. Opening the web interface therefore confirms deployment but does
not by itself make an Agent able to answer prompts.

Before creating an Agent, open the model-service settings and configure either:

- a supported cloud provider with its API key; or
- an independently installed local Ollama service, normally at
  `http://127.0.0.1:11434`.

Cloud API keys entered in the password field are submitted through the
authenticated same-origin proxy, stored only by the server, and never returned
in plaintext. Ollama is not installed or started by Agent Builder. See the
[model-service guide](docs-site/en/advanced/model-service-dialog.md) for the
complete workflow.

The managed outbound allowlist already contains the built-in provider endpoints
and local Ollama port. A custom model or MCP hostname must be added narrowly to
`AGENT_BUILDER_SSRF_ALLOWLIST` before startup, as described in
[SECURITY.md](SECURITY.md).

## Troubleshooting

- **A port is occupied:** `start.sh` refuses to take over an unmanaged process.
  Stop that process or set a different `*_PORT` value consistently before
  running `start.sh`; managed service hosts remain loopback-only.
- **A download fails:** confirm the domains under *Network and capacity* are
  reachable. Export corporate proxy and CA variables before bootstrap; the
  clean bootstrap environment passes through only the reviewed proxy/CA keys.
- **Startup fails:** inspect bounded logs under `.runtime/logs/`. A failed
  transactional start rolls back services it created.
- **A Skill is unavailable:** verify the host kernel exposes Landlock,
  seccomp, and `/proc`. The application deliberately has no unconfined fallback.
- **Dependencies are damaged:** stop the stack, run
  `./purge.sh dependencies --yes`, then run `./bootstrap.sh` again. User data is
  not part of the `dependencies` purge scope.
- **Disk use grows:** use `./purge.sh cache --yes` for reproducible caches,
  `./purge.sh logs --yes` for rotated logs, or
  `./purge.sh observability --yes` for Phoenix data.

The supported scripts bind only to loopback. Remote access requires a separately
reviewed authenticated reverse proxy; do not change managed hosts to `0.0.0.0`.

## Development

```bash
source ./env.sh
./.tools/uv sync --frozen
./.venv/bin/python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
npm run governance:check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow,
[CLAUDE.md](CLAUDE.md) for repository invariants, and the
[user guide](docs-site/en/getting-started.md) for product usage.

## License

No license file is currently included. Treat the repository as all-rights
reserved until the maintainers add an explicit license.
