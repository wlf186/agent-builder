"""Narrow command dispatch for the Harness V2 application boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import StartRunCommand
from .query_engine import QueryEngineRegistry, QueryRunHandle


RUN_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    run_id: str

    def validate(self) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise ValueError("invalid run_id")


class CommandBus:
    """Delegate validated application commands to logical QueryEngines.

    Authentication, CSRF, HTTP parsing, and response projection stay in the web
    adapter.  QueryEngine binds conversation identity while RunService remains
    the authoritative sequencer and Worker supervisor underneath it.
    """

    def __init__(self, query_engines: QueryEngineRegistry) -> None:
        self._query_engines = query_engines

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
