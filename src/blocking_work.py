"""Cancellation-safe bridge for bounded synchronous application work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar


Result = TypeVar("Result")


async def run_blocking_with_semaphore(
    semaphore: asyncio.Semaphore,
    function: Callable[..., Result],
    *args: Any,
) -> Result:
    """Run ``function`` without releasing capacity before its thread exits.

    Cancelling an ``asyncio.to_thread`` Future does not stop the Python thread.
    This helper drains that real worker before it propagates cancellation, so
    request timeouts and disconnects cannot accumulate untracked writers.
    """

    async with semaphore:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                if worker.done():
                    break
                continue
            except Exception:
                if cancellation is None:
                    raise
                # A caller cancellation remains the public outcome even when
                # the drained synchronous transaction subsequently fails.
                try:
                    worker.result()
                except Exception:
                    pass
                raise cancellation
        if cancellation is not None:
            try:
                worker.result()
            except Exception:
                pass
            raise cancellation
        return result
