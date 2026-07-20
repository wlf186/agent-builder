"""Project-local bootstrap token and bounded in-memory web sessions.

The web adapter owns cookie/header parsing.  This module deliberately has no
HTTP framework dependency and never persists browser sessions or CSRF tokens.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_TOKEN_PATH = Path(".runtime/secrets/web-bootstrap-token")
TOKEN_BYTES = 32
TOKEN_HEX_LENGTH = TOKEN_BYTES * 2
MIN_PROJECT_TOKEN_LENGTH = 10
MAX_PROJECT_TOKEN_LENGTH = 128
MAX_CREDENTIAL_LENGTH = 256
_PROJECT_TOKEN = re.compile(
    rf"[A-Za-z0-9._~+-]{{{MIN_PROJECT_TOKEN_LENGTH},{MAX_PROJECT_TOKEN_LENGTH}}}"
)


def is_valid_project_token(value: object) -> bool:
    """Accept generated tokens and bounded operator-chosen ASCII tokens."""

    return isinstance(value, str) and _PROJECT_TOKEN.fullmatch(value) is not None


class AuthenticationError(PermissionError):
    """A bootstrap token or browser session was not valid."""


class CsrfError(PermissionError):
    """A state-changing request did not prove CSRF possession."""


class SessionCapacityError(RuntimeError):
    """The bounded in-memory session store is full."""


class ProjectTokenStore:
    """Load or atomically create one checkout-contained bootstrap token.

    ``relative_path`` is configuration supplied by the composition root, never
    request data.  Publication uses a same-directory temporary file followed by
    Linux ``renameat2(RENAME_NOREPLACE)``, so concurrent starters cannot replace
    one another's credential and the published inode never has a hard-link
    count greater than one.
    """

    def __init__(
        self,
        repository_root: Path,
        relative_path: Path | str = DEFAULT_TOKEN_PATH,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        if not self.repository_root.is_dir():
            raise ValueError("repository_root must be a directory")

        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("token path must be a contained relative path")
        self.relative_path = relative
        self.path = self.repository_root / relative

    def load_or_create(self) -> str:
        """Return the stable token, creating it with mode ``0600`` if absent."""

        self._ensure_private_parent()
        try:
            return self._read_existing()
        except FileNotFoundError:
            pass

        token = secrets.token_hex(TOKEN_BYTES)
        temporary = self.path.parent / (
            f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        flags |= self._no_follow_flag()
        published = False
        try:
            self._write_candidate(temporary, token, flags)

            # A concurrent creator may win; its fully written file is then
            # authoritative and this process discards its candidate.
            published = self._publish_noreplace(temporary)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        if published:
            self._fsync_parent()
        return self._read_existing()

    def replace(self, token: str) -> None:
        """Atomically rotate the private token without exposing it in argv.

        Callers must obtain ``token`` through a non-logging channel such as an
        interactive no-echo prompt.  Existing unsafe secret paths are rejected
        instead of silently replaced.
        """

        if not is_valid_project_token(token):
            raise ValueError("project token has an invalid format")
        self._ensure_private_parent()
        try:
            self._read_existing()
        except FileNotFoundError:
            pass

        temporary = self.path.parent / (
            f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        flags |= self._no_follow_flag()
        try:
            self._write_candidate(temporary, token, flags)
            os.replace(temporary, self.path)
            self._fsync_parent()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        verified = self._read_existing()
        if not hmac.compare_digest(
            hashlib.sha256(verified.encode("ascii")).digest(),
            hashlib.sha256(token.encode("ascii")).digest(),
        ):
            raise RuntimeError("project token rotation verification failed")

    @staticmethod
    def _write_candidate(temporary: Path, token: str, flags: int) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            encoded = (token + "\n").encode("ascii")
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("failed to write project token")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _publish_noreplace(self, temporary: Path) -> bool:
        """Atomically move one complete candidate into an absent final path."""

        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as exc:
            raise RuntimeError("secure token publication requires renameat2") from exc
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(temporary),
            -100,
            os.fsencode(self.path),
            1,
        )
        if result == 0:
            return True
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            return False
        raise OSError(error, os.strerror(error))

    @staticmethod
    def _no_follow_flag() -> int:
        flag = getattr(os, "O_NOFOLLOW", None)
        if flag is None:
            raise RuntimeError("secure token files require O_NOFOLLOW")
        return flag

    def _ensure_private_parent(self) -> None:
        current = self.repository_root
        for component in self.relative_path.parent.parts:
            current = current / component
            try:
                os.mkdir(current, mode=0o700)
                created = True
            except FileExistsError:
                created = False
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("token directory is not a real directory")
            if created:
                os.chmod(current, 0o700)

        parent_metadata = os.lstat(self.path.parent)
        if parent_metadata.st_uid != os.getuid():
            raise RuntimeError("token directory is not owned by this user")
        os.chmod(self.path.parent, 0o700)

    def _read_existing(self) -> str:
        flags = os.O_RDONLY | os.O_CLOEXEC | self._no_follow_flag()
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("project token is not a regular file")
            if metadata.st_uid != os.getuid():
                raise RuntimeError("project token is not owned by this user")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise RuntimeError("project token must have mode 0600")
            if metadata.st_nlink != 1:
                raise RuntimeError("project token must not have another hard link")
            raw = os.read(descriptor, MAX_PROJECT_TOKEN_LENGTH + 2)
            if os.read(descriptor, 1):
                raise RuntimeError("project token is unexpectedly large")
        finally:
            os.close(descriptor)

        if raw.endswith(b"\n"):
            raw = raw[:-1]
        try:
            token = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("project token is not valid ASCII") from exc
        if not is_valid_project_token(token):
            raise RuntimeError("project token has an invalid format")
        return token

    def _fsync_parent(self) -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class Session:
    """Secrets returned once by a successful login response."""

    session_id: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Non-secret identity returned after cookie/header validation."""

    session_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    csrf_digest: bytes
    expires_at: datetime
    expires_monotonic: float


class SessionService:
    """Bounded, process-local fixed-lifetime sessions with bound CSRF tokens."""

    def __init__(
        self,
        project_token: str,
        *,
        ttl_seconds: float = 8 * 60 * 60,
        max_sessions: int = 256,
        monotonic_clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not self._valid_credential(project_token):
            raise ValueError("project_token has an invalid format")
        if ttl_seconds <= 0 or ttl_seconds > 30 * 24 * 60 * 60:
            raise ValueError("ttl_seconds must be between 0 and 30 days")
        if max_sessions <= 0 or max_sessions > 10_000:
            raise ValueError("max_sessions must be between 1 and 10000")

        self._project_token_digest = hashlib.sha256(
            project_token.encode("ascii")
        ).digest()
        self._ttl_seconds = float(ttl_seconds)
        self._max_sessions = max_sessions
        self._clock = monotonic_clock
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._pepper = secrets.token_bytes(32)
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._lock = threading.Lock()

    def create(self, presented_project_token: str | None) -> Session:
        """Exchange the project token for one cookie ID and one CSRF secret."""

        if not self._constant_time_token_match(presented_project_token):
            raise AuthenticationError("authentication failed")

        now_monotonic = self._clock()
        now_utc = self._utc_clock()
        if now_utc.tzinfo is None or now_utc.utcoffset() is None:
            raise RuntimeError("utc_clock must return a timezone-aware datetime")
        expires_at = now_utc + timedelta(seconds=self._ttl_seconds)

        with self._lock:
            self._purge_expired_locked(now_monotonic)
            if len(self._sessions) >= self._max_sessions:
                raise SessionCapacityError("session capacity exhausted")
            for _attempt in range(4):
                session_id = secrets.token_urlsafe(32)
                session_digest = self._digest(session_id)
                if session_digest not in self._sessions:
                    break
            else:
                raise RuntimeError("could not allocate a unique session")

            csrf_token = secrets.token_urlsafe(32)
            self._sessions[session_digest] = _SessionRecord(
                csrf_digest=self._digest(csrf_token),
                expires_at=expires_at,
                expires_monotonic=now_monotonic + self._ttl_seconds,
            )
        return Session(session_id, csrf_token, expires_at)

    def validate(self, session_id: str | None) -> AuthenticatedSession:
        """Validate a session cookie without returning its CSRF secret."""

        with self._lock:
            record = self._validate_locked(session_id, self._clock())
            assert session_id is not None
            return AuthenticatedSession(session_id, record.expires_at)

    def validate_csrf(
        self,
        session_id: str | None,
        csrf_token: str | None,
    ) -> AuthenticatedSession:
        """Validate the HttpOnly cookie and its separately supplied CSRF token."""

        with self._lock:
            record = self._validate_locked(session_id, self._clock())
            if not self._bounded_text(csrf_token):
                raise CsrfError("CSRF validation failed")
            assert csrf_token is not None
            if not hmac.compare_digest(record.csrf_digest, self._digest(csrf_token)):
                raise CsrfError("CSRF validation failed")
            assert session_id is not None
            return AuthenticatedSession(session_id, record.expires_at)

    def revoke(self, session_id: str | None) -> bool:
        """Idempotently revoke one session."""

        if not self._bounded_text(session_id):
            return False
        assert session_id is not None
        with self._lock:
            return self._sessions.pop(self._digest(session_id), None) is not None

    def revoke_all(self) -> int:
        """Invalidate every browser session, for shutdown or token rotation."""

        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def purge_expired(self) -> int:
        """Remove expired sessions and return the number removed."""

        with self._lock:
            return self._purge_expired_locked(self._clock())

    @property
    def active_count(self) -> int:
        with self._lock:
            self._purge_expired_locked(self._clock())
            return len(self._sessions)

    def _validate_locked(
        self,
        session_id: str | None,
        now_monotonic: float,
    ) -> _SessionRecord:
        if not self._bounded_text(session_id):
            raise AuthenticationError("authentication required")
        assert session_id is not None
        digest = self._digest(session_id)
        record = self._sessions.get(digest)
        if record is None:
            raise AuthenticationError("authentication required")
        if record.expires_monotonic <= now_monotonic:
            self._sessions.pop(digest, None)
            raise AuthenticationError("authentication required")
        return record

    def _purge_expired_locked(self, now_monotonic: float) -> int:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if record.expires_monotonic <= now_monotonic
        ]
        for digest in expired:
            del self._sessions[digest]
        return len(expired)

    def _constant_time_token_match(self, candidate: str | None) -> bool:
        valid = is_valid_project_token(candidate)
        if self._bounded_text(candidate):
            assert candidate is not None
            candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        else:
            # Perform the same fixed-size comparison for malformed or
            # unbounded input without hashing attacker-controlled large text.
            candidate_digest = b"\0" * hashlib.sha256().digest_size
        matched = hmac.compare_digest(self._project_token_digest, candidate_digest)
        return bool(valid and matched)

    def _digest(self, value: str) -> bytes:
        return hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _bounded_text(value: str | None) -> bool:
        return isinstance(value, str) and 0 < len(value) <= MAX_CREDENTIAL_LENGTH

    @staticmethod
    def _valid_credential(value: str) -> bool:
        return is_valid_project_token(value)
