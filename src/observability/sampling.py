"""Tail-sampling policy independent of any telemetry SDK."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class TailSamplingPolicy:
    """Keep errors and slow traces, then deterministically sample successes."""

    success_ratio: float = 0.2
    slow_request_ms: float = 5_000.0
    keep_errors: bool = True
    keep_slow: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.success_ratio <= 1.0:
            raise ValueError("success_ratio must be between 0 and 1")
        if self.slow_request_ms < 0:
            raise ValueError("slow_request_ms cannot be negative")

    def should_keep(
        self,
        trace_key: str,
        *,
        has_error: bool = False,
        max_duration_ms: float = 0.0,
    ) -> bool:
        if self.keep_errors and has_error:
            return True
        if self.keep_slow and max_duration_ms >= self.slow_request_ms:
            return True
        if self.success_ratio <= 0:
            return False
        if self.success_ratio >= 1:
            return True

        digest = hashlib.blake2b(trace_key.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest, "big") / float(2**64)
        return bucket < self.success_ratio
