"""Regression tests for cancellation-safe bounded worker threads."""

from __future__ import annotations

import asyncio
import threading

import pytest

from src.blocking_work import run_blocking_with_semaphore


@pytest.mark.asyncio
async def test_cancelled_work_holds_capacity_until_the_real_thread_stops() -> None:
    semaphore = asyncio.Semaphore(1)
    started = threading.Event()
    release = threading.Event()

    def blocking_mutation() -> str:
        started.set()
        release.wait(timeout=5)
        return "committed"

    task = asyncio.create_task(
        run_blocking_with_semaphore(semaphore, blocking_mutation)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    task.cancel()
    await asyncio.sleep(0)
    assert semaphore.locked()
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_worker_exception_is_propagated_and_capacity_is_released() -> None:
    semaphore = asyncio.Semaphore(1)

    def fail() -> None:
        raise RuntimeError("intentional")

    with pytest.raises(RuntimeError, match="intentional"):
        await run_blocking_with_semaphore(semaphore, fail)
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_cancellation_is_not_replaced_by_a_late_worker_failure() -> None:
    semaphore = asyncio.Semaphore(1)
    started = threading.Event()
    release = threading.Event()

    def fail_after_cancel() -> None:
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("late failure")

    task = asyncio.create_task(
        run_blocking_with_semaphore(semaphore, fail_after_cancel)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not semaphore.locked()
