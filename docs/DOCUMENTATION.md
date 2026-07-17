# Documentation governance

## Sources of truth

| Information | Authoritative location | Owner |
| --- | --- | --- |
| Repository invariants and common commands | `CLAUDE.md` (`AGENTS.md` links to it) | maintainers |
| Installation and operation | `README.md`, lifecycle `--help` output | runtime owners |
| Current architecture and protocols | `docs/design-docs/` | component owners |
| API and file layout reference | `docs/references/` | backend/platform owners |
| Product behavior and acceptance criteria | `docs/product-specs/` | feature owners |
| End-user workflows | `docs-site/` and component `@userGuide` blocks | UI/feature owners |
| Active implementation work | `docs/exec-plans/active/` | plan author |
| Security reporting and supported baseline | `SECURITY.md` | maintainers |
| Contributor workflow and definition of review | `CONTRIBUTING.md` | maintainers |
| Documentation governance and review ledger | this file | maintainers |
| Automated repository and release gates | `.github/workflows/`, `scripts/check_docs.py` | maintainers |

If two documents disagree, fix both in the same change and retain one canonical
statement with links from secondary locations. Do not copy large rule blocks.

## Change requirements

Update documentation in the same pull request when changing:

- CLI flags, lifecycle behavior, ports, defaults, paths, or dependencies;
- supported platforms, host tools, network access, capacity, or first-use setup;
- public APIs, streaming events, configuration fields, or error contracts;
- authentication, permissions, allowlists, upload/execution limits, or privacy;
- persistent schemas, migrations, retention, caching, or cleanup;
- a visible user workflow or component.

Component pages marked `generated: true` are generated from bilingual
`@userGuide` annotations. Edit the component source and run
`npm run docs:extract`; do not hand-edit those pages. Hand-written pages omit
the generated marker and are preserved by extraction.

## Lifecycle

Active plans must state an owner, expected outcome, verification, and remaining
risk. On completion, replace verbose scratch instructions with a concise outcome
under `docs/exec-plans/completed/`, or delete the plan if it no longer provides
durable context. Historical text must not preserve live credentials, obsolete
deployment instructions, or vendored generated artifacts.

The plan author reviews an active plan whenever scope, verification, risk, or
status changes. Component owners review maintained design/reference/product
documents whenever their component changes. Repository, runtime, and security
maintainers perform a full documentation audit at least quarterly and before a
release. Record the audit in the ledger below even when no content change is
required.

Before a release, CI must prove the deployment contract from a cold checkout:
no project toolchain or dependency cache, the README bootstrap command, the
default full-stack start, health and authentication checks, complete stop, and
no tracked or unignored generated files. Hosted `x86_64` validation is required;
an architecture is not described as fully validated until its equivalent job or
release hardware has passed.

Use Git history for obsolete implementation detail rather than leaving stale
guidance in an authoritative directory. Dated completed plans, superseded
design notes, and team iteration reports are non-authoritative historical
snapshots and may retain values that were current at the time. Current operator
or contributor documentation must not present or link to those snapshots as
live instructions. Historical snapshots must still be clearly dated and must
not retain credentials or retired product branding.

## Review ledger

| Review date | Review type | Scope | Reviewer role | Outcome |
| --- | --- | --- | --- | --- |
| 2026-07-16 | Full audit | repository, runtime, security, architecture, API, user guide | maintainers | aligned with project-local uv, sandboxing, authenticated API, and OTLP tracing |
| 2026-07-17 | Full audit | portability, deployment, documentation governance, CI, release readiness | maintainers | clarified the supported host contract; added cold-checkout CI, local generated-page drift checks, default-port consistency, and review-freshness enforcement |

## Automated gates

`npm run governance:check` performs:

- Markdown linting;
- bilingual `@userGuide` metadata validation;
- read-only generated-page drift validation;
- local-link validation;
- documented lifecycle command and flag validation;
- `AGENTS.md` to `CLAUDE.md` symlink validation;
- documented default-port consistency validation;
- documentation full-audit freshness validation with a 92-day limit;
- a complete VitePress production build;
- committed/unignored credential-pattern scanning.

The documentation workflow repeats those checks on every pull request and push
that can affect documentation or deployment. Code CI additionally performs
Python and Node dependency vulnerability audits and a cache-free clone,
bootstrap, start, health/authentication, stop, and worktree-cleanliness smoke
test. Both workflows run weekly so newly disclosed dependency issues and an
expired quarterly review ledger are detected even without a source change.

Run the non-Node governance checks directly with:

```bash
python3 scripts/check_docs.py
```
