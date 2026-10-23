from __future__ import annotations

from collections.abc import Awaitable, Iterator
from typing import Protocol

from dreamland.models import (
    CommandExecutionControl,
    CommandResult,
    CommandStreamEvent,
    ConnectionMetadata,
    SessionState,
)
from dreamland.state_profiles import StateProfile


class CommandBackend(Protocol):
    def create_session(
        self,
        username: str,
        connection_metadata: ConnectionMetadata | None = None,
        *,
        state_profile: StateProfile | None = None,
    ) -> SessionState:
        pass

    def prompt(self, session: SessionState) -> str:
        pass

    def execute(
        self,
        session: SessionState,
        command: str,
    ) -> CommandResult | Awaitable[CommandResult]:
        pass

    def execute_stream(
        self,
        session: SessionState,
        command: str,
        control: CommandExecutionControl | None = None,
    ) -> Iterator[CommandStreamEvent]:
        pass
