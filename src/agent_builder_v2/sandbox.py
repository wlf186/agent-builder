"""Fail-closed Linux containment for one Harness V2 Run Worker.

This module deliberately has no dependency on the legacy runtime.  A trusted
Worker imports it before reading a Run command, then installs a Landlock domain
and an architecture-bound seccomp filter in its own process.  Model, MCP and
other network access must be provided through already-open capability pipes;
the Worker cannot create sockets or child processes after this boundary.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import resource
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class SandboxUnavailableError(RuntimeError):
    """Raised when every required containment primitive cannot be installed."""


MINIMUM_LANDLOCK_ABI = 6

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_SYS_CLOSE_RANGE = 436
_CLOSE_RANGE_UNSHARE = 1 << 1
_UINT_MAX = (1 << 32) - 1
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
_HANDLED_FS_ACCESS = (1 << 16) - 1

_ACCESS_NET_BIND_TCP = 1 << 0
_ACCESS_NET_CONNECT_TCP = 1 << 1
_HANDLED_NET_ACCESS = _ACCESS_NET_BIND_TCP | _ACCESS_NET_CONNECT_TCP

_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1
_REQUIRED_SCOPES = _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL

_PR_SET_PDEATHSIG = 1
_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
_PR_GET_SECCOMP = 21
_PR_SET_SECCOMP = 22
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
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

_MACHINE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}

# These syscall numbers come from Linux's x86_64 table and the asm-generic
# table used by aarch64.  The policy is intentionally stricter than the Skill
# policy: a Harness Worker has no reason to create a process, execute another
# image, create a socket, enter a namespace, or touch privileged kernel APIs.
_SECCOMP_ARCHITECTURES: dict[str, tuple[int, bool, frozenset[int]]] = {
    "x86_64": (
        _AUDIT_ARCH_X86_64,
        True,
        frozenset(
            {
                # Network creation and io_uring bypass surface.
                41, 53, 425, 426, 427,
                # No persistent IPC objects, alternate async I/O or memfds.
                29, 30, 31, 64, 65, 66, 67, 68, 69, 70, 71, 206,
                207, 208, 209, 210, 220, 240, 241, 242, 243, 244, 245,
                319,
                # Process signalling, inspection and process-group escape.
                62, 101, 109, 112, 129, 200, 234, 297, 310, 311,
                312, 424, 434, 438, 440, 448, 72, 73, 141, 142, 144,
                203, 251, 256, 261, 274, 279, 302, 314,
                # No descendants and no replacement process image.
                56, 57, 58, 59, 157, 317, 322, 435, 444, 445, 446,
                # Namespace, mount and filesystem-handle escape surface.
                155, 161, 165, 166, 272, 303, 304, 308,
                428, 429, 430, 431, 432, 442,
                # Kernel attack surface unnecessary for a Worker.
                90, 91, 92, 93, 94, 105, 106, 113, 114, 117, 119,
                16, 74, 75, 122, 123, 126, 132, 162, 188, 189, 190,
                197, 198, 199, 235, 260, 261, 268, 277, 280, 306, 452,
                103, 135, 163, 167, 168, 169, 172, 173, 175, 176,
                246, 248, 249, 250, 298, 300, 313, 320, 321, 323,
                324, 447, 457, 458, 459, 460, 461,
            }
        ),
    ),
    "aarch64": (
        _AUDIT_ARCH_AARCH64,
        False,
        frozenset(
            {
                # Network creation and io_uring bypass surface.
                198, 199, 425, 426, 427,
                # No persistent IPC objects, alternate async I/O or memfds.
                0, 1, 2, 3, 4, 180, 181, 182, 183, 184, 185, 186,
                187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197,
                279, 418, 419, 420,
                # Process signalling, inspection and process-group escape.
                117, 129, 130, 131, 138, 154, 157, 240, 270, 271,
                272, 424, 434, 438, 440, 448, 25, 30, 32, 100, 118,
                119, 122, 140, 238, 239, 261, 274,
                # aarch64 has clone rather than fork/vfork syscalls.
                167, 220, 221, 277, 281, 435, 444, 445, 446,
                # Namespace, mount and filesystem-handle escape surface.
                39, 40, 41, 51, 97, 264, 265, 268,
                428, 429, 430, 431, 432, 442,
                # Kernel attack surface unnecessary for a Worker.
                5, 6, 7, 14, 15, 16, 29, 52, 53, 54, 55, 81, 82,
                83, 84, 88, 91, 143, 144, 145, 146, 147, 149, 151,
                152, 267, 412, 452,
                89, 92, 104, 105, 106, 116, 142, 217, 218, 219,
                224, 225, 241, 262, 273, 280, 282, 283, 294,
                447, 457, 458, 459, 460, 461,
            }
        ),
    ),
}

# Worker networking is brokered, so it needs only the loader/runtime subset of
# the host.  In particular, do not expose all of /etc or /usr/bin.
_SYSTEM_READABLE_CANDIDATES = (
    Path("/usr/lib"),
    Path("/usr/share/locale"),
    Path("/usr/share/zoneinfo"),
    Path("/etc/ld.so.cache"),
    Path("/dev/null"),
    Path("/dev/urandom"),
    Path("/dev/random"),
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


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


@dataclass(frozen=True)
class HostQualification:
    qualified: bool
    system: str
    machine: str
    landlock_abi: int
    reason: str | None = None


@dataclass(frozen=True)
class SandboxAttestation:
    landlock_abi: int
    seccomp_arch: str
    seccomp_mode: int
    no_new_privileges: bool
    parent_pid: int
    tcp_network_denied: bool = True
    abstract_unix_scoped: bool = True
    signal_scoped: bool = True
    process_creation_denied: bool = True
    descriptor_isolation: bool = True
    filesystem_write_denied: bool = True
    persistent_ipc_denied: bool = True
    dumpable: bool = False


@dataclass(frozen=True)
class WorkerResourceLimits:
    cpu_seconds: int = 35
    address_space_bytes: int = 512 * 1024 * 1024
    file_size_bytes: int = 2 * 1024 * 1024
    open_files: int = 64
    processes: int = 1

    def validate(self) -> None:
        if not 1 <= self.cpu_seconds <= 300:
            raise ValueError("cpu_seconds is outside the Worker safety range")
        if not 64 * 1024 * 1024 <= self.address_space_bytes <= 4 * 1024**3:
            raise ValueError("address_space_bytes is outside the Worker safety range")
        if not 1024 * 1024 <= self.file_size_bytes <= 64 * 1024 * 1024:
            raise ValueError("file_size_bytes is outside the Worker safety range")
        if not 16 <= self.open_files <= 256:
            raise ValueError("open_files is outside the Worker safety range")
        if not 1 <= self.processes <= 32:
            raise ValueError("processes is outside the Worker safety range")


@dataclass(frozen=True)
class _RuleRoot:
    path: Path
    is_directory: bool
    device: int
    inode: int


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _normalised_machine() -> str:
    raw = platform.machine().lower()
    return _MACHINE_ALIASES.get(raw, raw)


def landlock_abi() -> int:
    """Return the host Landlock ABI, or zero when the query is unavailable."""

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


def _new_ruleset_fd() -> int:
    attr = _RulesetAttr(
        handled_access_fs=_HANDLED_FS_ACCESS,
        handled_access_net=_HANDLED_NET_ACCESS,
        scoped=_REQUIRED_SCOPES,
    )
    libc = _libc()
    descriptor = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    if descriptor < 0:
        error = ctypes.get_errno()
        raise SandboxUnavailableError(
            f"required Landlock filesystem/network/scope features are unavailable: "
            f"errno={error}"
        )
    return int(descriptor)


def _new_filesystem_ruleset_fd() -> int:
    """Create a filesystem-only ruleset for trusted lifecycle qualification."""

    attr = _RulesetAttr(
        handled_access_fs=_HANDLED_FS_ACCESS,
        handled_access_net=0,
        scoped=0,
    )
    libc = _libc()
    descriptor = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    if descriptor < 0:
        error = ctypes.get_errno()
        raise SandboxUnavailableError(
            f"required Landlock filesystem features are unavailable: errno={error}"
        )
    return int(descriptor)


def _probe_required_landlock_features() -> None:
    descriptor = _new_ruleset_fd()
    os.close(descriptor)


def qualify_host() -> HostQualification:
    """Report whether this host can enforce the complete V2 Worker boundary."""

    system = platform.system()
    machine = _normalised_machine()
    abi = landlock_abi()
    reason: str | None = None
    if system != "Linux":
        reason = "Harness V2 Worker sandbox requires Linux"
    elif machine not in _SECCOMP_ARCHITECTURES:
        reason = f"unsupported seccomp architecture: {machine}"
    elif abi < MINIMUM_LANDLOCK_ABI:
        reason = (
            f"Landlock ABI {MINIMUM_LANDLOCK_ABI}+ is required; host reports {abi}"
        )
    elif not Path("/proc/self/stat").is_file():
        reason = "/proc is required for verified Worker supervision"
    else:
        try:
            _probe_required_landlock_features()
            _prctl(_PR_GET_SECCOMP, 0)
        except (OSError, SandboxUnavailableError) as exc:
            reason = f"required kernel sandbox features are unavailable: {type(exc).__name__}"
    return HostQualification(
        qualified=reason is None,
        system=system,
        machine=machine,
        landlock_abi=abi,
        reason=reason,
    )


host_qualification = qualify_host


def require_qualified_host() -> HostQualification:
    qualification = qualify_host()
    if not qualification.qualified:
        raise SandboxUnavailableError(
            qualification.reason or "host sandbox qualification failed"
        )
    return qualification


def _prctl(option: int, argument: int) -> int:
    libc = _libc()
    result = libc.prctl(option, argument, 0, 0, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def configure_parent_death_signal(expected_parent_pid: int | None = None) -> int:
    """Arm SIGKILL on supervisor death and close the classic prctl race."""

    parent_before = os.getppid()
    if parent_before <= 1:
        raise SandboxUnavailableError("Worker supervisor already exited")
    if expected_parent_pid is not None and parent_before != expected_parent_pid:
        raise SandboxUnavailableError("Worker parent identity does not match launch")
    _prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL))
    parent_after = os.getppid()
    if parent_after != parent_before or parent_after <= 1:
        raise SandboxUnavailableError("Worker supervisor exited during sandbox setup")
    return parent_after


def _set_process_nondumpable() -> None:
    _prctl(_PR_SET_DUMPABLE, 0)
    if _prctl(_PR_GET_DUMPABLE, 0) != 0:
        raise SandboxUnavailableError("Worker dumpability could not be disabled")


def apply_worker_umask(mask: int = 0o077) -> int:
    if mask != 0o077:
        raise ValueError("Worker umask is fixed at 0077")
    return os.umask(mask)


def _lower_resource_limit(resource_name: int, desired: int) -> None:
    current_soft, current_hard = resource.getrlimit(resource_name)
    hard = desired if current_hard == resource.RLIM_INFINITY else min(desired, current_hard)
    soft = desired if current_soft == resource.RLIM_INFINITY else min(desired, current_soft)
    resource.setrlimit(resource_name, (min(soft, hard), hard))


def apply_worker_resource_limits(
    limits: WorkerResourceLimits = WorkerResourceLimits(),
) -> None:
    """Install hard per-process limits without raising inherited stricter caps."""

    limits.validate()
    _lower_resource_limit(resource.RLIMIT_CORE, 0)
    _lower_resource_limit(resource.RLIMIT_CPU, limits.cpu_seconds)
    _lower_resource_limit(resource.RLIMIT_FSIZE, limits.file_size_bytes)
    _lower_resource_limit(resource.RLIMIT_NOFILE, limits.open_files)
    if hasattr(resource, "RLIMIT_NPROC"):
        _lower_resource_limit(resource.RLIMIT_NPROC, limits.processes)
    if not hasattr(resource, "RLIMIT_AS"):
        raise SandboxUnavailableError("RLIMIT_AS is required for Worker containment")
    _lower_resource_limit(resource.RLIMIT_AS, limits.address_space_bytes)


def verify_worker_file_descriptors() -> None:
    """Reject every inherited descriptor except the three bounded pipes."""

    unexpected: list[int] = []
    try:
        names = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise SandboxUnavailableError("Worker file descriptors cannot be audited") from exc
    for name in names:
        if not name.isdigit():
            continue
        descriptor = int(name)
        if descriptor <= 2:
            continue
        try:
            os.fstat(descriptor)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue
            raise
        unexpected.append(descriptor)
    if unexpected:
        raise SandboxUnavailableError("Worker inherited an unexpected file descriptor")


def close_worker_file_descriptors() -> None:
    """Atomically detach and close every inherited descriptor above stderr.

    uvloop's subprocess transport can preserve its socketpair endpoints even
    when ``close_fds`` is requested.  A Worker has exactly three pipe
    capabilities, so it closes the entire remaining descriptor range before
    touching input.  The caller must then perform the independent `/proc`
    audit.
    """

    if platform.system() != "Linux":
        raise SandboxUnavailableError("secure Worker descriptor cleanup requires Linux")
    libc = _libc()
    result = libc.syscall(
        _SYS_CLOSE_RANGE,
        ctypes.c_uint(3),
        ctypes.c_uint(_UINT_MAX),
        ctypes.c_uint(_CLOSE_RANGE_UNSHARE),
    )
    if result != 0:
        error = ctypes.get_errno()
        raise SandboxUnavailableError(
            f"could not isolate inherited Worker descriptors: errno={error}"
        )


def _open_component_verified(path: Path, *, expect_directory: bool | None) -> int:
    """Open an absolute path one no-follow component at a time."""

    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise SandboxUnavailableError(f"sandbox rule path is not canonical: {path}")
    if "\x00" in os.fspath(path):
        raise SandboxUnavailableError("sandbox rule path contains NUL")
    o_path = getattr(os, "O_PATH", None)
    o_nofollow = getattr(os, "O_NOFOLLOW", None)
    if o_path is None or o_nofollow is None:
        raise SandboxUnavailableError("O_PATH and O_NOFOLLOW are required")

    current = os.open("/", o_path | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        components = path.parts[1:]
        if not components:
            raise SandboxUnavailableError("filesystem root cannot be a sandbox rule")
        for index, component in enumerate(components):
            final = index == len(components) - 1
            flags = o_path | o_nofollow | os.O_CLOEXEC
            if not final or expect_directory is True:
                flags |= getattr(os, "O_DIRECTORY", 0)
            candidate = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = candidate
            metadata = os.fstat(current)
            if stat.S_ISLNK(metadata.st_mode):
                raise SandboxUnavailableError(f"sandbox rule path contains a symlink: {path}")
            if not final and not stat.S_ISDIR(metadata.st_mode):
                raise SandboxUnavailableError(
                    f"sandbox rule path component is not a directory: {path}"
                )
        metadata = os.fstat(current)
        if expect_directory is True and not stat.S_ISDIR(metadata.st_mode):
            raise SandboxUnavailableError(f"sandbox rule path is not a directory: {path}")
        if expect_directory is False and stat.S_ISDIR(metadata.st_mode):
            raise SandboxUnavailableError(f"sandbox rule path is not a file: {path}")
        return current
    except BaseException:
        os.close(current)
        raise


def _capture_rule_root(
    raw_path: Path, *, expect_directory: bool | None
) -> _RuleRoot:
    path = Path(os.path.abspath(os.fspath(raw_path)))
    descriptor = _open_component_verified(path, expect_directory=expect_directory)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SandboxUnavailableError(f"sandbox rule path changed: {path}")
        return _RuleRoot(
            path=path,
            is_directory=stat.S_ISDIR(opened.st_mode),
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    finally:
        os.close(descriptor)


def _optional_rule_root(path: Path) -> _RuleRoot | None:
    try:
        return _capture_rule_root(path, expect_directory=None)
    except FileNotFoundError:
        return None


def _add_path_rule(
    ruleset_fd: int,
    root: _RuleRoot,
    rights: int,
) -> None:
    descriptor = _open_component_verified(
        root.path, expect_directory=root.is_directory
    )
    try:
        opened = os.fstat(descriptor)
        current = os.stat(root.path, follow_symlinks=False)
        identity = (root.device, root.inode)
        if (
            (opened.st_dev, opened.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
        ):
            raise SandboxUnavailableError(f"sandbox rule path changed: {root.path}")
        attr = _PathBeneathAttr(allowed_access=rights, parent_fd=descriptor)
        libc = _libc()
        result = libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not add Landlock rule: errno={error}"
            )
    finally:
        os.close(descriptor)


def _prepare_rules(
    run_root: Path,
    environment_root: Path,
    source_root: Path,
) -> tuple[list[_RuleRoot], list[_RuleRoot]]:
    run = _capture_rule_root(run_root, expect_directory=True)
    environment = _capture_rule_root(environment_root, expect_directory=True)
    source = _capture_rule_root(source_root, expect_directory=True)
    base_runtime = _capture_rule_root(Path(sys.base_prefix), expect_directory=True)

    children: dict[str, _RuleRoot] = {}
    for name in ("input", "home", "tmp", "xdg", "work", "output"):
        child = _capture_rule_root(run.path / name, expect_directory=True)
        try:
            child.path.relative_to(run.path)
        except ValueError as exc:
            raise SandboxUnavailableError("Run child escaped its root") from exc
        children[name] = child

    for managed in (run, environment, source, *children.values()):
        metadata = os.stat(managed.path, follow_symlinks=False)
        if metadata.st_uid != os.getuid():
            raise SandboxUnavailableError("managed sandbox rule has the wrong owner")
    for writable_child in ("home", "tmp", "xdg", "work", "output"):
        metadata = os.stat(children[writable_child].path, follow_symlinks=False)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SandboxUnavailableError("Worker writable root is not private")

    readable = [environment, source, base_runtime, children["input"]]
    for candidate in _SYSTEM_READABLE_CANDIDATES:
        captured = _optional_rule_root(candidate)
        if captured is not None:
            readable.append(captured)
    run_roots = [children[name] for name in ("home", "tmp", "xdg", "work", "output")]

    # A duplicated physical rule root is almost always an unsafe layout error.
    run_identities = {(root.device, root.inode) for root in run_roots}
    if len(run_identities) != len(run_roots):
        raise SandboxUnavailableError("Worker Run roots are not independent")
    if any((root.device, root.inode) in run_identities for root in readable):
        raise SandboxUnavailableError("Worker runtime and Run roots overlap")
    return readable, run_roots


def _set_no_new_privileges() -> None:
    _prctl(_PR_SET_NO_NEW_PRIVS, 1)
    if _prctl(_PR_GET_NO_NEW_PRIVS, 0) != 1:
        raise SandboxUnavailableError("no_new_privs attestation failed")


def _install_landlock(
    readable: Iterable[_RuleRoot], run_roots: Iterable[_RuleRoot]
) -> None:
    ruleset_fd = _new_ruleset_fd()
    read_directory = _ACCESS_READ_FILE | _ACCESS_READ_DIR
    read_file = _ACCESS_READ_FILE
    # The fixed Worker has no direct filesystem mutation capability.  Its Run
    # directories are readable for context only; future writes must be explicit
    # bounded Tool-broker operations in the trusted control plane.  This is a
    # hard SSD-wear bound rather than a supervisor sampling heuristic.
    run_directory = _ACCESS_READ_FILE | _ACCESS_READ_DIR
    try:
        seen: set[tuple[int, int]] = set()
        for root in readable:
            identity = (root.device, root.inode)
            if identity in seen:
                continue
            seen.add(identity)
            _add_path_rule(
                ruleset_fd,
                root,
                read_directory if root.is_directory else read_file,
            )
        for root in run_roots:
            identity = (root.device, root.inode)
            if identity in seen:
                raise SandboxUnavailableError("duplicate Run-root Landlock rule")
            seen.add(identity)
            _add_path_rule(ruleset_fd, root, run_directory)

        _set_no_new_privileges()
        libc = _libc()
        result = libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not enter Landlock domain: errno={error}"
            )
    finally:
        os.close(ruleset_fd)


def apply_checkout_write_confinement(repository_root: Path) -> None:
    """Restrict this process tree to writes beneath one qualified checkout.

    This boundary is intentionally narrower than the untrusted Worker sandbox:
    trusted lifecycle commands retain networking and process creation, while
    Landlock denies every filesystem mutation outside ``repository_root``.
    The restriction is inherited across fork and exec and has no permissive
    fallback when a required rule cannot be installed.
    """

    qualification = require_qualified_host()
    if qualification.landlock_abi < MINIMUM_LANDLOCK_ABI:
        raise SandboxUnavailableError("checkout confinement requires Landlock")
    checkout = _capture_rule_root(
        Path(repository_root).resolve(strict=True), expect_directory=True
    )
    if checkout.path == Path("/"):
        raise SandboxUnavailableError("filesystem root cannot be a checkout")

    readable: list[_RuleRoot] = []
    for candidate in (
        Path("/usr"),
        Path("/etc"),
        Path("/proc"),
        Path("/sys"),
        Path("/run"),
        Path("/dev"),
    ):
        captured = _optional_rule_root(candidate)
        if captured is not None:
            readable.append(captured)
    null_device = _optional_rule_root(Path("/dev/null"))

    ruleset_fd = _new_filesystem_ruleset_fd()
    read_rights = (
        _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR | _ACCESS_IOCTL_DEV
    )
    try:
        for root in readable:
            _add_path_rule(ruleset_fd, root, read_rights)
        if null_device is not None:
            _add_path_rule(
                ruleset_fd,
                null_device,
                _ACCESS_READ_FILE | _ACCESS_WRITE_FILE | _ACCESS_IOCTL_DEV,
            )
        _add_path_rule(ruleset_fd, checkout, _HANDLED_FS_ACCESS)
        _set_no_new_privileges()
        result = _libc().syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not enter checkout confinement: errno={error}"
            )
    finally:
        os.close(ruleset_fd)


def apply_read_only_command_confinement(workspace: Path, executable: Path) -> None:
    """Confine a fixed metadata command to one read-only workspace.

    This is installed by a short-lived helper before ``execve``.  It avoids
    Python's unsafe ``preexec_fn`` in the multi-threaded Gateway and ensures a
    malicious Git repository cannot redirect reads through config, alternates
    or symlinks outside its Agent Capsule.
    """

    qualification = require_qualified_host()
    if qualification.landlock_abi < MINIMUM_LANDLOCK_ABI:
        raise SandboxUnavailableError("read-only command confinement requires Landlock")
    workspace_root = _capture_rule_root(workspace, expect_directory=True)
    executable_root = _capture_rule_root(executable, expect_directory=False)
    readable = [workspace_root, executable_root]
    for candidate in _SYSTEM_READABLE_CANDIDATES:
        captured = _optional_rule_root(candidate)
        if captured is not None:
            readable.append(captured)

    ruleset_fd = _new_ruleset_fd()
    directory_rights = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
    file_rights = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_IOCTL_DEV
    try:
        seen: set[tuple[int, int]] = set()
        for root in readable:
            identity = (root.device, root.inode)
            if identity in seen:
                continue
            seen.add(identity)
            rights = directory_rights if root.is_directory else file_rights
            if root.path == Path("/dev/null"):
                rights |= _ACCESS_WRITE_FILE
            _add_path_rule(
                ruleset_fd,
                root,
                rights,
            )
        configure_parent_death_signal()
        _set_process_nondumpable()
        _set_no_new_privileges()
        result = _libc().syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not enter read-only command domain: errno={error}"
            )
    finally:
        os.close(ruleset_fd)


def apply_bounded_bash_sandbox(
    bash_path: Path,
    work_root: Path,
    *,
    expected_parent_pid: int,
) -> SandboxAttestation:
    """Confine one pre-parsed builtin-only Bash image.

    Exactly one image replacement is required to enter Bash. Process creation,
    networking and every filesystem write remain denied. Landlock exposes the
    exact Bash inode, loader libraries and the private cwd, but not the Agent
    workspace, Capsule Python environment or command output paths.
    """

    qualification = require_qualified_host()
    bash = _capture_rule_root(bash_path, expect_directory=False)
    work = _capture_rule_root(work_root, expect_directory=True)
    bash_metadata = os.stat(bash.path, follow_symlinks=False)
    work_metadata = os.stat(work.path, follow_symlinks=False)
    if (
        bash_metadata.st_uid not in {0, os.getuid()}
        or work_metadata.st_uid != os.getuid()
        or not stat.S_ISREG(bash_metadata.st_mode)
    ):
        raise SandboxUnavailableError("bounded Bash sandbox root is unsafe")
    readable = [bash, work]
    for candidate in _SYSTEM_READABLE_CANDIDATES:
        captured = _optional_rule_root(candidate)
        if captured is not None:
            readable.append(captured)
    ruleset_fd = _new_ruleset_fd()
    read_directory = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
    read_file = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_IOCTL_DEV
    try:
        seen: set[tuple[int, int]] = set()
        for root in readable:
            identity = (root.device, root.inode)
            if identity in seen:
                continue
            seen.add(identity)
            _add_path_rule(
                ruleset_fd,
                root,
                read_directory if root.is_directory else read_file,
            )
        parent_pid = configure_parent_death_signal(expected_parent_pid)
        _set_process_nondumpable()
        _set_no_new_privileges()
        result = _libc().syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not enter bounded Bash Landlock domain: errno={error}"
            )
    finally:
        os.close(ruleset_fd)
    _install_seccomp_filter(
        qualification.machine, allow_image_replace=True
    )
    return SandboxAttestation(
        landlock_abi=qualification.landlock_abi,
        seccomp_arch=qualification.machine,
        seccomp_mode=_SECCOMP_MODE_FILTER,
        no_new_privileges=True,
        parent_pid=parent_pid,
    )


def apply_singleton_command_sandbox(
    source_root: Path,
    output_root: Path,
    work_root: Path,
    *,
    expected_parent_pid: int,
) -> SandboxAttestation:
    """Install the fail-closed sandbox for one fixed, in-process command.

    The command payload is already executing when this function is called.
    Seccomp therefore denies every later fork/clone/exec/setsid path: the
    supervised PID is the complete descendant container, rather than merely
    the leader of a process group.  Landlock exposes only trusted source as
    read-only and one exact Run output directory as writable.
    """

    qualification = require_qualified_host()
    source = _capture_rule_root(source_root, expect_directory=True)
    output = _capture_rule_root(output_root, expect_directory=True)
    work = _capture_rule_root(work_root, expect_directory=True)
    for root in (source, output, work):
        metadata = os.stat(root.path, follow_symlinks=False)
        if metadata.st_uid != os.getuid():
            raise SandboxUnavailableError("command sandbox root has the wrong owner")
    if stat.S_IMODE(os.stat(output.path, follow_symlinks=False).st_mode) & 0o077:
        raise SandboxUnavailableError("command output root is not private")
    identities = {(item.device, item.inode) for item in (source, output, work)}
    if len(identities) != 3:
        raise SandboxUnavailableError("command sandbox roots overlap")

    readable = [source, work, _capture_rule_root(Path(sys.base_prefix), expect_directory=True)]
    for candidate in _SYSTEM_READABLE_CANDIDATES:
        captured = _optional_rule_root(candidate)
        if captured is not None:
            readable.append(captured)
    ruleset_fd = _new_ruleset_fd()
    read_directory = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
    read_file = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_IOCTL_DEV
    output_rights = (
        _ACCESS_READ_FILE
        | _ACCESS_READ_DIR
        | _ACCESS_WRITE_FILE
        | _ACCESS_REMOVE_FILE
        | _ACCESS_MAKE_REG
        | _ACCESS_TRUNCATE
    )
    try:
        seen: set[tuple[int, int]] = set()
        for root in readable:
            identity = (root.device, root.inode)
            if identity in seen:
                continue
            seen.add(identity)
            _add_path_rule(
                ruleset_fd,
                root,
                read_directory if root.is_directory else read_file,
            )
        if (output.device, output.inode) in seen:
            raise SandboxUnavailableError("command output overlaps a readable root")
        _add_path_rule(ruleset_fd, output, output_rights)
        parent_pid = configure_parent_death_signal(expected_parent_pid)
        _set_process_nondumpable()
        _set_no_new_privileges()
        result = _libc().syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result != 0:
            error = ctypes.get_errno()
            raise SandboxUnavailableError(
                f"could not enter command Landlock domain: errno={error}"
            )
    finally:
        os.close(ruleset_fd)
    _install_seccomp_filter(qualification.machine)
    return SandboxAttestation(
        landlock_abi=qualification.landlock_abi,
        seccomp_arch=qualification.machine,
        seccomp_mode=_SECCOMP_MODE_FILTER,
        no_new_privileges=True,
        parent_pid=parent_pid,
    )


def _build_seccomp_filter(
    machine: str, *, allow_image_replace: bool = False
) -> list[_SockFilter]:
    normalised = _MACHINE_ALIASES.get(machine.lower(), machine.lower())
    specification = _SECCOMP_ARCHITECTURES.get(normalised)
    if specification is None:
        raise SandboxUnavailableError(f"unsupported seccomp architecture: {normalised}")
    audit_arch, reject_x32, blocked = specification
    blocked_syscalls = set(blocked)
    if allow_image_replace:
        for syscall_number in (
            (59, 322) if normalised == "x86_64" else (221, 281)
        ):
            blocked_syscalls.discard(syscall_number)
    instructions: list[_SockFilter] = [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 4),
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),
    ]
    if reject_x32:
        instructions.extend(
            (
                _SockFilter(_BPF_JMP_JSET_K, 0, 1, _X32_SYSCALL_BIT),
                _SockFilter(
                    _BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM
                ),
            )
        )
    for syscall_number in sorted(blocked_syscalls):
        instructions.extend(
            (
                _SockFilter(_BPF_JMP_JEQ_K, 0, 1, syscall_number),
                _SockFilter(
                    _BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM
                ),
            )
        )
    instructions.append(_SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    return instructions


def _install_seccomp_filter(
    machine: str, *, allow_image_replace: bool = False
) -> None:
    instructions = _build_seccomp_filter(
        machine, allow_image_replace=allow_image_replace
    )
    program_array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len=len(instructions), filter=program_array)
    _set_no_new_privileges()
    libc = _libc()
    result = libc.prctl(
        _PR_SET_SECCOMP,
        _SECCOMP_MODE_FILTER,
        ctypes.byref(program),
        0,
        0,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise SandboxUnavailableError(f"could not install seccomp: errno={error}")


def apply_worker_sandbox(
    run_root: Path,
    environment_root: Path,
    source_root: Path,
) -> SandboxAttestation:
    """Install the complete Worker kernel boundary before untrusted input.

    All rule roots are captured and revalidated before the irreversible steps.
    Failure raises; callers must treat that as a fatal Worker launch failure and
    must never continue in an unconfined mode.
    """

    qualification = require_qualified_host()
    readable, run_roots = _prepare_rules(
        Path(run_root), Path(environment_root), Path(source_root)
    )
    parent_pid = configure_parent_death_signal()
    _set_process_nondumpable()
    _install_landlock(readable, run_roots)
    _install_seccomp_filter(qualification.machine)
    return SandboxAttestation(
        landlock_abi=qualification.landlock_abi,
        seccomp_arch=qualification.machine,
        seccomp_mode=_SECCOMP_MODE_FILTER,
        no_new_privileges=True,
        parent_pid=parent_pid,
    )


__all__ = [
    "HostQualification",
    "MINIMUM_LANDLOCK_ABI",
    "SandboxAttestation",
    "SandboxUnavailableError",
    "WorkerResourceLimits",
    "apply_worker_resource_limits",
    "apply_worker_sandbox",
    "apply_worker_umask",
    "apply_checkout_write_confinement",
    "apply_bounded_bash_sandbox",
    "apply_read_only_command_confinement",
    "apply_singleton_command_sandbox",
    "close_worker_file_descriptors",
    "configure_parent_death_signal",
    "host_qualification",
    "landlock_abi",
    "qualify_host",
    "require_qualified_host",
    "verify_worker_file_descriptors",
]
