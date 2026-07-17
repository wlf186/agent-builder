# Contributing

## Set up

```bash
./bootstrap.sh
source ./env.sh
```

Use only checkout-local tools and caches. Do not install dependencies globally
or write project state into a user home directory.

## Make a change

1. Read [CLAUDE.md](CLAUDE.md) and the relevant design/reference document.
2. Keep each change scoped and preserve unrelated work in the checkout.
3. Add a regression test before or with a defect fix.
4. Update user, API, lifecycle, configuration, or security documentation in the
   same change as the behavior.
5. Run the checks below and inspect the diff for generated/runtime artifacts.

## Required checks

```bash
source ./env.sh
./.venv/bin/python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
npm run governance:check
```

For a narrow backend change, run the affected test first. For API security,
uploads, paths, URLs, archives, or execution changes, include negative and
boundary cases. For lifecycle changes, verify clean start, repeated start,
health failure rollback, clean stop, and stale PID handling.

Changes to bootstrap, dependency locks, supported platforms, service defaults,
or deployment documentation must also pass the cold-checkout lifecycle job:
the documented bootstrap, default full-stack start, health/authentication
checks, stop, and a clean Git worktree. The CI job intentionally restores no
project dependency cache so it exercises the same contract as a new clone.

## Commits and reviews

- Do not commit `.runtime/`, `.tools/`, environments, caches, logs, databases,
  uploads, browser artifacts, or credentials.
- Explain user-visible behavior, migration risk, security impact, and the exact
  verification performed in the pull request.
- Avoid drive-by formatting or deletion of unrelated files.
- Resolve generated documentation by editing the component `@userGuide` block
  and running `npm run docs:extract`.

Documentation ownership and freshness rules are in
[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md). Security reports follow
[SECURITY.md](SECURITY.md).
