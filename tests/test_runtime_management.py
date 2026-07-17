"""Focused regression tests for project-local runtime management."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from src.environment_creator import EnvironmentCreator
from src.environment_manager import EnvironmentError, EnvironmentManager
from src.execution_engine import ExecutionEngine
from src.models import AgentEnvironment, EnvironmentStatus, EnvironmentType


class EnvironmentManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.manager = EnvironmentManager(root / "data", root / "environments")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare_ready_environment(self, agent_name: str) -> None:
        env_path = self.manager.get_env_path(agent_name)
        python_path = self.manager._environment_python(env_path)
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.symlink_to(sys.executable)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name=agent_name,
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )

    def test_agent_paths_are_contained_and_collision_resistant(self) -> None:
        first = self.manager.get_env_path("../../outside")
        second = self.manager.get_env_path("..\\..\\outside")
        first.resolve().relative_to(self.manager.environments_dir)
        second.resolve().relative_to(self.manager.environments_dir)
        self.assertNotEqual(first, second)

    def test_constructor_rejects_symlinked_managed_roots(self) -> None:
        target = Path(self.temporary.name) / "real-environments"
        target.mkdir()
        linked = Path(self.temporary.name) / "linked-environments"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(EnvironmentError, "不能是软链接"):
            EnvironmentManager(Path(self.temporary.name) / "data-2", linked)

    def test_package_specs_reject_options_urls_and_paths(self) -> None:
        self.assertEqual(
            self.manager._validate_packages(["httpx>=0.27", "Pillow==10.2.0"]),
            ["httpx>=0.27", "Pillow==10.2.0"],
        )
        for unsafe in (
            "--index-url=https://example.invalid/simple",
            "https://example.invalid/package.whl",
            "../local-package",
            "package @ https://example.invalid/package.whl",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(EnvironmentError):
                self.manager._validate_packages([unsafe])
        with self.assertRaises(EnvironmentError):
            self.manager._validate_packages(["unapproved-package==1.0"])

    async def test_execution_uses_environment_python_and_bounds_output(self) -> None:
        env_path = self.manager.get_env_path("demo")
        python_path = self.manager._environment_python(env_path)
        python_path.parent.mkdir(parents=True)
        python_path.symlink_to(sys.executable)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name="demo",
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )
        old_limit = self.manager.MAX_CAPTURE_BYTES
        self.manager.MAX_CAPTURE_BYTES = 128
        try:
            code, stdout, stderr, _ = await self.manager.execute_in_environment(
                "demo", ["python", "-c", "print('x' * 1000)"], timeout=5
            )
        finally:
            self.manager.MAX_CAPTURE_BYTES = old_limit
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertLessEqual(len(stdout.encode()), 128)
        self.assertIn("output truncated", stdout)

    async def test_execution_process_group_count_limit_terminates_skill(self) -> None:
        self._prepare_ready_environment("process-limit")
        source = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "time.sleep(30)\n"
        )
        with (
            patch.dict(
                os.environ,
                {"AGENT_BUILDER_EXECUTION_PROCESS_LIMIT": "1"},
            ),
            self.assertRaisesRegex(EnvironmentError, "进程数量超过限制"),
        ):
            await asyncio.wait_for(
                self.manager.execute_in_environment(
                    "process-limit", ["python", "-c", source], timeout=5
                ),
                timeout=8,
            )

    async def test_execution_aggregate_rss_limit_terminates_skill(self) -> None:
        self._prepare_ready_environment("memory-limit")
        source = (
            "import time\n"
            "payload = bytearray(96 * 1024 * 1024)\n"
            "for offset in range(0, len(payload), 4096): payload[offset] = 1\n"
            "time.sleep(30)\n"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT": str(
                        64 * 1024**2
                    )
                },
            ),
            self.assertRaisesRegex(EnvironmentError, "聚合内存超过限制"),
        ):
            await asyncio.wait_for(
                self.manager.execute_in_environment(
                    "memory-limit", ["python", "-c", source], timeout=5
                ),
                timeout=8,
            )

    async def test_terminate_kills_descendant_that_ignores_term(self) -> None:
        child_source = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        leader_source = (
            "import subprocess, sys, time\n"
            f"source = {child_source!r}\n"
            "child = subprocess.Popen([sys.executable, '-c', source], "
            "stdout=subprocess.PIPE, text=True)\n"
            "assert child.stdout.readline().strip() == 'ready'\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            leader_source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        descendant_pid = int(
            (await asyncio.wait_for(process.stdout.readline(), timeout=2)).decode()
        )

        await self.manager._terminate_process(process, grace_seconds=0.5)

        self.assertIsNotNone(process.returncode)
        self.assertFalse(Path(f"/proc/{descendant_pid}").exists())

    async def test_runtime_probe_cancel_during_spawn_preserves_cancelled_error(self) -> None:
        entered_spawn = asyncio.Event()
        never = asyncio.Event()

        async def hanging_spawn(*_args, **_kwargs):
            self.assertTrue(_kwargs["start_new_session"])
            entered_spawn.set()
            await never.wait()

        with (
            patch("src.environment_manager.get_uv_path", return_value="/project/uv"),
            patch(
                "src.environment_manager.asyncio.create_subprocess_exec",
                side_effect=hanging_spawn,
            ),
        ):
            task = asyncio.create_task(self.manager.check_runtime_available())
            await asyncio.wait_for(entered_spawn.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_invalid_execution_quota_never_spawns_or_tracks_process(self) -> None:
        self._prepare_ready_environment("invalid-quota")
        quota_names = (
            "AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT",
            "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT",
            "AGENT_BUILDER_EXECUTION_MEMORY_LIMIT",
            "AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT",
            "AGENT_BUILDER_EXECUTION_FILE_LIMIT",
        )
        for quota_name in quota_names:
            with (
                self.subTest(quota_name=quota_name),
                patch.dict(os.environ, {quota_name: "not-an-integer"}),
                patch(
                    "src.environment_manager.asyncio.create_subprocess_exec",
                    new=AsyncMock(),
                ) as spawn,
            ):
                with self.assertRaisesRegex(EnvironmentError, quota_name):
                    await self.manager.execute_in_environment(
                        "invalid-quota",
                        ["python", "-c", "print('must not run')"],
                    )
                spawn.assert_not_awaited()
                self.assertEqual(self.manager._agent_processes, {})
                self.assertEqual(self.manager._running_processes, {})

    async def test_concurrent_same_agent_create_runs_uv_once(self) -> None:
        entered_uv = asyncio.Event()
        release_uv = asyncio.Event()
        calls = 0

        async def fake_uv(_args, **kwargs):
            nonlocal calls
            calls += 1
            entered_uv.set()
            await release_uv.wait()
            agent_name = kwargs["agent_name"]
            python = self.manager._environment_python(
                self.manager.get_env_path(agent_name)
            )
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            python.chmod(0o700)
            return 0, "", ""

        with patch.object(self.manager, "_run_uv_command", side_effect=fake_uv):
            first_task = asyncio.create_task(
                self.manager.create_environment("same-agent")
            )
            await asyncio.wait_for(entered_uv.wait(), timeout=1)
            second_task = asyncio.create_task(
                self.manager.create_environment("same-agent")
            )
            await asyncio.sleep(0)
            release_uv.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_task, second_task), timeout=2
            )

        self.assertEqual(calls, 1)
        self.assertEqual(first.environment_id, second.environment_id)
        self.assertEqual(first.status, EnvironmentStatus.READY)

    async def test_different_agent_lifecycle_operations_remain_concurrent(self) -> None:
        barrier = asyncio.Barrier(2)
        entered: list[str] = []

        async def fake_uv(_args, **kwargs):
            agent_name = kwargs["agent_name"]
            entered.append(agent_name)
            await barrier.wait()
            python = self.manager._environment_python(
                self.manager.get_env_path(agent_name)
            )
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            python.chmod(0o700)
            return 0, "", ""

        with patch.object(self.manager, "_run_uv_command", side_effect=fake_uv):
            first, second = await asyncio.wait_for(
                asyncio.gather(
                    self.manager.create_environment("agent-one"),
                    self.manager.create_environment("agent-two"),
                ),
                timeout=2,
            )

        self.assertCountEqual(entered, ["agent-one", "agent-two"])
        self.assertEqual(first.status, EnvironmentStatus.READY)
        self.assertEqual(second.status, EnvironmentStatus.READY)

    async def test_install_then_delete_cannot_resurrect_environment_metadata(self) -> None:
        agent_name = "install-delete"
        env_path = self.manager.get_env_path(agent_name)
        python = self.manager._environment_python(env_path)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
        python.chmod(0o700)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name=agent_name,
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )
        install_started = asyncio.Event()
        release_install = asyncio.Event()

        async def fake_uv(_args, **_kwargs):
            install_started.set()
            await release_install.wait()
            return 0, "installed", ""

        with patch.object(self.manager, "_run_uv_command", side_effect=fake_uv):
            install_task = asyncio.create_task(
                self.manager.install_packages(agent_name, ["httpx==0.28.1"])
            )
            await asyncio.wait_for(install_started.wait(), timeout=1)
            delete_task = asyncio.create_task(
                self.manager.delete_environment(agent_name)
            )
            await asyncio.sleep(0.05)
            self.assertFalse(delete_task.done())
            release_install.set()
            installed, deleted = await asyncio.wait_for(
                asyncio.gather(install_task, delete_task), timeout=2
            )

        self.assertTrue(installed[0])
        self.assertTrue(deleted)
        self.assertFalse(env_path.exists())
        self.assertFalse(self.manager.get_metadata_path(agent_name).exists())

    async def test_package_install_never_runs_source_build_hooks(self) -> None:
        agent_name = "wheel-only"
        env_path = self.manager.get_env_path(agent_name)
        python = self.manager._environment_python(env_path)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.symlink_to(sys.executable)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name=agent_name,
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )
        observed: list[str] = []

        async def fake_uv(arguments, **_kwargs):
            observed.extend(arguments)
            return 0, "installed", ""

        with patch.object(self.manager, "_run_uv_command", side_effect=fake_uv):
            success, _ = await self.manager.install_packages(
                agent_name, ["httpx==0.28.1"]
            )

        self.assertTrue(success)
        self.assertIn("--only-binary", observed)
        self.assertIn(":all:", observed)

    async def test_delete_terminates_active_agent_process_before_removing_env(self) -> None:
        agent_name = "active-delete"
        env_path = self.manager.get_env_path(agent_name)
        env_path.mkdir(parents=True)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name=agent_name,
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )

        class FakeProcess:
            returncode = None

        process = FakeProcess()
        reader_started = asyncio.Event()
        process_terminated = asyncio.Event()

        async def active_reader():
            async with self.manager._shared_agent_operation(agent_name):
                self.manager._agent_processes.setdefault(agent_name, set()).add(process)
                reader_started.set()
                await process_terminated.wait()
                self.manager._agent_processes[agent_name].discard(process)

        async def terminate(candidate):
            self.assertIs(candidate, process)
            candidate.returncode = -15
            process_terminated.set()

        reader_task = asyncio.create_task(active_reader())
        await asyncio.wait_for(reader_started.wait(), timeout=1)
        with patch.object(
            self.manager, "_terminate_process", new=AsyncMock(side_effect=terminate)
        ) as terminate_mock:
            deleted = await asyncio.wait_for(
                self.manager.delete_environment(agent_name), timeout=2
            )
        await reader_task

        self.assertTrue(deleted)
        terminate_mock.assert_awaited_once_with(process)
        self.assertFalse(env_path.exists())

    async def test_uv_command_cancel_terminates_and_untracks_child(self) -> None:
        communicate_started = asyncio.Event()
        never = asyncio.Event()

        class FakeProcess:
            returncode = None

            async def communicate(self):
                communicate_started.set()
                await never.wait()

        process = FakeProcess()

        async def terminate(candidate):
            self.assertIs(candidate, process)
            candidate.returncode = -15

        with (
            patch("src.environment_manager.get_uv_path", return_value="/project/uv"),
            patch(
                "src.environment_manager.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch.object(
                self.manager, "_terminate_process", new=AsyncMock(side_effect=terminate)
            ) as terminate_mock,
        ):
            task = asyncio.create_task(
                self.manager._run_uv_command(
                    ["venv", "target"], agent_name="cancelled-agent"
                )
            )
            await asyncio.wait_for(communicate_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        terminate_mock.assert_awaited_once_with(process)
        self.assertNotIn(process, self.manager._uv_processes)
        self.assertNotIn("cancelled-agent", self.manager._agent_uv_processes)

    async def test_cancelled_creation_does_not_leave_creating_metadata(self) -> None:
        entered_uv = asyncio.Event()
        never = asyncio.Event()

        async def fake_uv(_args, **_kwargs):
            entered_uv.set()
            await never.wait()

        with patch.object(self.manager, "_run_uv_command", side_effect=fake_uv):
            task = asyncio.create_task(
                self.manager.create_environment("cancelled-create")
            )
            await asyncio.wait_for(entered_uv.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        status = await self.manager.get_environment_status("cancelled-create")
        self.assertIsNotNone(status)
        self.assertEqual(status.status, EnvironmentStatus.ERROR)
        self.assertEqual(status.error_message, "环境创建已取消")

    async def test_oversized_environment_metadata_fails_closed(self) -> None:
        metadata = self.manager.get_metadata_path("oversized")
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_bytes(b"x" * (self.manager.MAX_METADATA_BYTES + 1))
        with self.assertRaisesRegex(EnvironmentError, "元数据超过"):
            await self.manager.get_environment_status("oversized")

    async def test_environment_creator_registers_only_one_task_per_agent(self) -> None:
        creator = EnvironmentCreator(self.manager, max_concurrent=1)
        entered_create = asyncio.Event()
        never = asyncio.Event()
        calls = 0

        async def fake_create(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered_create.set()
            await never.wait()

        with patch.object(
            self.manager, "create_environment", side_effect=fake_create
        ):
            started = await asyncio.gather(
                creator.create("one-agent"), creator.create("one-agent")
            )
            await asyncio.wait_for(entered_create.wait(), timeout=1)
            self.assertEqual(started, [True, True])
            self.assertEqual(calls, 1)
            self.assertEqual(creator.get_active_tasks(), ["one-agent"])
            self.assertTrue(await creator.cancel("one-agent"))

        self.assertEqual(creator.get_active_tasks(), [])

    async def test_per_agent_environment_and_metadata_symlinks_fail_closed(self) -> None:
        agent_name = "linked-agent"
        external_env = Path(self.temporary.name) / "external-env"
        external_env.mkdir()
        sentinel = external_env / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        env_path = self.manager.get_env_path(agent_name)
        env_path.symlink_to(external_env, target_is_directory=True)

        with self.assertRaisesRegex(EnvironmentError, "环境目录不能是软链接"):
            await self.manager.get_environment_status(agent_name)
        with self.assertRaisesRegex(EnvironmentError, "环境目录不能是软链接"):
            await self.manager.create_environment(agent_name)
        with self.assertRaisesRegex(EnvironmentError, "环境目录不能是软链接"):
            await self.manager.install_packages(agent_name, ["httpx==0.28.1"])
        with self.assertRaisesRegex(EnvironmentError, "环境目录不能是软链接"):
            await self.manager.execute_in_environment(
                agent_name, ["python", "-c", "print('unsafe')"]
            )

        self.assertTrue(await self.manager.delete_environment(agent_name))
        self.assertFalse(env_path.exists())
        self.assertTrue(sentinel.exists())

        external_metadata = Path(self.temporary.name) / "external-metadata.json"
        external_metadata.write_text('{"secret": "keep"}', encoding="utf-8")
        metadata_path = self.manager.get_metadata_path(agent_name)
        metadata_path.symlink_to(external_metadata)
        with self.assertRaisesRegex(EnvironmentError, "元数据不能是软链接"):
            await self.manager.get_environment_status(agent_name)
        self.assertTrue(await self.manager.delete_environment(agent_name))
        self.assertFalse(metadata_path.exists())
        self.assertEqual(
            external_metadata.read_text(encoding="utf-8"), '{"secret": "keep"}'
        )

    async def test_external_environment_interpreter_is_rejected(self) -> None:
        agent_name = "external-python"
        env_path = self.manager.get_env_path(agent_name)
        python = self.manager._environment_python(env_path)
        python.parent.mkdir(parents=True)
        external_python = Path("/usr/bin/python3")
        if not external_python.exists():
            self.skipTest("system Python probe is unavailable")
        python.symlink_to(external_python)
        self.manager._save_metadata(
            AgentEnvironment(
                agent_name=agent_name,
                environment_type=EnvironmentType.UV,
                status=EnvironmentStatus.READY,
                python_version="3.11",
            )
        )

        status = await self.manager.get_environment_status(agent_name)

        self.assertIsNotNone(status)
        self.assertEqual(status.status, EnvironmentStatus.ERROR)
        self.assertIn("解释器无效", status.error_message or "")

    def test_child_environments_pin_all_temporary_roots_locally(self) -> None:
        workdir = Path(self.temporary.name) / "work"
        workdir.mkdir()
        execution_env = self.manager._execution_env(str(workdir))
        runtime_env = self.manager._runtime_env()

        for environment, expected in (
            (execution_env, workdir / ".tmp"),
            (runtime_env, Path(runtime_env["TMPDIR"])),
        ):
            self.assertEqual(environment["TMPDIR"], str(expected.resolve()))
            self.assertEqual(environment["TEMP"], environment["TMPDIR"])
            self.assertEqual(environment["TMP"], environment["TMPDIR"])
            Path(environment["XDG_RUNTIME_DIR"]).resolve().relative_to(
                Path(self.temporary.name).parents[2]
            )
        self.assertEqual(execution_env["PYTHONDONTWRITEBYTECODE"], "1")


class _FailingRuntime:
    async def get_or_create_environment(self, _agent_name: str):
        return AgentEnvironment(
            agent_name="demo",
            environment_type=EnvironmentType.UV,
            status=EnvironmentStatus.READY,
        )

    async def install_skill_dependencies(self, **_kwargs):
        return True, "ok", []

    async def execute_in_environment(self, **_kwargs):
        raise EnvironmentError("intentional failure")

    async def cancel_process(self, _execution_id: str):
        return False


class _UnusedFileStorage:
    async def copy_file_to_workdir(self, **_kwargs):
        raise AssertionError("no input files were requested")


class ExecutionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_directory_is_removed_on_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_runtime = os.environ.get("AGENT_BUILDER_RUNTIME_DIR")
            os.environ["AGENT_BUILDER_RUNTIME_DIR"] = str(root / ".runtime")
            try:
                scripts = root / "skill" / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "main.py").write_text("print('ok')\n", encoding="utf-8")
                engine = ExecutionEngine(
                    _FailingRuntime(), _UnusedFileStorage(), root / "data"
                )
                record = await engine.execute_script(
                    agent_name="demo",
                    skill_name="test-skill",
                    script_path="main.py",
                    skill_base_path=str(scripts),
                )
                self.assertEqual(record.status.value, "failed")
                self.assertEqual(list(engine.work_root.iterdir()), [])
            finally:
                if old_runtime is None:
                    os.environ.pop("AGENT_BUILDER_RUNTIME_DIR", None)
                else:
                    os.environ["AGENT_BUILDER_RUNTIME_DIR"] = old_runtime


if __name__ == "__main__":
    unittest.main()
