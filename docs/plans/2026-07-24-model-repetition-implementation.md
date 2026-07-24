---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-24
review_cycle: quarterly
---

# Model Repetition Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect exact model-output repetition, close the Ollama stream immediately, commit a bounded marked answer, and keep the next Turn accurate and usable.

**Architecture:** A pure bounded suffix detector feeds the existing trusted Ollama normalization layer. A new `repetition_truncated` normalized stop travels through the existing Worker IPC v2 and canonical event path; it completes the Turn with incomplete Provider usage while preserving the single SQLite transcript and versioned next-turn projection.

**Tech Stack:** Python 3.12, asyncio, httpx streaming, SQLite/WAL, pytest, vanilla JavaScript, existing Agent Builder Control/Worker IPC v2.

## Global Constraints

- Work only under `/home/dev/share/aaa/agent-builder`; load `source ./env.sh` before development and validation commands.
- The fixed Provider remains `iollama:11434/qwen3.5:2b`; browser, Worker and request data cannot override endpoint, model, generation options or detector thresholds.
- Provider raw limits remain bounded: 4096 frames, 64 KiB per NDJSON line, 1 MiB total after this change, 12 KiB normalized assistant content, and 128 content IPC frames.
- Repetition completion is allowed only for a tool-free ordinary response with visible content; explicit cancellation, Tool phases and empty output keep their existing semantics.
- A repetition-truncated Provider observation uses zero token fields, `usage_complete=false` and `error_code=null`; it never enters soft calibration and is never presented as exact usage.
- No Provider frame, token delta or detector tick is persisted; only existing semantic boundaries and the final fixed marker are durable.
- Canonical ConversationTurn content, `assistant.block.finished` content and `CompletedTurnContext` assistant content must be byte-identical.
- Existing Conversation data is not rewritten; target-conversation validation creates three fresh isolated Conversations.
- Every production-code change follows RED → verified failure → minimal GREEN → verified pass; each task ends in a focused commit.

---

### Task 1: Bounded exact suffix detector

**Files:**
- Create: `src/agent_builder_v2/repetition.py`
- Create: `tests/test_repetition.py`

**Interfaces:**
- Produces: `RepetitionMatch(repeat_start: int, keep_end: int, unit_bytes: int, repetitions: int)`.
- Produces: `detect_repeating_suffix(text: str) -> RepetitionMatch | None`.
- Produces constants `MIN_REPEAT_UNIT_BYTES=32`, `MAX_REPEAT_UNIT_BYTES=512`, `MIN_REPEAT_COPIES=3`, `MIN_REPEAT_EVIDENCE_BYTES=512`, `REPETITION_CHECK_INTERVAL_BYTES=64`, and `REPETITION_SCAN_CODEPOINTS=2048`.

- [ ] **Step 1: Write detector boundary tests**

```python
from agent_builder_v2.repetition import (
    MIN_REPEAT_EVIDENCE_BYTES,
    REPETITION_SCAN_CODEPOINTS,
    detect_repeating_suffix,
)


def test_detects_exact_suffix_and_keeps_one_cycle() -> None:
    unit = "Because they make up everything!\nThat is why they are not trusted.\n"
    text = "valid joke\n" + unit * 9
    match = detect_repeating_suffix(text)
    assert match is not None
    assert text[match.repeat_start:match.keep_end] == unit
    assert text[match.keep_end:] == unit * 8
    assert match.repetitions == 9


def test_does_not_match_two_copies_or_non_suffix() -> None:
    unit = "a sufficiently varied repeated sentence.\n"
    assert detect_repeating_suffix(unit * 2) is None
    assert detect_repeating_suffix(unit * 8 + "natural ending") is None


def test_detects_utf8_without_splitting_codepoints() -> None:
    unit = "中文循环🙂必须保持 UTF-8 边界。\n"
    text = "前缀\n" + unit * 12
    match = detect_repeating_suffix(text)
    assert match is not None
    assert text[:match.keep_end].encode("utf-8").decode("utf-8")


def test_scan_window_and_evidence_are_bounded() -> None:
    assert MIN_REPEAT_EVIDENCE_BYTES == 512
    assert REPETITION_SCAN_CODEPOINTS == 2048
    assert detect_repeating_suffix("x" * 511) is None
```

- [ ] **Step 2: Run the detector tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_repetition.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent_builder_v2.repetition'`.

- [ ] **Step 3: Implement the pure bounded detector**

```python
from dataclasses import dataclass

MIN_REPEAT_UNIT_BYTES = 32
MAX_REPEAT_UNIT_BYTES = 512
MIN_REPEAT_COPIES = 3
MIN_REPEAT_EVIDENCE_BYTES = 512
REPETITION_CHECK_INTERVAL_BYTES = 64
REPETITION_SCAN_CODEPOINTS = 2048


@dataclass(frozen=True, slots=True)
class RepetitionMatch:
    repeat_start: int
    keep_end: int
    unit_bytes: int
    repetitions: int


def detect_repeating_suffix(text: str) -> RepetitionMatch | None:
    if not isinstance(text, str) or len(text.encode("utf-8")) < MIN_REPEAT_EVIDENCE_BYTES:
        return None
    scan_start = max(0, len(text) - REPETITION_SCAN_CODEPOINTS)
    maximum = min(MAX_REPEAT_UNIT_BYTES, (len(text) - scan_start) // MIN_REPEAT_COPIES)
    matches: list[tuple[int, int, str, int]] = []
    for width in range(1, maximum + 1):
        unit = text[-width:]
        unit_bytes = len(unit.encode("utf-8"))
        if not MIN_REPEAT_UNIT_BYTES <= unit_bytes <= MAX_REPEAT_UNIT_BYTES:
            continue
        cursor = len(text)
        copies = 0
        while cursor - width >= scan_start and text[cursor - width:cursor] == unit:
            cursor -= width
            copies += 1
        if copies >= MIN_REPEAT_COPIES and copies * unit_bytes >= MIN_REPEAT_EVIDENCE_BYTES:
            matches.append((unit_bytes, width, unit, copies))
    if not matches:
        return None
    _unit_bytes, width, unit, copies = min(matches, key=lambda item: (item[0], item[1]))
    cursor = len(text) - width * copies
    while cursor - width >= 0 and text[cursor - width:cursor] == unit:
        cursor -= width
        copies += 1
    return RepetitionMatch(cursor, cursor + width, len(unit.encode("utf-8")), copies)
```

- [ ] **Step 4: Run detector tests and verify GREEN**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_repetition.py -q
```

Expected: all detector tests pass.

- [ ] **Step 5: Commit the detector**

```bash
git add src/agent_builder_v2/repetition.py tests/test_repetition.py
git commit -m "feat: detect bounded model repetition"
```

### Task 2: Trusted response sampling and raw stream budget

**Files:**
- Modify: `src/agent_builder_v2/generation.py`
- Modify: `src/agent_builder_v2/ollama.py`
- Modify: `tests/test_generation.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Produces generation policy `tool-phase-generation-v2` with response options `temperature=1.0`, `top_p=0.95`, `top_k=20`, `presence_penalty=1.5`, `seed=0`.
- Produces `MAX_STREAM_BYTES = 1024 * 1024`; every other raw and normalized limit remains unchanged.

- [ ] **Step 1: Change generation and raw-budget expectations first**

```python
def test_no_tool_phase_uses_qualified_response_sampling() -> None:
    assert generation_options_for(
        has_tools=False, deterministic_temperature=0, seed=0
    ) == {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "presence_penalty": 1.5,
        "seed": 0,
    }


def test_raw_stream_budget_matches_4096_token_profile() -> None:
    assert MAX_STREAM_BYTES == 1024 * 1024
```

Add an Ollama stream test whose encoded NDJSON is greater than 512 KiB but no greater than 1 MiB, uses fewer than 4096 frames, emits no more than 12 KiB normalized text, and reaches a valid terminal frame.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_generation.py tests/test_ollama.py -q
```

Expected: response options and `MAX_STREAM_BYTES` assertions fail against v1 values.

- [ ] **Step 3: Update the trusted constants and manifest**

```python
GENERATION_POLICY_VERSION = "tool-phase-generation-v2"
RESPONSE_TEMPERATURE = 1.0
RESPONSE_TOP_P = 0.95
RESPONSE_TOP_K = 20
RESPONSE_PRESENCE_PENALTY = 1.5
```

Return all five response-phase options from `generation_options_for`; keep the Tool phase exactly `{"temperature": 0, "seed": 0}`. Set `MAX_STREAM_BYTES = 1024 * 1024` without changing frame, line, normalized-output or IPC ceilings.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_generation.py tests/test_model_catalog.py tests/test_ollama.py -q
```

Expected: all selected tests pass and catalog digests reflect generation policy v2.

- [ ] **Step 5: Commit sampling and budget**

```bash
git add src/agent_builder_v2/generation.py src/agent_builder_v2/ollama.py tests/test_generation.py tests/test_ollama.py
git commit -m "fix: qualify response sampling and stream budget"
```

### Task 3: Ollama repetition guard and immediate stream close

**Files:**
- Modify: `src/agent_builder_v2/ollama.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Consumes: `detect_repeating_suffix(text)` and `REPETITION_CHECK_INTERVAL_BYTES` from Task 1.
- Produces: `REPETITION_TRUNCATION_MARKER`.
- Produces normalized `OllamaFrame("stop", {"reason": "repetition_truncated", "usage": None})` only for visible tool-free output.

- [ ] **Step 1: Add a failing close-and-stop transport test**

Create a bounded `httpx.AsyncByteStream` test helper that yields a valid nonterminal prefix followed by repeated units, sets `closed=True` in `aclose`, and raises if any sentinel tail is consumed after detection. Assert:

```python
frames = await _collect(session.stream_turn("write a joke"))
content = "".join(frame.payload["text"] for frame in frames if frame.kind == "content")
assert stream.closed is True
assert "must-not-be-consumed" not in content
assert content.count(REPETITION_TRUNCATION_MARKER) == 1
assert len(content.encode("utf-8")) <= MAX_OUTPUT_BYTES
assert frames[-1] == OllamaFrame(
    "stop", {"reason": "repetition_truncated", "usage": None}
)
```

Add separate tests proving two copies do not trigger, a valid Provider terminal wins, Tool-visible calls do not complete through the guard, cancellation wins, no retry occurs, and a second stream acquires the released Broker slot.

- [ ] **Step 2: Run the new Ollama tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_ollama.py -k "repetition or released_broker_slot" -q
```

Expected: imports or assertions fail because the marker and special stop do not exist.

- [ ] **Step 3: Add the guard to `OllamaSession.stream_turn`**

Define the marker as the same 67-byte reservation already used by the output-limit path:

```python
REPETITION_TRUNCATION_MARKER = (
    "\n\n[回答因重复死循环被截断；后续忽略重复尾部。]"
)
assert len(REPETITION_TRUNCATION_MARKER.encode("utf-8")) == 67
```

Implement these exact state transitions inside the existing accepted-content branch:

```python
accepted_since_repeat_check += accepted_bytes
if (
    not available_tools
    and not raw_frame["done"]
    and accepted_since_repeat_check >= REPETITION_CHECK_INTERVAL_BYTES
):
    accepted_since_repeat_check = 0
    full_content = "".join(content_parts)
    repetition = detect_repeating_suffix(full_content)
    if repetition is not None:
        if is_cancelled():
            raise OllamaCancelledError()
        keep_end = max(emitted_content_characters, repetition.keep_end)
        guarded_content = full_content[:keep_end] + REPETITION_TRUNCATION_MARKER
        pending_text = guarded_content[emitted_content_characters:]
        content_parts = [guarded_content]
        coalesced = [pending_text] if pending_text else []
        coalesced_bytes = len(pending_text.encode("utf-8"))
        output_bytes = len(guarded_content.encode("utf-8"))
        repetition_truncated = True
        break
```

Increment `emitted_content_characters` only after each normalized content chunk is yielded. After the raw loop, handle `repetition_truncated` before requiring `final_frame`: flush the pending marker/content, append the guarded assistant message, set `_stopped=True`, and yield the special stop. The existing `finally: await result.aclose()` closes HTTP and releases the slot; do not raise a Broker error and do not call `observe_context_usage`.

- [ ] **Step 4: Run Ollama and generation tests and verify GREEN**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_repetition.py tests/test_generation.py tests/test_ollama.py -q
```

Expected: repetition close tests pass, no existing Tool/output-limit tests regress.

- [ ] **Step 5: Commit the active guard**

```bash
git add src/agent_builder_v2/ollama.py tests/test_ollama.py
git commit -m "feat: stop repeated Ollama streams"
```

### Task 4: Canonical completed outcome with incomplete usage

**Files:**
- Modify: `src/agent_builder_v2/model.py`
- Modify: `src/agent_builder_v2/kernel.py`
- Modify: `src/agent_builder_v2/control.py`
- Modify: `src/agent_builder_v2/sessions.py`
- Modify: `src/agent_builder_v2/replay.py`
- Modify: `tests/test_kernel.py`
- Modify: `tests/test_control.py`
- Modify: `tests/test_sessions.py`
- Modify: `tests/test_replay.py`
- Modify: `tests/test_worker_integration.py`

**Interfaces:**
- Consumes: normalized stop reason `repetition_truncated` from Task 3.
- Produces: `model.response.finished.outcome=repetition_truncated` with zero tokens, incomplete usage and null error.
- Produces: `run.completed.reason=repetition_truncated`, byte-identical final assistant and completed bundle.

- [ ] **Step 1: Add failing protocol tests from the outside inward**

Add a `StreamingModel` fixture that emits visible text and `ModelBlock("stop", {"reason": "repetition_truncated"})`; assert `HarnessKernel` ends in one `run.completed` with that reason. Add Control/Worker integration assertions:

```python
assert model_finished.payload == {
    "request_id": "model-1",
    "iteration": 1,
    "attempt": 0,
    "recovery_id": None,
    "provider_call_index": 1,
    "outcome": "repetition_truncated",
    "input_tokens": 0,
    "output_tokens": 0,
    "usage_complete": False,
    "error_code": None,
}
assert terminal.payload["reason"] == "repetition_truncated"
assert terminal.payload["usage"]["complete"] is False
assert completed_turn.assistant_content.endswith(REPETITION_TRUNCATION_MARKER)
assert completed_context.items[-1].content == completed_turn.assistant_content
```

In sessions/replay tests, accept only this exact special combination. Parametrize corrupt variants with nonzero tokens, `usage_complete=True`, non-null `error_code`, wrong terminal reason, open Tool state, or missing visible assistant content; each must raise its existing corruption/conflict exception.

- [ ] **Step 2: Run protocol tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_kernel.py tests/test_control.py tests/test_sessions.py tests/test_replay.py tests/test_worker_integration.py -q
```

Expected: new stop/outcome/reason is rejected by the current enum validators.

- [ ] **Step 3: Extend the existing protocol without changing IPC version**

Make these minimal changes:

```python
# model.py and kernel.py
VALID_COMPLETED_STOP_REASONS = {"end_turn", "max_output", "repetition_truncated"}
```

In Control's special stop branch call:

```python
await finish_response(
    "repetition_truncated",
    input_tokens=0,
    output_tokens=0,
    usage_complete=False,
    error_code=None,
)
record.model_usage["complete"] = False
record.broker_stop_iteration = expected_iteration
record.final_assistant_content = assistant_content
```

Do not call `_apply_validated_model_usage` for this observation. Extend sessions and replay validators so `repetition_truncated` is the only completed outcome allowed with incomplete zero usage and null error. Permit the matching Run reason everywhere a completed terminal is validated, and permit it as the final model-call outcome in replay projections.

- [ ] **Step 4: Run protocol tests and verify GREEN**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_kernel.py tests/test_control.py tests/test_sessions.py tests/test_replay.py tests/test_worker_integration.py -q
```

Expected: all selected tests pass, including restart/replay and corruption negatives.

- [ ] **Step 5: Commit the canonical outcome**

```bash
git add src/agent_builder_v2/model.py src/agent_builder_v2/kernel.py src/agent_builder_v2/control.py src/agent_builder_v2/sessions.py src/agent_builder_v2/replay.py tests/test_kernel.py tests/test_control.py tests/test_sessions.py tests/test_replay.py tests/test_worker_integration.py
git commit -m "feat: complete repetition-truncated turns"
```

### Task 5: Web terminal and usage presentation

**Files:**
- Modify: `src/agent_builder_v2/static/app.js`
- Modify: `tests/test_frontend.py`
- Modify: `tests/test_browser_behavior.py`

**Interfaces:**
- Consumes canonical outcome/reason from Task 4.
- Produces a non-error terminal label and an explicitly incomplete computation indicator; canonical terminal refresh continues to replace live content.

- [ ] **Step 1: Add failing static and browser behavior assertions**

Assert `model.response.finished` accepts `repetition_truncated`, `run.completed` accepts the same reason, and event details contain:

```text
检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整
```

Simulate live content followed by `assistant.block.finished` and `run.completed/repetition_truncated`; assert one Turn card, one marker, completed status, and `usage.complete === false`.

- [ ] **Step 2: Run frontend tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_frontend.py tests/test_browser_behavior.py -q
```

Expected: JavaScript validators return unavailable/invalid for the new enum.

- [ ] **Step 3: Extend the existing UI enum branches**

Add `repetition_truncated` to the exact model outcome and completed-reason allowlists. Render it as completed, never retryable, never a circuit failure, and preserve the existing terminal canonical refresh. Keep raw Provider bodies hidden.

- [ ] **Step 4: Run frontend tests and verify GREEN**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_frontend.py tests/test_browser_behavior.py -q
```

Expected: both suites pass with no browser-console error.

- [ ] **Step 5: Commit Web presentation**

```bash
git add src/agent_builder_v2/static/app.js tests/test_frontend.py tests/test_browser_behavior.py
git commit -m "feat: present repetition truncation completion"
```

### Task 6: Continuation, calibration and full deterministic regression

**Files:**
- Modify: `tests/test_context_projection.py`
- Modify: `tests/test_sessions.py`
- Modify: `tests/test_replay.py`
- Modify: `tests/test_worker_integration.py`
- Modify: `tests/test_ux_reliability.py`

**Interfaces:**
- Consumes the completed bundle and incomplete provider row from Task 4.
- Proves next-turn preview/admission derives from committed content and excludes the incomplete observation.

- [ ] **Step 1: Add a failing two-Turn continuation test**

Build a first Run whose fake Provider triggers repetition and a second Run with user message `ok`. Assert after Turn 1:

```python
assert first_turn.status == "completed"
assert first_turn.assistant_content.count(REPETITION_TRUNCATION_MARKER) == 1
assert first_usage.status == "incomplete"
assert first_usage.input_tokens is None
assert first_usage.output_tokens is None
```

Then assert Turn 2 starts and completes, its `run.started` context history digest matches the committed Conversation revision, its rendered request contains the marker exactly once, and calibration observations do not include the incomplete row. Add a restart between the two Turns and repeat the same assertions.

- [ ] **Step 2: Run continuation tests and verify RED**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_context_projection.py tests/test_sessions.py tests/test_replay.py tests/test_worker_integration.py tests/test_ux_reliability.py -k "repetition or incomplete_usage_continuation" -q
```

Expected: fixture construction reaches an enum or projection assertion failure until Tasks 3–4 are complete; if those tasks already make the behavior green, temporarily assert the exact missing calibration/restart invariant and verify that assertion fails before adding the minimal supporting code.

- [ ] **Step 3: Make only the projection/calibration changes required by RED**

Keep `ProviderObservedUsage` unchanged. Ensure incomplete rows are filtered by the existing complete-observation query and no preview path reads the aborted call's zero fields. If no production change is required, retain the red/green test as proof of the existing typed-count contract rather than adding duplicate logic.

- [ ] **Step 4: Run the deterministic affected matrix**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest \
  tests/test_repetition.py tests/test_generation.py tests/test_ollama.py \
  tests/test_kernel.py tests/test_control.py tests/test_sessions.py \
  tests/test_replay.py tests/test_worker_integration.py \
  tests/test_context_projection.py tests/test_ux_reliability.py \
  tests/test_frontend.py tests/test_browser_behavior.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit continuation coverage**

```bash
git add tests/test_context_projection.py tests/test_sessions.py tests/test_replay.py tests/test_worker_integration.py tests/test_ux_reliability.py
git commit -m "test: prove repetition-truncated continuation"
```

### Task 7: Qualification, authoritative docs and Gate closure

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/design/architecture.md`
- Modify: `docs/design/event-protocol.md`
- Modify: `docs/design/release.md`
- Modify: `docs/plans/runtime-rebuild.md`
- Modify then delete at final closure: `docs/plans/model-repetition-remediation.md`
- Delete at final closure: `docs/plans/2026-07-24-model-repetition-implementation.md`

**Interfaces:**
- Produces release evidence `RR-REPETITION-20260724-01` without prompt, token, endpoint, IDs or raw frames.
- Closes `REP-R03-01..08`, `GATE-05` and `GATE-07` only after every command below passes.

- [ ] **Step 1: Update authoritative behavior and protocol docs**

Document the fixed sampling policy, 1 MiB raw budget, bounded exact detector, immediate close, special completed/incomplete-usage event combination, next-turn count-domain behavior, rollback decoder rule and known intentional-repeat false-positive tradeoff. Do not copy the work ledger outside the activity plan.

- [ ] **Step 2: Run the full local code gates**

Run:

```bash
source ./env.sh
./.venv/bin/python -m pytest
./governance.sh
git diff --check
bash -n bootstrap.sh start.sh stop.sh release.sh governance.sh
node --check src/agent_builder_v2/static/app.js
test "$(readlink AGENTS.md)" = "CLAUDE.md"
```

Expected: every command exits 0; record the exact pytest pass count.

- [ ] **Step 3: Run lifecycle qualification before the target fixture**

Run:

```bash
./stop.sh
./start.sh
curl --fail --silent http://127.0.0.1:20815/health >/dev/null
```

Expected: clean start, qualified `qwen3.5:2b`, `landlock+seccomp`, healthy Gateway and no stale managed process warning.

- [ ] **Step 4: Execute the five-input sequence three times**

Using the authenticated Agent-scoped API and three newly created Conversations, submit exactly:

```text
hello
who are you
932748+29183/11=?
write a joke for around 300 words
ok
```

For each pass, inspect authenticated Conversation detail and durable Run events in memory. Require 5 completed Turns, zero failed/cancelled/interrupted, correct math `935401`, no `model_protocol_error`, Turn 4 natural completion or one repetition marker/reason, and a nonempty completed Turn 5 bound to Turn 4's committed history. Delete all three qualification Conversations after collecting only bounded aggregate evidence under `.runtime/test-results/RR-REPETITION-20260724-01/`.

- [ ] **Step 5: Force the real cancellation path once**

In a trusted test process only, monkeypatch `agent_builder_v2.ollama.generation_options_for` to return the old response options `temperature=0.7/top_p=0.8/seed=0` for the exact joke fixture. Require the guard marker/reason, `usage_complete=false`, elapsed time below the old 100-second reproduction, immediate completion of a following short request, and zero active Broker slot/Run/Worker residual. Do not expose this override through production API, Worker IPC or ModelCatalog.

- [ ] **Step 6: Stop normally, start once more, then force-stop and audit residuals**

Run:

```bash
./stop.sh
./start.sh
curl --fail --silent http://127.0.0.1:20815/health >/dev/null
./stop.sh --force
```

Expected: both stop paths validate process identity; no managed Gateway/Worker, `worker.pid`, active Run root or orphan HTTP stream remains.

- [ ] **Step 7: Close the activity ledger and delete temporary plans**

Record the bounded RR summary in `docs/plans/runtime-rebuild.md`, mark `REP-R03` done, close `GATE-05/GATE-07`, update maintained-document review dates, and delete the two active temporary plan files in this task as required by documentation governance. Run full pytest and governance again after deletion.

- [ ] **Step 8: Commit qualification and documentation closure**

```bash
git add -A CLAUDE.md README.md SECURITY.md docs src tests
git commit -m "docs: qualify repetition loop recovery"
```

Expected final state: clean worktree, all commits on the feature branch, no production data modification, and all target outcomes supported by fresh command output.
