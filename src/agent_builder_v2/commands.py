"""Typed slash-command registry at the authenticated application boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol

from .contracts import RESOURCE_ID, StartRunCommand
from .query_engine import (
    QueryContextUnavailableError,
    QueryEngineOwnershipError,
    QueryEngineRegistry,
    QueryRunHandle,
    QueryRunNotRetainedError,
)


RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:+-]{1,128}$")
MAX_SLASH_INPUT_BYTES = 4_096
MAX_SLASH_ARGUMENTS = 4
MAX_SLASH_ARGUMENT_BYTES = 256
MAX_COMMAND_RESULT_BYTES = 64 * 1024
MAX_PERMISSION_PROJECTIONS = 6


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    run_id: str

    def validate(self) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise ValueError("invalid run_id")


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    command_id: str
    aliases: tuple[str, ...]
    description: str
    argument_schema: str
    modifies_state: bool
    feature_gate: str

    def __post_init__(self) -> None:
        if (
            _COMMAND_NAME.fullmatch(self.command_id) is None
            or not self.aliases
            or self.aliases != tuple(sorted(set(self.aliases)))
            or any(not value.startswith("/") for value in self.aliases)
            or any(len(value.encode("ascii")) > 32 for value in self.aliases)
            or not self.description
            or len(self.description.encode("utf-8")) > 512
            or len(self.argument_schema.encode("utf-8")) > 256
            or not isinstance(self.modifies_state, bool)
            or not self.feature_gate
        ):
            raise ValueError("invalid slash command specification")

    def public_metadata(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "name": f"/{self.command_id}",
            "aliases": list(self.aliases),
            "description": self.description,
            "argument_schema": self.argument_schema,
            "modifies_state": self.modifies_state,
            "feature_gate": self.feature_gate,
            "availability": "available",
        }


COMMAND_SPECS = (
    SlashCommandSpec(
        "cancel", ("/cancel", "/stop"), "Cancel the active or named Run.",
        "[run_id]", True, "cancel-v1",
    ),
    SlashCommandSpec(
        "clear", ("/clear",), "Delete this idle Conversation and its retained state.",
        "", True, "conversation-delete-v1",
    ),
    SlashCommandSpec(
        "compact", ("/compact",), "Compact the next ordinary Turn using the trusted context pipeline.",
        "", False, "semantic-summary-v1",
    ),
    SlashCommandSpec(
        "context", ("/context", "/ctx"), "Inspect bounded context metadata for the latest or named Run.",
        "[run_id]", False, "context-inspection-v1",
    ),
    SlashCommandSpec(
        "model", ("/model",), "List trusted models or select one for the next ordinary Turn.",
        "[model_id]", False, "model-catalog-v1",
    ),
    SlashCommandSpec(
        "permissions", ("/permissions", "/perms"), "List bounded pending permission previews for this Conversation.",
        "", False, "capability-permission-v1",
    ),
    SlashCommandSpec(
        "status", ("/status",), "Show current Conversation and Run status without invoking the model.",
        "", False, "conversation-status-v1",
    ),
)


class CommandControlServices(Protocol):
    async def list_permission_requests(self, *, pending_only: bool = True) -> tuple[object, ...]: ...


class TrustedModelCatalog(Protocol):
    def public_metadata(self) -> dict[str, object]: ...

    def select(self, model_id: str | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class ParsedSlashCommand:
    spec: SlashCommandSpec
    arguments: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class SlashCommandResult:
    command_id: str
    modifies_state: bool
    result: dict[str, object]
    ui_effect: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema_version": 1,
            "kind": "slash_command_result",
            "command_id": self.command_id,
            "modifies_state": self.modifies_state,
            "result": self.result,
            "ui_effect": self.ui_effect,
            "model_invoked": False,
            "turn_created": False,
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_COMMAND_RESULT_BYTES:
            raise RuntimeError("slash command result exceeded its byte limit")
        return value


class SlashCommandRegistry:
    def __init__(self, specs: tuple[SlashCommandSpec, ...] = COMMAND_SPECS) -> None:
        if tuple(sorted(specs, key=lambda item: item.command_id)) != specs:
            raise ValueError("slash command registry must be stably ordered")
        aliases: dict[str, SlashCommandSpec] = {}
        for spec in specs:
            for alias in spec.aliases:
                if alias in aliases:
                    raise ValueError("duplicate slash command alias")
                aliases[alias] = spec
        self._specs = specs
        self._aliases = aliases

    def public_metadata(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "commands": [spec.public_metadata() for spec in self._specs],
        }

    def parse(self, value: str) -> ParsedSlashCommand | None:
        if not isinstance(value, str):
            raise ValueError("slash command input must be text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("slash command input is not valid UTF-8") from exc
        if len(encoded) > MAX_SLASH_INPUT_BYTES:
            raise ValueError("slash command input exceeds its byte limit")
        stripped = value.strip()
        if not stripped.startswith("/"):
            return None
        if any(character in stripped for character in ("\x00", "\r", "\n", "\t")):
            raise ValueError("slash command contains a forbidden control character")
        tokens = stripped.split(" ")
        if any(not token for token in tokens) or len(tokens) > MAX_SLASH_ARGUMENTS + 1:
            raise ValueError("slash command has invalid spacing or too many arguments")
        if any(len(token.encode("utf-8")) > MAX_SLASH_ARGUMENT_BYTES for token in tokens[1:]):
            raise ValueError("slash command argument exceeds its byte limit")
        spec = self._aliases.get(tokens[0].lower())
        if spec is None:
            raise ValueError("unknown slash command")
        arguments = tuple(tokens[1:])
        maximum = 1 if spec.command_id in {"cancel", "context", "model"} else 0
        if len(arguments) > maximum:
            raise ValueError("slash command has invalid arguments")
        return ParsedSlashCommand(spec, arguments, stripped)


class CommandBus:
    """Delegate typed application commands without creating a second state owner."""

    def __init__(
        self,
        query_engines: QueryEngineRegistry,
        *,
        services: CommandControlServices | None = None,
        model_catalog: TrustedModelCatalog | None = None,
    ) -> None:
        self._query_engines = query_engines
        self._services = services
        self._model_catalog = model_catalog
        self.registry = SlashCommandRegistry()

    async def start(self, command: StartRunCommand) -> QueryRunHandle:
        if not isinstance(command, StartRunCommand):
            raise TypeError("start requires StartRunCommand")
        command.validate()
        return await self._query_engines.submit(command)

    async def cancel(self, run_id: str) -> None:
        command = CancelRunCommand(run_id)
        command.validate()
        await self._query_engines.cancel(command.run_id)

    async def dispatch(
        self, command: StartRunCommand | CancelRunCommand
    ) -> QueryRunHandle | None:
        if isinstance(command, StartRunCommand):
            return await self.start(command)
        if isinstance(command, CancelRunCommand):
            command.validate()
            await self._query_engines.cancel(command.run_id)
            return None
        raise TypeError("unsupported command")

    async def _owned_run(self, conversation_id: str, run_id: str) -> QueryRunHandle:
        if RESOURCE_ID.fullmatch(run_id) is None:
            raise ValueError("invalid run_id")
        handle = await self._query_engines.resolve_run_identity(run_id)
        if handle.conversation_id != conversation_id:
            raise QueryEngineOwnershipError("run not found")
        return handle

    async def execute_slash(
        self, conversation_id: str, value: str
    ) -> SlashCommandResult:
        if RESOURCE_ID.fullmatch(conversation_id) is None:
            raise ValueError("invalid conversation_id")
        parsed = self.registry.parse(value)
        if parsed is None:
            raise ValueError("input is not a slash command")
        conversation = await self._query_engines.get_conversation(conversation_id)
        spec = parsed.spec
        result: dict[str, object]
        ui_effect: dict[str, object] = {}

        if spec.command_id == "status":
            result = {
                "conversation_id": conversation.conversation_id,
                "revision": conversation.revision,
                "active_run_id": conversation.active_run_id,
                "turn_count": len(conversation.turns),
                "completed_turn_count": sum(
                    turn.status == "completed" for turn in conversation.turns
                ),
                "last_run_id": conversation.turns[-1].run_id if conversation.turns else None,
            }
        elif spec.command_id == "context":
            run_id = parsed.arguments[0] if parsed.arguments else (
                conversation.turns[-1].run_id if conversation.turns else None
            )
            if run_id is None:
                raise ValueError("conversation has no Run context")
            await self._owned_run(conversation_id, run_id)
            try:
                inspection = await self._query_engines.inspect_retained_context(run_id)
            except (QueryRunNotRetainedError, QueryContextUnavailableError) as exc:
                raise ValueError("Run context is no longer retained") from exc
            result = inspection.to_dict()
        elif spec.command_id == "model":
            if self._model_catalog is None:
                raise RuntimeError("model catalog is unavailable")
            metadata = self._model_catalog.public_metadata()
            selected = parsed.arguments[0] if parsed.arguments else None
            if selected is not None:
                if _MODEL_ID.fullmatch(selected) is None:
                    raise ValueError("invalid model_id")
                self._model_catalog.select(selected)
                ui_effect = {"next_turn_model_id": selected}
            result = {**metadata, "selected_for_next_turn": selected}
        elif spec.command_id == "compact":
            if conversation.active_run_id is not None:
                raise ValueError("conversation has an active Run")
            result = {"compact_next_turn": True}
            ui_effect = {"compact_next_turn": True}
        elif spec.command_id == "permissions":
            if self._services is None:
                raise RuntimeError("permission service is unavailable")
            records = await self._services.list_permission_requests(pending_only=True)
            matching = [
                item for item in records
                if getattr(item, "conversation_id", None) == conversation_id
            ]
            projected = []
            for item in matching[:MAX_PERMISSION_PROJECTIONS]:
                projected.append(
                    {
                        "permission_id": item.permission_id,
                        "run_id": item.run_id,
                        "call_id": item.call_id,
                        "capability_id": item.capability_id,
                        "preview": item.preview,
                        "preview_digest": item.preview_digest,
                        "status": item.status,
                        "expires_at_milliseconds": item.expires_at_milliseconds,
                    }
                )
            result = {
                "permissions": projected,
                "total_pending": len(matching),
                "truncated": len(matching) > MAX_PERMISSION_PROJECTIONS,
            }
        elif spec.command_id == "cancel":
            run_id = parsed.arguments[0] if parsed.arguments else conversation.active_run_id
            if run_id is None:
                result = {"cancelled": False, "run_id": None}
            else:
                await self._owned_run(conversation_id, run_id)
                await self.cancel(run_id)
                result = {"cancelled": True, "run_id": run_id}
        elif spec.command_id == "clear":
            if conversation.active_run_id is not None:
                raise ValueError("conversation has an active Run")
            deletion = await self._query_engines.delete_conversation(conversation_id)
            if not deletion.deleted:
                raise QueryEngineOwnershipError("conversation not found")
            result = {
                "deleted": True,
                "deleted_turns": deletion.deleted_turns,
                "deleted_events": deletion.deleted_events,
            }
            ui_effect = {"conversation_deleted": True}
        else:
            raise RuntimeError("slash command registry drifted")
        return SlashCommandResult(spec.command_id, spec.modifies_state, result, ui_effect)


__all__ = [
    "COMMAND_SPECS",
    "CancelRunCommand",
    "CommandBus",
    "ParsedSlashCommand",
    "SlashCommandRegistry",
    "SlashCommandResult",
    "SlashCommandSpec",
]
