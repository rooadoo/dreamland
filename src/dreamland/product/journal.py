"""Bounded, protocol-neutral product event and transcript journals.

One :class:`ProductSessionJournal` belongs to one product session.  It has no
transport, runtime, authority, provider, worker, or timer dependency and is not
imported by normal DREAMLAND startup.  Callers may append only already-safe,
typed product projections; raw runtime state and private authority material are
deliberately outside this API.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PAYLOAD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_ID_PART_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_STREAM_ID_PATTERN = re.compile(r"^stream:[A-Za-z0-9_-]{8,96}$")
_TRANSCRIPT_ID_PATTERN = re.compile(r"^transcript:[A-Za-z0-9_-]{8,96}$")
_EVENT_ID_PATTERN = re.compile(r"^event:[A-Za-z0-9_-]{8,96}:[1-9][0-9]*$")
_ENTRY_ID_PATTERN = re.compile(r"^entry:[A-Za-z0-9_-]{8,96}:[1-9][0-9]*$")
_MAX_COUNTER = 2**63 - 1
_MAX_SAFE_INTEGER = 2**53 - 1
_SCHEMA_REVISION = "1"

# These names identify data classes that must be projected by an owning layer,
# never accepted as an allegedly safe event payload.  This is a structural
# guard, not a claim that arbitrary strings can be inspected for secrets.
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "admin",
        "admin_payload",
        "admin_request",
        "admin_response",
        "api_key",
        "authority",
        "authorization",
        "capability",
        "credential",
        "credentials",
        "environment",
        "internal_path",
        "launch_grant",
        "model_prompt",
        "password",
        "private_key",
        "prompt",
        "provider",
        "provider_envelope",
        "provider_payload",
        "provider_request",
        "provider_response",
        "provider_state",
        "raw_state",
        "reasoning",
        "runtime_document",
        "runtime_state",
        "secret",
        "session_authority",
        "stack_trace",
        "state_profile",
        "system_prompt",
        "token",
        "traceback",
        "world_state",
    }
)

SafeValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["SafeValue", ...]
    | Mapping[str, "SafeValue"]
)


class ProductEventCriticality(StrEnum):
    """How consumers handle an event type they do not recognize."""

    AUTHORITATIVE = "authoritative"
    INFORMATIONAL = "informational"


class TranscriptKind(StrEnum):
    """Canonical product transcript entry kinds."""

    COMMAND = "command"
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"
    SAFE_ERROR = "safe_error"


class TranscriptClip(StrEnum):
    """Which edge of oversized projected text remains visible."""

    KEEP_START = "keep_start"
    KEEP_END = "keep_end"


class CursorDomain(StrEnum):
    """Independent journal coordinate namespaces."""

    EVENT = "event"
    TRANSCRIPT = "transcript"


class SnapshotRequiredReason(StrEnum):
    """Stable reasons a syntactically valid cursor cannot be replayed."""

    WRONG_STREAM = "wrong_stream"
    BEFORE_RETENTION_FLOOR = "before_retention_floor"
    FUTURE_POSITION = "future_position"
    POSITION_MISMATCH = "position_mismatch"


@dataclass(frozen=True, slots=True)
class JournalLimits:
    """Explicit content and collection bounds for one session journal.

    Byte limits count canonical UTF-8 payload bytes for events and retained
    projected UTF-8 text bytes for transcript entries.  Fixed-size bounded
    metadata is additionally bounded by the corresponding item-count limit.
    """

    max_events: int = 256
    max_event_bytes: int = 256 * 1024
    max_event_payload_bytes: int = 16 * 1024
    max_payload_depth: int = 8
    max_payload_items: int = 256
    max_transcript_entries: int = 512
    max_transcript_bytes: int = 1024 * 1024
    max_transcript_entry_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_events",
            "max_event_bytes",
            "max_event_payload_bytes",
            "max_payload_depth",
            "max_payload_items",
            "max_transcript_entries",
            "max_transcript_bytes",
            "max_transcript_entry_bytes",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_COUNTER
            ):
                raise ValueError(f"{name} must be a positive bounded integer")
        if self.max_event_payload_bytes > self.max_event_bytes:
            raise ValueError("max_event_payload_bytes cannot exceed max_event_bytes")
        if self.max_transcript_entry_bytes > self.max_transcript_bytes:
            raise ValueError(
                "max_transcript_entry_bytes cannot exceed max_transcript_bytes"
            )


@dataclass(frozen=True, slots=True)
class EventCursor:
    """An exact replay position in one event stream."""

    stream_id: str
    sequence: int
    event_id: str | None

    def __post_init__(self) -> None:
        _validate_identifier(self.stream_id, _STREAM_ID_PATTERN, "stream_id")
        _validate_counter(self.sequence, "sequence", allow_zero=True)
        if self.sequence == 0:
            if self.event_id is not None:
                raise ValueError("the initial event cursor has no event_id")
        elif self.event_id is None:
            raise ValueError("a noninitial event cursor requires event_id")
        else:
            _validate_identifier(self.event_id, _EVENT_ID_PATTERN, "event_id")

    def to_dict(self) -> dict[str, object]:
        """Allocate a transport-friendly copy."""
        return {
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    """An exact replay position in the independent transcript namespace."""

    transcript_id: str
    position: int
    entry_id: str | None

    def __post_init__(self) -> None:
        _validate_identifier(
            self.transcript_id, _TRANSCRIPT_ID_PATTERN, "transcript_id"
        )
        _validate_counter(self.position, "position", allow_zero=True)
        if self.position == 0:
            if self.entry_id is not None:
                raise ValueError("the initial transcript cursor has no entry_id")
        elif self.entry_id is None:
            raise ValueError("a noninitial transcript cursor requires entry_id")
        else:
            _validate_identifier(self.entry_id, _ENTRY_ID_PATTERN, "entry_id")

    def to_dict(self) -> dict[str, object]:
        """Allocate a transport-friendly copy."""
        return {
            "transcript_id": self.transcript_id,
            "position": self.position,
            "entry_id": self.entry_id,
        }


class JournalSnapshotRequired(RuntimeError):
    """A valid cursor cannot produce a complete within-stream replay."""

    def __init__(
        self,
        *,
        domain: CursorDomain,
        reason: SnapshotRequiredReason,
        requested_position: int,
        retention_floor: EventCursor | TranscriptCursor,
        current_position: EventCursor | TranscriptCursor,
    ) -> None:
        super().__init__("The requested journal position requires a snapshot.")
        self.domain = domain
        self.reason = reason
        self.requested_position = requested_position
        self.retention_floor = retention_floor
        self.current_position = current_position


@dataclass(frozen=True, slots=True)
class ProductEvent:
    """One deeply immutable safe product event."""

    stream_id: str
    sequence: int
    event_id: str
    event_type: str
    criticality: ProductEventCriticality
    emitted_at_ms: int
    command_id: str | None
    data: Mapping[str, SafeValue] = field(repr=False)
    schema_revision: str = _SCHEMA_REVISION

    @property
    def cursor(self) -> EventCursor:
        return EventCursor(self.stream_id, self.sequence, self.event_id)

    def to_dict(self) -> dict[str, object]:
        """Allocate a deep transport-friendly copy of this immutable event."""
        return {
            "schema_revision": self.schema_revision,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "type": self.event_type,
            "criticality": self.criticality.value,
            "emitted_at_ms": self.emitted_at_ms,
            "command_id": self.command_id,
            "data": _thaw(self.data),
        }


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One deeply immutable inert-text transcript projection."""

    transcript_id: str
    position: int
    entry_id: str
    kind: TranscriptKind
    recorded_at_ms: int
    command_id: str | None
    text: str
    omitted_before_bytes: int
    omitted_after_bytes: int
    transformed: bool
    chunk_index: int | None
    final: bool | None
    encoding: str = "utf-8"
    projection: str = "inert-text-v1"

    @property
    def cursor(self) -> TranscriptCursor:
        return TranscriptCursor(self.transcript_id, self.position, self.entry_id)

    @property
    def clipped(self) -> bool:
        return self.omitted_before_bytes > 0 or self.omitted_after_bytes > 0

    def to_dict(self) -> dict[str, object]:
        """Allocate a transport-friendly copy."""
        return {
            "transcript_id": self.transcript_id,
            "position": self.position,
            "entry_id": self.entry_id,
            "kind": self.kind.value,
            "recorded_at_ms": self.recorded_at_ms,
            "command_id": self.command_id,
            "text": self.text,
            "encoding": self.encoding,
            "projection": self.projection,
            "transformed": self.transformed,
            "clipped": self.clipped,
            "omitted_before_bytes": self.omitted_before_bytes,
            "omitted_after_bytes": self.omitted_after_bytes,
            "chunk_index": self.chunk_index,
            "final": self.final,
        }


@dataclass(frozen=True, slots=True)
class EventJournalSnapshot:
    """An immutable event snapshot or complete replay result."""

    events: tuple[ProductEvent, ...]
    retention_floor: EventCursor
    position: EventCursor
    retained_payload_bytes: int


@dataclass(frozen=True, slots=True)
class TranscriptJournalSnapshot:
    """An immutable transcript snapshot or complete replay result."""

    entries: tuple[TranscriptEntry, ...]
    retention_floor: TranscriptCursor
    position: TranscriptCursor
    omitted_before_entries: int
    omitted_after_entries: int
    retained_text_bytes: int


@dataclass(frozen=True, slots=True)
class ProductJournalSnapshot:
    """One lock-consistent immutable view of both independent journals."""

    events: EventJournalSnapshot
    transcript: TranscriptJournalSnapshot


@dataclass(frozen=True, slots=True)
class _TranscriptProjection:
    text: str
    omitted_before_bytes: int
    omitted_after_bytes: int
    transformed: bool
    text_bytes: int

    @property
    def clipped(self) -> bool:
        return self.omitted_before_bytes > 0 or self.omitted_after_bytes > 0


class ProductSessionJournal:
    """Append-only bounded event and transcript state for exactly one session."""

    def __init__(
        self,
        *,
        limits: JournalLimits | None = None,
        id_factory: Callable[[str], str] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if limits is not None and not isinstance(limits, JournalLimits):
            raise TypeError("limits must be JournalLimits")
        if id_factory is not None and not callable(id_factory):
            raise TypeError("id_factory must be callable")
        if clock_ms is not None and not callable(clock_ms):
            raise TypeError("clock_ms must be callable")

        self.limits = limits or JournalLimits()
        self._id_factory = id_factory or _random_id_part
        self._clock_ms = clock_ms or _system_clock_ms
        event_part = self._new_id_part("event_stream")
        transcript_part = self._new_id_part("transcript")
        self._stream_id = f"stream:{event_part}"
        self._event_id_part = event_part
        self._transcript_id = f"transcript:{transcript_part}"
        self._transcript_id_part = transcript_part

        self._events: deque[ProductEvent] = deque()
        self._event_payload_sizes: deque[int] = deque()
        self._event_bytes = 0
        self._event_sequence = 0
        self._event_floor = 0

        self._transcript: deque[TranscriptEntry] = deque()
        self._transcript_text_sizes: deque[int] = deque()
        self._transcript_bytes = 0
        self._transcript_position = 0
        self._transcript_floor = 0
        self._lock = threading.RLock()

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def transcript_id(self) -> str:
        return self._transcript_id

    @property
    def event_position(self) -> EventCursor:
        with self._lock:
            return self._event_cursor(self._event_sequence)

    @property
    def transcript_position(self) -> TranscriptCursor:
        with self._lock:
            return self._transcript_cursor(self._transcript_position)

    def append_event(
        self,
        event_type: str,
        *,
        criticality: ProductEventCriticality,
        data: Mapping[str, object] | None = None,
        command_id: str | None = None,
    ) -> ProductEvent:
        """Validate and append one immutable event, evicting oldest content."""
        _validate_identifier(event_type, _EVENT_TYPE_PATTERN, "event_type")
        if not isinstance(criticality, ProductEventCriticality):
            raise TypeError("criticality must be ProductEventCriticality")
        _validate_optional_correlation_id(command_id)
        frozen_data, payload_size = _safe_payload(
            data,
            max_depth=self.limits.max_payload_depth,
            max_items=self.limits.max_payload_items,
        )
        if payload_size > self.limits.max_event_payload_bytes:
            raise ValueError("event data exceeds max_event_payload_bytes")

        with self._lock:
            event = self._construct_event_locked(
                event_type=event_type,
                criticality=criticality,
                command_id=command_id,
                frozen_data=frozen_data,
            )
            self._commit_event_locked(event, payload_size)
            return event

    def append_transcript(
        self,
        kind: TranscriptKind,
        text: str | bytes,
        *,
        command_id: str | None = None,
        clip: TranscriptClip = TranscriptClip.KEEP_START,
        omitted_before_bytes: int = 0,
        omitted_after_bytes: int = 0,
        chunk_index: int | None = None,
        final: bool | None = None,
    ) -> TranscriptEntry:
        """Project, clip, and append one canonical transcript entry."""
        projection = self._project_transcript(
            kind,
            text,
            command_id=command_id,
            clip=clip,
            omitted_before_bytes=omitted_before_bytes,
            omitted_after_bytes=omitted_after_bytes,
            chunk_index=chunk_index,
            final=final,
        )

        with self._lock:
            entry = self._construct_transcript_locked(
                kind=kind,
                command_id=command_id,
                chunk_index=chunk_index,
                final=final,
                projection=projection,
            )
            self._commit_transcript_locked(entry, projection.text_bytes)
            return entry

    def append_command_accepted(
        self,
        command_text: str | bytes,
        *,
        command_id: str,
        slot_generation: int,
        accepted_state_revision: int,
        clip: TranscriptClip = TranscriptClip.KEEP_START,
        omitted_before_bytes: int = 0,
        omitted_after_bytes: int = 0,
    ) -> tuple[TranscriptEntry, ProductEvent]:
        """Atomically append a command transcript and ``command.accepted`` event."""
        _validate_counter(slot_generation, "slot_generation", allow_zero=False)
        _validate_counter(
            accepted_state_revision,
            "accepted_state_revision",
            allow_zero=True,
        )
        projection = self._project_transcript(
            TranscriptKind.COMMAND,
            command_text,
            command_id=command_id,
            clip=clip,
            omitted_before_bytes=omitted_before_bytes,
            omitted_after_bytes=omitted_after_bytes,
            chunk_index=None,
            final=None,
        )
        with self._lock:
            position = self._next_counter(
                self._transcript_position, "transcript position"
            )
            sequence = self._next_counter(self._event_sequence, "event sequence")
            event_data, payload_size = self._bounded_event_data(
                {
                    "slot_generation": slot_generation,
                    "accepted_state_revision": accepted_state_revision,
                    "transcript_position": position,
                }
            )
            entry = self._construct_transcript_at_locked(
                position=position,
                kind=TranscriptKind.COMMAND,
                command_id=command_id,
                chunk_index=None,
                final=None,
                projection=projection,
                recorded_at_ms=self._read_clock(),
            )
            event = self._construct_event_at_locked(
                sequence=sequence,
                event_type="command.accepted",
                criticality=ProductEventCriticality.AUTHORITATIVE,
                command_id=command_id,
                frozen_data=event_data,
                emitted_at_ms=self._read_clock(),
            )
            self._commit_pair_locked(
                entry,
                projection.text_bytes,
                event,
                payload_size,
            )
            return entry, event

    def append_command_output(
        self,
        kind: TranscriptKind,
        text: str | bytes,
        *,
        command_id: str,
        chunk_index: int,
        final: bool,
        clip: TranscriptClip = TranscriptClip.KEEP_START,
        omitted_before_bytes: int = 0,
        omitted_after_bytes: int = 0,
    ) -> tuple[TranscriptEntry, ProductEvent]:
        """Atomically append one output transcript entry and matching event.

        The inert text is clipped further when necessary so the event's exact
        copy and complete omission metadata fit ``max_event_payload_bytes``.
        """
        if kind not in {TranscriptKind.STDOUT, TranscriptKind.STDERR}:
            raise ValueError("command output kind must be stdout or stderr")
        projection = self._project_transcript(
            kind,
            text,
            command_id=command_id,
            clip=clip,
            omitted_before_bytes=omitted_before_bytes,
            omitted_after_bytes=omitted_after_bytes,
            chunk_index=chunk_index,
            final=final,
        )
        projected_text, transformed = project_inert_text(text)
        with self._lock:
            position = self._next_counter(
                self._transcript_position, "transcript position"
            )
            sequence = self._next_counter(self._event_sequence, "event sequence")
            projection, event_data, payload_size = self._fit_output_event_locked(
                projected_text=projected_text,
                transformed=transformed,
                initial_projection=projection,
                clip=clip,
                caller_omitted_before=omitted_before_bytes,
                caller_omitted_after=omitted_after_bytes,
                kind=kind,
                chunk_index=chunk_index,
                final=final,
                transcript_position=position,
            )
            entry = self._construct_transcript_at_locked(
                position=position,
                kind=kind,
                command_id=command_id,
                chunk_index=chunk_index,
                final=final,
                projection=projection,
                recorded_at_ms=self._read_clock(),
            )
            event = self._construct_event_at_locked(
                sequence=sequence,
                event_type="command.output",
                criticality=ProductEventCriticality.INFORMATIONAL,
                command_id=command_id,
                frozen_data=event_data,
                emitted_at_ms=self._read_clock(),
            )
            self._commit_pair_locked(
                entry,
                projection.text_bytes,
                event,
                payload_size,
            )
            return entry, event

    def _project_transcript(
        self,
        kind: TranscriptKind,
        text: str | bytes,
        *,
        command_id: str | None,
        clip: TranscriptClip,
        omitted_before_bytes: int,
        omitted_after_bytes: int,
        chunk_index: int | None,
        final: bool | None,
    ) -> _TranscriptProjection:
        if not isinstance(kind, TranscriptKind):
            raise TypeError("kind must be TranscriptKind")
        if not isinstance(clip, TranscriptClip):
            raise TypeError("clip must be TranscriptClip")
        _validate_optional_correlation_id(command_id)
        if kind is not TranscriptKind.SYSTEM and command_id is None:
            raise ValueError(f"{kind.value} transcript entries require command_id")
        _validate_counter(
            omitted_before_bytes,
            "omitted_before_bytes",
            allow_zero=True,
        )
        _validate_counter(
            omitted_after_bytes,
            "omitted_after_bytes",
            allow_zero=True,
        )
        _validate_chunk_fields(kind, chunk_index, final)
        projected, transformed = project_inert_text(text)
        return _transcript_projection(
            projected,
            transformed=transformed,
            limit=self.limits.max_transcript_entry_bytes,
            clip=clip,
            caller_omitted_before=omitted_before_bytes,
            caller_omitted_after=omitted_after_bytes,
        )

    def _bounded_event_data(
        self, data: Mapping[str, object]
    ) -> tuple[Mapping[str, SafeValue], int]:
        frozen_data, payload_size = _safe_payload(
            data,
            max_depth=self.limits.max_payload_depth,
            max_items=self.limits.max_payload_items,
        )
        if payload_size > self.limits.max_event_payload_bytes:
            raise ValueError("event data exceeds max_event_payload_bytes")
        return frozen_data, payload_size

    def _fit_output_event_locked(
        self,
        *,
        projected_text: str,
        transformed: bool,
        initial_projection: _TranscriptProjection,
        clip: TranscriptClip,
        caller_omitted_before: int,
        caller_omitted_after: int,
        kind: TranscriptKind,
        chunk_index: int,
        final: bool,
        transcript_position: int,
    ) -> tuple[_TranscriptProjection, Mapping[str, SafeValue], int]:
        projection = initial_projection
        while True:
            data = _output_event_data(
                projection,
                stream=kind.value,
                chunk_index=chunk_index,
                final=final,
                transcript_position=transcript_position,
            )
            frozen_data, payload_size = _safe_payload(
                data,
                max_depth=self.limits.max_payload_depth,
                max_items=self.limits.max_payload_items,
            )
            if payload_size <= self.limits.max_event_payload_bytes:
                return projection, frozen_data, payload_size
            if projection.text_bytes == 0:
                raise ValueError("command output metadata exceeds event payload bound")
            reduction = max(
                1,
                payload_size - self.limits.max_event_payload_bytes,
            )
            next_limit = max(0, projection.text_bytes - reduction)
            projection = _transcript_projection(
                projected_text,
                transformed=transformed,
                limit=next_limit,
                clip=clip,
                caller_omitted_before=caller_omitted_before,
                caller_omitted_after=caller_omitted_after,
            )

    def _construct_event_locked(
        self,
        *,
        event_type: str,
        criticality: ProductEventCriticality,
        command_id: str | None,
        frozen_data: Mapping[str, SafeValue],
    ) -> ProductEvent:
        sequence = self._next_counter(self._event_sequence, "event sequence")
        return self._construct_event_at_locked(
            sequence=sequence,
            event_type=event_type,
            criticality=criticality,
            command_id=command_id,
            frozen_data=frozen_data,
            emitted_at_ms=self._read_clock(),
        )

    def _construct_event_at_locked(
        self,
        *,
        sequence: int,
        event_type: str,
        criticality: ProductEventCriticality,
        command_id: str | None,
        frozen_data: Mapping[str, SafeValue],
        emitted_at_ms: int,
    ) -> ProductEvent:
        return ProductEvent(
            stream_id=self._stream_id,
            sequence=sequence,
            event_id=self._event_id(sequence),
            event_type=event_type,
            criticality=criticality,
            emitted_at_ms=emitted_at_ms,
            command_id=command_id,
            data=frozen_data,
        )

    def _construct_transcript_locked(
        self,
        *,
        kind: TranscriptKind,
        command_id: str | None,
        chunk_index: int | None,
        final: bool | None,
        projection: _TranscriptProjection,
    ) -> TranscriptEntry:
        position = self._next_counter(
            self._transcript_position,
            "transcript position",
        )
        return self._construct_transcript_at_locked(
            position=position,
            kind=kind,
            command_id=command_id,
            chunk_index=chunk_index,
            final=final,
            projection=projection,
            recorded_at_ms=self._read_clock(),
        )

    def _construct_transcript_at_locked(
        self,
        *,
        position: int,
        kind: TranscriptKind,
        command_id: str | None,
        chunk_index: int | None,
        final: bool | None,
        projection: _TranscriptProjection,
        recorded_at_ms: int,
    ) -> TranscriptEntry:
        return TranscriptEntry(
            transcript_id=self._transcript_id,
            position=position,
            entry_id=self._entry_id(position),
            kind=kind,
            recorded_at_ms=recorded_at_ms,
            command_id=command_id,
            text=projection.text,
            omitted_before_bytes=projection.omitted_before_bytes,
            omitted_after_bytes=projection.omitted_after_bytes,
            transformed=projection.transformed,
            chunk_index=chunk_index,
            final=final,
        )

    def _commit_pair_locked(
        self,
        entry: TranscriptEntry,
        text_bytes: int,
        event: ProductEvent,
        payload_size: int,
    ) -> None:
        self._commit_transcript_locked(entry, text_bytes)
        self._commit_event_locked(event, payload_size)

    def _commit_event_locked(self, event: ProductEvent, payload_size: int) -> None:
        self._events.append(event)
        self._event_payload_sizes.append(payload_size)
        self._event_bytes += payload_size
        self._event_sequence = event.sequence
        while (
            len(self._events) > self.limits.max_events
            or self._event_bytes > self.limits.max_event_bytes
        ):
            evicted = self._events.popleft()
            self._event_bytes -= self._event_payload_sizes.popleft()
            self._event_floor = evicted.sequence

    def _commit_transcript_locked(
        self,
        entry: TranscriptEntry,
        text_bytes: int,
    ) -> None:
        self._transcript.append(entry)
        self._transcript_text_sizes.append(text_bytes)
        self._transcript_bytes += text_bytes
        self._transcript_position = entry.position
        while (
            len(self._transcript) > self.limits.max_transcript_entries
            or self._transcript_bytes > self.limits.max_transcript_bytes
        ):
            evicted = self._transcript.popleft()
            self._transcript_bytes -= self._transcript_text_sizes.popleft()
            self._transcript_floor = evicted.position

    def event_snapshot(self) -> EventJournalSnapshot:
        """Return all retained events and exact immutable recovery metadata."""
        with self._lock:
            return self._event_snapshot(tuple(self._events))

    def transcript_snapshot(self) -> TranscriptJournalSnapshot:
        """Return all retained transcript entries and omission metadata."""
        with self._lock:
            return self._transcript_snapshot(tuple(self._transcript))

    def snapshot(self) -> ProductJournalSnapshot:
        """Observe event and transcript positions under one journal lock."""
        with self._lock:
            return ProductJournalSnapshot(
                events=self._event_snapshot(tuple(self._events)),
                transcript=self._transcript_snapshot(tuple(self._transcript)),
            )

    def replay_events(self, after: EventCursor) -> EventJournalSnapshot:
        """Return a complete ordered replay strictly after ``after``."""
        if not isinstance(after, EventCursor):
            raise TypeError("after must be EventCursor")
        with self._lock:
            self._validate_event_replay_cursor(after)
            events = tuple(
                event for event in self._events if event.sequence > after.sequence
            )
            return self._event_snapshot(events)

    def replay_transcript(self, after: TranscriptCursor) -> TranscriptJournalSnapshot:
        """Return a complete ordered transcript replay strictly after ``after``."""
        if not isinstance(after, TranscriptCursor):
            raise TypeError("after must be TranscriptCursor")
        with self._lock:
            self._validate_transcript_replay_cursor(after)
            entries = tuple(
                entry for entry in self._transcript if entry.position > after.position
            )
            return self._transcript_snapshot(entries)

    def _event_snapshot(self, events: tuple[ProductEvent, ...]) -> EventJournalSnapshot:
        return EventJournalSnapshot(
            events=events,
            retention_floor=self._event_cursor(self._event_floor),
            position=self._event_cursor(self._event_sequence),
            retained_payload_bytes=self._event_bytes,
        )

    def _transcript_snapshot(
        self, entries: tuple[TranscriptEntry, ...]
    ) -> TranscriptJournalSnapshot:
        return TranscriptJournalSnapshot(
            entries=entries,
            retention_floor=self._transcript_cursor(self._transcript_floor),
            position=self._transcript_cursor(self._transcript_position),
            omitted_before_entries=self._transcript_floor,
            omitted_after_entries=0,
            retained_text_bytes=self._transcript_bytes,
        )

    def _validate_event_replay_cursor(self, cursor: EventCursor) -> None:
        if cursor.stream_id != self._stream_id:
            self._snapshot_required(
                CursorDomain.EVENT,
                SnapshotRequiredReason.WRONG_STREAM,
                cursor.sequence,
            )
        if cursor.sequence < self._event_floor:
            self._snapshot_required(
                CursorDomain.EVENT,
                SnapshotRequiredReason.BEFORE_RETENTION_FLOOR,
                cursor.sequence,
            )
        if cursor.sequence > self._event_sequence:
            self._snapshot_required(
                CursorDomain.EVENT,
                SnapshotRequiredReason.FUTURE_POSITION,
                cursor.sequence,
            )
        expected_id = None if cursor.sequence == 0 else self._event_id(cursor.sequence)
        if cursor.event_id != expected_id:
            self._snapshot_required(
                CursorDomain.EVENT,
                SnapshotRequiredReason.POSITION_MISMATCH,
                cursor.sequence,
            )

    def _validate_transcript_replay_cursor(self, cursor: TranscriptCursor) -> None:
        if cursor.transcript_id != self._transcript_id:
            self._snapshot_required(
                CursorDomain.TRANSCRIPT,
                SnapshotRequiredReason.WRONG_STREAM,
                cursor.position,
            )
        if cursor.position < self._transcript_floor:
            self._snapshot_required(
                CursorDomain.TRANSCRIPT,
                SnapshotRequiredReason.BEFORE_RETENTION_FLOOR,
                cursor.position,
            )
        if cursor.position > self._transcript_position:
            self._snapshot_required(
                CursorDomain.TRANSCRIPT,
                SnapshotRequiredReason.FUTURE_POSITION,
                cursor.position,
            )
        expected_id = None if cursor.position == 0 else self._entry_id(cursor.position)
        if cursor.entry_id != expected_id:
            self._snapshot_required(
                CursorDomain.TRANSCRIPT,
                SnapshotRequiredReason.POSITION_MISMATCH,
                cursor.position,
            )

    def _snapshot_required(
        self,
        domain: CursorDomain,
        reason: SnapshotRequiredReason,
        requested_position: int,
    ) -> None:
        if domain is CursorDomain.EVENT:
            floor: EventCursor | TranscriptCursor = self._event_cursor(
                self._event_floor
            )
            current: EventCursor | TranscriptCursor = self._event_cursor(
                self._event_sequence
            )
        else:
            floor = self._transcript_cursor(self._transcript_floor)
            current = self._transcript_cursor(self._transcript_position)
        raise JournalSnapshotRequired(
            domain=domain,
            reason=reason,
            requested_position=requested_position,
            retention_floor=floor,
            current_position=current,
        )

    def _event_id(self, sequence: int) -> str:
        return f"event:{self._event_id_part}:{sequence}"

    def _entry_id(self, position: int) -> str:
        return f"entry:{self._transcript_id_part}:{position}"

    def _event_cursor(self, sequence: int) -> EventCursor:
        event_id = None if sequence == 0 else self._event_id(sequence)
        return EventCursor(self._stream_id, sequence, event_id)

    def _transcript_cursor(self, position: int) -> TranscriptCursor:
        entry_id = None if position == 0 else self._entry_id(position)
        return TranscriptCursor(self._transcript_id, position, entry_id)

    def _new_id_part(self, purpose: str) -> str:
        value = self._id_factory(purpose)
        _validate_identifier(value, _OPAQUE_ID_PART_PATTERN, f"{purpose} id")
        return value

    def _read_clock(self) -> int:
        value = self._clock_ms()
        _validate_counter(value, "clock_ms", allow_zero=True)
        return value

    @staticmethod
    def _next_counter(current: int, label: str) -> int:
        if current >= _MAX_COUNTER:
            raise OverflowError(f"{label} is exhausted")
        return current + 1


def project_inert_text(value: str | bytes) -> tuple[str, bool]:
    """Return strict UTF-8 text with terminal, control, and bidi effects inert.

    LF and horizontal tab remain meaningful transcript whitespace.  CRLF and
    lone CR become LF.  Other C0/C1 controls, DEL, and all Unicode ``Cf``
    formatting controls (including bidi and zero-width controls) become the
    visible replacement character.  Invalid UTF-8 and unpaired surrogates are
    rejected instead of repaired ambiguously.
    """
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("transcript text must be valid UTF-8") from error
    elif isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("transcript text must be valid UTF-8") from error
        text = value
    else:
        raise TypeError("transcript text must be str or bytes")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    transformed = normalized != text
    projected: list[str] = []
    for character in normalized:
        codepoint = ord(character)
        unsafe_control = (
            (codepoint < 0x20 and character not in {"\t", "\n"})
            or 0x7F <= codepoint <= 0x9F
            or unicodedata.category(character) == "Cf"
        )
        if unsafe_control:
            projected.append("\N{REPLACEMENT CHARACTER}")
            transformed = True
        else:
            projected.append(character)
    return "".join(projected), transformed


def _safe_payload(
    data: Mapping[str, object] | None,
    *,
    max_depth: int,
    max_items: int,
) -> tuple[Mapping[str, SafeValue], int]:
    source: Mapping[str, object] = {} if data is None else data
    if not isinstance(source, Mapping):
        raise TypeError("event data must be a mapping")
    item_count = [0]
    frozen = _freeze_safe_value(
        source,
        depth=0,
        max_depth=max_depth,
        max_items=max_items,
        item_count=item_count,
    )
    if not isinstance(frozen, Mapping):  # Defensive: source is a mapping.
        raise RuntimeError("safe event data did not remain a mapping")
    thawed = _thaw(frozen)
    encoded = json.dumps(
        thawed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return frozen, len(encoded)


def _freeze_safe_value(
    value: object,
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    item_count: list[int],
) -> SafeValue:
    if depth > max_depth:
        raise ValueError("event data exceeds max_payload_depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError("event data integer is outside the JSON-safe range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event data numbers must be finite")
        return value
    if isinstance(value, str):
        _validate_safe_payload_text(value)
        return value
    if isinstance(value, Mapping):
        copied: dict[str, SafeValue] = {}
        for key, nested in value.items():
            item_count[0] += 1
            if item_count[0] > max_items:
                raise ValueError("event data exceeds max_payload_items")
            if not isinstance(key, str):
                raise TypeError("event data keys must be strings")
            _validate_identifier(key, _PAYLOAD_KEY_PATTERN, "event data key")
            if key in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(f"event data key {key!r} is not a safe projection")
            copied[key] = _freeze_safe_value(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                item_count=item_count,
            )
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        copied_items: list[SafeValue] = []
        for nested in value:
            item_count[0] += 1
            if item_count[0] > max_items:
                raise ValueError("event data exceeds max_payload_items")
            copied_items.append(
                _freeze_safe_value(
                    nested,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    item_count=item_count,
                )
            )
        return tuple(copied_items)
    raise TypeError("event data must contain only JSON-safe values")


def _validate_safe_payload_text(value: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("event data strings must be valid UTF-8") from error
    for character in value:
        codepoint = ord(character)
        if (
            (codepoint < 0x20 and character not in {"\t", "\n"})
            or 0x7F <= codepoint <= 0x9F
            or unicodedata.category(character) == "Cf"
        ):
            raise ValueError("event data strings must be inert safe text")


def _thaw(value: SafeValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw(nested) for nested in value]
    return value


def _clip_utf8(text: str, limit: int, clip: TranscriptClip) -> tuple[str, int, int]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, 0, 0
    if limit == 0:
        if clip is TranscriptClip.KEEP_START:
            return "", 0, len(encoded)
        return "", len(encoded), 0
    if clip is TranscriptClip.KEEP_START:
        retained = encoded[:limit].decode("utf-8", errors="ignore")
        retained_size = len(retained.encode("utf-8"))
        return retained, 0, len(encoded) - retained_size
    retained = encoded[-limit:].decode("utf-8", errors="ignore")
    retained_size = len(retained.encode("utf-8"))
    return retained, len(encoded) - retained_size, 0


def _transcript_projection(
    projected_text: str,
    *,
    transformed: bool,
    limit: int,
    clip: TranscriptClip,
    caller_omitted_before: int,
    caller_omitted_after: int,
) -> _TranscriptProjection:
    retained, added_before, added_after = _clip_utf8(
        projected_text,
        limit,
        clip,
    )
    total_before = _checked_sum(
        caller_omitted_before,
        added_before,
        "omitted_before_bytes",
    )
    total_after = _checked_sum(
        caller_omitted_after,
        added_after,
        "omitted_after_bytes",
    )
    return _TranscriptProjection(
        text=retained,
        omitted_before_bytes=total_before,
        omitted_after_bytes=total_after,
        transformed=transformed,
        text_bytes=len(retained.encode("utf-8")),
    )


def _output_event_data(
    projection: _TranscriptProjection,
    *,
    stream: str,
    chunk_index: int,
    final: bool,
    transcript_position: int,
) -> dict[str, object]:
    return {
        "stream": stream,
        "chunk_index": chunk_index,
        "text": projection.text,
        "encoding": "utf-8",
        "final": final,
        "clipped": projection.clipped,
        "omitted_before_bytes": projection.omitted_before_bytes,
        "omitted_after_bytes": projection.omitted_after_bytes,
        "transformed": projection.transformed,
        "projection": "inert-text-v1",
        "transcript_position": transcript_position,
    }


def _validate_chunk_fields(
    kind: TranscriptKind, chunk_index: int | None, final: bool | None
) -> None:
    if kind in {TranscriptKind.STDOUT, TranscriptKind.STDERR}:
        if chunk_index is None:
            raise ValueError(f"{kind.value} transcript entries require chunk_index")
        _validate_counter(chunk_index, "chunk_index", allow_zero=True)
        if not isinstance(final, bool):
            raise ValueError(f"{kind.value} transcript entries require final")
    elif chunk_index is not None or final is not None:
        raise ValueError("chunk_index and final apply only to stdout/stderr entries")


def _validate_optional_correlation_id(value: str | None) -> None:
    if value is not None:
        _validate_identifier(value, _CORRELATION_ID_PATTERN, "command_id")


def _validate_identifier(value: object, pattern: re.Pattern[str], name: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _validate_counter(value: object, name: str, *, allow_zero: bool) -> None:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_COUNTER
    ):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} bounded integer")


def _checked_sum(first: int, second: int, name: str) -> int:
    total = first + second
    if total > _MAX_COUNTER:
        raise ValueError(f"{name} exceeds its bounded range")
    return total


def _random_id_part(_purpose: str) -> str:
    return secrets.token_urlsafe(24)


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000
