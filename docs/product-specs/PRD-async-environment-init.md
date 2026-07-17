# Asynchronous project-local environment initialization

| Field | Value |
| --- | --- |
| Status | current |
| Product owner | agent runtime owner |
| Technical owner | backend/platform owner |
| Last reviewed | 2026-07-16 |

## Purpose

Creating an agent must return promptly while its isolated Python environment is
prepared in the background. The UI reports the environment state and prevents
Skill execution until the environment is ready.

This feature uses the pinned uv and managed Python installed by
`./bootstrap.sh`. It must not discover, install into, or modify a user or system
Python installation.

## Invariants

- The uv executable is `.tools/uv`; the root application environment is
  `.venv/`.
- Per-agent virtual environments live under `.runtime/environments/`.
- Managed Python installations and dependency caches live under `.runtime/`.
- Environment metadata lives under `data/environments/` and contains no
  credentials.
- Every derived agent path is contained beneath its managed root; symlink and
  traversal escapes are rejected.
- At most three environment-creation jobs run concurrently. Repeated creation
  requests for the same agent reuse the in-flight job.
- Package specifications are syntactically validated and package names must be
  present in `AGENT_BUILDER_PACKAGE_ALLOWLIST`.
- Environment deletion cancels an in-flight creation job before removing its
  virtual environment and metadata.

## User flow

1. `POST /api/agents` persists the agent, records environment state
   `creating`, schedules background creation, and returns immediately.
2. The frontend polls `GET /api/agents/{name}/environment` while the state is
   `creating`.
3. Skill selection and execution remain unavailable until state `ready`.
   Agent settings unrelated to Skill execution remain editable.
4. On failure, the state becomes `error` with a bounded diagnostic message.
   `POST /api/agents/{name}/environment/retry` removes failed state and starts
   one replacement job.
5. Deleting the agent or environment cancels the active job and removes its
   project-local runtime state.

The UI may show estimated progress, but it must treat `status` as authoritative
and must not infer readiness from elapsed time.

## API contract

All endpoints below are authenticated according to
[the API reference](../references/api-reference.md).

### Create an agent

`POST /api/agents` returns without waiting for environment creation:

```json
{
  "success": true,
  "name": "example-agent",
  "environment_status": "creating"
}
```

### Read environment state

`GET /api/agents/{name}/environment` returns:

```json
{
  "exists": true,
  "environment": {
    "environment_id": "generated-id",
    "agent_name": "example-agent",
    "status": "creating",
    "environment_type": "uv",
    "python_version": "3.11",
    "packages": [],
    "installed_dependencies": {},
    "created_at": "2026-07-16T00:00:00",
    "updated_at": "2026-07-16T00:00:00",
    "error_message": null
  },
  "progress": 30.0,
  "estimated_remaining": 25000
}
```

`status` is one of `creating`, `ready`, `error`, or `deleted`. `progress` and
`estimated_remaining` may be null and are informational only.

### Explicit environment operations

| Method | Endpoint | Behavior |
| --- | --- | --- |
| POST | `/api/agents/{name}/environment` | Create and wait for a managed environment |
| DELETE | `/api/agents/{name}/environment` | Cancel creation and remove environment state |
| POST | `/api/agents/{name}/environment/retry` | Retry failed asynchronous creation |
| POST | `/api/agents/{name}/environment/packages` | Install validated, allowlisted packages with uv |
| GET | `/api/agents/{name}/environment/packages` | List packages from the managed environment |

## Execution security

An environment becoming `ready` does not grant unrestricted process execution.
On Linux, each uploaded Skill process must enter the Landlock filesystem
sandbox before execution. It receives bounded CPU time, memory, output, file
size, process count, descriptors, wall time, and workspace quota. If Landlock is
unavailable, execution fails closed.

Network socket creation is denied by seccomp unless the operator explicitly
sets `AGENT_BUILDER_SKILL_NETWORK=allow` before startup. The control-plane API
token is removed from child environments.

## Acceptance criteria

| ID | Requirement |
| --- | --- |
| ENV-01 | Agent creation returns promptly with `environment_status=creating`. |
| ENV-02 | Concurrent agents never exceed the configured creation concurrency. |
| ENV-03 | Duplicate creation for one agent does not create duplicate environments. |
| ENV-04 | Ready environments and caches remain entirely inside the checkout. |
| ENV-05 | Retry replaces failed state; delete cancels an active job. |
| ENV-06 | Invalid or non-allowlisted packages are rejected before uv runs. |
| ENV-07 | Skill execution refuses to run without the Linux filesystem sandbox. |
| ENV-08 | Skill networking is denied by default and credentials do not reach children. |
| ENV-09 | Bootstrap, environment, and execution logs are bounded and contain no secrets. |

## Verification

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_runtime_management.py
./.venv/bin/python -m pytest tests/test_process_sandbox.py
./.venv/bin/python -m pytest tests/security_test.py
```

Lifecycle and UI changes additionally require the full checks in
[the testing guide](../references/testing-guide.md).
