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
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .capsule import AgentCapsule, CapsuleManager, PROTOTYPE_AGENT_ID, SAFE_ID
from .context import ConversationMessage, ContextCompiler, ContextPlan, ContextPlanError
from .context_projection import ContextProjectionBoundary
from .context_counts import (
    CountScope,
    SoftContextCalibration,
    SoftContextCalibrationRegistry,
)
from .completed_context import CompletedContextItem, CompletedTurnContext
from .semantic_summary_v2 import (
    SUMMARY_V2_POLICY_DIGEST,
    SUMMARY_V2_PROMPT_DIGEST,
    SemanticSummaryV2Snapshot,
    completed_bundle_digest,
    summary_v2_source_digest,
)
from .contracts import (
    LoopLimits,
    RUN_CURSOR_RESERVED_THROUGH,
    TERMINAL_KINDS,
    EventEnvelope,
    StartRunCommand,
    new_id,
    utc_now,
)
from .runtime import TurnRuntimeSnapshot
from .model import BROKER_PROTOCOL_VERSION, MAX_BROKER_FRAME_BYTES
from .ollama import (
    OllamaBroker,
    OllamaBrokerError,
    OllamaCancelledError,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaRunSession,
    OllamaToolResult,
    OllamaTransportAttempt,
)
from .file_read import FileReadExecutor
from .file_search import FileSearchExecutor
from .file_write import FileMutationExecutor, FileWriteError, FullReadReceipt
from .extensions import ExtensionCatalog, ExtensionError, ExtensionExecutor
from .command_exec import CommandExecutionError, CommandExecutor
from .research import (
    ResearchDocumentExecutor,
    ResearchEnvironmentError,
    ResearchEnvironmentManager,
)
from .permissions import (
    CapabilityBroker,
    CapabilityPolicy,
    CapabilityRequest,
)
from .replay import DurableReplay, RunIdentity
from .sandbox import HostQualification, require_qualified_host
from .sessions import (
    CapabilityAuditEvent,
    Conversation,
    ConversationDeleteResult,
    ConversationPage,
    ConversationNotFoundError,
    ConversationConflictError,
    ConversationStore,
    ConversationStoreUnavailableError,
    ConversationSummary,
    MAX_USER_CONTENT_BYTES,
    MAX_TURNS_PER_CONVERSATION,
    PermissionRecord,
    SummaryProjectionRecord,
)
from .state import (
    EventJournal,
    JournalCorruptionError,
    JournalUnavailableError,
)
from .sync_counter import qualification_environment
from .tasks import (
    BackgroundTaskManager,
    TaskNotification,
    TaskParentIdentity,
    TaskRecord,
    TaskStore,
)
from .subagents import (
    MailboxMessage,
    SubagentCoordinator,
    SubagentError,
    SubagentLink,
    SubagentStore,
)
from .skills import SkillError, SkillExecutor, SkillRecord, SkillRegistry
from .tools import (
    EffectiveToolSet,
    ToolPolicy,
    ToolSpec,
    project_tool_result,
    runtime_tool_catalog,
    runtime_tool_policy,
    toolset_digest,
)
from .workspace_context import PromptSourceSnapshot, collect_prompt_sources


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
SUMMARY_TRANSIENT_NEGATIVE_TTL_SECONDS = 60
DURABLE_CALIBRATION_SAMPLE_LIMIT = 16
PREPARATION_STAGES = (
    "reading_history",
    "building_context",
    "evaluating_compaction",
    "summarizing_history",
    "verifying_continuation",
    "admitting_run",
)

# The release model is intentionally small.  Sending the complete execution
# ToolSet on every conversational turn makes it both slower and more likely to
# invent a Tool call.  This fixed, inspectable router activates execution
# capabilities only when the current authenticated user message contains a
# concrete workspace/action signal.  It is not an LLM classifier and grants no
# authority: every selected Tool remains frozen in the TurnRuntimeSnapshot and
# passes the normal capability broker.
_TURN_TOOL_DIRECT_INTENT = re.compile(
    r"(?ix)(?:"
    r"https?://|(?:^|\s)(?:\.{0,2}/)[^\s]+|"
    r"\b[A-Za-z0-9_-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|"
    r"pdf|docx?|xlsx?|csv|html?|css|sh|sql)\b|"
    r"\b(?:browse\s+(?:the\s+)?(?:web|internet)|web\s+search|"
    r"search\s+(?:the\s+)?(?:web|internet)|deep\s+research|"
    r"delegate\s+(?:this|the\s+task)|subagent)\b|"
    r"浏览(?:网页|互联网)|联网搜索|深度研究|委派(?:任务|给)|子智能体"
    r")"
)
_TURN_TOOL_ACTION = re.compile(
    r"(?ix)(?:"
    r"\b(?:read|open|list|find|search|inspect|check|edit|modify|rename|"
    r"delete|update|fix|implement|run|execute|install|compile|build|deploy|test)\b|"
    r"读取|打开|列出|查找|搜索|检查|查看|编辑|修改|重命名|删除|更新|修复|"
    r"实现|运行|执行|安装|编译|构建|部署|测试"
    r")"
)
_TURN_TOOL_OBJECT = re.compile(
    r"(?ix)(?:"
    r"\b(?:files?|folders?|director(?:y|ies)|workspace|repository|repo|"
    r"codebase|source\s+code|frontend|backend|database|terminal|shell|"
    r"commands?|scripts?|packages?|dependenc(?:y|ies)|pdf|docx?|documents?|"
    r"spreadsheets?|websites?|webpages?|skills?|extensions?|mcp|tests?|bugs?)\b|"
    r"文件|目录|文件夹|工作区|仓库|代码库|源代码|前端|后端|数据库|终端|命令|"
    r"脚本|依赖|安装包|文档|网页|网站|技能|扩展|测试|缺陷"
    r")"
)
_TURN_TOOL_REQUEST = re.compile(
    r"(?ix)(?:"
    r"(?:^|\b(?:please|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:need|want)\s+you\s+to)\s+)(?:(?:use|call)\s+"
    r"(?:the\s+)?(?:available\s+)?tools?|fix|implement|run|execute|"
    r"install|compile|build|deploy|test|research)\b|"
    r"^\s*(?:请|帮我|请帮我)?(?:使用工具|调用工具|修复|实现|运行|执行|安装|"
    r"编译|构建|部署|测试|研究)"
    r")"
)
_TURN_NO_TOOLS = re.compile(
    r"(?i)(?:\b(?:(?:do\s+not|don't)\s+use|without\s+using)\s+"
    r"(?:any\s+)?tools?\b|"
    r"\bno[- ]tools?\b|不要使用工具|不使用工具|无需工具)"
)
_DELEGATION_INTENT = re.compile(
    r"(?i)(?:\b(?:delegate|subagent|ask\s+(?:the\s+)?agent)\b|委派|子智能体|"
    r"让.{0,16}(?:智能体|agent))"
)
_AGENT_ID_IN_TEXT = re.compile(r"(?<![a-f0-9-])([a-f0-9-]{32,36})(?![a-f0-9-])")


def _turn_needs_tools(message: str) -> bool:
    """Return the deterministic conversational/execution routing decision."""

    if _TURN_NO_TOOLS.search(message) is not None:
        return False
    return bool(
        _TURN_TOOL_DIRECT_INTENT.search(message)
        or _TURN_TOOL_REQUEST.search(message)
        or (
            _TURN_TOOL_ACTION.search(message)
            and _TURN_TOOL_OBJECT.search(message)
        )
    )


def _summary_negative_is_reusable(
    record: SummaryProjectionRecord,
    *, now: datetime | None = None,
) -> bool:
    """Keep deterministic source limits, but expire transient negative results."""

    if record.status == "source_limit":
        return True
    if record.status not in {"timeout", "invalid", "circuit_open"}:
        return False
    try:
        updated = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        return False
    current = now or datetime.now(timezone.utc)
    age = (current - updated.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < SUMMARY_TRANSIENT_NEGATIVE_TTL_SECONDS


def _soft_calibration_from_samples(
    scope: CountScope,
    samples: tuple[tuple[int, int], ...],
) -> SoftContextCalibration | None:
    """Rebuild the exact bounded calibrator from durable ordered samples."""

    registry = SoftContextCalibrationRegistry(
        maximum_scopes=1,
        samples_per_scope=DURABLE_CALIBRATION_SAMPLE_LIMIT,
    )
    for admission_upper_bound_tokens, actual_input_tokens in samples:
        registry.observe(
            scope,
            admission_upper_bound_tokens=admission_upper_bound_tokens,
            actual_input_tokens=actual_input_tokens,
        )
    return registry.calibration_for(scope)


MAX_DURABLE_BYTES_PER_RUN = 256 * 1024
TERMINAL_EVENT_RESERVE = 8 * 1024
RECOVERY_EVENT_RESERVE = 32 * 1024
# Worst case after one accepted nonterminal event: discard one open assistant
# block, start and finish one requested-only Tool, then publish the terminal.
RECOVERY_EVENT_SLOTS = 4
MAX_DURABLE_EVENT_BYTES = 65_536
RUN_WALL_TIMEOUT_SECONDS = 240
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
    runtime_snapshot: TurnRuntimeSnapshot | None = None
    user_message: str | None = field(default=None, repr=False)
    conversation_managed: bool = False
    conversation_revision: int | None = None
    preparation_operation_id: str | None = None
    turn_position: int | None = None
    summary_status: str = "disabled"
    context_plan: ContextPlan | None = None
    active_context_plan: ContextPlan | None = None
    active_runtime_snapshot: TurnRuntimeSnapshot | None = None
    recovery_context_plan: ContextPlan | None = None
    recovery_history: tuple[ConversationMessage, ...] = field(default=(), repr=False)
    recovery_prompt_sources: PromptSourceSnapshot | None = field(default=None, repr=False)
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
    completed_tool_calls: list[
        tuple[str, str, str, dict[str, str | int | bool]]
    ] = field(default_factory=list, repr=False)
    brokered_capability_calls: set[str] = field(default_factory=set)
    brokered_capability_results: dict[str, tuple[str, str]] = field(
        default_factory=dict, repr=False
    )
    file_read_receipts: dict[str, FullReadReceipt] = field(
        default_factory=dict, repr=False
    )
    model_request_count: int = 0
    model_response_count: int = 0
    provider_call_count: int = 0
    overflow_recovery_count: int = 0
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


@dataclass
class PreparationStatus:
    """Bounded, prompt-free progress for one pre-Run admission operation."""

    operation_id: str
    conversation_id: str
    stage: str
    started_at: float
    cancel_requested: asyncio.Event

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or SAFE_ID.fullmatch(self.operation_id) is None
            or not isinstance(self.conversation_id, str)
            or SAFE_ID.fullmatch(self.conversation_id) is None
            or self.stage not in PREPARATION_STAGES
            or not isinstance(self.started_at, float)
            or self.started_at < 0
            or not isinstance(self.cancel_requested, asyncio.Event)
        ):
            raise ValueError("invalid Run preparation status")


class RunService:
    def __init__(
        self,
        repository_root: Path,
        source_root: Path,
        *,
        agent_id: str = PROTOTYPE_AGENT_ID,
        model_broker: OllamaBroker | None = None,
        manage_model_broker: bool | None = None,
        context_compiler: ContextCompiler | None = None,
        extension_catalog: ExtensionCatalog | None = None,
        subagent_coordinator: SubagentCoordinator | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.source_root = source_root.resolve(strict=True)
        self._worker_qualification_environment = qualification_environment(
            self.repository_root,
            os.environ,
            expected_role="gateway",
            child_role="worker",
        )
        if SAFE_ID.fullmatch(agent_id) is None:
            raise ValueError("invalid RunService Agent identity")
        self.agent_id = agent_id
        self.capsules = CapsuleManager(self.repository_root)
        self.capsule: AgentCapsule | None = None
        self.journal: EventJournal | None = None
        self.conversations: ConversationStore | None = None
        self.model_broker = model_broker or OllamaBroker()
        self._manage_model_broker = (
            model_broker is None
            if manage_model_broker is None
            else manage_model_broker
        )
        self.context_compiler = context_compiler or ContextCompiler()
        self.extension_catalog = extension_catalog or ExtensionCatalog.empty()
        self.subagent_coordinator = subagent_coordinator
        self.tool_catalog = runtime_tool_catalog()
        self.tool_policy = runtime_tool_policy()
        unavailable_optional = {"skill/run", "document/extract_text"}
        if not self.extension_catalog.public_metadata():
            unavailable_optional.add("extension/call")
        if unavailable_optional:
            self.tool_catalog = type(self.tool_catalog).create(
                tuple(
                    spec
                    for spec in self.tool_catalog.specs
                    if spec.tool_id not in unavailable_optional
                )
            )
            self.tool_policy = ToolPolicy(
                revision=self.tool_policy.revision,
                allowed_tool_ids=tuple(
                    tool_id
                    for tool_id in self.tool_policy.allowed_tool_ids
                    if tool_id not in unavailable_optional
                ),
                denied_tool_ids=self.tool_policy.denied_tool_ids,
                allowed_risks=self.tool_policy.allowed_risks,
            )
        self.effective_toolset = EffectiveToolSet.resolve(
            self.tool_catalog, self.tool_policy
        )
        self.effective_tools = self.effective_toolset.specs
        self.capability_policy = CapabilityPolicy(
            revision="capability-policy-v4",
            allow=(
                "document/extract_text",
                "file/glob",
                "file/grep",
                "file/read_text",
                "file/stat",
            ),
            ask=(
                "agent/delegate", "exec/run", "extension/call", "file/edit", "file/write", "skill/run"
            ),
            default="deny",
        )
        self.capability_broker: CapabilityBroker | None = None
        self.file_read_executor: FileReadExecutor | None = None
        self.file_search_executor: FileSearchExecutor | None = None
        self.file_mutation_executor: FileMutationExecutor | None = None
        self.command_executor: CommandExecutor | None = None
        self.research_environment: ResearchEnvironmentManager | None = None
        self.research_executor: ResearchDocumentExecutor | None = None
        self.extension_executor: ExtensionExecutor | None = None
        self.task_store: TaskStore | None = None
        self.task_manager: BackgroundTaskManager | None = None
        self.subagent_store: SubagentStore | None = None
        self.skill_registry: SkillRegistry | None = None
        self.skill_executor: SkillExecutor | None = None
        self.model_qualification: OllamaQualification | None = None
        self.sandbox_qualification: HostQualification | None = None
        self.runs: dict[str, RunRecord] = {}
        self._preparations: dict[str, PreparationStatus] = {}
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
            if self._manage_model_broker:
                self.model_qualification = await self.model_broker.start()
            else:
                self.model_qualification = self.model_broker.qualification
                if self.model_qualification is None:
                    raise RuntimeError("shared model broker is not initialized")
            if self.agent_id == PROTOTYPE_AGENT_ID:
                try:
                    self.capsule = await asyncio.to_thread(
                        self.capsules.load_agent, self.agent_id
                    )
                except FileNotFoundError:
                    self.capsule = await asyncio.to_thread(
                        self.capsules.ensure_prototype_agent
                    )
            else:
                self.capsule = await asyncio.to_thread(
                    self.capsules.load_agent, self.agent_id
                )
            await asyncio.to_thread(
                self.capsules.cleanup_orphan_run_roots,
                self.capsule,
            )
            self.journal = EventJournal(self.capsule.data_root / "state.sqlite")
            self.conversations = ConversationStore(
                self.capsule.data_root / "state.sqlite",
                self.capsule.agent_id,
            )
            self.capability_broker = CapabilityBroker(
                self.conversations,
                generation_provider=lambda: (
                    self.capsule.generation if self.capsule is not None else 0
                ),
                toolset_digest_provider=self._trusted_toolset_digests,
                policy=self.capability_policy,
            )
            self.file_read_executor = FileReadExecutor(self.capsule)
            self.file_search_executor = FileSearchExecutor(self.capsule)
            self.file_mutation_executor = FileMutationExecutor(self.capsule)
            self.command_executor = CommandExecutor(
                self.repository_root, self.source_root, self.capsule
            )
            self.research_environment = ResearchEnvironmentManager(
                self.repository_root, self.capsule
            )
            self.research_executor = ResearchDocumentExecutor(
                self.research_environment, self.command_executor
            )
            # External endpoints are operator construction, never request data.
            # The default release catalog is intentionally empty/fail-closed.
            self.extension_executor = ExtensionExecutor(self.extension_catalog)
            self.skill_registry = SkillRegistry(
                self.repository_root,
                self.capsule,
                self.capsule.data_root / "state.sqlite",
            )
            self.skill_executor = SkillExecutor(
                self.skill_registry, self.command_executor
            )
            self._refresh_optional_tools()
            self.task_store = TaskStore(
                self.capsule.data_root / "state.sqlite", self.capsule.agent_id
            )
            self.task_manager = BackgroundTaskManager(
                self.capsule,
                self.capsules,
                self.command_executor,
                self.task_store,
            )
            await self.task_manager.initialize()
            self.subagent_store = SubagentStore(
                self.capsule.data_root / "state.sqlite", self.capsule.agent_id
            )
            await asyncio.to_thread(self.subagent_store.recover_incomplete)
            await asyncio.to_thread(
                self.conversations.recover_running_as_interrupted
            )
            await asyncio.to_thread(self.journal.prune_to_recent_runs, 256)
        except BaseException:
            if self.task_manager is not None:
                await self.task_manager.close()
                self.task_manager = None
                self.task_store = None
            elif self.task_store is not None:
                self.task_store.close()
                self.task_store = None
            if self.subagent_store is not None:
                self.subagent_store.close()
                self.subagent_store = None
            if self.conversations is not None:
                self.conversations.close()
                self.conversations = None
            self.capability_broker = None
            self.file_read_executor = None
            self.file_search_executor = None
            self.file_mutation_executor = None
            self.command_executor = None
            self.research_environment = None
            self.research_executor = None
            self.extension_executor = None
            if self.skill_registry is not None:
                self.skill_registry.close()
                self.skill_registry = None
            self.skill_executor = None
            if self.journal is not None:
                self.journal.close()
                self.journal = None
            self.sandbox_qualification = None
            self.model_qualification = None
            if self._manage_model_broker:
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
        if self.task_manager is not None:
            await self.task_manager.close()
            self.task_manager = None
            self.task_store = None
        if self.subagent_store is not None:
            self.subagent_store.close()
            self.subagent_store = None
        if self.skill_registry is not None:
            self.skill_registry.close()
            self.skill_registry = None
        self.skill_executor = None
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        if self.conversations is not None:
            self.conversations.close()
            self.conversations = None
        self.capability_broker = None
        self.file_read_executor = None
        self.file_search_executor = None
        self.file_mutation_executor = None
        self.command_executor = None
        self.research_environment = None
        self.research_executor = None
        self.extension_executor = None
        self.model_qualification = None
        self.sandbox_qualification = None
        if self._manage_model_broker:
            await self.model_broker.close()

    def _conversation_store(self) -> ConversationStore:
        if self.conversations is None:
            raise RuntimeError("conversation store is unavailable")
        return self.conversations

    def _refresh_optional_tools(self) -> None:
        catalog = runtime_tool_catalog()
        policy = runtime_tool_policy()
        excluded = set()
        if self.skill_registry is None or not self.skill_registry.list():
            excluded.add("skill/run")
        if (
            self.research_environment is None
            or self.research_environment.status() is None
        ):
            excluded.add("document/extract_text")
        if not self.extension_catalog.public_metadata():
            excluded.add("extension/call")
        self.tool_catalog = type(catalog).create(
            tuple(spec for spec in catalog.specs if spec.tool_id not in excluded)
        )
        self.tool_policy = ToolPolicy(
            revision=policy.revision,
            allowed_tool_ids=tuple(
                item for item in policy.allowed_tool_ids if item not in excluded
            ),
            denied_tool_ids=policy.denied_tool_ids,
            allowed_risks=policy.allowed_risks,
        )
        self.effective_toolset = EffectiveToolSet.resolve(
            self.tool_catalog, self.tool_policy
        )
        self.effective_tools = self.effective_toolset.specs

    def _toolset_projection(
        self, *, allow_tools: bool, allow_delegation: bool = False
    ) -> EffectiveToolSet:
        if not allow_tools:
            policy = ToolPolicy(
                revision="turn-conversation-only-v1",
                allowed_tool_ids=(),
            )
        elif allow_delegation:
            policy = self.tool_policy
        else:
            policy = ToolPolicy(
                revision="turn-tools-no-delegation-v1",
                allowed_tool_ids=tuple(
                    item
                    for item in self.tool_policy.allowed_tool_ids
                    if item != "agent/delegate"
                ),
                denied_tool_ids=self.tool_policy.denied_tool_ids,
                allowed_risks=self.tool_policy.allowed_risks,
            )
        return EffectiveToolSet.resolve(self.tool_catalog, policy)

    def _trusted_toolset_digests(self) -> tuple[str, ...]:
        """Enumerate the bounded projections that a current Run may freeze."""

        return tuple(
            sorted(
                {
                    self._toolset_projection(allow_tools=False).toolset_digest,
                    self._toolset_projection(allow_tools=True).toolset_digest,
                    self._toolset_projection(
                        allow_tools=True, allow_delegation=True
                    ).toolset_digest,
                }
            )
        )

    async def _turn_effective_toolset(
        self, message: str, *, supports_tools: bool
    ) -> EffectiveToolSet:
        if not supports_tools or not _turn_needs_tools(message):
            return self._toolset_projection(allow_tools=False)
        allow_delegation = False
        if self.subagent_coordinator is not None and _DELEGATION_INTENT.search(message):
            mentioned = set(_AGENT_ID_IN_TEXT.findall(message))
            if mentioned:
                records = await asyncio.to_thread(
                    self.subagent_coordinator.registry.list
                )
                allow_delegation = any(
                    item.agent_id != self.agent_id
                    and item.state == "active"
                    and item.agent_id in mentioned
                    for item in records
                )
        return self._toolset_projection(
            allow_tools=True,
            allow_delegation=allow_delegation,
        )

    async def list_skills(self) -> tuple[SkillRecord, ...]:
        if self.skill_registry is None:
            raise RuntimeError("Skill registry is unavailable")
        return await asyncio.to_thread(self.skill_registry.list)

    async def install_skill(self, raw: bytes, expected_digest: str) -> SkillRecord:
        if self._closing or self.skill_registry is None:
            raise RuntimeError("Skill registry is unavailable")
        record = await asyncio.to_thread(
            self.skill_registry.install, raw, expected_digest
        )
        self._refresh_optional_tools()
        return record

    async def delete_skill(self, skill_id: str) -> None:
        if self._closing or self.skill_registry is None:
            raise RuntimeError("Skill registry is unavailable")
        await asyncio.to_thread(self.skill_registry.delete, skill_id)
        self._refresh_optional_tools()

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

    async def create_conversation_continuation(
        self, source_conversation_id: str, *, title: str = "续接会话"
    ) -> tuple[Conversation, int, int]:
        if self._closing:
            raise RuntimeError("RunService is closing")
        return await asyncio.to_thread(
            self._conversation_store().create_continuation,
            source_conversation_id,
            title=title,
        )

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

    async def get_conversation_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before_position: int | None = None,
        expected_revision: int | None = None,
    ) -> ConversationPage:
        return await asyncio.to_thread(
            self._conversation_store().get_conversation_page,
            conversation_id,
            limit=limit,
            before_position=before_position,
            expected_revision=expected_revision,
        )

    async def rename_conversation(
        self,
        conversation_id: str,
        title: str,
        *,
        expected_revision: int,
    ) -> Conversation:
        """Rename through the public RunService persistence boundary."""

        if self._closing:
            raise RuntimeError("RunService is closing")
        return await asyncio.to_thread(
            self._conversation_store().rename_conversation,
            conversation_id,
            title,
            expected_revision=expected_revision,
        )

    async def _durable_soft_calibration(
        self, plan: ContextPlan
    ) -> SoftContextCalibration | None:
        scope = plan.count_scope
        samples = await asyncio.to_thread(
            self._conversation_store().recent_provider_calibration_samples,
            **scope.to_dict(),
            limit=DURABLE_CALIBRATION_SAMPLE_LIMIT,
        )
        return _soft_calibration_from_samples(scope, samples)

    async def next_turn_preview(
        self, conversation_id: str, *, model_id: str | None = None
    ) -> dict[str, object]:
        """Project committed fixed context from durable state only."""

        if self.capsule is None:
            raise RuntimeError("RunService is not initialized")
        store = self._conversation_store()
        conversation = await asyncio.to_thread(
            store.get_conversation, conversation_id
        )
        selector = getattr(self.model_broker, "qualification_for", None)
        if callable(selector):
            qualification = selector(model_id)
        elif model_id is None and self.model_qualification is not None:
            qualification = self.model_qualification
        else:
            raise RuntimeError("model qualification is unavailable")
        profile = qualification.model_profile
        # A next message is not available yet, so preview the conservative
        # execution-capable default while withholding delegation (which can be
        # activated only by an explicit current-message target).
        effective_toolset = self._toolset_projection(
            allow_tools=profile.supports_tools,
            allow_delegation=False,
        )
        projection_mode = (
            "conservative_tools" if effective_toolset.specs else "chat_only"
        )
        base: dict[str, object] = {
            "version": "next-turn-projection-v1",
            "agent_id": self.capsule.agent_id,
            "conversation_id": conversation_id,
            "conversation_revision": conversation.revision,
            "model_id": profile.catalog_model_id or profile.model,
            "model_profile_digest": profile.profile_digest,
            "capsule_generation": self.capsule.generation,
            "turn_count": len(conversation.turns),
            "turn_limit": MAX_TURNS_PER_CONVERSATION,
            "turns_remaining": max(
                0, MAX_TURNS_PER_CONVERSATION - len(conversation.turns)
            ),
            "single_message_byte_limit": MAX_USER_CONTENT_BYTES,
            "operational_context_tokens": profile.operational_context_tokens,
            "output_reserve_tokens": profile.max_output_tokens,
            "availability": "unavailable",
            "stale_reason": None,
            "fixed_context_tokens": None,
            "fixed_context_error_margin_tokens": None,
            "safe_user_tokens": None,
            "compact_before_user_tokens": None,
            "soft_trigger_tokens": None,
            "hard_input_tokens": None,
            "projection_strategy": None,
            "count_basis": None,
            "count_version": "soft-context-estimate-v1",
            "last_run_usage": None,
            "summary_status": "disabled",
            "projection_mode": projection_mode,
            "chat_calibration_available": False,
            "chat_only_projection": None,
        }
        if conversation.active_run_id is not None:
            return {**base, "stale_reason": "active_run"}
        if len(conversation.turns) >= MAX_TURNS_PER_CONVERSATION:
            return {**base, "stale_reason": "conversation_turn_capacity_exhausted"}
        snapshot = await asyncio.to_thread(store.snapshot_for_turn, conversation_id)
        prompt_sources = await asyncio.to_thread(collect_prompt_sources, self.capsule)

        def compile_preview(
            tools: tuple[ToolSpec, ...],
            *,
            soft_calibration: SoftContextCalibration | None = None,
        ) -> ContextPlan:
            return self.context_compiler.compile(
                ".",
                model_profile=profile,
                tools=tools,
                agent_id=self.capsule.agent_id,
                capsule_generation=self.capsule.generation,
                history=(
                    () if snapshot.completed_turn_contexts else tuple(
                        ConversationMessage(item.message_id, item.role, item.content)
                        for item in snapshot.committed_history
                    )
                ),
                completed_turns=snapshot.completed_turn_contexts,
                continuation_context=snapshot.continuation_context,
                prompt_sources=prompt_sources,
                soft_calibration=soft_calibration,
            )

        try:
            plan = compile_preview(effective_toolset.specs)
        except ContextPlanError:
            return {**base, "stale_reason": "projection_unavailable"}
        summary = await asyncio.to_thread(
            store.read_summary_projection, conversation_id
        )
        common = {
            **base,
            "toolset_digest": plan.reference.toolset_digest,
            "renderer_version": plan.count_scope.renderer_version,
            "policy_digest": plan.policy.policy_digest,
            "soft_trigger_tokens": plan.policy.compact_at_tokens,
            "hard_input_tokens": plan.policy.hard_input_tokens,
            "projection_strategy": plan.windowing_strategy,
            "summary_status": summary.status if summary is not None else "disabled",
        }
        completed = next(
            (turn for turn in reversed(conversation.turns) if turn.status == "completed"),
            None,
        )
        usage = (
            await asyncio.to_thread(
                store.provider_usage_for_run, completed.run_id
            )
            if completed is not None
            else ()
        )
        usage_summary = (
            {
                "input_tokens": sum(item.input_tokens or 0 for item in usage),
                "output_tokens": sum(item.output_tokens or 0 for item in usage),
                "provider_calls": len(usage),
                "complete": all(item.status == "complete" for item in usage),
            }
            if usage
            else None
        )
        calibration = await self._durable_soft_calibration(plan)
        chat_calibration_available = False
        chat_plan: ContextPlan | None = None
        chat_calibration: SoftContextCalibration | None = None
        if projection_mode == "chat_only":
            chat_plan = plan
            chat_calibration = calibration
            chat_calibration_available = calibration is not None
        else:
            # A pure-chat observation belongs to the empty ToolSet scope.  It
            # may be returned as a separately labelled chat-only projection,
            # but its ratio and capacity are never transferred to the
            # conservative ToolSet scope.
            try:
                chat_plan = compile_preview(
                    self._toolset_projection(allow_tools=False).specs
                )
            except ContextPlanError:
                chat_plan = None
            if chat_plan is not None:
                chat_calibration = await self._durable_soft_calibration(chat_plan)
                chat_calibration_available = chat_calibration is not None

        chat_only_projection: dict[str, object] | None = None
        if chat_plan is not None and chat_calibration is not None:
            calibrated_chat = compile_preview(
                chat_plan.tools,
                soft_calibration=chat_calibration,
            )
            chat_estimate = calibrated_chat.soft_context_estimate
            assert chat_estimate.estimated_tokens is not None
            assert chat_estimate.error_margin_tokens is not None
            chat_upper = chat_estimate.upper_tokens_for(calibrated_chat.count_scope)
            chat_only_projection = {
                "version": "next-turn-chat-only-projection-v1",
                "availability": "available",
                "projection_mode": "chat_only",
                "fixed_context_tokens": chat_estimate.estimated_tokens,
                "fixed_context_error_margin_tokens": (
                    chat_estimate.error_margin_tokens
                ),
                "safe_user_tokens": max(
                    0, calibrated_chat.policy.hard_input_tokens - chat_upper
                ),
                "compact_before_user_tokens": max(
                    0, calibrated_chat.policy.compact_at_tokens - chat_upper
                ),
                "soft_trigger_tokens": calibrated_chat.policy.compact_at_tokens,
                "hard_input_tokens": calibrated_chat.policy.hard_input_tokens,
                "operational_context_tokens": (
                    calibrated_chat.model_profile.operational_context_tokens
                ),
                "projection_strategy": calibrated_chat.windowing_strategy,
                "count_basis": chat_estimate.basis,
                "renderer_version": calibrated_chat.count_scope.renderer_version,
                "toolset_digest": calibrated_chat.reference.toolset_digest,
            }
        if calibration is None:
            if (
                projection_mode == "conservative_tools"
                and chat_calibration_available
            ):
                stale_reason = "toolset_calibration_unavailable"
            elif not usage:
                stale_reason = "no_provider_observation"
            elif any(item.status != "complete" for item in usage):
                stale_reason = "incomplete_provider_usage"
            else:
                stale_reason = "profile_or_projection_drift"
            return {
                **common,
                "stale_reason": stale_reason,
                "last_run_usage": usage_summary,
                "chat_calibration_available": chat_calibration_available,
                "chat_only_projection": chat_only_projection,
            }
        projected = self.context_compiler.compile(
            ".",
            model_profile=profile,
            tools=effective_toolset.specs,
            agent_id=self.capsule.agent_id,
            capsule_generation=self.capsule.generation,
            history=(
                () if snapshot.completed_turn_contexts else tuple(
                    ConversationMessage(item.message_id, item.role, item.content)
                    for item in snapshot.committed_history
                )
            ),
            completed_turns=snapshot.completed_turn_contexts,
            continuation_context=snapshot.continuation_context,
            prompt_sources=prompt_sources,
            soft_calibration=calibration,
        )
        estimate = projected.soft_context_estimate
        assert estimate.estimated_tokens is not None
        assert estimate.error_margin_tokens is not None
        upper = estimate.upper_tokens_for(projected.count_scope)
        return {
            **common,
            "availability": "available",
            "stale_reason": None,
            "fixed_context_tokens": estimate.estimated_tokens,
            "fixed_context_error_margin_tokens": estimate.error_margin_tokens,
            "safe_user_tokens": max(0, projected.policy.hard_input_tokens - upper),
            "compact_before_user_tokens": max(
                0, projected.policy.compact_at_tokens - upper
            ),
            "projection_strategy": projected.windowing_strategy,
            "count_basis": estimate.basis,
            "last_run_usage": usage_summary,
            "chat_calibration_available": chat_calibration_available,
            "chat_only_projection": chat_only_projection,
        }

    async def list_permission_requests(
        self, *, pending_only: bool = True
    ) -> tuple[PermissionRecord, ...]:
        await asyncio.to_thread(
            self._conversation_store().expire_pending_permissions
        )
        return await asyncio.to_thread(
            self._conversation_store().permission_requests,
            pending_only=pending_only,
            limit=64,
        )

    async def resolve_permission_request(
        self, permission_id: str, decision: str
    ) -> PermissionRecord:
        if decision not in {"approve", "deny"}:
            raise ValueError("invalid permission decision")
        broker = self.capability_broker
        if broker is None:
            raise RuntimeError("capability broker is unavailable")
        existing = await asyncio.to_thread(
            self._conversation_store().get_permission_request,
            permission_id,
        )
        try:
            record = self.get(existing.run_id)
        except KeyError:
            mutation = await asyncio.to_thread(
                self._conversation_store().resolve_permission_request,
                permission_id,
                "cancelled",
                resolution_source="system",
            )
            return mutation.record
        if record.terminal_kind is not None or record.cancel_requested:
            mutation = await asyncio.to_thread(
                self._conversation_store().resolve_permission_request,
                permission_id,
                "cancelled",
                resolution_source="system",
            )
            return mutation.record
        return await asyncio.to_thread(
            broker.resolve,
            permission_id,
            decision,
        )

    async def capability_audit_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 128
    ) -> tuple[CapabilityAuditEvent, ...]:
        return await asyncio.to_thread(
            self._conversation_store().capability_audit_events,
            run_id,
            after_seq=after_seq,
            limit=limit,
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
            if conversation_id in self._preparations:
                raise ConversationConflictError(
                    "cannot delete a conversation with a preparing Run"
                )
            if self.subagent_coordinator is not None:
                await self.subagent_coordinator.cleanup_conversation(
                    self, conversation_id
                )
            if self.task_manager is not None:
                await self.task_manager.cancel_conversation(conversation_id)
            result = await asyncio.to_thread(
                self._conversation_store().delete_conversation,
                conversation_id,
            )
            if result.deleted:
                if self.task_store is not None:
                    await asyncio.to_thread(
                        self.task_store.delete_conversation, conversation_id
                    )
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
                    record.recovery_context_plan = None
                    record.recovery_history = ()
                    record.recovery_prompt_sources = None
                    record.open_blocks.clear()
                    record.seen_blocks.clear()
                    record.pending_tools.clear()
                    record.pending_tool_arguments.clear()
                    record.started_tools.clear()
                    record.seen_tools.clear()
                    record.broker_pending_tool_calls.clear()
                    record.broker_tool_results.clear()
                    record.file_read_receipts.clear()
                    retired.append(record)
                for record in retired:
                    async with record.condition:
                        record.condition.notify_all()
        return result

    def _task_services(self) -> tuple[BackgroundTaskManager, TaskStore]:
        if self.task_manager is None or self.task_store is None:
            raise RuntimeError("background Task service is unavailable")
        return self.task_manager, self.task_store

    async def submit_background_task(
        self, run_id: str, arguments: dict[str, str] | None = None
    ) -> TaskRecord:
        """Detach one fixed, sandboxed command from an owned parent Run."""

        if self._closing:
            raise RuntimeError("RunService is closing")
        manager, _store = self._task_services()
        async with self._lock:
            identity = await asyncio.to_thread(
                self._conversation_store().resolve_run_identity, run_id
            )
            if identity.agent_id != self.agent_id:
                raise KeyError("run not found")
            return await manager.submit(
                TaskParentIdentity(
                    agent_id=identity.agent_id,
                    conversation_id=identity.conversation_id,
                    turn_id=identity.turn_id,
                    run_id=identity.run_id,
                ),
                arguments,
            )

    async def list_background_tasks(self) -> tuple[TaskRecord, ...]:
        _manager, store = self._task_services()
        return await asyncio.to_thread(store.list)

    async def get_background_task(self, task_id: str) -> TaskRecord:
        _manager, store = self._task_services()
        return await asyncio.to_thread(store.get, task_id)

    async def background_task_notifications(
        self, task_id: str
    ) -> tuple[TaskNotification, ...]:
        _manager, store = self._task_services()
        return await asyncio.to_thread(store.notifications, task_id)

    async def cancel_background_task(self, task_id: str) -> TaskRecord:
        manager, store = self._task_services()
        task = await asyncio.to_thread(store.get, task_id)
        if (
            task.command_id == "agent-delegate"
            and self.subagent_coordinator is not None
        ):
            await self.subagent_coordinator.cancel_task(task_id)
            return await asyncio.to_thread(store.get, task_id)
        return await manager.cancel(task_id)

    async def list_subagent_links(
        self, conversation_id: str
    ) -> tuple[tuple[SubagentLink, tuple[MailboxMessage, ...]], ...]:
        if self.subagent_store is None:
            raise RuntimeError("subagent store is unavailable")
        links = await asyncio.to_thread(
            self.subagent_store.list_for_conversation, conversation_id
        )
        records: list[tuple[SubagentLink, tuple[MailboxMessage, ...]]] = []
        for link in links:
            mailbox = await asyncio.to_thread(
                self.subagent_store.mailbox, link.task_id
            )
            records.append((link, mailbox))
        return tuple(records)

    async def preparation_status(
        self, conversation_id: str
    ) -> dict[str, object] | None:
        """Return safe transient progress without exposing prompt or context."""

        async with self._lock:
            value = self._preparations.get(conversation_id)
            if value is not None:
                elapsed = max(
                    0.0, asyncio.get_running_loop().time() - value.started_at
                )
                return {
                    "version": "run-preparation-v1",
                    "state": (
                        "cancelling"
                        if value.cancel_requested.is_set()
                        else "preparing"
                    ),
                    "operation_id": value.operation_id,
                    "stage": value.stage,
                    "elapsed_ms": min(300_000, int(elapsed * 1_000)),
                }
            handed_off_run = next(
                (
                    record
                    for record in self.runs.values()
                    if record.conversation_id == conversation_id
                    and record.terminal_kind is None
                    and record.preparation_operation_id is not None
                ),
                None,
            )
            if handed_off_run is not None:
                started = handed_off_run.cancel_requested_at
                elapsed = (
                    max(0.0, asyncio.get_running_loop().time() - started)
                    if started is not None
                    else 0.0
                )
                return {
                    "version": "run-preparation-v1",
                    "state": (
                        "cancelling"
                        if handed_off_run.cancel_requested
                        else "preparing"
                    ),
                    "operation_id": handed_off_run.preparation_operation_id,
                    "stage": "admitting_run",
                    "elapsed_ms": min(300_000, int(elapsed * 1_000)),
                }
            # Keep the same fence used by delete/start while performing this
            # fixed-cost lookup.  Thus an idle response cannot race a delete
            # into returning success for a now-missing Conversation.
            exists = await asyncio.to_thread(
                self._conversation_store().conversation_exists,
                conversation_id,
            )
            if not exists:
                raise ConversationNotFoundError("conversation not found")
            return None

    async def cancel_preparation(
        self, conversation_id: str, operation_id: str
    ) -> dict[str, object]:
        """Cancel admission, including its atomic handoff to an active Run.

        This control path deliberately bypasses QueryEngine admission locks so
        it remains responsive while a model-backed summary is blocked. The
        preparation event and in-memory Run lookup share ``_lock``: there is no
        gap in which the admission has left one state but is absent from both.
        """

        if SAFE_ID.fullmatch(operation_id) is None:
            raise ValueError("invalid preparation operation identity")
        run_id: str | None = None
        async with self._lock:
            preparation = self._preparations.get(conversation_id)
            if preparation is not None:
                if preparation.operation_id != operation_id:
                    return {
                        "version": "run-preparation-cancel-v1",
                        "state": "stale",
                        "target": None,
                    }
                preparation.cancel_requested.set()
                return {
                    "version": "run-preparation-cancel-v1",
                    "state": "cancellation_requested",
                    "target": "preparation",
                }
            active = next(
                (
                    record
                    for record in self.runs.values()
                    if record.conversation_id == conversation_id
                    and record.terminal_kind is None
                    and record.preparation_operation_id == operation_id
                ),
                None,
            )
            if active is not None:
                run_id = active.run_id
            elif any(
                record.conversation_id == conversation_id
                and record.terminal_kind is None
                and record.preparation_operation_id != operation_id
                for record in self.runs.values()
            ):
                return {
                    "version": "run-preparation-cancel-v1",
                    "state": "stale",
                    "target": None,
                }
            else:
                exists = await asyncio.to_thread(
                    self._conversation_store().conversation_exists,
                    conversation_id,
                )
                if not exists:
                    raise ConversationNotFoundError("conversation not found")
        if run_id is None:
            return {
                "version": "run-preparation-cancel-v1",
                "state": "idle",
                "target": None,
            }
        await self.cancel(run_id)
        return {
            "version": "run-preparation-cancel-v1",
            "state": "cancellation_requested",
            "target": "run",
        }

    def _set_preparation_stage(
        self, preparation: PreparationStatus | None, stage: str
    ) -> None:
        if preparation is None:
            return
        if stage not in PREPARATION_STAGES:
            raise ValueError("invalid Run preparation stage")
        current = self._preparations.get(preparation.conversation_id)
        if current is preparation:
            current.stage = stage

    async def _prepare_run_owned(
        self,
        command: StartRunCommand,
        request_cancelled: asyncio.Event,
        preparation: PreparationStatus | None,
    ) -> RunRecord:
        try:
            return await self._prepare_run(
                command, request_cancelled, preparation=preparation
            )
        finally:
            if preparation is not None:
                async with self._lock:
                    current = self._preparations.get(preparation.conversation_id)
                    if current is preparation:
                        self._preparations.pop(preparation.conversation_id, None)

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
        preparation: PreparationStatus | None = None
        if command.conversation_id is not None:
            async with self._lock:
                active = sum(
                    existing.terminal_kind is None for existing in self.runs.values()
                )
                if command.conversation_id in self._preparations:
                    raise ConversationConflictError(
                        "conversation already has a preparing Run"
                    )
                if active + len(self._preparations) >= MAX_ACTIVE_RUNS:
                    raise ValueError("prototype active Run capacity exhausted")
                preparation = PreparationStatus(
                    operation_id=new_id(),
                    conversation_id=command.conversation_id,
                    stage="reading_history",
                    started_at=asyncio.get_running_loop().time(),
                    cancel_requested=request_cancelled,
                )
                self._preparations[command.conversation_id] = preparation
        operation = asyncio.create_task(
            self._prepare_run_owned(
                command, request_cancelled, preparation
            ),
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
        *,
        preparation: PreparationStatus | None = None,
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
            self._set_preparation_stage(preparation, "building_context")
            context_history = tuple(
                ConversationMessage(message.message_id, message.role, message.content)
                for message in snapshot.committed_history
            )
            completed_contexts = snapshot.completed_turn_contexts
            compile_history = () if completed_contexts else context_history
            try:
                selector = getattr(self.model_broker, "qualification_for", None)
                if callable(selector):
                    model_qualification = selector(command.model_id)
                elif command.model_id is None and self.model_qualification is not None:
                    # Narrow compatibility seam for deterministic test brokers;
                    # the production OllamaBroker always owns a ModelCatalog.
                    model_qualification = self.model_qualification
                else:
                    raise OllamaBrokerError(
                        "model_rejected", "The model is not catalog-qualified."
                    )
            except OllamaBrokerError as exc:
                raise ValueError("model is not available") from exc
            effective_toolset = await self._turn_effective_toolset(
                command.message,
                supports_tools=model_qualification.model_profile.supports_tools,
            )
            prompt_sources = await asyncio.to_thread(
                collect_prompt_sources, self.capsule
            )
            if request_cancelled.is_set() or self._closing:
                raise asyncio.CancelledError
            self._set_preparation_stage(preparation, "evaluating_compaction")
            context_plan = self.context_compiler.compile(
                command.message,
                model_profile=model_qualification.model_profile,
                tools=effective_toolset.specs,
                agent_id=command.agent_id,
                capsule_generation=self.capsule.generation,
                history=compile_history,
                prompt_sources=prompt_sources,
                force_compact=command.compact,
                completed_turns=completed_contexts,
                continuation_context=snapshot.continuation_context,
            )
            # Soft policy is rebuilt from scope-bound durable samples for every
            # admission.  Provider-local memory may remain as a diagnostic
            # cache, but it is never an authoritative production decision.
            soft_calibration = await self._durable_soft_calibration(context_plan)
            if soft_calibration is not None:
                context_plan = self.context_compiler.compile(
                    command.message,
                    model_profile=model_qualification.model_profile,
                    tools=effective_toolset.specs,
                    agent_id=command.agent_id,
                    capsule_generation=self.capsule.generation,
                    history=compile_history,
                    prompt_sources=prompt_sources,
                    force_compact=command.compact,
                    soft_calibration=soft_calibration,
                    completed_turns=completed_contexts,
                    continuation_context=snapshot.continuation_context,
                )
            summary_status = "disabled"
            if (
                context_plan.windowing_strategy == "completed-turn-collapse-v2"
                and context_plan.collapse_projection is not None
                and completed_contexts
                and getattr(self.model_broker, "semantic_summary_enabled", False)
                and callable(getattr(self.model_broker, "summarize_v2", None))
                and not request_cancelled.is_set()
                and not self._closing
            ):
                self._set_preparation_stage(preparation, "summarizing_history")
                omitted_turns = (
                    context_plan.history_message_count
                    - context_plan.included_history_message_count
                ) // 2
                summary_source = completed_contexts[:omitted_turns]
                source_digest = summary_v2_source_digest(summary_source)
                stored = await asyncio.to_thread(
                    self.conversations.read_summary_projection, conversation_id
                )
                semantic_summary: SemanticSummaryV2Snapshot | None = None
                if (
                    stored is not None
                    and stored.status in {"generated", "reused"}
                    and stored.source_digest == source_digest
                    and stored.snapshot is not None
                    and stored.snapshot.source_digest == source_digest
                    and stored.snapshot.model_profile_digest
                    == model_qualification.model_profile.profile_digest
                    and stored.snapshot.prompt_digest == SUMMARY_V2_PROMPT_DIGEST
                    and stored.snapshot.policy_digest == SUMMARY_V2_POLICY_DIGEST
                ):
                    semantic_summary = stored.snapshot
                    summary_status = "reused"
                elif (
                    stored is not None
                    and stored.source_digest == source_digest
                    and stored.status in {
                        "source_limit", "timeout", "invalid", "circuit_open"
                    }
                    and _summary_negative_is_reusable(stored)
                ):
                    summary_status = stored.status
                else:
                    parent = stored.snapshot if stored is not None else None
                    if parent is not None:
                        prefix = len(parent.source_turn_ids)
                        if (
                            tuple(turn.turn_id for turn in summary_source[:prefix])
                            != parent.source_turn_ids
                            or tuple(
                                completed_bundle_digest(turn)
                                for turn in summary_source[:prefix]
                            )
                            != parent.source_bundle_digests
                            or parent.model_profile_digest
                            != model_qualification.model_profile.profile_digest
                        ):
                            parent = None
                    delta = (
                        summary_source[len(parent.source_turn_ids) :]
                        if parent is not None else summary_source
                    )
                    try:
                        if not delta:
                            raise ContextPlanError("summary source has no delta")
                        semantic_summary = await self.model_broker.summarize_v2(
                            delta,
                            aggregate_source=summary_source,
                            parent=parent,
                            model_id=(
                                model_qualification.model_profile.catalog_model_id
                                or model_qualification.model
                            ),
                            is_cancelled=lambda: (
                                request_cancelled.is_set() or self._closing
                            ),
                        )
                        summary_status = "generated"
                    except OllamaBrokerError as exc:
                        summary_status = {
                            "summary_source_limit": "source_limit",
                            "summary_timeout": "timeout",
                            "summary_circuit_open": "circuit_open",
                        }.get(exc.code, "invalid")
                    except ContextPlanError:
                        summary_status = "invalid"
                if semantic_summary is not None:
                    try:
                        candidate = self.context_compiler.compile(
                            command.message,
                            model_profile=model_qualification.model_profile,
                            tools=effective_toolset.specs,
                            agent_id=command.agent_id,
                            capsule_generation=self.capsule.generation,
                            prompt_sources=prompt_sources,
                            semantic_summary=semantic_summary,
                            force_compact=command.compact,
                            soft_calibration=soft_calibration,
                            completed_turns=completed_contexts,
                            continuation_context=snapshot.continuation_context,
                        )
                        if (
                            candidate.soft_estimate is not None
                            and candidate.soft_estimate.upper_tokens_for(
                                candidate.count_scope
                            ) > candidate.policy.compact_target_tokens
                        ):
                            raise ContextPlanError(
                                "summary does not satisfy compact target"
                            )
                        context_plan = candidate
                    except ContextPlanError:
                        semantic_summary = None
                        summary_status = "invalid"
                try:
                    await asyncio.to_thread(
                        self.conversations.write_summary_projection,
                        conversation_id,
                        status=summary_status,
                        source_digest=source_digest,
                        snapshot=semantic_summary or (
                            stored.snapshot if stored is not None else None
                        ),
                    )
                except (
                    ValueError,
                    ConversationConflictError,
                    ConversationStoreUnavailableError,
                ):
                    # The projection is optional and deterministic collapse is
                    # already valid. A deletion race will fail admission later.
                    if semantic_summary is not None:
                        context_plan = self.context_compiler.compile(
                            command.message,
                            model_profile=model_qualification.model_profile,
                            tools=effective_toolset.specs,
                            agent_id=command.agent_id,
                            capsule_generation=self.capsule.generation,
                            prompt_sources=prompt_sources,
                            force_compact=command.compact,
                            soft_calibration=soft_calibration,
                            completed_turns=completed_contexts,
                            continuation_context=snapshot.continuation_context,
                        )
                    summary_status = "invalid"
            self._set_preparation_stage(preparation, "verifying_continuation")
            # A completed Turn may commit the Broker's maximum assistant body.
            # Prove before Worker/model/Tool side effects that the same frozen
            # profile, ToolSet, generation and prompt sources can still admit a
            # minimal following user Turn. The canonical transcript remains
            # intact; the proof may select deterministic history projection.
            self.context_compiler.require_completed_turn_continuation(
                command.message,
                model_profile=model_qualification.model_profile,
                tools=effective_toolset.specs,
                agent_id=command.agent_id,
                capsule_generation=self.capsule.generation,
                history=(() if completed_contexts else context_history),
                completed_turns=completed_contexts,
                continuation_context=snapshot.continuation_context,
                prompt_sources=prompt_sources,
            )
            recovery_context_plan: ContextPlan | None = None
            if context_plan.history_message_count >= 4:
                candidate_recovery = self.context_compiler.compile(
                    command.message,
                    model_profile=model_qualification.model_profile,
                    tools=effective_toolset.specs,
                    agent_id=command.agent_id,
                    capsule_generation=self.capsule.generation,
                    history=compile_history,
                    prompt_sources=prompt_sources,
                    force_compact=True,
                    collapse_to_recent=True,
                    completed_turns=completed_contexts,
                    continuation_context=snapshot.continuation_context,
                )
                if candidate_recovery.reference != context_plan.reference:
                    recovery_context_plan = candidate_recovery
            runtime_snapshot = TurnRuntimeSnapshot.create(
                context_plan=context_plan,
                loop_limits=LoopLimits(
                    max_model_iterations=4,
                    max_tool_calls=2,
                ),
                wall_timeout_seconds=RUN_WALL_TIMEOUT_SECONDS,
                effective_toolset=effective_toolset,
                projection_reason=(
                    "manual_compact"
                    if command.compact
                    else (
                        "semantic_summary"
                        if context_plan.semantic_summary is not None else "admission"
                    )
                ),
            )
            if request_cancelled.is_set() or self._closing:
                raise asyncio.CancelledError
            self._set_preparation_stage(preparation, "admitting_run")
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
            preparation_operation_id=(
                preparation.operation_id if preparation is not None else None
            ),
            summary_status=summary_status,
            context_plan=context_plan,
            active_context_plan=context_plan,
            recovery_context_plan=recovery_context_plan,
            recovery_history=(context_history if recovery_context_plan is not None else ()),
            recovery_prompt_sources=(
                prompt_sources if recovery_context_plan is not None else None
            ),
            effective_tools=effective_toolset.specs,
            runtime_snapshot=runtime_snapshot,
            active_runtime_snapshot=runtime_snapshot,
            deadline_at=(
                asyncio.get_running_loop().time()
                + runtime_snapshot.wall_timeout_seconds
            ),
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
                    "model": context_plan.model_profile.model,
                    "model_id": (
                        context_plan.model_profile.catalog_model_id
                        or context_plan.model_profile.model
                    ),
                    "model_profile_digest": context_plan.model_profile.profile_digest,
                    "visible_tools": [
                        spec.tool_id for spec in record.effective_tools
                    ],
                    "protocol_features": [
                        "model-call-boundaries-v1",
                        "sequential-multi-tool-v1",
                        "one-shot-overflow-recovery-v1",
                    ],
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
        # Permission invalidation is durable and precedes signalling the
        # Worker.  No approval can therefore outlive an accepted cancellation.
        await asyncio.to_thread(
            self._conversation_store().cancel_pending_permissions_for_run,
            run_id,
        )
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
            runtime_snapshot = record.runtime_snapshot
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
                or runtime_snapshot is None
                or len(record.seen_tools)
                >= runtime_snapshot.loop_limits.max_tool_calls
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
                or (
                    spec is not None
                    and spec.tool_id
                    in {
                        "file/stat", "file/read_text", "file/glob", "file/grep",
                        "file/edit", "file/write",
                        "agent/delegate",
                    }
                    and not (
                        record.brokered_capability_results.get(str(call_id))
                        == (str(payload.get("outcome")), str(validated_result))
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
            runtime_snapshot = record.runtime_snapshot
            if (
                keys != {"reason", "model_iterations"}
                or payload.get("reason") not in {"end_turn", "max_output"}
                or not isinstance(payload.get("model_iterations"), int)
                or isinstance(payload.get("model_iterations"), bool)
                or payload["model_iterations"] != record.model_request_count
                or record.model_response_count != record.model_request_count
                or record.broker_stop_iteration != record.model_request_count
                or runtime_snapshot is None
                or not 1 <= record.model_request_count <= (
                    runtime_snapshot.loop_limits.max_model_iterations
                )
                or len(record.seen_tools) > runtime_snapshot.loop_limits.max_tool_calls
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
            spec = next(
                (item for item in record.effective_tools if item.tool_id == tool_id),
                None,
            )
            if spec is None:
                raise RuntimeError("Tool result lost its frozen specification")
            arguments = record.pending_tool_arguments.get(call_id)
            if arguments is None:
                raise RuntimeError("Tool result lost its validated arguments")
            projection = project_tool_result(spec, call_id, payload["result"])
            record.broker_tool_results.append(
                OllamaToolResult(
                    call_id=call_id,
                    tool_id=tool_id,
                    content=projection.content,
                    outcome=str(payload["outcome"]),
                    original_bytes=projection.original_bytes,
                    content_digest=projection.content_digest,
                    truncated=projection.truncated,
                    truncation_reason=projection.truncation_reason,
                    projection_digest=projection.projection_digest,
                )
            )
            record.completed_tool_calls.append(
                (call_id, tool_id, spec.provider_name, dict(arguments))
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
            record.recovery_context_plan = None
            record.recovery_history = ()
            record.recovery_prompt_sources = None
            record.file_read_receipts.clear()
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
        context_recovery: tuple[ContextProjectionBoundary, str] | None = None,
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
                context_recovery=context_recovery,
            )

    @staticmethod
    def _completed_turn_context(record: RunRecord) -> CompletedTurnContext:
        plan = record.active_context_plan or record.context_plan
        if (
            plan is None
            or record.user_message is None
            or record.final_assistant_content is None
            or record.turn_position is None
            or len(record.completed_tool_calls) != len(record.broker_tool_results)
        ):
            raise RuntimeError("completed Run context is incomplete")
        items: list[CompletedContextItem] = [
            CompletedContextItem.plain(0, "user", record.user_message)
        ]
        for call, result in zip(
            record.completed_tool_calls, record.broker_tool_results, strict=True
        ):
            call_id, tool_id, provider_name, arguments = call
            if result.call_id != call_id or result.tool_id != tool_id:
                raise RuntimeError("completed Tool context identity changed")
            if result.projection_digest is None:
                raise RuntimeError("completed Tool result has no projection digest")
            items.append(
                CompletedContextItem.tool_use(
                    len(items),
                    call_id=call_id,
                    tool_id=tool_id,
                    provider_name=provider_name,
                    arguments=arguments,
                )
            )
            items.append(
                CompletedContextItem.tool_result(
                    len(items),
                    call_id=call_id,
                    tool_id=tool_id,
                    provider_name=provider_name,
                    content=result.content,
                    outcome=result.outcome,
                    original_bytes=result.original_bytes or 0,
                    projection_reason=result.truncation_reason or "none",
                    projection_digest=result.projection_digest,
                )
            )
        items.append(
            CompletedContextItem.plain(
                len(items), "assistant_final", record.final_assistant_content
            )
        )
        return CompletedTurnContext(
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            position=record.turn_position,
            model_profile_digest=plan.model_profile.profile_digest,
            context_plan_digest=plan.reference.digest,
            items=tuple(items),
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
        context_recovery: tuple[ContextProjectionBoundary, str] | None = None,
    ) -> EventEnvelope:
        """Validate and publish one event under the Control-plane state fence."""

        if record.retired:
            raise ConversationNotFoundError("conversation was deleted")
        if record.terminal_kind is not None:
            raise RuntimeError("attempted event after terminal")
        if provider_usage_start is not None and provider_usage_complete is not None:
            raise RuntimeError("provider boundary cannot start and complete together")
        if context_recovery is not None and (
            kind != "model.recovery.started"
            or durability != "durable"
            or provider_usage_start is not None
            or provider_usage_complete is not None
        ):
            raise RuntimeError("context recovery persistence is invalid")
        if record.conversation_managed and (
            (kind == "model.request.started" and provider_usage_start is None)
            or (
                kind == "model.response.finished"
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
                if record.runtime_snapshot is None:
                    raise RuntimeError("Turn runtime snapshot is unavailable")
                context_projection = ContextProjectionBoundary.create(
                    record.runtime_snapshot,
                    conversation_id=record.conversation_id,
                    turn_id=record.turn_id,
                    run_id=record.run_id,
                    conversation_revision=record.conversation_revision,
                )
                begin_result = await asyncio.to_thread(
                    self.conversations.begin_turn,
                    record.conversation_id,
                    turn_id=record.turn_id,
                    run_id=record.run_id,
                    user_content=record.user_message,
                    expected_revision=record.conversation_revision,
                    started_event=envelope,
                    context_projection=context_projection,
                )
                record.turn_position = begin_result.turn.position
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
            elif record.conversation_managed and context_recovery is not None:
                if self.conversations is None:
                    raise RuntimeError("conversation store is unavailable")
                recovery_boundary, expected_boundary_digest = context_recovery
                await asyncio.to_thread(
                    self.conversations.replace_context_projection_boundary_with_event,
                    recovery_boundary,
                    envelope,
                    expected_boundary_digest=expected_boundary_digest,
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
                        self._completed_turn_context(record),
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
        except Exception as exc:
            LOGGER.error(
                "Harness V2 durable publish failed kind=%s exception=%s",
                kind,
                type(exc).__name__,
            )
            record.journal_failed = True
            raise
        record.events.append(envelope)
        record.live_event_bytes += encoded_size
        if durability == "durable":
            record.durable_event_bytes += encoded_size
        if kind in TERMINAL_KINDS:
            record.terminal_kind = kind
            record.recovery_context_plan = None
            record.recovery_history = ()
            record.recovery_prompt_sources = None
            record.file_read_receipts.clear()
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

    @staticmethod
    async def _write_capability_response(
        stream: asyncio.StreamWriter,
        *,
        request_id: str,
        call_id: str,
        tool_id: str,
        outcome: str,
        content: str,
    ) -> None:
        value = {
            "internal": "capability.response",
            "version": BROKER_PROTOCOL_VERSION,
            "request_id": request_id,
            "type": "result",
            "call_id": call_id,
            "tool_id": tool_id,
            "outcome": outcome,
            "content": content,
        }
        encoded = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        if len(encoded) > MAX_BROKER_FRAME_BYTES:
            raise RuntimeError("capability response IPC frame exceeded its limit")
        stream.write(encoded)
        await stream.drain()

    async def _serve_capability_request(
        self,
        *,
        record: RunRecord,
        stream: asyncio.StreamWriter,
        value: dict[str, Any],
    ) -> None:
        if set(value) != {
            "internal", "version", "request_id", "call_id", "tool_id", "arguments"
        }:
            raise ValueError("Worker capability request is invalid")
        request_id = value.get("request_id")
        call_id = value.get("call_id")
        tool_id = value.get("tool_id")
        arguments = value.get("arguments")
        spec = self._effective_tool(record, tool_id)
        try:
            validated = spec.validate_arguments(arguments) if spec is not None else None
        except ValueError:
            validated = None
        if (
            value.get("internal") != "capability.request"
            or value.get("version") != BROKER_PROTOCOL_VERSION
            or not self._worker_id(request_id)
            or not self._worker_id(call_id)
            or tool_id not in {
                "file/stat", "file/read_text", "file/glob", "file/grep",
                "file/edit", "file/write", "exec/run", "extension/call", "skill/run",
                "document/extract_text",
                "agent/delegate",
            }
            or spec is None
            or validated is None
            or record.pending_tools.get(str(call_id)) != tool_id
            or call_id not in record.started_tools
            or record.pending_tool_arguments.get(str(call_id)) != validated
            or record.broker_pending_tool_calls.get(str(call_id))
            != (tool_id, validated)
            or call_id in record.brokered_capability_calls
            or self.capability_broker is None
            or self.file_read_executor is None
            or self.file_search_executor is None
            or self.file_mutation_executor is None
            or self.command_executor is None
            or self.capsule is None
        ):
            raise ValueError("Worker capability request is invalid")
        assert isinstance(request_id, str)
        assert isinstance(call_id, str)
        assert isinstance(tool_id, str)
        record.brokered_capability_calls.add(call_id)
        now_milliseconds = int(time.time() * 1000)
        capability_arguments: object = validated
        executor = None
        if tool_id in {"file/edit", "file/write"}:
            try:
                capability_arguments, capability_preview = (
                    self.file_mutation_executor.prepare(
                        tool_id,
                        validated,
                        record.file_read_receipts,
                    )
                )
            except FileWriteError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "File mutation preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="File mutation preparation failed closed.",
                )
                return
            executor = self.file_mutation_executor
        elif tool_id == "exec/run":
            try:
                capability_arguments, capability_preview, executor = (
                    self.command_executor.prepare(
                        validated,
                        self.capsule.runtime_root / "runs" / record.run_id,
                    )
                )
            except CommandExecutionError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "Command preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="Command preparation failed closed.",
                )
                return
        elif tool_id == "extension/call":
            if self.extension_executor is None:
                raise RuntimeError("extension executor is unavailable")
            try:
                capability_arguments, capability_preview, executor = (
                    self.extension_executor.prepare(validated)
                )
            except ExtensionError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "Extension preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="Extension preparation failed closed.",
                )
                return
        elif tool_id == "skill/run":
            if self.skill_executor is None:
                raise RuntimeError("Skill executor is unavailable")
            try:
                capability_arguments, capability_preview, executor = (
                    self.skill_executor.prepare(
                        validated,
                        self.capsule.runtime_root / "runs" / record.run_id,
                    )
                )
            except SkillError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "Skill preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="Skill preparation failed closed.",
                )
                return
        elif tool_id == "document/extract_text":
            if self.research_executor is None:
                raise RuntimeError("research document executor is unavailable")
            try:
                capability_arguments, capability_preview, executor = (
                    self.research_executor.prepare(
                        validated,
                        self.capsule.runtime_root / "runs" / record.run_id,
                    )
                )
            except ResearchEnvironmentError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "Document extraction preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="Document extraction preparation failed closed.",
                )
                return
        elif tool_id == "agent/delegate":
            if self.subagent_coordinator is None:
                raise RuntimeError("subagent coordinator is unavailable")
            try:
                capability_arguments, capability_preview, executor = (
                    self.subagent_coordinator.prepare(
                        self,
                        TaskParentIdentity(
                            agent_id=record.agent_id,
                            conversation_id=record.conversation_id,
                            turn_id=record.turn_id,
                            run_id=record.run_id,
                        ),
                        validated,
                    )
                )
            except SubagentError:
                record.brokered_capability_results[call_id] = (
                    "failed",
                    "Subagent delegation preparation failed closed.",
                )
                await self._write_capability_response(
                    stream,
                    request_id=request_id,
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome="failed",
                    content="Subagent delegation preparation failed closed.",
                )
                return
        else:
            target_preview = json.dumps(
                str(validated.get("path", validated.get("pattern", ""))),
                ensure_ascii=True,
            )
            capability_preview = f"Use {tool_id} in workspace target {target_preview}"
        runtime_snapshot = record.runtime_snapshot
        if runtime_snapshot is None:
            raise RuntimeError("Run ToolSet snapshot is unavailable")
        capability_request = CapabilityRequest.create(
            agent_id=record.agent_id,
            capsule_generation=self.capsule.generation,
            conversation_id=record.conversation_id,
            run_id=record.run_id,
            call_id=call_id,
            capability_id=tool_id,
            toolset_digest=runtime_snapshot.context_plan.reference.toolset_digest,
            policy_digest=self.capability_policy.digest,
            arguments=capability_arguments,
            preview=capability_preview,
            expires_at_milliseconds=now_milliseconds + 30_000,
            now_milliseconds=now_milliseconds,
        )
        try:
            permission = await asyncio.to_thread(
                self.capability_broker.request,
                capability_request,
                turn_id=record.turn_id,
                interactive=tool_id in {
                    "agent/delegate", "file/edit", "file/write", "exec/run", "extension/call", "skill/run"
                },
            )
            while permission.status == "pending":
                if (
                    record.cancel_requested
                    or self._closing
                    or int(time.time() * 1000)
                    >= permission.expires_at_milliseconds
                    or (
                        record.deadline_at is not None
                        and asyncio.get_running_loop().time() >= record.deadline_at
                    )
                ):
                    await asyncio.to_thread(
                        self.conversations.expire_pending_permissions,
                        int(time.time() * 1000),
                    )
                    permission = await asyncio.to_thread(
                        self.conversations.get_permission_request,
                        permission.permission_id,
                    )
                    break
                await asyncio.sleep(0.05)
                permission = await asyncio.to_thread(
                    self.conversations.get_permission_request,
                    permission.permission_id,
                )
            if executor is None:
                executor = (
                    self.file_search_executor
                    if tool_id in {"file/glob", "file/grep"}
                    else self.file_read_executor
                )
            execution = await asyncio.to_thread(
                self.capability_broker.execute,
                permission.permission_id,
                capability_request,
                executor,
                turn_id=record.turn_id,
                cancelled=lambda: record.cancel_requested,
            )
            if execution.status == "succeeded" and execution.result is not None:
                outcome = "succeeded"
                content = spec.validate_result(execution.result)
                if tool_id == "file/read_text":
                    try:
                        result_value = json.loads(content)
                        receipt = FullReadReceipt.from_result(result_value)
                    except (json.JSONDecodeError, FileWriteError):
                        pass
                    else:
                        record.file_read_receipts[receipt.path] = receipt
            elif execution.status == "cancelled" or record.cancel_requested:
                outcome = "cancelled"
                content = "cancelled"
            elif execution.status == "outcome_unknown":
                outcome = "failed"
                content = "Capability outcome is unknown; it was not replayed."
            else:
                outcome = "failed"
                content = "Capability failed closed."
        except ConversationConflictError:
            outcome = "cancelled" if record.cancel_requested else "failed"
            content = "cancelled" if record.cancel_requested else "Capability failed closed."
        record.brokered_capability_results[call_id] = (outcome, content)
        await self._write_capability_response(
            stream,
            request_id=request_id,
            call_id=call_id,
            tool_id=tool_id,
            outcome=outcome,
            content=content,
        )

    async def _activate_overflow_recovery(
        self,
        *,
        record: RunRecord,
        session: OllamaRunSession,
        iteration: int,
        recovery_id: str,
        overflow_code: str,
    ) -> ContextPlan:
        """Install the sole pre-admitted recovery projection for one Run."""

        admission = record.context_plan
        current = record.active_context_plan or admission
        recovery = record.recovery_context_plan
        runtime = record.active_runtime_snapshot or record.runtime_snapshot
        if (
            record.overflow_recovery_count != 0
            or admission is None
            or current is None
            or recovery is None
            or runtime is None
            or overflow_code not in {
                "model_context_overflow",
                "model_media_overflow",
            }
            or record.cancel_requested
            or (
                record.deadline_at is not None
                and asyncio.get_running_loop().time() >= record.deadline_at
            )
        ):
            raise OllamaBrokerError(
                "model_recovery_unavailable",
                "A bounded overflow recovery projection is unavailable.",
            )
        effective_toolset = EffectiveToolSet(
            specs=recovery.tools,
            catalog_digest=runtime.tool_catalog_digest,
            policy_digest=runtime.tool_policy_digest,
            toolset_digest=recovery.reference.toolset_digest,
        )
        recovery_runtime = TurnRuntimeSnapshot.create(
            context_plan=recovery,
            loop_limits=runtime.loop_limits,
            wall_timeout_seconds=runtime.wall_timeout_seconds,
            effective_toolset=effective_toolset,
            projection_reason="overflow_recovery",
        )
        if self.conversations is None:
            raise RuntimeError("conversation store is unavailable")
        previous_boundary = await asyncio.to_thread(
            self.conversations.read_context_projection_boundary,
            record.run_id,
        )
        if previous_boundary is None:
            raise RuntimeError("context projection boundary is unavailable")
        recovery_boundary = ContextProjectionBoundary.create(
            recovery_runtime,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            conversation_revision=record.conversation_revision or 0,
        )
        session.install_recovery_context(recovery)
        await self._publish(
            record,
            "model.recovery.started",
            "durable",
            {
                "recovery_id": recovery_id,
                "iteration": iteration,
                "attempt": 1,
                "overflow_code": overflow_code,
                "from_context_plan_id": current.reference.plan_id,
                "from_context_plan_digest": current.reference.digest,
                "to_context_plan_id": recovery.reference.plan_id,
                "to_context_plan_digest": recovery.reference.digest,
                "boundary_digest": recovery_boundary.boundary_digest,
            },
            context_recovery=(
                recovery_boundary,
                previous_boundary.boundary_digest,
            ),
        )
        record.active_context_plan = recovery
        record.active_runtime_snapshot = recovery_runtime
        record.overflow_recovery_count = 1
        record.model_usage["complete"] = False
        return recovery

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

        recovery_id = hashlib.sha256(
            b"agent-builder-overflow-recovery-v1\0"
            + record.run_id.encode("ascii")
            + b"\0"
            + str(expected_iteration).encode("ascii")
        ).hexdigest()[:32]
        active_context_plan = record.active_context_plan or context_plan

        for attempt in range(2):
            request_started = False
            response_finished = False
            transport_attempt_states: dict[int, str] = {}
            provider_call_index = record.provider_call_count + 1
            attempt_request_id = (
                request_id if attempt == 0 else f"{request_id}-recovery-1"
            )

            async def observe_request(metadata: OllamaRequestMetadata) -> None:
                nonlocal request_started
                runtime_snapshot = record.runtime_snapshot
                active_tools_by_id = {
                    spec.tool_id: spec for spec in active_context_plan.tools
                }
                metadata_tool_ids = (
                    metadata.tool_ids
                    if isinstance(metadata, OllamaRequestMetadata)
                    else ()
                )
                selected_tools = tuple(
                    active_tools_by_id[tool_id]
                    for tool_id in metadata_tool_ids
                    if tool_id in active_tools_by_id
                )
                if (
                    not isinstance(metadata, OllamaRequestMetadata)
                    or request_started
                    or runtime_snapshot is None
                    or metadata.iteration != expected_iteration
                    or not 1 <= metadata.message_count <= 256
                    or not isinstance(metadata.tool_ids, tuple)
                    or metadata.tool_count != len(metadata.tool_ids)
                    or len(set(metadata.tool_ids)) != len(metadata.tool_ids)
                    or tuple(
                        spec.tool_id
                        for spec in active_context_plan.tools
                        if spec.tool_id in set(metadata.tool_ids)
                    )
                    != metadata.tool_ids
                    or len(selected_tools) != metadata.tool_count
                    or metadata.toolset_digest != toolset_digest(selected_tools)
                    or not 1
                    <= metadata.estimated_input_tokens
                    <= active_context_plan.policy.hard_input_tokens
                    or not 1
                    <= metadata.request_bytes
                    <= active_context_plan.model_profile.request_byte_budget
                    or re.fullmatch(r"[a-f0-9]{64}", metadata.request_digest) is None
                ):
                    raise RuntimeError("trusted model request metadata is invalid")
                await self._publish(
                    record,
                    "model.request.started",
                    "durable",
                    {
                        "request_id": attempt_request_id,
                        "iteration": expected_iteration,
                        "attempt": attempt,
                        "recovery_id": recovery_id if attempt else None,
                        "provider_call_index": provider_call_index,
                        "context_plan_id": active_context_plan.reference.plan_id,
                        "context_plan_digest": active_context_plan.reference.digest,
                        "request_digest": metadata.request_digest,
                        "request_bytes": metadata.request_bytes,
                        "estimated_input_tokens": metadata.estimated_input_tokens,
                        "message_count": metadata.message_count,
                        "tool_count": metadata.tool_count,
                        "tool_result_call_ids": list(trusted_result_call_ids),
                    },
                    provider_usage_start={
                        "call_index": provider_call_index,
                        "provider": active_context_plan.model_profile.provider,
                        "model": active_context_plan.model_profile.model,
                        "profile_digest": profile_digest,
                        "context_plan_id": active_context_plan.reference.plan_id,
                        "toolset_digest": metadata.toolset_digest,
                        "estimated_input_tokens": metadata.estimated_input_tokens,
                        "hard_input_tokens": active_context_plan.policy.hard_input_tokens,
                    },
                )
                request_started = True
                record.provider_call_count = provider_call_index
                record.model_request_count = expected_iteration
                record.broker_stop_iteration = None
                record.model_failure = None
                record.model_usage["complete"] = False

            async def observe_transport_attempt(
                observation: OllamaTransportAttempt,
            ) -> None:
                if (
                    not isinstance(observation, OllamaTransportAttempt)
                    or not request_started
                    or response_finished
                    or not 1 <= observation.attempt <= observation.max_attempts <= 4
                    or observation.phase not in {
                        "attempt_started", "attempt_finished"
                    }
                    or not 0 <= observation.elapsed_ms <= 3_600_000
                    or (
                        observation.first_frame_ms is not None
                        and not 0 <= observation.first_frame_ms <= observation.elapsed_ms
                    )
                ):
                    raise RuntimeError("trusted transport attempt metadata is invalid")
                previous = transport_attempt_states.get(observation.attempt)
                if observation.phase == "attempt_started":
                    if (
                        previous is not None
                        or observation.outcome is not None
                        or observation.elapsed_ms != 0
                        or observation.first_frame_ms is not None
                        or observation.attempt != len(transport_attempt_states) + 1
                    ):
                        raise RuntimeError("transport attempt start is out of sequence")
                elif (
                    previous != "attempt_started"
                    or observation.outcome not in {
                        "first_frame_received",
                        "first_frame_timeout",
                        "transport_timeout",
                        "turn_deadline",
                        "unavailable",
                        "rejected",
                        "cancelled",
                        "failed_before_first_frame",
                    }
                    or (
                        observation.outcome == "first_frame_received"
                        and observation.first_frame_ms != observation.elapsed_ms
                    )
                    or (
                        observation.outcome != "first_frame_received"
                        and observation.first_frame_ms is not None
                    )
                ):
                    raise RuntimeError("transport attempt finish is out of sequence")
                await self._publish(
                    record,
                    "model.transport.attempt",
                    "durable",
                    {
                        "version": "provider-transport-attempt-v1",
                        "request_id": attempt_request_id,
                        "iteration": expected_iteration,
                        "provider_call_index": provider_call_index,
                        "attempt": observation.attempt,
                        "max_attempts": observation.max_attempts,
                        "phase": observation.phase,
                        "outcome": observation.outcome,
                        "elapsed_ms": observation.elapsed_ms,
                        "first_frame_ms": observation.first_frame_ms,
                    },
                )
                transport_attempt_states[observation.attempt] = observation.phase

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
                        "request_id": attempt_request_id,
                        "iteration": expected_iteration,
                        "attempt": attempt,
                        "recovery_id": recovery_id if attempt else None,
                        "provider_call_index": provider_call_index,
                        "outcome": outcome,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "usage_complete": usage_complete,
                        "error_code": error_code,
                    },
                    provider_usage_complete={
                        "call_index": provider_call_index,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
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
                    observe_transport_attempt,
                )
                async for frame in model_frames:
                    if frame.kind == "content":
                        if set(frame.payload) != {"text"}:
                            raise OllamaBrokerError(
                                "model_protocol_error",
                                "Invalid normalized content frame.",
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
                                "model_protocol_error",
                                "Invalid normalized Tool frame.",
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
                                "model_protocol_error",
                                "Invalid normalized Tool call.",
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
                                "model_protocol_error",
                                "Invalid normalized Tool call.",
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
                                "model_protocol_error",
                                "Invalid normalized stop frame.",
                            )
                        stop_reason = frame.payload.get("reason")
                        if stop_reason not in {"end_turn", "max_output"}:
                            raise OllamaBrokerError(
                                "model_protocol_error",
                                "Invalid normalized stop reason.",
                            )
                        prompt_tokens, output_tokens = self._validated_model_usage(
                            record, frame.payload["usage"]
                        )
                        assistant_content = "".join(trusted_content)
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
                        if not assistant_content.strip():
                            raise OllamaBrokerError(
                                "model_empty_response",
                                "The model returned no visible assistant content.",
                                retryable=True,
                            )
                        record.broker_stop_iteration = expected_iteration
                        record.final_assistant_content = assistant_content
                        saw_terminal_frame = True
                        await self._write_model_response(
                            stream,
                            request_id,
                            "stop",
                            reason=stop_reason,
                        )
                    else:
                        raise OllamaBrokerError(
                            "model_protocol_error",
                            "Unknown normalized model frame.",
                        )
                if not saw_terminal_frame:
                    raise OllamaBrokerError(
                        "model_protocol_error", "Model stream had no terminal frame."
                    )
                return
            except (OllamaCancelledError, OllamaBrokerError) as exc:
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
                can_recover = (
                    attempt == 0
                    and not trusted_content
                    and exc.code
                    in {"model_context_overflow", "model_media_overflow"}
                )
                if can_recover:
                    try:
                        active_context_plan = await self._activate_overflow_recovery(
                            record=record,
                            session=session,
                            iteration=expected_iteration,
                            recovery_id=recovery_id,
                            overflow_code=exc.code,
                        )
                    except (OllamaBrokerError, RuntimeError):
                        can_recover = False
                if can_recover:
                    continue
                record.model_failure = (exc.code, exc.retryable)
                await self._write_model_response(
                    stream,
                    request_id,
                    "error",
                    code=exc.code,
                )
                return
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
        runtime_snapshot = record.runtime_snapshot
        if (
            input_total > 1_000_000_000
            or output_total > 1_000_000_000
            or runtime_snapshot is None
            or input_total > runtime_snapshot.max_total_input_tokens
            or output_total > runtime_snapshot.max_total_output_tokens
        ):
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
        record.model_usage["complete"] = record.overflow_recovery_count == 0

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
                    if record.runtime_snapshot is None:
                        raise RuntimeError("Run lost its trusted runtime snapshot")
                    model_session = self.model_broker.new_run(
                        record.context_plan,
                        max_tool_calls=record.runtime_snapshot.loop_limits.max_tool_calls,
                    )
                    diagnostic_stage = "command_write"
                    process.stdin.write(
                        json.dumps(
                            {
                                "message": message,
                                "context_plan": record.context_plan.reference.to_dict(),
                                "loop_limits": (
                                    record.runtime_snapshot.loop_limits.to_dict()
                                ),
                                "effective_tool_ids": [
                                    spec.tool_id for spec in record.effective_tools
                                ],
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
                                if (
                                    record.runtime_snapshot is None
                                    or model_requests
                                    > record.runtime_snapshot.loop_limits.max_model_iterations
                                ):
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
                            if raw.get("internal") == "capability.request":
                                await self._serve_capability_request(
                                    record=record,
                                    stream=process.stdin,
                                    value=raw,
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
