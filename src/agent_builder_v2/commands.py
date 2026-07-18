"""Narrow command dispatch for the Harness V2 application boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import StartRunCommand
from .control import RunRecord, RunService


RUN_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    run_id: str

    def validate(self) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise ValueError("invalid run_id")


class CommandBus:
    """Delegate validated application commands to the authoritative RunService.

    Authentication, CSRF, HTTP parsing, and response projection stay in the web
    adapter.  The bus does not duplicate Run state or introduce another loop.
    """

    def __init__(self, run_service: RunService) -> None:
        self._run_service = run_service

    async def start(self, command: StartRunCommand) -> RunRecord:
        if not isinstance(command, StartRunCommand):
            raise TypeError("start requires StartRunCommand")
        command.validate()
        return await self._run_service.start(command)

    async def cancel(self, run_id: str) -> None:
        command = CancelRunCommand(run_id)
        command.validate()
        await self._run_service.cancel(command.run_id)

    async def dispatch(
        self, command: StartRunCommand | CancelRunCommand
    ) -> RunRecord | None:
        if isinstance(command, StartRunCommand):
            return await self.start(command)
        if isinstance(command, CancelRunCommand):
            command.validate()
            await self._run_service.cancel(command.run_id)
            return None
        raise TypeError("unsupported command")
