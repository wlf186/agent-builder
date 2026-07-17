import ctypes
import errno
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import process_sandbox
from src.process_sandbox import apply_skill_sandbox, landlock_abi


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TEST_DIR = PROJECT_ROOT / ".runtime" / "tests"


@unittest.skipUnless(sys.platform.startswith("linux") and landlock_abi() >= 1, "Landlock required")
class ProcessSandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_DIR.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_DIR)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.outside = self.root / "outside.txt"
        self.outside.write_text("host-secret", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        source: str,
        *,
        additional_readable_paths: tuple[Path, ...] = (),
        use_resource_callback: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        def sandbox() -> None:
            if use_resource_callback:
                from src.environment_manager import EnvironmentManager

                callback = EnvironmentManager._resource_limit_callback(
                    2,
                    self.work,
                    Path(sys.prefix),
                    additional_readable_paths,
                )
                assert callback is not None
                callback()
            else:
                apply_skill_sandbox(
                    work_directory=self.work,
                    environment_directory=Path(sys.prefix),
                    runtime_root=PROJECT_ROOT / ".runtime",
                    allow_network=False,
                    additional_readable_paths=additional_readable_paths,
                )

        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=self.work,
            text=True,
            capture_output=True,
            timeout=10,
            preexec_fn=sandbox,
            start_new_session=True,
            check=False,
        )

    def test_work_directory_is_writable_but_outside_is_not_readable_or_writable(self) -> None:
        source = f"""
import json
from pathlib import Path
result = {{}}
Path('inside.txt').write_text('ok')
for operation, callback in (
    ('read', lambda: Path({str(self.outside)!r}).read_text()),
    ('write', lambda: Path({str(self.outside)!r}).write_text('changed')),
):
    try:
        callback()
        result[operation] = 'allowed'
    except OSError:
        result[operation] = 'denied'
print(json.dumps(result))
"""
        completed = self._run(source)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"read": "denied", "write": "denied"})
        self.assertEqual((self.work / "inside.txt").read_text(), "ok")
        self.assertEqual(self.outside.read_text(), "host-secret")

    def test_network_socket_creation_is_denied(self) -> None:
        completed = self._run(
            "import socket\n"
            "try:\n socket.socket(); print('allowed')\n"
            "except OSError: print('denied')\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "denied")

    @unittest.skipUnless(
        platform.machine().lower() in {"x86_64", "amd64"},
        "x32 syscall encoding is x86_64-specific",
    )
    def test_x32_syscall_encoding_is_denied(self) -> None:
        completed = self._run(
            "import ctypes, errno\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "result = libc.syscall(0x40000000 | 41, 2, 1, 0)\n"
            "print(result, ctypes.get_errno())\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"-1 {errno.EPERM}")

    def test_io_uring_entry_points_are_denied(self) -> None:
        completed = self._run(
            "import ctypes, json\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "result = {}\n"
            "for number in (425, 426, 427):\n"
            " ctypes.set_errno(0)\n"
            " result[number] = [libc.syscall(number, -1, 0, 0, 0, 0, 0), ctypes.get_errno()]\n"
            "print(json.dumps(result, sort_keys=True))\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {str(number): [-1, errno.EPERM] for number in (425, 426, 427)},
        )

    def test_descendant_inherits_network_filesystem_and_process_isolation(self) -> None:
        child_source = f"""
import json
import os
import socket
from pathlib import Path
result = {{}}
for operation, callback in (
    ('network', lambda: socket.socket()),
    ('outside_read', lambda: Path({str(self.outside)!r}).read_text()),
    ('signal_parent', lambda: os.kill(os.getppid(), 0)),
    ('new_session', lambda: os.setsid()),
):
    try:
        callback()
        result[operation] = 'allowed'
    except OSError:
        result[operation] = 'denied'
Path('descendant.txt').write_text('ok')
print(json.dumps(result, sort_keys=True))
"""
        completed = self._run(
            "import json, subprocess, sys\n"
            f"source = {child_source!r}\n"
            "child = subprocess.run([sys.executable, '-c', source], text=True, capture_output=True)\n"
            "print(json.dumps({'code': child.returncode, 'stdout': child.stdout, 'stderr': child.stderr}))\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        child = json.loads(completed.stdout)
        self.assertEqual(child["code"], 0, child["stderr"])
        self.assertEqual(
            json.loads(child["stdout"]),
            {
                "network": "denied",
                "new_session": "denied",
                "outside_read": "denied",
                "signal_parent": "denied",
            },
        )
        self.assertEqual((self.work / "descendant.txt").read_text(), "ok")

    def test_resource_limits_are_hard_after_exec(self) -> None:
        source = """
import json
import resource
limits = {
    'cpu': resource.getrlimit(resource.RLIMIT_CPU),
    'core': resource.getrlimit(resource.RLIMIT_CORE),
    'file': resource.getrlimit(resource.RLIMIT_FSIZE),
    'nofile': resource.getrlimit(resource.RLIMIT_NOFILE),
}
try:
    hard = limits['nofile'][1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard + 1, hard + 1))
    limits['raise_hard'] = 'allowed'
except (OSError, ValueError):
    limits['raise_hard'] = 'denied'
print(json.dumps(limits, sort_keys=True))
"""
        completed = self._run(
            source,
            use_resource_callback=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        limits = json.loads(completed.stdout)
        self.assertEqual(limits["cpu"], [7, 7])
        self.assertEqual(limits["core"], [0, 0])
        self.assertEqual(limits["file"], [100 * 1024**2, 100 * 1024**2])
        self.assertEqual(limits["nofile"], [256, 256])
        self.assertEqual(limits["raise_hard"], "denied")

    def test_resource_callback_allows_fork_exec_descendant(self) -> None:
        completed = self._run(
            "import subprocess, sys\n"
            "child = subprocess.run(\n"
            " [sys.executable, '-c', \"print('child-ok')\"],\n"
            " text=True, capture_output=True, check=False,\n"
            ")\n"
            "print(child.returncode, child.stdout.strip())\n",
            use_resource_callback=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "0 child-ok")

    def test_additional_skill_source_is_read_only(self) -> None:
        source_dir = self.root / "skill-source"
        source_dir.mkdir()
        source_file = source_dir / "main.py"
        source_file.write_text("original", encoding="utf-8")
        completed = self._run(
            f"""
import json
from pathlib import Path
path = Path({str(source_file)!r})
result = {{'read': path.read_text()}}
try:
    path.write_text('changed')
    result['write'] = 'allowed'
except OSError:
    result['write'] = 'denied'
print(json.dumps(result))
""",
            additional_readable_paths=(source_dir,),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"read": "original", "write": "denied"},
        )
        self.assertEqual(source_file.read_text(encoding="utf-8"), "original")

    def test_symlink_rule_root_is_rejected(self) -> None:
        source_dir = self.root / "skill-source"
        source_dir.mkdir()
        source_link = self.root / "skill-link"
        source_link.symlink_to(source_dir, target_is_directory=True)
        with self.assertRaises(subprocess.SubprocessError):
            self._run(
                "print('must not execute')",
                additional_readable_paths=(source_link,),
            )

    def test_unlisted_etc_file_is_not_readable(self) -> None:
        target = Path("/etc/environment")
        if not target.is_file() or not os.access(target, os.R_OK):
            self.skipTest("No readable unlisted /etc fixture")
        completed = self._run(
            "from pathlib import Path\n"
            "try:\n"
            f" Path({str(target)!r}).read_bytes(); print('allowed')\n"
            "except OSError:\n"
            " print('denied')\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "denied")


class SeccompFilterConstructionTest(unittest.TestCase):
    @staticmethod
    def _evaluate(instructions, *, audit_arch: int, syscall_number: int) -> int:
        accumulator = 0
        program_counter = 0
        while program_counter < len(instructions):
            instruction = instructions[program_counter]
            if instruction.code == process_sandbox._BPF_LD_W_ABS:
                accumulator = audit_arch if instruction.k == 4 else syscall_number
                program_counter += 1
                continue
            if instruction.code == process_sandbox._BPF_JMP_JEQ_K:
                jump = instruction.jt if accumulator == instruction.k else instruction.jf
                program_counter += int(jump) + 1
                continue
            if instruction.code == process_sandbox._BPF_JMP_JSET_K:
                jump = instruction.jt if accumulator & instruction.k else instruction.jf
                program_counter += int(jump) + 1
                continue
            if instruction.code == process_sandbox._BPF_RET_K:
                return int(instruction.k)
            raise AssertionError(f"Unexpected BPF opcode: {instruction.code:#x}")
        raise AssertionError("Seccomp program did not return")

    def test_filter_binds_architecture_and_blocks_bypass_syscalls(self) -> None:
        instructions = process_sandbox._build_seccomp_filter(
            "x86_64",
            block_network=True,
            isolate_process=True,
        )
        denied = process_sandbox._SECCOMP_RET_ERRNO | errno.EPERM
        evaluate = lambda arch, number: self._evaluate(
            instructions,
            audit_arch=arch,
            syscall_number=number,
        )
        self.assertEqual(evaluate(process_sandbox._AUDIT_ARCH_X86_64, 41), denied)
        self.assertEqual(evaluate(process_sandbox._AUDIT_ARCH_X86_64, 425), denied)
        self.assertEqual(evaluate(process_sandbox._AUDIT_ARCH_X86_64, 62), denied)
        self.assertEqual(
            evaluate(
                process_sandbox._AUDIT_ARCH_X86_64,
                process_sandbox._X32_SYSCALL_BIT | 39,
            ),
            denied,
        )
        self.assertEqual(
            evaluate(process_sandbox._AUDIT_ARCH_X86_64, 39),
            process_sandbox._SECCOMP_RET_ALLOW,
        )
        self.assertEqual(
            evaluate(process_sandbox._AUDIT_ARCH_AARCH64, 39),
            process_sandbox._SECCOMP_RET_KILL_PROCESS,
        )


if __name__ == "__main__":
    unittest.main()
