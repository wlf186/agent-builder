"""Dependency-free Linux sandbox primitives for uploaded Skill processes.

Landlock confines filesystem reads/writes without a container or privileged
helper.  A small seccomp filter disables networking by default.  Both controls
are applied in the child immediately before ``execve``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
from pathlib import Path
from typing import Iterable


class SandboxUnavailableError(RuntimeError):
    pass


_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14
_ACCESS_IOCTL_DEV = 1 << 15

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JSET_K = 0x45
_BPF_RET_K = 0x06

_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_AARCH64 = 0xC00000B7
_X32_SYSCALL_BIT = 0x40000000

# io_uring operations are executed by kernel workers and have historically
# bypassed filters which only rejected socket(2).  Deny the complete interface
# whenever network access is disabled, not just IORING_OP_SOCKET itself.
_IO_URING_SYSCALLS = frozenset({425, 426, 427})

# An uploaded Skill must not inspect or signal the backend (which runs as the
# same Unix user), nor detach descendants from the process group that the
# timeout/cancellation path terminates.  Signal receipt and ordinary
# fork/exec/wait remain available.
_SECCOMP_ARCHITECTURES = {
    "x86_64": {
        "audit_arch": _AUDIT_ARCH_X86_64,
        "socket_syscalls": frozenset({41, 53}),
        "process_isolation_syscalls": frozenset(
            {
                62,   # kill
                101,  # ptrace
                109,  # setpgid
                112,  # setsid
                129,  # rt_sigqueueinfo
                200,  # tkill
                234,  # tgkill
                297,  # rt_tgsigqueueinfo
                310,  # process_vm_readv
                311,  # process_vm_writev
                424,  # pidfd_send_signal
                434,  # pidfd_open
                438,  # pidfd_getfd
            }
        ),
        "reject_x32": True,
    },
    "aarch64": {
        "audit_arch": _AUDIT_ARCH_AARCH64,
        "socket_syscalls": frozenset({198, 199}),
        "process_isolation_syscalls": frozenset(
            {
                117,  # ptrace
                129,  # kill
                130,  # tkill
                131,  # tgkill
                138,  # rt_sigqueueinfo
                154,  # setpgid
                157,  # setsid
                240,  # rt_tgsigqueueinfo
                270,  # process_vm_readv
                271,  # process_vm_writev
                424,  # pidfd_send_signal
                434,  # pidfd_open
                438,  # pidfd_getfd
            }
        ),
        "reject_x32": False,
    },
}

_MACHINE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}

# Do not expose all of /etc: local deployments commonly keep service secrets
# there.  These are the public runtime files/directories needed by the dynamic
# loader, name resolution, TLS, account lookup, and common rendering tools.
_SYSTEM_READABLE_CANDIDATES = (
    Path("/usr"),
    Path("/bin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc/alternatives"),
    Path("/etc/ca-certificates"),
    Path("/etc/fonts"),
    Path("/etc/pki"),
    Path("/etc/ssl"),
    Path("/etc/gai.conf"),
    Path("/etc/group"),
    Path("/etc/host.conf"),
    Path("/etc/hosts"),
    Path("/etc/ld.so.cache"),
    Path("/etc/ld.so.preload"),
    Path("/etc/localtime"),
    Path("/etc/mime.types"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/os-release"),
    Path("/etc/passwd"),
    Path("/etc/protocols"),
    Path("/etc/resolv.conf"),
    Path("/etc/services"),
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """Return the supported Landlock ABI, or zero when unavailable."""
    if platform.system() != "Linux":
        return 0
    libc = _libc()
    result = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    return int(result) if result >= 1 else 0


def _rights_for_abi(abi: int) -> int:
    rights = (1 << 13) - 1
    if abi >= 2:
        rights |= _ACCESS_REFER
    if abi >= 3:
        rights |= _ACCESS_TRUNCATE
    if abi >= 5:
        rights |= _ACCESS_IOCTL_DEV
    return rights


def _canonical_rule_path(raw_path: Path) -> Path:
    """Resolve an existing rule root while refusing a symlink at the root."""
    path = Path(raw_path)
    if path.is_symlink():
        raise SandboxUnavailableError(f"Sandbox rule path is a symlink: {path}")
    try:
        return path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise SandboxUnavailableError(
            f"Sandbox rule path is unavailable: {path}"
        ) from exc


def _add_path_rule(
    libc: ctypes.CDLL,
    ruleset_fd: int,
    path: Path,
    rights: int,
    *,
    expect_directory: bool,
) -> None:
    flags = (
        getattr(os, "O_PATH", os.O_RDONLY)
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(opened.st_mode)
            or stat.S_ISDIR(opened.st_mode) != expect_directory
        ):
            raise SandboxUnavailableError(f"Sandbox rule path changed: {path}")
        current = os.stat(path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SandboxUnavailableError(f"Sandbox rule path changed: {path}")
        attr = _PathBeneathAttr(allowed_access=rights, parent_fd=descriptor)
        result = libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
    finally:
        os.close(descriptor)


def restrict_filesystem(
    *,
    writable_paths: Iterable[Path],
    readable_paths: Iterable[Path],
) -> None:
    """Confine the current process to explicit read and write path sets."""
    abi = landlock_abi()
    if abi < 1:
        raise SandboxUnavailableError("Linux Landlock is unavailable")

    libc = _libc()
    handled = _rights_for_abi(abi)
    ruleset_attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

    read_directory = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
    read_file = _ACCESS_EXECUTE | _ACCESS_READ_FILE
    try:
        seen: set[Path] = set()
        for raw_path in readable_paths:
            path = _canonical_rule_path(Path(raw_path))
            if path in seen:
                continue
            seen.add(path)
            is_directory = path.is_dir()
            _add_path_rule(
                libc,
                ruleset_fd,
                path,
                read_directory if is_directory else read_file,
                expect_directory=is_directory,
            )
        for raw_path in writable_paths:
            path = _canonical_rule_path(Path(raw_path))
            if not path.is_dir():
                raise SandboxUnavailableError(
                    f"Sandbox writable path is not a directory: {path}"
                )
            # Device ioctl is never required for an execution work directory.
            _add_path_rule(
                libc,
                ruleset_fd,
                path,
                handled & ~_ACCESS_IOCTL_DEV,
                expect_directory=True,
            )

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        os.close(ruleset_fd)


def _build_seccomp_filter(
    machine: str,
    *,
    block_network: bool,
    isolate_process: bool,
) -> list[_SockFilter]:
    """Build an architecture-bound classic BPF filter."""
    machine = _MACHINE_ALIASES.get(machine.lower(), machine.lower())
    specification = _SECCOMP_ARCHITECTURES.get(machine)
    if specification is None:
        raise SandboxUnavailableError(f"Unsupported seccomp architecture: {machine}")

    blocked_syscalls: set[int] = set()
    if block_network:
        blocked_syscalls.update(specification["socket_syscalls"])
        blocked_syscalls.update(_IO_URING_SYSCALLS)
    if isolate_process:
        blocked_syscalls.update(specification["process_isolation_syscalls"])

    # seccomp_data.arch is at offset 4 and nr is at offset 0.  Binding the
    # filter to the expected audit architecture prevents a later exec of a
    # compat binary from reaching differently-numbered networking syscalls.
    instructions: list[_SockFilter] = [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 4),
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, int(specification["audit_arch"])),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),
    ]
    if specification["reject_x32"]:
        instructions.extend(
            [
                _SockFilter(_BPF_JMP_JSET_K, 0, 1, _X32_SYSCALL_BIT),
                _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
            ]
        )
    for syscall_number in sorted(blocked_syscalls):
        instructions.extend(
            [
                _SockFilter(_BPF_JMP_JEQ_K, 0, 1, syscall_number),
                _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
            ]
        )
    instructions.append(_SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    return instructions


def _install_seccomp_filter(*, block_network: bool, isolate_process: bool) -> None:
    instructions = _build_seccomp_filter(
        platform.machine(),
        block_network=block_network,
        isolate_process=isolate_process,
    )
    program_array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len=len(instructions), filter=program_array)
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def disable_network() -> None:
    """Reject native, compat-ABI, and io_uring network socket creation."""
    _install_seccomp_filter(block_network=True, isolate_process=False)


def _existing_system_readable_paths() -> list[Path]:
    paths: list[Path] = []
    for candidate in _SYSTEM_READABLE_CANDIDATES:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            continue
        if resolved not in paths:
            paths.append(resolved)
    return paths


def apply_skill_sandbox(
    *,
    work_directory: Path,
    environment_directory: Path,
    runtime_root: Path,
    allow_network: bool = False,
    additional_readable_paths: Iterable[Path] = (),
) -> None:
    """Apply the complete fail-closed sandbox to the current child process."""
    required_paths = (
        work_directory,
        environment_directory,
        runtime_root / "python",
        *additional_readable_paths,
    )
    canonical_required = [
        _canonical_rule_path(Path(path)) for path in required_paths
    ]
    canonical_work, canonical_environment, canonical_python, *canonical_additional = (
        canonical_required
    )
    readable = [
        canonical_environment,
        canonical_python,
        *_existing_system_readable_paths(),
        Path("/dev/null"),
        Path("/dev/urandom"),
        Path("/dev/random"),
    ]
    readable.extend(canonical_additional)
    restrict_filesystem(writable_paths=[canonical_work], readable_paths=readable)
    # Always prevent cross-process inspection/signalling and process-group
    # escape.  Network syscalls are added to the same filter by default.
    _install_seccomp_filter(
        block_network=not allow_network,
        isolate_process=True,
    )
