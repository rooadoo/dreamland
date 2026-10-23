"""Inert, protocol-neutral logical admission for public attendee traffic.

The ledger deliberately keys admission by an opaque attendee-context digest.
It has no knowledge of network addresses, browser cookies, HTTP requests,
providers, workers, or product idempotency.  The owning attendee facade must
resolve an existing idempotency claim before asking this ledger to admit a
fresh logical operation.  That facade charges context bootstrap, both
grant-issuing exchanges, create, submit, reset, and switch.  Reads plus cancel,
close, and revoke remain uncharged so rate exhaustion cannot prevent recovery,
interruption, cleanup, or authority revocation.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class AttendeeRequestAdmissionPolicy:
    """Bounds for one monotonic fixed-window logical admission ledger."""

    window_ms: int = 1_000
    max_fresh_operations_per_context: int = 8
    max_fresh_operations_global: int = 256
    max_tracked_contexts: int = 256

    def __post_init__(self) -> None:
        for name in (
            "window_ms",
            "max_fresh_operations_per_context",
            "max_fresh_operations_global",
            "max_tracked_contexts",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a positive safe integer")


class AttendeeRequestAdmissionDenied(RuntimeError):
    """Retryable fresh-operation rejection with no downstream claim."""

    def __init__(self, retry_after_ms: int) -> None:
        super().__init__("attendee logical request rate is temporarily exhausted")
        self.retry_after_ms = retry_after_ms


@dataclass(slots=True)
class _Window:
    opened_at_ms: int
    accepted: int = 0


class AttendeeRequestAdmissionLedger:
    """Thread-safe per-context and global fresh-operation admission.

    Windows use a trusted monotonic deadline supplied by the composition.  A
    denial changes no acceptance counter, making it safe for the caller to
    return an unclaimed retryable outcome.  Expired context windows are pruned,
    so attacker-controlled labels cannot create an unbounded map.
    """

    def __init__(self, policy: AttendeeRequestAdmissionPolicy | None = None) -> None:
        self._policy = policy or AttendeeRequestAdmissionPolicy()
        self._lock = threading.Lock()
        self._last_deadline_ms = 0
        self._global: _Window | None = None
        self._contexts: dict[str, _Window] = {}

    @property
    def policy(self) -> AttendeeRequestAdmissionPolicy:
        return self._policy

    def admit_fresh(self, *, context_sha256: str, deadline_ms: int) -> None:
        """Atomically charge one fresh logical operation or reject it."""
        if not isinstance(context_sha256, str) or not _SHA256_PATTERN.fullmatch(
            context_sha256
        ):
            raise ValueError("context_sha256 must be a lowercase SHA-256 digest")
        if (
            isinstance(deadline_ms, bool)
            or not isinstance(deadline_ms, int)
            or not 0 <= deadline_ms <= _MAX_SAFE_INTEGER
        ):
            raise ValueError("deadline_ms must be a non-negative safe integer")
        with self._lock:
            if deadline_ms < self._last_deadline_ms:
                raise RuntimeError("deadline_ms must not move backward")
            self._last_deadline_ms = deadline_ms
            self._prune_locked(deadline_ms)

            global_window = self._global
            if global_window is None:
                global_window = _Window(deadline_ms)
            context_window = self._contexts.get(context_sha256)
            if context_window is None:
                if len(self._contexts) >= self._policy.max_tracked_contexts:
                    retry_after_ms = min(
                        self._remaining_ms(window, deadline_ms)
                        for window in self._contexts.values()
                    )
                    if (
                        global_window.accepted
                        >= self._policy.max_fresh_operations_global
                    ):
                        retry_after_ms = max(
                            retry_after_ms,
                            self._remaining_ms(global_window, deadline_ms),
                        )
                    raise AttendeeRequestAdmissionDenied(retry_after_ms)
                context_window = _Window(deadline_ms)

            retry_after_ms = 0
            if global_window.accepted >= self._policy.max_fresh_operations_global:
                retry_after_ms = max(
                    retry_after_ms,
                    self._remaining_ms(global_window, deadline_ms),
                )
            if context_window.accepted >= self._policy.max_fresh_operations_per_context:
                retry_after_ms = max(
                    retry_after_ms,
                    self._remaining_ms(context_window, deadline_ms),
                )
            if retry_after_ms:
                raise AttendeeRequestAdmissionDenied(retry_after_ms)

            global_window.accepted += 1
            context_window.accepted += 1
            self._global = global_window
            self._contexts[context_sha256] = context_window

    def snapshot(self) -> dict[str, int]:
        """Return aggregate safe accounting without projecting context keys."""
        with self._lock:
            return {
                "window_ms": self._policy.window_ms,
                "tracked_contexts": len(self._contexts),
                "global_accepted": (
                    0 if self._global is None else self._global.accepted
                ),
            }

    def _prune_locked(self, deadline_ms: int) -> None:
        if self._global is not None and not self._remaining_ms(
            self._global, deadline_ms
        ):
            self._global = None
        for context_sha256, window in tuple(self._contexts.items()):
            if not self._remaining_ms(window, deadline_ms):
                self._contexts.pop(context_sha256, None)

    def _remaining_ms(self, window: _Window, deadline_ms: int) -> int:
        return max(0, window.opened_at_ms + self._policy.window_ms - deadline_ms)


__all__ = [
    "AttendeeRequestAdmissionDenied",
    "AttendeeRequestAdmissionLedger",
    "AttendeeRequestAdmissionPolicy",
]
