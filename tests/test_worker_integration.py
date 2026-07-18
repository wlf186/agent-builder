"""Real one-process-per-Run vertical slice without network or legacy services."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.contracts import TERMINAL_KINDS, StartRunCommand
from agent_builder_v2.control import RunService
from agent_builder_v2.ollama import (
    OllamaFrame,
    OllamaQualification,
    OllamaToolResult,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class _FakeModelSession:
    async def stream_turn(
        self,
        user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
    ) -> object:
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "real-broker-call",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": user_message},
                },
            )
            return
        assert tool_results == (
            OllamaToolResult(
                call_id="real-broker-call",
                tool_id="builtin/echo",
                content=user_message,
                outcome="succeeded",
            ),
        )
        yield OllamaFrame("content", {"text": f"broker result: {user_message}"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 10, "eval_count": 4},
            },
        )


class _FakeModelBroker:
    def __init__(self) -> None:
        self.qualification = OllamaQualification(
            version="test",
            model="qwen3.5:2b",
            digest="a" * 64,
            size=1,
            address="10.89.0.18",
        )

    async def start(self) -> OllamaQualification:
        return self.qualification

    def new_run(self) -> _FakeModelSession:
        return _FakeModelSession()

    async def close(self) -> None:
        return None


def test_control_plane_runs_and_cleans_agent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_launch = asyncio.create_subprocess_exec
    launch_options: list[dict[str, object]] = []

    async def capture_launch(*args: object, **kwargs: object) -> object:
        launch_options.append(dict(kwargs))
        return await original_launch(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)

    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            assert service.capsule is not None
            assert service.capsule.interpreter.is_file()
            assert service.capsule.interpreter.is_relative_to(
                service.capsule.runtime_root
            )

            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "real process integration")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[0].kind == "run.started"
            assert events[-1].kind == "run.completed"
            assert [event.seq for event in events] == list(
                range(1, len(events) + 1)
            )
            assert sum(event.kind in TERMINAL_KINDS for event in events) == 1
            requested = {
                event.payload["call_id"]
                for event in events
                if event.kind == "tool.call.requested"
            }
            finished = {
                event.payload["call_id"]
                for event in events
                if event.kind == "tool.call.finished"
            }
            assert requested == finished == {"real-broker-call"}
            assert record.process is None
            assert not (
                service.capsule.runtime_root / "runs" / record.run_id
            ).exists()
            assert len(launch_options) == 1
            assert launch_options[0]["start_new_session"] is True
            assert "preexec_fn" not in launch_options[0]
            worker_environment = launch_options[0]["env"]
            assert isinstance(worker_environment, dict)
            assert not any("OLLAMA" in str(key) for key in worker_environment)
            assert events[0].payload["model"] == "qwen3.5:2b"
            assert events[0].payload["sandbox"] == "harness-v2-worker-v1"
        finally:
            await service.close()

    asyncio.run(exercise())
