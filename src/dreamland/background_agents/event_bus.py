"""Small in-process event bus for session-local background-agent triggers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from dreamland.background_agents.events import (
    EventSource,
    JsonValue,
    SessionEvent,
    SessionEventType,
)

LOGGER = logging.getLogger("dreamland.enrichment.events")
SessionEventHandler = Callable[[SessionEvent], None]


@dataclass(frozen=True, slots=True)
class EventDeliveryFailure:
    subscriber_id: str
    error_type: str


class EventSubscription:
    def __init__(self, bus: SessionEventBus, subscriber_id: str) -> None:
        self._bus = bus
        self.subscriber_id = subscriber_id
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._bus.unsubscribe(self.subscriber_id)

    def __enter__(self) -> EventSubscription:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class SessionEventBus:
    """Fan neutral session events out to independently gated subscribers.

    Delivery is synchronous and isolated per handler. Subscribers should only
    enqueue work; model calls and other slow operations belong in their own
    workers.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, SessionEventHandler] = {}

    def subscribe(
        self,
        subscriber_id: str,
        handler: SessionEventHandler,
    ) -> EventSubscription:
        if not subscriber_id.strip():
            raise ValueError("subscriber_id must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            if subscriber_id in self._subscribers:
                raise ValueError(f"subscriber already registered: {subscriber_id}")
            self._subscribers[subscriber_id] = handler
        return EventSubscription(self, subscriber_id)

    def unsubscribe(self, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def publish(self, event: SessionEvent) -> tuple[EventDeliveryFailure, ...]:
        if not isinstance(event, SessionEvent):
            raise TypeError("event must be a SessionEvent")
        with self._lock:
            subscribers = tuple(self._subscribers.items())
        failures: list[EventDeliveryFailure] = []
        LOGGER.debug(
            "event.published event_id=%s correlation_id=%s session_id=%s "
            "event_type=%s source=%s subscriber_count=%s",
            event.event_id,
            event.correlation_id,
            event.session_id,
            event.event_type.value,
            event.source.value,
            len(subscribers),
        )
        for subscriber_id, handler in subscribers:
            try:
                handler(event)
            except Exception as exc:
                failures.append(
                    EventDeliveryFailure(
                        subscriber_id=subscriber_id,
                        error_type=type(exc).__name__,
                    )
                )
                LOGGER.warning(
                    "event.delivery_failed event_id=%s correlation_id=%s "
                    "session_id=%s event_type=%s subscriber_id=%s error=%s",
                    event.event_id,
                    event.correlation_id,
                    event.session_id,
                    event.event_type.value,
                    subscriber_id,
                    type(exc).__name__,
                    exc_info=True,
                )
        return tuple(failures)

    def subscriber_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._subscribers)


def new_session_event(
    *,
    session_id: str,
    event_type: SessionEventType,
    source: EventSource,
    correlation_id: str | None = None,
    caused_by_event_id: str | None = None,
    source_agent_id: str | None = None,
    command_id: str | None = None,
    target_host: str | None = None,
    surface: str | None = None,
    anchors: tuple[str, ...] = (),
    payload: dict[str, JsonValue] | None = None,
    occurred_at_ms: int | None = None,
) -> SessionEvent:
    event_id = uuid4().hex
    return SessionEvent(
        event_id=event_id,
        correlation_id=correlation_id or command_id or event_id,
        session_id=session_id,
        event_type=event_type,
        source=source,
        occurred_at_ms=(
            time.time_ns() // 1_000_000 if occurred_at_ms is None else occurred_at_ms
        ),
        caused_by_event_id=caused_by_event_id,
        source_agent_id=source_agent_id,
        command_id=command_id,
        target_host=target_host,
        surface=surface,
        anchors=anchors,
        payload={} if payload is None else payload,
    )
