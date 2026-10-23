from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dreamland.background_agents.event_bus import SessionEventBus, new_session_event
from dreamland.background_agents.events import EventSource, SessionEventType
from dreamland.models import SessionState
from dreamland.world_state import active_host_ref

if TYPE_CHECKING:
    from dreamland.evaluation_runtime import EvaluationSessionRuntime


@dataclass(frozen=True, slots=True)
class RegisteredSession:
    session_id: str
    session: SessionState
    lock: threading.RLock = field(default_factory=threading.RLock)
    runtime: EvaluationSessionRuntime | None = None
    close_callback: Callable[[], None] | None = None
    closed: threading.Event = field(default_factory=threading.Event)


class LiveSessionRegistry:
    def __init__(self, *, event_bus: SessionEventBus | None = None) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, RegisteredSession] = {}
        self.event_bus = event_bus

    def register(
        self,
        session: SessionState,
        *,
        runtime: EvaluationSessionRuntime | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> RegisteredSession:
        registration = RegisteredSession(
            session_id=session.langfuse_trace_id,
            session=session,
            runtime=runtime,
            close_callback=close_callback,
        )
        with self._lock:
            self._sessions[registration.session_id] = registration
        event_bus = runtime.event_bus if runtime is not None else self.event_bus
        if event_bus is not None:
            event_bus.publish(
                new_session_event(
                    session_id=registration.session_id,
                    event_type=SessionEventType.SESSION_REGISTERED,
                    source=EventSource.SESSION_RUNTIME,
                    target_host=active_host_ref(session.world_state),
                )
            )
        return registration

    def unregister(
        self,
        session_id: str,
        *,
        session: SessionState | None = None,
    ) -> None:
        with self._lock:
            registration = self._sessions.get(session_id)
            if registration is None:
                return
            if session is not None and registration.session is not session:
                return
            registration.closed.set()
            del self._sessions[session_id]
        if registration.close_callback is not None:
            registration.close_callback()
        runtime = registration.runtime
        event_bus = runtime.event_bus if runtime is not None else self.event_bus
        if event_bus is not None:
            event_bus.publish(
                new_session_event(
                    session_id=session_id,
                    event_type=SessionEventType.SESSION_CLOSING,
                    source=EventSource.SESSION_RUNTIME,
                    target_host=active_host_ref(registration.session.world_state),
                )
            )
        if runtime is not None:
            runtime.close()

    def get(self, session_id: str) -> RegisteredSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    @contextmanager
    def if_live(self, registration: RegisteredSession) -> Iterator[bool]:
        """Hold exact-registration liveness across a short mutation only."""
        with self._lock:
            yield (
                self._sessions.get(registration.session_id) is registration
                and not registration.closed.is_set()
            )

    def list_sessions(self) -> tuple[RegisteredSession, ...]:
        with self._lock:
            return tuple(self._sessions.values())
