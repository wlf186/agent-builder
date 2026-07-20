"""Command application service, Worker supervisor and canonical sequencer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .capsule import AgentCapsule, CapsuleManager
from .context import ConversationMessage, ContextCompiler, ContextPlan
from .contracts import (
    RUN_CURSOR_RESERVED_THROUGH,
    TERMINAL_KINDS,
    EventEnvelope,
    StartRunCommand,
    new_id,
    utc_now,
)
from .model import BROKER_PROTOCOL_VERSION, MAX_BROKER_FRAME_BYTES
from .ollama import (
    OllamaBroker,
    OllamaBrokerError,
    OllamaCancelledError,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaRunSession,
    OllamaToolResult,
)
from .replay import DurableReplay, RunIdentity
from .sandbox import HostQualification, require_qualified_host
from .sessions import (
    Conversation,
    ConversationDeleteResult,
    ConversationNotFoundError,
    ConversationStore,
    ConversationSummary,
)
from .state import (
    EventJournal,
    JournalCorruptionError,
    JournalUnavailableError,
)
from .sync_counter import qualification_environment
from .tools import ToolSpec, prototype_tool_specs, toolset_digest


WORKER_EVENT_KINDS = frozenset(
    {
        "assistant.block.started",
        "assistant.block.delta",
        "assistant.block.finished",
        "assistant.block.discarded",
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
)
MAX_LIVE_EVENTS = RUN_CURSOR_RESERVED_THROUGH
MAX_WORKER_EVENT_BYTES = 65_536
MAX_ACTIVE_RUNS = 4
MAX_RETAINED_RUNS = 64
MAX_LIVE_EVENT_BYTES = 1024 * 1024
MAX_DURABLE_BYTES_PER_RUN = 256 * 1024
TERMINAL_EVENT_RESERVE = 8 * 1024
RECOVERY_EVENT_RESERVE = 32 * 1024
# Worst case after one accepted nonterminal event: discard one open assistant
# block, start and finish one requested-only Tool, then publish the terminal.
RECOVERY_EVENT_SLOTS = 4
MAX_DURABLE_EVENT_BYTES = 65_536
RUN_WALL_TIMEOUT_SECONDS = 60.0
CANCEL_GRACE_SECONDS = 2.0
RUN_QUOTA_INTERVAL_SECONDS = 1.0
MAX_RUN_TREE_ENTRIES = 1_024
MAX_RUN_LOGICAL_BYTES = 16 * 1024 * 1024
MAX_RUN_ALLOCATED_BYTES = 32 * 1024 * 1024
WORKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
WORKER_TEXT_BYTES = 12_288
SANDBOX_POLICY = "harness-v2-worker-v1"
LOGGER = logging.getLogger(__name__)


def _marker_from_proc_stat(raw: str) -> str:
    """Return Linux starttime while tolerating spaces and ')' in comm."""

    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("process stat has no command terminator")
    fields = raw[closing + 1 :].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("process stat has no valid starttime")
    return f"linux:{fields[19]}"


def _process_marker(pid: int) -> str:
    return _marker_from_proc_stat(
        Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    )


def _process_status(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/status").read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("Worker process status exceeded its limit")
    values: dict[str, str] = {}
    for line in raw.decode("ascii", errors="strict").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    return values


def _validate_sandbox_ready(pid: int, value: object) -> None:
    expected_keys = {
        "internal",
        "version",
        "policy",
        "landlock_abi",
        "seccomp_arch",
        "seccomp_mode",
        "no_new_privileges",
        "parent_pid",
        "tcp_network_denied",
        "abstract_unix_scoped",
        "signal_scoped",
        "process_creation_denied",
        "descriptor_isolation",
        "filesystem_write_denied",
        "persistent_ipc_denied",
        "dumpable",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("Worker sandbox handshake has invalid fields")
    if (
        value.get("internal") != "sandbox.ready"
        or value.get("version") != BROKER_PROTOCOL_VERSION
        or value.get("policy") != SANDBOX_POLICY
        or not isinstance(value.get("landlock_abi"), int)
        or isinstance(value.get("landlock_abi"), bool)
        or value["landlock_abi"] < 6
        or value.get("seccomp_arch") not in {"x86_64", "aarch64"}
        or value.get("seccomp_mode") != 2
        or value.get("no_new_privileges") is not True
        or value.get("parent_pid") != os.getpid()
        or value.get("tcp_network_denied") is not True
        or value.get("abstract_unix_scoped") is not True
        or value.get("signal_scoped") is not True
        or value.get("process_creation_denied") is not True
        or value.get("descriptor_isolation") is not True
        or value.get("filesystem_write_denied") is not True
        or value.get("persistent_ipc_denied") is not True
        or value.get("dumpable") is not False
    ):
        raise ValueError("Worker sandbox handshake failed policy validation")
    status = _process_status(pid)
    try:
        parent_pid = int(status["PPid"].split()[0])
        no_new_privileges = int(status["NoNewPrivs"].split()[0])
        seccomp_mode = int(status["Seccomp"].split()[0])
        seccomp_filters = int(status["Seccomp_filters"].split()[0])
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("Worker kernel sandbox state is unavailable") from exc
    if (
        parent_pid != os.getpid()
        or no_new_privileges != 1
        or seccomp_mode != 2
        or seccomp_filters < 1
    ):
        raise ValueError("Worker kernel sandbox state is invalid")


def _process_group_members(process_group: int, maximum_processes: int = 32_768) -> list[int]:
    members: list[int] = []
    inspected = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        inspected += 1
        if inspected > maximum_processes:
            raise RuntimeError("process-group scan exceeded its safety bound")
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            closing = raw.rfind(")")
            fields = raw[closing + 1 :].split()
            if closing < 0 or len(fields) < 3:
                continue
            if int(fields[2]) == process_group:
                members.append(int(entry.name))
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            continue
    return members


def _measure_run_tree(root: Path) -> tuple[int, int, int]:
    """Bound one untrusted Run tree without following links or mount changes."""

    root_metadata = os.lstat(root)
    if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.getuid():
        raise RuntimeError("Run quota root is unsafe")
    root_device = root_metadata.st_dev
    entries = 0
    logical_bytes = 0
    allocated_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries += 1
                if entries > MAX_RUN_TREE_ENTRIES:
                    raise RuntimeError("Run entry quota exceeded")
                metadata = entry.stat(follow_symlinks=False)
                if metadata.st_uid != os.getuid() or metadata.st_dev != root_device:
                    raise RuntimeError("Run tree ownership or mount boundary changed")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise RuntimeError("Run tree contains an unsafe entry")
                logical_bytes += metadata.st_size
                allocated_bytes += metadata.st_blocks * 512
                if logical_bytes > MAX_RUN_LOGICAL_BYTES:
                    raise RuntimeError("Run logical-byte quota exceeded")
                if allocated_bytes > MAX_RUN_ALLOCATED_BYTES:
                    raise RuntimeError("Run allocated-byte quota exceeded")
    return entries, logical_bytes, allocated_bytes


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write Worker PID record")
        view = view[written:]


def _atomic_worker_pid_record(path: Path, values: dict[str, str | int]) -> None:
    """Publish one complete private record by same-directory replacement."""

    ordered_keys = (
        "schema",
        "role",
        "pid",
        "pgid",
        "marker",
        "root",
        "agent_id",
        "run",
        "run_root",
        "module",
        "interpreter",
        "cwd",
        "command",
    )
    if set(values) != set(ordered_keys):
        raise ValueError("Worker PID record fields are incomplete")
    lines: list[str] = []
    for key in ordered_keys:
        value = str(values[key])
        if not value or "\n" in value or "\r" in value or "\0" in value:
            raise ValueError("Worker PID record contains an unsafe value")
        lines.append(f"{key}={value}")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    temporary = path.parent / f".worker.pid.{os.getpid()}.{new_id()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("secure Worker PID records require O_NOFOLLOW")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags | no_follow, 0o600)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeError("Worker PID record publication was unsafe")
        parent_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        parent_descriptor = os.open(path.parent, parent_flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_worker_pid_record(
    *,
    path: Path,
    repository_root: Path,
    run_root: Path,
    capsule: AgentCapsule,
    run_id: str,
    pid: int,
    marker: str,
) -> None:
    pgid = os.getpgid(pid)
    if pgid != pid:
        raise RuntimeError("Worker did not enter its own process group")
    interpreter = str(capsule.interpreter)
    module = "agent_builder_v2.worker"
    _atomic_worker_pid_record(
        path,
        {
            "schema": 1,
            "role": "worker",
            "pid": pid,
            "pgid": pgid,
            "marker": marker,
            "root": str(repository_root),
            "agent_id": capsule.agent_id,
            "run": run_id,
            "run_root": str(run_root),
            "module": module,
            "interpreter": interpreter,
            "cwd": str(run_root / "work"),
            "command": f"{interpreter} -m {module}",
        },
    )


def _signal_worker_group(
    process: asyncio.subprocess.Process,
    marker: str | None,
    signum: signal.Signals,
) -> bool:
    """Signal only while PID, PGID and Linux start marker still match."""

    if process.returncode is not None or marker is None:
        return False
    try:
        if _process_marker(process.pid) != marker or os.getpgid(process.pid) != process.pid:
            return False
        os.killpg(process.pid, signum)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False


@dataclass
class RunRecord:
    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    user_message: str | None = field(default=None, repr=False)
    conversation_managed: bool = False
    conversation_revision: int | None = None
    context_plan: ContextPlan | None = None
    effective_tools: tuple[ToolSpec, ...] = ()
    events: list[EventEnvelope] = field(default_factory=list)
    terminal_kind: str | None = None
    cancel_requested: bool = False
    cancel_requested_at: float | None = None
    live_event_bytes: int = 0
    durable_event_bytes: int = 0
    process: asyncio.subprocess.Process | None = None
    process_marker: str | None = None
    task: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    deadline_at: float | None = None
    open_blocks: set[str] = field(default_factory=set)
    seen_blocks: set[str] = field(default_factory=set)
    pending_tools: dict[str, str] = field(default_factory=dict)
    pending_tool_arguments: dict[str, dict[str, str]] = field(default_factory=dict)
    started_tools: set[str] = field(default_factory=set)
    seen_tools: set[str] = field(default_factory=set)
    broker_pending_tool_calls: dict[
        str, tuple[str, dict[str, str]]
    ] = field(default_factory=dict)
    broker_tool_results: list[OllamaToolResult] = field(default_factory=list)
    model_request_count: int = 0
    model_response_count: int = 0
    broker_stop_iteration: int | None = None
    final_assistant_content: str | None = field(default=None, repr=False)
    model_failure: tuple[str, bool] | None = None
    model_usage: dict[str, int | bool] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "last_input_tokens": 0,
            "complete": True,
        }
    )
    journal_failed: bool = False
    resource_failure: str | None = None
    retired: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def events_after(
        self, after: int, timeout: float = 15.0
    ) -> tuple[list[EventEnvelope], bool]:
        async with self.condition:
            if self.retired:
                return [], True
            available = [event for event in self.events if event.seq > after]
            if not available and self.terminal_kind is None:
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=timeout)
                except TimeoutError:
                    return [], False
                if self.retired:
                    return [], True
                available = [event for event in self.events if event.seq > after]
            done = self.retired or (
                self.terminal_kind is not None
                and (not available or available[-1].kind in TERMINAL_KINDS)
            )
            return available, done


class RunService:
    def __init__(
        self,
        repository_root: Path,
        source_root: Path,
        *,
        model_broker: OllamaBroker | None = None,
        context_compiler: ContextCompiler | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.source_root = source_root.resolve(strict=True)
        self._worker_qualification_environment = qualification_environment(
            self.repository_root,
            os.environ,
            expected_role="gateway",
            child_role="worker",
        )
        self.capsules = CapsuleManager(self.repository_root)
        self.capsule: AgentCapsule | None = None
        self.journal: EventJournal | None = None
        self.conversations: ConversationStore | None = None
        self.model_broker = model_broker or OllamaBroker()
        self.context_compiler = context_compiler or ContextCompiler()
        self.effective_tools = prototype_tool_specs()
        toolset_digest(self.effective_tools)
        self.model_qualification: OllamaQualification | None = None
        self.sandbox_qualification: HostQualification | None = None
        self.runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()
        self._control_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    def _track_control_task(self, task: asyncio.Task[Any]) -> None:
        """Keep a mutation alive after its request task has been cancelled."""

        self._control_tasks.add(task)

        def completed(value: asyncio.Task[Any]) -> None:
            self._control_tasks.discard(value)
            if not value.cancelled():
                # Retrieve background exceptions.  Awaiting callers still see
                # the same exception; abandoned request tasks do not produce
                # an unhandled-task warning or hide a failed mutation.
                value.exception()

        task.add_done_callback(completed)

    async def _drain_control_tasks(self) -> None:
        while self._control_tasks:
            await asyncio.gather(
                *tuple(self._control_tasks), return_exceptions=True
            )

    async def initialize(self) -> None:
        try:
            self.sandbox_qualification = await asyncio.to_thread(require_qualified_host)
            self.model_qualification = await self.model_broker.start()
            self.capsule = await asyncio.to_thread(self.capsules.ensure_prototype_agent)
            await asyncio.to_thread(
                self.capsules.cleanup_orphan_run_roots,
                self.capsule,
            )
            self.journal = EventJournal(self.capsule.data_root / "state.sqlite")
            self.conversations = ConversationStore(
                self.capsule.data_root / "state.sqlite",
                self.capsule.agent_id,
            )
            await asyncio.to_thread(
                self.conversations.recover_running_as_interrupted
            )
            await asyncio.to_thread(self.journal.prune_to_recent_runs, 256)
        except BaseException:
            if self.conversations is not None:
                self.conversations.close()
                self.conversations = None
            if self.journal is not None:
                self.journal.close()
                self.journal = None
            self.sandbox_qualification = None
            self.model_qualification = None
            await self.model_broker.close()
            raise

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
        # Admissions and deletes registered before the closing fence may still
        # be completing checkout-local thread commits.  Drain them first, then
        # take a fresh Run snapshot so a late admitted lifecycle cannot outlive
        # the stores it uses.
        await self._drain_control_tasks()
        async with self._lock:
            records = list(self.runs.values())
        for record in records:
            await self.cancel(record.run_id)
        tasks = [record.task for record in records if record.task is not None]
        tasks.extend(
            record.cancel_task
            for record in records
            if record.cancel_task is not None
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._drain_control_tasks()
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        if self.conversations is not None:
            self.conversations.close()
            self.conversations = None
        self.model_qualification = None
        self.sandbox_qualification = None
        await self.model_broker.close()

    def _conversation_store(self) -> ConversationStore:
        if self.conversations is None:
            raise RuntimeError("conversation store is unavailable")
        return self.conversations

    async def create_conversation(self, title: str = "新会话") -> Conversation:
        if self._closing:
            raise RuntimeError("RunService is closing")
        request_cancelled = asyncio.Event()
        operation = asyncio.create_task(
            self._create_conversation_owned(title, request_cancelled),
            name="harness-v2-create-conversation",
        )
        self._track_control_task(operation)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            request_cancelled.set()

            def delete_if_created(value: asyncio.Task[Conversation]) -> None:
                if value.cancelled():
                    return
                try:
                    conversation = value.result()
                except BaseException:
                    return
                cleanup = asyncio.create_task(
                    self._delete_conversation_fenced(
                        conversation.conversation_id
                    ),
                    name=(
                        "harness-v2-abandoned-conversation-"
                        f"{conversation.conversation_id}"
                    ),
                )
                self._track_control_task(cleanup)

            operation.add_done_callback(delete_if_created)
            raise

    async def _create_conversation_owned(
        self,
        title: str,
        request_cancelled: asyncio.Event,
    ) -> Conversation:
        conversation = await asyncio.to_thread(
            self._conversation_store().create_conversation,
            title,
        )
        if request_cancelled.is_set() or self._closing:
            await asyncio.to_thread(
                self._conversation_store().delete_conversation,
                conversation.conversation_id,
            )
            raise asyncio.CancelledError
        return conversation

    async def list_conversations(self) -> tuple[ConversationSummary, ...]:
        return await asyncio.to_thread(
            self._conversation_store().list_conversations,
            limit=100,
        )

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await asyncio.to_thread(
            self._conversation_store().get_conversation,
            conversation_id,
        )

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        if self._closing:
            raise RuntimeError("RunService is closing")
        operation = asyncio.create_task(
            self._delete_conversation_fenced(conversation_id),
            name=f"harness-v2-delete-{conversation_id}",
        )
        self._track_control_task(operation)
        return await asyncio.shield(operation)

    async def _delete_conversation_fenced(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        # This lock is also held while a managed terminal is committed and
        # published in memory.  Therefore an inactive Conversation cannot be
        # deleted in the gap between the SQLite terminal commit and the
        # matching RunRecord transition.  The caller shields this owned task,
        # so request cancellation cannot abandon a committed database delete
        # before the corresponding records have been retired.
        async with self._lock:
            result = await asyncio.to_thread(
                self._conversation_store().delete_conversation,
                conversation_id,
            )
            if result.deleted:
                retired: list[RunRecord] = []
                for run_id in tuple(self.runs):
                    record = self.runs[run_id]
                    if record.conversation_id != conversation_id:
                        continue
                    del self.runs[run_id]
                    record.retired = True
                    record.events.clear()
                    record.live_event_bytes = 0
                    record.durable_event_bytes = 0
                    record.user_message = None
                    record.final_assistant_content = None
                    record.context_plan = None
                    record.open_blocks.clear()
                    record.seen_blocks.clear()
                    record.pending_tools.clear()
                    record.pending_tool_arguments.clear()
                    record.started_tools.clear()
                    record.seen_tools.clear()
                    record.broker_pending_tool_calls.clear()
                    record.broker_tool_results.clear()
                    retired.append(record)
                for record in retired:
                    async with record.condition:
                        record.condition.notify_all()
        return result

    async def start(self, command: StartRunCommand) -> RunRecord:
        command.validate()
        if self._closing:
            raise RuntimeError("RunService is closing")
        if (
            self.capsule is None
            or self.journal is None
            or self.conversations is None
            or self.model_qualification is None
        ):
            raise RuntimeError("RunService is not initialized")
        if command.agent_id != self.capsule.agent_id:
            raise ValueError("unknown agent")

        request_cancelled = asyncio.Event()
        operation = asyncio.create_task(
            self._prepare_run(command, request_cancelled),
            name="harness-v2-start-admission",
        )
        self._track_control_task(operation)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The owned admission task is intentionally not cancelled.  This
            # covers checkout-local SQLite commits occurring in to_thread both
            # before and during begin_turn.  It either removes an auto-created
            # empty Conversation or hands an admitted Run to its supervisor.
            request_cancelled.set()

            def cancel_if_admitted(value: asyncio.Task[RunRecord]) -> None:
                if value.cancelled():
                    return
                try:
                    record = value.result()
                except BaseException:
                    return
                cancel_task = asyncio.create_task(
                    self.cancel(record.run_id),
                    name=f"harness-v2-abandoned-start-{record.run_id}",
                )
                self._track_control_task(cancel_task)

            operation.add_done_callback(cancel_if_admitted)
            raise

    async def _prepare_run(
        self,
        command: StartRunCommand,
        request_cancelled: asyncio.Event,
    ) -> RunRecord:
        """Prepare one Run in an owned task that survives request cancellation."""

        if (
            self.capsule is None
            or self.journal is None
            or self.conversations is None
            or self.model_qualification is None
        ):
            raise RuntimeError("RunService is not initialized")
        created_conversation = False
        try:
            if command.conversation_id is None:
                conversation = await asyncio.to_thread(
                    self.conversations.create_conversation,
                    "新会话",
                )
                conversation_id = conversation.conversation_id
                created_conversation = True
            else:
                conversation_id = command.conversation_id
            if request_cancelled.is_set() or self._closing:
                raise asyncio.CancelledError
            snapshot = await asyncio.to_thread(
                self.conversations.snapshot_for_turn,
                conversation_id,
            )
            if request_cancelled.is_set() or self._closing:
                raise asyncio.CancelledError
            context_history = tuple(
                ConversationMessage(message.message_id, message.role, message.content)
                for message in snapshot.committed_history
            )
            context_plan = self.context_compiler.compile(
                command.message,
                model_profile=self.model_qualification.model_profile,
                tools=self.effective_tools,
                agent_id=command.agent_id,
                capsule_generation=self.capsule.generation,
                history=context_history,
            )
            if request_cancelled.is_set() or self._closing:
                raise asyncio.CancelledError
        except BaseException:
            if created_conversation:
                await asyncio.to_thread(
                    self.conversations.delete_conversation,
                    conversation_id,
                )
            raise
        record = RunRecord(
            agent_id=command.agent_id,
            conversation_id=conversation_id,
            turn_id=new_id(),
            run_id=new_id(),
            user_message=command.message,
            conversation_managed=True,
            conversation_revision=snapshot.revision,
            context_plan=context_plan,
            effective_tools=self.effective_tools,
            deadline_at=asyncio.get_running_loop().time() + RUN_WALL_TIMEOUT_SECONDS,
        )
        reservation_error: ValueError | None = None
        async with self._lock:
            active = sum(
                existing.terminal_kind is None for existing in self.runs.values()
            )
            if active >= MAX_ACTIVE_RUNS:
                reservation_error = ValueError(
                    "prototype active Run capacity exhausted"
                )
            else:
                while len(self.runs) >= MAX_RETAINED_RUNS:
                    completed_id = next(
                        (
                            run_id
                            for run_id, existing in self.runs.items()
                            if existing.terminal_kind is not None
                        ),
                        None,
                    )
                    if completed_id is None:
                        reservation_error = ValueError(
                            "prototype Run capacity exhausted"
                        )
                        break
                    del self.runs[completed_id]
                if reservation_error is None:
                    self.runs[record.run_id] = record
        if reservation_error is not None:
            if created_conversation:
                await asyncio.to_thread(
                    self.conversations.delete_conversation,
                    conversation_id,
                )
            raise reservation_error
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        record.task = asyncio.create_task(
            self._admit_and_run(
                record,
                command.message,
                context_plan=context_plan,
                created_conversation=created_conversation,
                ready=ready,
                request_cancelled=request_cancelled,
            ),
            name=f"harness-v2-run-{record.run_id}",
        )
        if request_cancelled.is_set() or self._closing:
            if not record.cancel_requested:
                record.cancel_requested_at = asyncio.get_running_loop().time()
            record.cancel_requested = True
        await ready
        if request_cancelled.is_set() or self._closing:
            if not record.cancel_requested:
                record.cancel_requested_at = asyncio.get_running_loop().time()
            record.cancel_requested = True
        return record

    async def _admit_and_run(
        self,
        record: RunRecord,
        message: str,
        *,
        context_plan: ContextPlan,
        created_conversation: bool,
        ready: asyncio.Future[None],
        request_cancelled: asyncio.Event,
    ) -> None:
        """Own admission through terminal publication independently of a request."""

        try:
            assert self.journal is not None
            async with self._lock:
                protected_run_ids = tuple(
                    run_id
                    for run_id, existing in self.runs.items()
                    if existing.terminal_kind is None
                )
            await asyncio.to_thread(
                self.journal.prune_to_recent_runs,
                256 - len(protected_run_ids),
                protected_run_ids,
            )
            if self.model_qualification is None:
                raise RuntimeError("model qualification is unavailable")
            await self._publish(
                record,
                "run.started",
                "durable",
                {
                    "prototype": True,
                    "model": self.model_qualification.model,
                    "visible_tools": [
                        spec.tool_id for spec in record.effective_tools
                    ],
                    "protocol_features": ["model-call-boundaries-v1"],
                    "sandbox": SANDBOX_POLICY,
                    "context_plan": context_plan.public_metadata(),
                },
            )
        except BaseException as exc:
            async with self._lock:
                if self.runs.get(record.run_id) is record:
                    self.runs.pop(record.run_id, None)
            try:
                if self.conversations is not None:
                    await asyncio.to_thread(
                        self.conversations.finalize_noncompleted,
                        record.run_id,
                        "interrupted",
                    )
            except Exception:
                pass
            if created_conversation:
                try:
                    if self.conversations is not None:
                        await asyncio.to_thread(
                            self.conversations.delete_conversation,
                            record.conversation_id,
                        )
                except Exception:
                    pass
            if not ready.done():
                ready.set_exception(exc)
            return

        if not ready.done():
            ready.set_result(None)
        if request_cancelled.is_set() or self._closing:
            if not record.cancel_requested:
                record.cancel_requested_at = asyncio.get_running_loop().time()
            record.cancel_requested = True
        await self._run_worker(record, message)

    def get(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError("run not found") from exc

    async def resolve_run_identity(self, run_id: str) -> RunIdentity:
        """Resolve an owned Run from durable state, independent of RAM retention."""

        return await asyncio.to_thread(
            self._conversation_store().resolve_run_identity,
            run_id,
        )

    async def replay_run(
        self,
        run_id: str,
        *,
        after: int,
        limit: int,
        expected_identity: RunIdentity,
    ) -> DurableReplay:
        """Return a bounded, identity-bound durable replay or retained snapshot."""

        if self.journal is None:
            raise JournalUnavailableError("journal is unavailable")
        store = self._conversation_store()
        identity = await asyncio.to_thread(store.resolve_run_identity, run_id)
        if identity != expected_identity:
            raise KeyError("Run identity changed during replay")
        replay = await asyncio.to_thread(
            self.journal.replay,
            run_id,
            after=after,
            limit=limit,
            expected_identity=identity,
        )
        if replay is None:
            raise JournalCorruptionError(
                "owned Run has no durable replay metadata"
            )
        return replay

    async def cancel(self, run_id: str) -> None:
        try:
            record = self.get(run_id)
        except KeyError:
            return
        if record.terminal_kind is not None:
            return
        if not record.cancel_requested:
            record.cancel_requested_at = asyncio.get_running_loop().time()
        record.cancel_requested = True
        process = record.process
        if process is None or process.returncode is not None:
            return
        if not _signal_worker_group(process, record.process_marker, signal.SIGTERM):
            return
        if record.cancel_task is None or record.cancel_task.done():
            record.cancel_task = asyncio.create_task(
                self._kill_after_deadline(record, process),
                name=f"harness-v2-cancel-{record.run_id}",
            )

    async def stream(self, run_id: str, after: int = 0) -> AsyncIterator[EventEnvelope | None]:
        record = self.get(run_id)
        cursor = after
        while True:
            events, done = await record.events_after(cursor)
            if not events:
                if done:
                    return
                yield None
                continue
            for event in events:
                if record.retired:
                    return
                cursor = event.seq
                yield event
            if events[-1].kind in TERMINAL_KINDS:
                return

    @staticmethod
    def _bounded_worker_text(value: object, maximum: int = WORKER_TEXT_BYTES) -> bool:
        return (
            isinstance(value, str)
            and len(value) <= maximum
            and len(value.encode("utf-8")) <= maximum
        )

    @staticmethod
    def _worker_id(value: object) -> bool:
        return isinstance(value, str) and WORKER_ID.fullmatch(value) is not None

    @staticmethod
    def _effective_tool(record: RunRecord, tool_id: object) -> ToolSpec | None:
        if not isinstance(tool_id, str):
            return None
        return next(
            (spec for spec in record.effective_tools if spec.tool_id == tool_id),
            None,
        )

    @staticmethod
    def _model_failure_payload(record: RunRecord) -> dict[str, Any] | None:
        if record.model_failure is None:
            return None
        code, retryable = record.model_failure
        if WORKER_ID.fullmatch(code) is None or not isinstance(retryable, bool):
            raise RuntimeError("trusted model failure metadata is invalid")
        return {
            "code": code,
            "message": "The trusted model broker could not complete the request.",
            "retryable": retryable,
        }

    def _validate_worker_event(
        self,
        record: RunRecord,
        kind: str,
        durability: str,
        payload: dict[str, Any],
    ) -> None:
        expected_durability = (
            "ephemeral" if kind == "assistant.block.delta" else "durable"
        )
        if durability != expected_durability:
            raise ValueError("Worker event has invalid durability")

        keys = set(payload)
        if kind == "assistant.block.started":
            block_id = payload.get("block_id")
            if (
                keys != {"block_id", "block_type"}
                or not self._worker_id(block_id)
                or payload.get("block_type") != "content"
                or block_id in record.seen_blocks
                or record.open_blocks
            ):
                raise ValueError("invalid assistant block start")
        elif kind == "assistant.block.delta":
            block_id = payload.get("block_id")
            if (
                keys != {"block_id", "text"}
                or block_id not in record.open_blocks
                or not self._bounded_worker_text(payload.get("text"))
            ):
                raise ValueError("invalid assistant block delta")
        elif kind == "assistant.block.finished":
            block_id = payload.get("block_id")
            if (
                keys != {"block_id", "content"}
                or block_id not in record.open_blocks
                or not self._bounded_worker_text(payload.get("content"))
            ):
                raise ValueError("invalid assistant block finish")
        elif kind == "assistant.block.discarded":
            block_id = payload.get("block_id")
            if (
                keys != {"block_id", "reason"}
                or block_id not in record.open_blocks
                or payload.get("reason") not in {"cancelled", "runtime_failure"}
            ):
                raise ValueError("invalid assistant block discard")
        elif kind == "tool.call.requested":
            call_id = payload.get("call_id")
            arguments = payload.get("arguments")
            spec = self._effective_tool(record, payload.get("tool_id"))
            broker_call = record.broker_pending_tool_calls.get(str(call_id))
            try:
                validated_arguments = (
                    spec.validate_arguments(arguments) if spec is not None else None
                )
            except ValueError:
                validated_arguments = None
            if (
                keys != {"call_id", "tool_id", "arguments"}
                or not self._worker_id(call_id)
                or call_id in record.seen_tools
                or record.pending_tools
                or spec is None
                or validated_arguments is None
                or broker_call
                != (spec.tool_id, validated_arguments)
            ):
                raise ValueError("invalid tool call request")
        elif kind == "tool.call.started":
            call_id = payload.get("call_id")
            if (
                keys != {"call_id", "tool_id"}
                or call_id not in record.pending_tools
                or call_id in record.started_tools
                or payload.get("tool_id") != record.pending_tools.get(call_id)
            ):
                raise ValueError("invalid tool call start")
        elif kind == "tool.call.finished":
            call_id = payload.get("call_id")
            pending_tool_id = record.pending_tools.get(str(call_id))
            spec = self._effective_tool(record, pending_tool_id)
            try:
                validated_result = (
                    spec.validate_result(payload.get("result"))
                    if spec is not None
                    else None
                )
            except ValueError:
                validated_result = None
            allowed_keys = {"call_id", "outcome", "result"}
            if "tool_id" in payload:
                allowed_keys.add("tool_id")
            if (
                keys != allowed_keys
                or call_id not in record.started_tools
                or payload.get("outcome") not in {"succeeded", "failed", "cancelled"}
                or validated_result is None
                or (
                    "tool_id" in payload
                    and payload.get("tool_id") != record.pending_tools.get(call_id)
                )
                or (
                    spec is not None
                    and spec.tool_id == "builtin/echo"
                    and not (
                        (
                            payload.get("outcome") == "succeeded"
                            and validated_result
                            == record.pending_tool_arguments.get(
                                str(call_id), {}
                            ).get("text")
                        )
                        or (
                            payload.get("outcome") == "cancelled"
                            and record.cancel_requested
                            and validated_result == "cancelled"
                        )
                    )
                )
            ):
                raise ValueError("invalid tool call finish")
        elif kind == "run.completed":
            if (
                keys != {"reason", "model_iterations"}
                or payload.get("reason") != "end_turn"
                or not isinstance(payload.get("model_iterations"), int)
                or isinstance(payload.get("model_iterations"), bool)
                or payload["model_iterations"] != record.model_request_count
                or record.model_response_count != record.model_request_count
                or record.broker_stop_iteration != record.model_request_count
                or not 1 <= record.model_request_count <= 3
                or record.open_blocks
                or record.pending_tools
                or record.broker_pending_tool_calls
            ):
                raise ValueError("invalid completed terminal")
        elif kind == "run.failed":
            if (
                keys != {"code", "message", "retryable"}
                or not self._worker_id(payload.get("code"))
                or not self._bounded_worker_text(payload.get("message"), 512)
                or not isinstance(payload.get("retryable"), bool)
                or record.open_blocks
                or record.pending_tools
            ):
                raise ValueError("invalid failed terminal")
        elif kind == "run.cancelled":
            if (
                keys != {"reason"}
                or payload.get("reason") != "cancelled"
                or not record.cancel_requested
                or record.open_blocks
                or record.pending_tools
            ):
                raise ValueError("invalid cancelled terminal")
        else:
            raise ValueError("unknown Worker event")

    @staticmethod
    def _apply_worker_event(
        record: RunRecord, kind: str, payload: dict[str, Any]
    ) -> None:
        if kind == "assistant.block.started":
            block_id = str(payload["block_id"])
            record.open_blocks.add(block_id)
            record.seen_blocks.add(block_id)
        elif kind in {"assistant.block.finished", "assistant.block.discarded"}:
            record.open_blocks.remove(str(payload["block_id"]))
        elif kind == "tool.call.requested":
            call_id = str(payload["call_id"])
            record.pending_tools[call_id] = str(payload["tool_id"])
            record.pending_tool_arguments[call_id] = dict(payload["arguments"])
            record.seen_tools.add(call_id)
        elif kind == "tool.call.started":
            record.started_tools.add(str(payload["call_id"]))
        elif kind == "tool.call.finished":
            call_id = str(payload["call_id"])
            tool_id = record.pending_tools.get(call_id)
            if tool_id is None:
                raise RuntimeError("Tool result lost its pending call")
            record.broker_tool_results.append(
                OllamaToolResult(
                    call_id=call_id,
                    tool_id=tool_id,
                    content=str(payload["result"]),
                    outcome=str(payload["outcome"]),
                )
            )
            record.pending_tools.pop(call_id, None)
            record.pending_tool_arguments.pop(call_id, None)
            record.broker_pending_tool_calls.pop(call_id, None)
            record.started_tools.discard(call_id)

    async def _close_incomplete_worker_events(
        self, record: RunRecord, *, cancelled: bool
    ) -> None:
        reason = "cancelled" if cancelled else "worker_failure"
        for block_id in sorted(record.open_blocks):
            await self._publish(
                record,
                "assistant.block.discarded",
                "durable",
                {"block_id": block_id, "reason": reason},
                recovery=True,
            )
            record.open_blocks.discard(block_id)
        for call_id, tool_id in sorted(record.pending_tools.items()):
            if call_id not in record.started_tools:
                started_payload = {"call_id": call_id, "tool_id": tool_id}
                await self._publish(
                    record,
                    "tool.call.started",
                    "durable",
                    started_payload,
                    recovery=True,
                )
                self._apply_worker_event(
                    record, "tool.call.started", started_payload
                )
            finished_payload = {
                "call_id": call_id,
                "tool_id": tool_id,
                "outcome": "cancelled" if cancelled else "failed",
                "result": "cancelled" if cancelled else "Worker stopped",
            }
            await self._publish(
                record,
                "tool.call.finished",
                "durable",
                finished_payload,
                recovery=True,
            )
            self._apply_worker_event(
                record, "tool.call.finished", finished_payload
            )
        record.broker_pending_tool_calls.clear()

    async def _publish_memory_only(
        self,
        record: RunRecord,
        kind: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        """Converge a live stream honestly when durable storage is unavailable."""

        async with self._lock:
            return await self._publish_memory_only_locked(record, kind, payload)

    async def _publish_memory_only_locked(
        self,
        record: RunRecord,
        kind: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        """Publish while holding the Control-plane state fence."""

        if record.retired:
            raise ConversationNotFoundError("conversation was deleted")
        if record.terminal_kind is not None:
            raise RuntimeError("attempted event after terminal")
        if len(record.events) >= MAX_LIVE_EVENTS:
            raise RuntimeError("prototype live event capacity exhausted")
        envelope = EventEnvelope(
            event_id=new_id(),
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            seq=len(record.events) + 1,
            occurred_at=utc_now(),
            kind=kind,
            durability="ephemeral",
            payload=payload,
        )
        encoded_size = len(
            json.dumps(
                envelope.to_dict(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if (
            encoded_size > MAX_DURABLE_EVENT_BYTES
            or record.live_event_bytes + encoded_size > MAX_LIVE_EVENT_BYTES
        ):
            raise RuntimeError("prototype emergency event capacity exhausted")
        if (
            kind in TERMINAL_KINDS
            and record.conversation_managed
            and self.conversations is not None
        ):
            status = "cancelled" if kind == "run.cancelled" else "failed"
            try:
                await asyncio.to_thread(
                    self.conversations.finalize_noncompleted,
                    record.run_id,
                    status,
                )
            except Exception:
                LOGGER.error(
                    "Conversation turn could not be closed after durable-state failure"
                )
        record.events.append(envelope)
        record.live_event_bytes += encoded_size
        if kind in TERMINAL_KINDS:
            record.terminal_kind = kind
        async with record.condition:
            record.condition.notify_all()
        return envelope

    async def _publish_degraded_failure(self, record: RunRecord) -> None:
        if record.terminal_kind is not None:
            return
        cancelled = record.cancel_requested
        for block_id in sorted(record.open_blocks):
            await self._publish_memory_only(
                record,
                "assistant.block.discarded",
                {
                    "block_id": block_id,
                    "reason": "persistence_unavailable",
                },
            )
            record.open_blocks.discard(block_id)
        for call_id, tool_id in sorted(record.pending_tools.items()):
            if call_id not in record.started_tools:
                started_payload = {"call_id": call_id, "tool_id": tool_id}
                await self._publish_memory_only(
                    record,
                    "tool.call.started",
                    started_payload,
                )
                self._apply_worker_event(
                    record, "tool.call.started", started_payload
                )
            finished_payload = {
                "call_id": call_id,
                "tool_id": tool_id,
                "outcome": "cancelled" if cancelled else "failed",
                "result": (
                    "cancelled" if cancelled else "Persistence unavailable"
                ),
            }
            await self._publish_memory_only(
                record,
                "tool.call.finished",
                finished_payload,
            )
            self._apply_worker_event(
                record, "tool.call.finished", finished_payload
            )
        record.broker_pending_tool_calls.clear()
        await self._publish_memory_only(
            record,
            "run.failed",
            {
                "code": "journal_unavailable",
                "message": "Durable state became unavailable; this Run was stopped.",
                "retryable": False,
                "usage": dict(record.model_usage),
            },
        )

    async def _publish(
        self,
        record: RunRecord,
        kind: str,
        durability: str,
        payload: dict[str, Any],
        *,
        recovery: bool = False,
        provider_usage_start: dict[str, Any] | None = None,
        provider_usage_complete: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        # Managed terminal commits, RunRecord state, and Conversation deletion
        # share this fence.  A successful DELETE can therefore retire every
        # matching record without a post-delete terminal publication racing in.
        async with self._lock:
            return await self._publish_locked(
                record,
                kind,
                durability,
                payload,
                recovery=recovery,
                provider_usage_start=provider_usage_start,
                provider_usage_complete=provider_usage_complete,
            )

    async def _publish_locked(
        self,
        record: RunRecord,
        kind: str,
        durability: str,
        payload: dict[str, Any],
        *,
        recovery: bool = False,
        provider_usage_start: dict[str, Any] | None = None,
        provider_usage_complete: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        """Validate and publish one event under the Control-plane state fence."""

        if record.retired:
            raise ConversationNotFoundError("conversation was deleted")
        if record.terminal_kind is not None:
            raise RuntimeError("attempted event after terminal")
        if provider_usage_start is not None and provider_usage_complete is not None:
            raise RuntimeError("provider boundary cannot start and complete together")
        if record.conversation_managed and (
            (kind == "model.request.started" and provider_usage_start is None)
            or (
                kind == "model.response.finished"
                and payload.get("usage_complete") is True
                and provider_usage_complete is None
            )
            or (
                provider_usage_start is not None
                and kind != "model.request.started"
            )
            or (
                provider_usage_complete is not None
                and kind != "model.response.finished"
            )
        ):
            raise RuntimeError("provider usage boundary persistence is invalid")
        if kind in TERMINAL_KINDS:
            payload = {**payload, "usage": dict(record.model_usage)}
            # A failed/cancelled Worker may stop after receiving a broker-owned
            # Tool call but before acknowledging it.  The call never became a
            # canonical Tool event, so terminal publication must forget its
            # arguments.  Successful terminals are separately rejected while
            # any broker-owned call remains pending.
            record.broker_pending_tool_calls.clear()
        reserved_slots = 0
        if kind not in TERMINAL_KINDS:
            reserved_slots = 1 if recovery else RECOVERY_EVENT_SLOTS
        if len(record.events) >= MAX_LIVE_EVENTS - reserved_slots:
            raise RuntimeError("prototype live event capacity exhausted")
        envelope = EventEnvelope(
            event_id=new_id(),
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            seq=len(record.events) + 1,
            occurred_at=utc_now(),
            kind=kind,
            durability="durable" if durability == "durable" else "ephemeral",
            payload=payload,
        )
        encoded_size = len(
            json.dumps(
                envelope.to_dict(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if encoded_size > MAX_DURABLE_EVENT_BYTES:
            raise RuntimeError("prototype event exceeds the per-event byte limit")
        live_limit = MAX_LIVE_EVENT_BYTES
        durable_limit = MAX_DURABLE_BYTES_PER_RUN
        if kind not in TERMINAL_KINDS:
            reserve = TERMINAL_EVENT_RESERVE if recovery else RECOVERY_EVENT_RESERVE
            live_limit -= reserve
            durable_limit -= reserve
        if record.live_event_bytes + encoded_size > live_limit:
            raise RuntimeError("prototype live event byte capacity exhausted")
        if (
            durability == "durable"
            and record.durable_event_bytes + encoded_size > durable_limit
        ):
            raise RuntimeError("prototype durable event byte capacity exhausted")
        if self.journal is None:
            raise RuntimeError("journal is unavailable")
        try:
            if record.conversation_managed and kind == "run.started":
                if self.conversations is None or record.user_message is None:
                    raise RuntimeError("conversation store is unavailable")
                if record.conversation_revision is None:
                    raise RuntimeError("conversation revision is unavailable")
                await asyncio.to_thread(
                    self.conversations.begin_turn,
                    record.conversation_id,
                    turn_id=record.turn_id,
                    run_id=record.run_id,
                    user_content=record.user_message,
                    expected_revision=record.conversation_revision,
                    started_event=envelope,
                )
            elif record.conversation_managed and provider_usage_start is not None:
                if self.conversations is None:
                    raise RuntimeError("conversation store is unavailable")
                await asyncio.to_thread(
                    self.conversations.start_provider_usage_with_event,
                    record.run_id,
                    boundary_event=envelope,
                    **provider_usage_start,
                )
            elif record.conversation_managed and provider_usage_complete is not None:
                if self.conversations is None:
                    raise RuntimeError("conversation store is unavailable")
                await asyncio.to_thread(
                    self.conversations.complete_provider_usage_with_event,
                    record.run_id,
                    boundary_event=envelope,
                    **provider_usage_complete,
                )
            elif record.conversation_managed and kind in TERMINAL_KINDS:
                if self.conversations is None:
                    raise RuntimeError("conversation store is unavailable")
                if kind == "run.completed":
                    if record.final_assistant_content is None:
                        raise RuntimeError("completed Run has no trusted assistant content")
                    await asyncio.to_thread(
                        self.conversations.finalize_completed,
                        record.run_id,
                        record.final_assistant_content,
                        envelope,
                    )
                else:
                    status = "cancelled" if kind == "run.cancelled" else "failed"
                    await asyncio.to_thread(
                        self.conversations.finalize_noncompleted,
                        record.run_id,
                        status,
                        envelope,
                    )
            else:
                self.journal.append(envelope)
        except Exception:
            record.journal_failed = True
            raise
        record.events.append(envelope)
        record.live_event_bytes += encoded_size
        if durability == "durable":
            record.durable_event_bytes += encoded_size
        if kind in TERMINAL_KINDS:
            record.terminal_kind = kind
        async with record.condition:
            record.condition.notify_all()
        return envelope

    async def _publish_failure(
        self,
        record: RunRecord,
        code: str,
        *,
        forced_terminal: str | None = None,
    ) -> None:
        if record.terminal_kind is not None:
            return
        if not self._worker_id(code):
            raise ValueError("invalid failure code")
        if forced_terminal not in {None, "run.failed", "run.cancelled"}:
            raise ValueError("invalid forced terminal kind")
        terminal = forced_terminal or (
            "run.cancelled" if record.cancel_requested else "run.failed"
        )
        payload: dict[str, Any]
        if terminal == "run.cancelled":
            payload = {"reason": "cancelled"}
        else:
            model_payload = (
                self._model_failure_payload(record)
                if code
                in {"worker_crash", "worker_exit", "worker_stopped_without_terminal"}
                else None
            )
            payload = model_payload or {
                "code": code,
                "message": "The prototype Worker stopped unexpectedly.",
                "retryable": False,
            }
        await self._publish(record, terminal, "durable", payload)

    def _worker_environment(self, run_root: Path, capsule: AgentCapsule) -> dict[str, str]:
        environment = {
            "PATH": str(capsule.interpreter.parent),
            "HOME": str(run_root / "home"),
            "TMPDIR": str(run_root / "tmp"),
            "TMP": str(run_root / "tmp"),
            "TEMP": str(run_root / "tmp"),
            "XDG_CACHE_HOME": str(run_root / "xdg" / "cache"),
            "XDG_CONFIG_HOME": str(run_root / "xdg" / "config"),
            "XDG_DATA_HOME": str(run_root / "xdg" / "data"),
            "XDG_STATE_HOME": str(run_root / "xdg" / "state"),
            "PYTHONPATH": str(self.source_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "HARNESS_V2_RUN_ROOT": str(run_root),
            "HARNESS_V2_ENVIRONMENT_ROOT": str(capsule.interpreter.parent.parent),
            "HARNESS_V2_SOURCE_ROOT": str(self.source_root),
        }
        environment.update(self._worker_qualification_environment)
        return environment

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Drain without retaining data so a noisy Worker cannot deadlock on stderr."""

        while True:
            chunk = await stream.read(4_096)
            if not chunk:
                return

    async def _watch_run_quota(
        self,
        record: RunRecord,
        run_root: Path,
        process: asyncio.subprocess.Process,
        process_marker: str,
    ) -> None:
        while process.returncode is None:
            try:
                await asyncio.to_thread(_measure_run_tree, run_root)
            except Exception:
                record.resource_failure = "worker_disk_quota"
                _signal_worker_group(process, process_marker, signal.SIGKILL)
                return
            await asyncio.sleep(RUN_QUOTA_INTERVAL_SECONDS)

    @staticmethod
    async def _write_model_response(
        stream: asyncio.StreamWriter,
        request_id: str,
        frame_type: str,
        **payload: object,
    ) -> None:
        value = {
            "internal": "model.response",
            "version": BROKER_PROTOCOL_VERSION,
            "request_id": request_id,
            "type": frame_type,
            **payload,
        }
        encoded = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        if len(encoded) > MAX_BROKER_FRAME_BYTES:
            raise RuntimeError("model response IPC frame exceeded its limit")
        stream.write(encoded)
        await stream.drain()

    async def _serve_model_request(
        self,
        *,
        record: RunRecord,
        stream: asyncio.StreamWriter,
        session: OllamaRunSession,
        user_message: str,
        value: dict[str, Any],
        expected_iteration: int,
    ) -> None:
        expected_keys = {
            "internal",
            "version",
            "request_id",
            "iteration",
            "context_plan",
            "tool_result_call_ids",
        }
        request_id = value.get("request_id")
        result_call_ids = value.get("tool_result_call_ids")
        trusted_result_call_ids = [
            item.call_id for item in record.broker_tool_results
        ]
        context_plan = record.context_plan
        if (
            set(value) != expected_keys
            or value.get("internal") != "model.request"
            or value.get("version") != BROKER_PROTOCOL_VERSION
            or value.get("iteration") != expected_iteration
            or request_id != f"model-{expected_iteration}"
            or context_plan is None
            or value.get("context_plan") != context_plan.reference.to_dict()
            or not isinstance(result_call_ids, list)
            or result_call_ids != trusted_result_call_ids
        ):
            raise ValueError("Worker model request is invalid")
        assert isinstance(request_id, str)
        if self.conversations is None:
            raise RuntimeError("conversation store is unavailable")
        profile_payload = json.dumps(
            context_plan.model_profile.canonical_manifest(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        profile_digest = hashlib.sha256(
            b"agent-builder-model-profile-v1\0" + profile_payload
        ).hexdigest()

        request_started = False
        response_finished = False

        async def observe_request(metadata: OllamaRequestMetadata) -> None:
            nonlocal request_started
            if (
                not isinstance(metadata, OllamaRequestMetadata)
                or request_started
                or metadata.iteration != expected_iteration
                or not 1 <= metadata.message_count <= 256
                or not 0 <= metadata.tool_count <= len(record.effective_tools)
                or not 1
                <= metadata.estimated_input_tokens
                <= context_plan.policy.hard_input_tokens
                or not 1
                <= metadata.request_bytes
                <= context_plan.model_profile.request_byte_budget
                or re.fullmatch(r"[a-f0-9]{64}", metadata.request_digest) is None
                or (
                    expected_iteration == 1
                    and metadata.tool_count != len(context_plan.tools)
                )
                or (expected_iteration > 1 and metadata.tool_count != 0)
            ):
                raise RuntimeError("trusted model request metadata is invalid")
            await self._publish(
                record,
                "model.request.started",
                "durable",
                {
                    "request_id": request_id,
                    "iteration": expected_iteration,
                    "context_plan_id": context_plan.reference.plan_id,
                    "context_plan_digest": context_plan.reference.digest,
                    "request_digest": metadata.request_digest,
                    "request_bytes": metadata.request_bytes,
                    "estimated_input_tokens": metadata.estimated_input_tokens,
                    "message_count": metadata.message_count,
                    "tool_count": metadata.tool_count,
                    "tool_result_call_ids": list(trusted_result_call_ids),
                },
                provider_usage_start={
                    "call_index": expected_iteration,
                    "provider": context_plan.model_profile.provider,
                    "model": context_plan.model_profile.model,
                    "profile_digest": profile_digest,
                    "context_plan_id": context_plan.reference.plan_id,
                    "estimated_input_tokens": metadata.estimated_input_tokens,
                    "hard_input_tokens": context_plan.policy.hard_input_tokens,
                },
            )
            request_started = True
            record.model_request_count = expected_iteration
            record.broker_stop_iteration = None
            record.model_failure = None
            record.model_usage["complete"] = False

        async def finish_response(
            outcome: str,
            *,
            input_tokens: int,
            output_tokens: int,
            usage_complete: bool,
            error_code: str | None,
        ) -> None:
            nonlocal response_finished
            if not request_started or response_finished:
                raise RuntimeError("model response boundary is out of sequence")
            await self._publish(
                record,
                "model.response.finished",
                "durable",
                {
                    "request_id": request_id,
                    "iteration": expected_iteration,
                    "outcome": outcome,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "usage_complete": usage_complete,
                    "error_code": error_code,
                },
                provider_usage_complete=(
                    {
                        "call_index": expected_iteration,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                    if usage_complete
                    else None
                ),
            )
            response_finished = True
            record.model_response_count = expected_iteration

        saw_terminal_frame = False
        trusted_content: list[str] = []
        model_frames: AsyncIterator[Any] | None = None
        try:
            model_frames = session.stream_turn(
                user_message,
                tuple(record.broker_tool_results),
                lambda: record.cancel_requested,
                observe_request,
            )
            async for frame in model_frames:
                if frame.kind == "content":
                    if set(frame.payload) != {"text"}:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Invalid normalized content frame."
                        )
                    await self._write_model_response(
                        stream,
                        request_id,
                        "content",
                        text=frame.payload["text"],
                    )
                    trusted_content.append(str(frame.payload["text"]))
                elif frame.kind == "tool.use":
                    if set(frame.payload) != {
                        "call_id",
                        "tool_id",
                        "arguments",
                        "usage",
                    }:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Invalid normalized Tool frame."
                        )
                    call_id = frame.payload.get("call_id")
                    tool_id = frame.payload.get("tool_id")
                    spec = self._effective_tool(record, tool_id)
                    try:
                        arguments = (
                            spec.validate_arguments(frame.payload.get("arguments"))
                            if spec is not None
                            else None
                        )
                    except ValueError as exc:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Invalid normalized Tool call."
                        ) from exc
                    if (
                        not self._worker_id(call_id)
                        or spec is None
                        or arguments is None
                        or call_id in record.seen_tools
                        or call_id in record.broker_pending_tool_calls
                        or record.pending_tools
                    ):
                        raise OllamaBrokerError(
                            "model_protocol_error", "Invalid normalized Tool call."
                        )
                    prompt_tokens, output_tokens = self._validated_model_usage(
                        record, frame.payload["usage"]
                    )
                    await finish_response(
                        "tool_use",
                        input_tokens=prompt_tokens,
                        output_tokens=output_tokens,
                        usage_complete=True,
                        error_code=None,
                    )
                    self._apply_validated_model_usage(
                        record, prompt_tokens, output_tokens
                    )
                    assert isinstance(call_id, str)
                    record.broker_pending_tool_calls[call_id] = (
                        spec.tool_id,
                        arguments,
                    )
                    saw_terminal_frame = True
                    await self._write_model_response(
                        stream,
                        request_id,
                        "tool.use",
                        call_id=call_id,
                        tool_id=spec.tool_id,
                        arguments=arguments,
                    )
                elif frame.kind == "stop":
                    if set(frame.payload) != {"reason", "usage"}:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Invalid normalized stop frame."
                        )
                    prompt_tokens, output_tokens = self._validated_model_usage(
                        record, frame.payload["usage"]
                    )
                    await finish_response(
                        "end_turn",
                        input_tokens=prompt_tokens,
                        output_tokens=output_tokens,
                        usage_complete=True,
                        error_code=None,
                    )
                    self._apply_validated_model_usage(
                        record, prompt_tokens, output_tokens
                    )
                    record.broker_stop_iteration = expected_iteration
                    record.final_assistant_content = "".join(trusted_content)
                    saw_terminal_frame = True
                    await self._write_model_response(
                        stream,
                        request_id,
                        "stop",
                        reason=frame.payload["reason"],
                    )
                else:
                    raise OllamaBrokerError(
                        "model_protocol_error", "Unknown normalized model frame."
                    )
            if not saw_terminal_frame:
                raise OllamaBrokerError(
                    "model_protocol_error", "Model stream had no terminal frame."
                )
        except (OllamaCancelledError, OllamaBrokerError) as exc:
            record.model_failure = (exc.code, exc.retryable)
            if request_started and not response_finished:
                await finish_response(
                    "cancelled"
                    if isinstance(exc, OllamaCancelledError)
                    else "error",
                    input_tokens=0,
                    output_tokens=0,
                    usage_complete=False,
                    error_code=exc.code,
                )
            await self._write_model_response(
                stream,
                request_id,
                "error",
                code=exc.code,
            )
        finally:
            if model_frames is not None:
                await model_frames.aclose()

    @staticmethod
    def _validated_model_usage(
        record: RunRecord, value: object
    ) -> tuple[int, int]:
        if not isinstance(value, dict) or set(value) != {
            "prompt_eval_count",
            "eval_count",
        }:
            raise OllamaBrokerError(
                "model_protocol_error", "Invalid normalized model usage."
            )
        prompt_tokens = value.get("prompt_eval_count", 0)
        output_tokens = value.get("eval_count", 0)
        context_plan = record.context_plan
        if any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or not 0 <= item <= 1_000_000_000
            for item in (prompt_tokens, output_tokens)
        ) or context_plan is None:
            raise OllamaBrokerError(
                "model_protocol_error", "Invalid normalized model usage."
            )
        if (
            prompt_tokens > context_plan.policy.hard_input_tokens
            or output_tokens > context_plan.model_profile.max_output_tokens
            or prompt_tokens + output_tokens
            > context_plan.model_profile.operational_context_tokens
        ):
            raise OllamaBrokerError(
                "model_protocol_error", "Model usage exceeded its qualified profile."
            )
        current_input = record.model_usage["input_tokens"]
        current_output = record.model_usage["output_tokens"]
        if (
            not isinstance(current_input, int)
            or isinstance(current_input, bool)
            or not isinstance(current_output, int)
            or isinstance(current_output, bool)
        ):
            raise OllamaBrokerError(
                "model_protocol_error", "Invalid accumulated model usage."
            )
        input_total = current_input + prompt_tokens
        output_total = current_output + output_tokens
        if input_total > 1_000_000_000 or output_total > 1_000_000_000:
            raise OllamaBrokerError(
                "model_protocol_error", "Model usage exceeded its bound."
            )
        return prompt_tokens, output_tokens

    @staticmethod
    def _apply_validated_model_usage(
        record: RunRecord, prompt_tokens: int, output_tokens: int
    ) -> None:
        current_input = record.model_usage["input_tokens"]
        current_output = record.model_usage["output_tokens"]
        if (
            not isinstance(current_input, int)
            or isinstance(current_input, bool)
            or not isinstance(current_output, int)
            or isinstance(current_output, bool)
        ):
            raise OllamaBrokerError(
                "model_protocol_error", "Invalid accumulated model usage."
            )
        input_total = current_input + prompt_tokens
        output_total = current_output + output_tokens
        if input_total > 1_000_000_000 or output_total > 1_000_000_000:
            raise OllamaBrokerError(
                "model_protocol_error", "Model usage exceeded its bound."
            )
        record.model_usage["input_tokens"] = input_total
        record.model_usage["output_tokens"] = output_total
        record.model_usage["last_input_tokens"] = prompt_tokens
        record.model_usage["complete"] = True

    @classmethod
    def _apply_model_usage(cls, record: RunRecord, value: object) -> None:
        """Validate and apply one usage frame (kept for focused unit tests)."""

        prompt_tokens, output_tokens = cls._validated_model_usage(record, value)
        cls._apply_validated_model_usage(record, prompt_tokens, output_tokens)

    async def _run_worker(self, record: RunRecord, message: str) -> None:
        assert self.capsule is not None
        run_root: Path | None = None
        process: asyncio.subprocess.Process | None = None
        process_marker: str | None = None
        stderr_task: asyncio.Task[None] | None = None
        quota_task: asyncio.Task[None] | None = None
        pending_terminal: tuple[str, str, dict[str, Any]] | None = None
        failure_code: str | None = None
        deadline_expired = False
        model_requests = 0
        diagnostic_stage = "run_root"
        loop = asyncio.get_running_loop()
        deadline_at = record.deadline_at
        if deadline_at is None:
            deadline_at = loop.time() + RUN_WALL_TIMEOUT_SECONDS
            record.deadline_at = deadline_at
        try:
            if record.cancel_requested:
                failure_code = "cancelled_before_launch"
            else:
                run_root = self.capsule.runtime_root / "runs" / record.run_id
                created_root = await asyncio.to_thread(
                    self.capsules.create_run_root, self.capsule, record.run_id
                )
                if created_root != run_root:
                    raise RuntimeError("Capsule returned an unexpected Run root")
                if loop.time() >= deadline_at:
                    raise TimeoutError

                async with asyncio.timeout_at(deadline_at):
                    diagnostic_stage = "process_launch"
                    process = await asyncio.create_subprocess_exec(
                        str(self.capsule.interpreter),
                        "-m",
                        "agent_builder_v2.worker",
                        cwd=run_root / "work",
                        env=self._worker_environment(run_root, self.capsule),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        limit=MAX_WORKER_EVENT_BYTES + 1,
                    )
                    diagnostic_stage = "pid_record"
                    process_marker = _process_marker(process.pid)
                    _write_worker_pid_record(
                        path=run_root / "worker.pid",
                        repository_root=self.repository_root,
                        run_root=run_root,
                        capsule=self.capsule,
                        run_id=record.run_id,
                        pid=process.pid,
                        marker=process_marker,
                    )
                    record.process_marker = process_marker
                    record.process = process
                    quota_task = asyncio.create_task(
                        self._watch_run_quota(
                            record,
                            run_root,
                            process,
                            process_marker,
                        ),
                        name=f"harness-v2-quota-{record.run_id}",
                    )
                    if record.cancel_requested:
                        _signal_worker_group(process, process_marker, signal.SIGTERM)
                    assert process.stdout is not None
                    assert process.stderr is not None
                    assert process.stdin is not None
                    stderr_task = asyncio.create_task(
                        self._drain_stderr(process.stderr)
                    )
                    diagnostic_stage = "sandbox_handshake_read"
                    try:
                        sandbox_line = await process.stdout.readline()
                    except (asyncio.LimitOverrunError, ValueError) as exc:
                        raise ValueError("Worker sandbox handshake was too large") from exc
                    if (
                        not sandbox_line
                        or len(sandbox_line) > MAX_WORKER_EVENT_BYTES
                        or not sandbox_line.endswith(b"\n")
                    ):
                        raise ValueError("Worker sandbox handshake is missing")
                    diagnostic_stage = "sandbox_handshake_validate"
                    try:
                        sandbox_ready = json.loads(sandbox_line)
                        _validate_sandbox_ready(process.pid, sandbox_ready)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                        raise ValueError("Worker sandbox handshake is invalid") from exc
                    diagnostic_stage = "model_session"
                    if record.context_plan is None:
                        raise RuntimeError("Run lost its trusted context plan")
                    model_session = self.model_broker.new_run(record.context_plan)
                    diagnostic_stage = "command_write"
                    process.stdin.write(
                        json.dumps(
                            {
                                "message": message,
                                "context_plan": record.context_plan.reference.to_dict(),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    await process.stdin.drain()
                    diagnostic_stage = "worker_stream"
                    while True:
                        try:
                            line = await process.stdout.readline()
                        except (asyncio.LimitOverrunError, ValueError):
                            failure_code = "worker_event_too_large"
                            _signal_worker_group(
                                process, process_marker, signal.SIGKILL
                            )
                            break
                        if not line:
                            break
                        if len(line) > MAX_WORKER_EVENT_BYTES:
                            failure_code = "worker_event_too_large"
                            _signal_worker_group(
                                process, process_marker, signal.SIGKILL
                            )
                            break
                        try:
                            raw = json.loads(line)
                            if not isinstance(raw, dict):
                                raise ValueError("Worker frame must be an object")
                            if raw.get("internal") == "model.request":
                                model_requests += 1
                                if model_requests > 3:
                                    raise ValueError("Worker model request limit exceeded")
                                await self._serve_model_request(
                                    record=record,
                                    stream=process.stdin,
                                    session=model_session,
                                    user_message=message,
                                    value=raw,
                                    expected_iteration=model_requests,
                                )
                                continue
                            if "internal" in raw:
                                raise ValueError("Worker sent an unknown internal frame")
                            kind = raw["kind"]
                            durability = raw["durability"]
                            payload = raw["payload"]
                            if (
                                kind not in WORKER_EVENT_KINDS
                                or durability not in {"durable", "ephemeral"}
                                or not isinstance(payload, dict)
                            ):
                                raise ValueError("invalid Worker event")
                            self._validate_worker_event(
                                record, kind, durability, payload
                            )
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            failure_code = "invalid_worker_event"
                            _signal_worker_group(
                                process, process_marker, signal.SIGKILL
                            )
                            break
                        if kind == "run.failed":
                            payload = self._model_failure_payload(record) or payload
                        if kind in TERMINAL_KINDS:
                            pending_terminal = (kind, durability, payload)
                            break
                        await self._publish(record, kind, durability, payload)
                        self._apply_worker_event(record, kind, payload)
                    if not process.stdin.is_closing():
                        process.stdin.close()
                        try:
                            await process.stdin.wait_closed()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    try:
                        return_code = await asyncio.wait_for(
                            process.wait(), timeout=CANCEL_GRACE_SECONDS
                        )
                    except TimeoutError:
                        _signal_worker_group(process, process_marker, signal.SIGKILL)
                        return_code = await process.wait()
                        failure_code = failure_code or "worker_exit_timeout"
                    await stderr_task
                    if return_code != 0:
                        failure_code = (
                            failure_code
                            or record.resource_failure
                            or "worker_crash"
                        )
                    elif pending_terminal is None:
                        failure_code = failure_code or "worker_exit"
                    if record.cancel_requested and pending_terminal is not None:
                        failure_code = failure_code or "cancelled"
                        pending_terminal = None
        except TimeoutError:
            cancelled_before_deadline = (
                record.cancel_requested_at is not None
                and record.cancel_requested_at < deadline_at
            )
            deadline_expired = not cancelled_before_deadline
            failure_code = (
                "worker_deadline_exceeded" if deadline_expired else "cancel_deadline"
            )
            pending_terminal = None
            if process is not None and process.returncode is None:
                if process_marker is not None:
                    _signal_worker_group(process, process_marker, signal.SIGKILL)
                else:
                    process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=CANCEL_GRACE_SECONDS,
                    )
                except TimeoutError:
                    failure_code = "worker_reap_timeout"
        except Exception as exc:
            # Do not log the exception message: later Worker implementations may
            # process secrets or attacker-controlled text.  A fixed stage and
            # exception class are enough to diagnose containment failures.
            LOGGER.error(
                "Harness V2 Worker failed at stage=%s exception=%s",
                diagnostic_stage,
                type(exc).__name__,
            )
            failure_code = failure_code or "worker_launch_failure"
            if process is not None and process.returncode is None:
                if process_marker is not None:
                    _signal_worker_group(process, process_marker, signal.SIGKILL)
                else:
                    process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=CANCEL_GRACE_SECONDS,
                    )
                except TimeoutError:
                    failure_code = "worker_reap_timeout"
        finally:
            if process is not None and process.stdin is not None:
                if not process.stdin.is_closing():
                    process.stdin.close()
            if quota_task is not None and not quota_task.done():
                quota_task.cancel()
                await asyncio.gather(quota_task, return_exceptions=True)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            record.process = None
            record.process_marker = None
            if run_root is not None:
                if process is None or process.returncode is not None:
                    try:
                        residual_members = await asyncio.to_thread(
                            _process_group_members,
                            process.pid if process is not None else -1,
                        )
                        if residual_members:
                            raise RuntimeError("Worker process group still has members")
                        await asyncio.to_thread(
                            self.capsules.remove_run_root,
                            self.capsule,
                            record.run_id,
                        )
                    except Exception:
                        failure_code = failure_code or "worker_cleanup_failure"
                        pending_terminal = None
                else:
                    failure_code = failure_code or "worker_reap_timeout"
                    pending_terminal = None
        if record.resource_failure is not None:
            failure_code = record.resource_failure
            pending_terminal = None
        if pending_terminal is not None and failure_code is None:
            try:
                terminal_kind, terminal_durability, terminal_payload = pending_terminal
                await self._publish(
                    record,
                    terminal_kind,
                    terminal_durability,
                    terminal_payload,
                )
                return
            except Exception:
                pending_terminal = None
                failure_code = "invalid_worker_terminal"

        if record.journal_failed:
            await self._publish_degraded_failure(record)
            return

        if record.terminal_kind is None:
            forced_terminal = "run.failed" if deadline_expired else None
            cancelled = forced_terminal is None and record.cancel_requested
            try:
                await self._close_incomplete_worker_events(
                    record,
                    cancelled=cancelled,
                )
                await self._publish_failure(
                    record,
                    failure_code or "worker_stopped_without_terminal",
                    forced_terminal=forced_terminal,
                )
            except Exception:
                if not record.journal_failed:
                    raise
                await self._publish_degraded_failure(record)

    async def _kill_after_deadline(
        self, record: RunRecord, process: asyncio.subprocess.Process
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(process.wait()), timeout=CANCEL_GRACE_SECONDS
            )
        except TimeoutError:
            if process.returncode is None:
                if not _signal_worker_group(
                    process, record.process_marker, signal.SIGKILL
                ):
                    return
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=CANCEL_GRACE_SECONDS,
                    )
                except TimeoutError:
                    return
