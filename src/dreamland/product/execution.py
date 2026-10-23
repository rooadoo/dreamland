"""Protocol-neutral execution boundary for product-session commands.

The shared product core owns acceptance, fencing, publication, journals, and
resource accounting.  An executor receives only an isolated provisional
document and returns a qualified result; it never receives the live aggregate
or mutation authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias

_SIGNAL_PATTERN = re.compile(r"^SIG[A-Z0-9]{1,15}$")
_SAFE_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProductExecutionOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ProductGuestTerminationKind(StrEnum):
    EXITED = "exited"
    SIGNALED = "signaled"


class ProductStateEffectKind(StrEnum):
    NONE = "none"
    COMMITTED = "committed"
    REJECTED = "rejected"


class ProductOutputStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class ProductGuestTermination:
    """Exactly one guest termination variant for a completed command."""

    kind: ProductGuestTerminationKind
    exit_status: int | None = None
    signal: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ProductGuestTerminationKind.EXITED:
            if (
                isinstance(self.exit_status, bool)
                or not isinstance(self.exit_status, int)
                or not -(2**31) <= self.exit_status < 2**31
                or self.signal is not None
            ):
                raise ValueError("exited termination requires one integer status")
            return
        if self.kind is ProductGuestTerminationKind.SIGNALED:
            if (
                self.exit_status is not None
                or not isinstance(self.signal, str)
                or not _SIGNAL_PATTERN.fullmatch(self.signal)
            ):
                raise ValueError("signaled termination requires one canonical signal")
            return
        raise ValueError("guest termination kind is invalid")


@dataclass(frozen=True, slots=True)
class ProductStateEffect:
    """Qualified optional semantic effect produced against provisional state."""

    kind: ProductStateEffectKind
    provisional_document: Mapping[str, object] | None = field(default=None, repr=False)
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ProductStateEffectKind.COMMITTED:
            if not isinstance(self.provisional_document, Mapping):
                raise ValueError(
                    "committed state effect requires a provisional document"
                )
            if self.safe_reason is not None:
                raise ValueError(
                    "committed state effect cannot carry a rejection reason"
                )
            return
        if self.provisional_document is not None:
            raise ValueError("non-committed state effect cannot carry a document")
        if self.kind is ProductStateEffectKind.NONE:
            if self.safe_reason is not None:
                raise ValueError("none state effect cannot carry a rejection reason")
            return
        if self.kind is ProductStateEffectKind.REJECTED:
            if self.safe_reason is not None and not _safe_reason(self.safe_reason):
                raise ValueError("state-effect rejection reason is invalid")
            return
        raise ValueError("state effect kind is invalid")


@dataclass(frozen=True, slots=True)
class ProductExecutionResult:
    """One legal execution terminal result before aggregate publication."""

    outcome: ProductExecutionOutcome
    guest_termination: ProductGuestTermination | None = None
    state_effect: ProductStateEffect | None = None
    reason: str | None = None
    usage_tokens: int = 0
    usage_spend_units: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("usage_tokens", self.usage_tokens),
            ("usage_spend_units", self.usage_spend_units),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < 2**63
            ):
                raise ValueError(f"{name} must be a non-negative bounded integer")
        if self.outcome is ProductExecutionOutcome.COMPLETED:
            if self.guest_termination is None or self.state_effect is None:
                raise ValueError(
                    "completed execution requires termination and state effect"
                )
            if self.reason is not None:
                raise ValueError("completed execution cannot carry a failure reason")
            return
        if self.guest_termination is not None or self.state_effect is not None:
            raise ValueError(
                "interrupted or failed execution cannot carry guest/state outcomes"
            )
        if not _safe_reason(self.reason):
            raise ValueError("interrupted or failed execution requires a safe reason")

    @classmethod
    def completed_exited(
        cls,
        *,
        exit_status: int,
        state_effect: ProductStateEffect | None = None,
        usage_tokens: int = 0,
        usage_spend_units: int = 0,
    ) -> ProductExecutionResult:
        return cls(
            outcome=ProductExecutionOutcome.COMPLETED,
            guest_termination=ProductGuestTermination(
                ProductGuestTerminationKind.EXITED,
                exit_status=exit_status,
            ),
            state_effect=state_effect
            or ProductStateEffect(ProductStateEffectKind.NONE),
            usage_tokens=usage_tokens,
            usage_spend_units=usage_spend_units,
        )

    @classmethod
    def completed_signaled(
        cls,
        *,
        signal: str,
        state_effect: ProductStateEffect | None = None,
        usage_tokens: int = 0,
        usage_spend_units: int = 0,
    ) -> ProductExecutionResult:
        return cls(
            outcome=ProductExecutionOutcome.COMPLETED,
            guest_termination=ProductGuestTermination(
                ProductGuestTerminationKind.SIGNALED,
                signal=signal,
            ),
            state_effect=state_effect
            or ProductStateEffect(ProductStateEffectKind.NONE),
            usage_tokens=usage_tokens,
            usage_spend_units=usage_spend_units,
        )

    @classmethod
    def interrupted(
        cls,
        reason: str = "cancelled",
        *,
        usage_tokens: int = 0,
        usage_spend_units: int = 0,
    ) -> ProductExecutionResult:
        return cls(
            outcome=ProductExecutionOutcome.INTERRUPTED,
            reason=reason,
            usage_tokens=usage_tokens,
            usage_spend_units=usage_spend_units,
        )

    @classmethod
    def failed(
        cls,
        reason: str,
        *,
        usage_tokens: int = 0,
        usage_spend_units: int = 0,
    ) -> ProductExecutionResult:
        return cls(
            outcome=ProductExecutionOutcome.FAILED,
            reason=reason,
            usage_tokens=usage_tokens,
            usage_spend_units=usage_spend_units,
        )


@dataclass(frozen=True, slots=True)
class ProductOutputChunk:
    stream: ProductOutputStream
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream, ProductOutputStream):
            raise ValueError("output stream is invalid")
        if not isinstance(self.text, str):
            raise ValueError("output text must be a string")


ProductOutputPublisher: TypeAlias = Callable[[ProductOutputChunk], bool | None]


@dataclass(frozen=True, slots=True)
class ProductExecutionRequest:
    """Isolated input for one already accepted logical command."""

    service_epoch: str
    session_id: str
    command_id: str
    slot_generation: int
    canonical_command: str = field(repr=False)
    provisional_document: Mapping[str, object] = field(repr=False)
    checkpoint_namespace: str
    deadline_ms: int
    max_tokens: int
    max_spend_units: int
    max_attempts: int
    cancelled: Callable[[], bool] = field(repr=False)

    def __post_init__(self) -> None:
        for identity in (
            self.service_epoch,
            self.session_id,
            self.command_id,
            self.checkpoint_namespace,
        ):
            if not isinstance(identity, str) or not identity:
                raise ValueError("execution identity is invalid")
        if (
            isinstance(self.slot_generation, bool)
            or not isinstance(self.slot_generation, int)
            or self.slot_generation <= 0
        ):
            raise ValueError("slot generation must be a positive integer")
        if not isinstance(self.canonical_command, str) or not self.canonical_command:
            raise ValueError("canonical command must not be empty")
        if not isinstance(self.provisional_document, Mapping):
            raise ValueError("provisional document must be a mapping")
        if (
            isinstance(self.deadline_ms, bool)
            or not isinstance(self.deadline_ms, int)
            or self.deadline_ms < 0
        ):
            raise ValueError("execution deadline is invalid")
        for name, value in (
            ("max_tokens", self.max_tokens),
            ("max_spend_units", self.max_spend_units),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < 2**63
            ):
                raise ValueError(f"{name} must be a non-negative bounded integer")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts < 2**63
        ):
            raise ValueError("max_attempts must be a positive bounded integer")
        if not callable(self.cancelled):
            raise ValueError("cancelled must be callable")


class ProductCommandExecutor(Protocol):
    """Execute against provisional state and emit bounded candidate output."""

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult: ...


def _safe_reason(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_REASON_PATTERN.fullmatch(value))


__all__ = [
    "ProductCommandExecutor",
    "ProductExecutionOutcome",
    "ProductExecutionRequest",
    "ProductExecutionResult",
    "ProductGuestTermination",
    "ProductGuestTerminationKind",
    "ProductOutputChunk",
    "ProductOutputPublisher",
    "ProductOutputStream",
    "ProductStateEffect",
    "ProductStateEffectKind",
]
