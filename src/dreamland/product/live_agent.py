"""Explicit live-agent collaborators for the development-only product target.

This module is inert until imported and constructed by the manual live Stage
tool.  It does not compose a listener, read provider credentials, or alter the
ordinary DREAMLAND runtime.  The adapter deliberately charges each dispatched
command at its full reservation because exact cross-provider spend accounting
is not yet production evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

from dreamland.models import (
    CommandContext,
    CommandExecutionControl,
    CommandOutputChunk,
    CommandResult,
    CommandStreamEvent,
    CommandStreamResult,
)
from dreamland.product.runtime_executor import (
    ProductRuntimeControl,
    ProductRuntimeLimits,
    ProductRuntimeUsage,
)

DEFAULT_PUBLICATION_CHUNK_BYTES = 4 * 1024


class StreamingCommandAgent(Protocol):
    """The narrow streaming surface required from a DREAMLAND command agent."""

    def stream_respond(
        self,
        context: CommandContext,
        control: CommandExecutionControl | None = None,
    ) -> Iterator[CommandStreamEvent]: ...


class ProductLiveAgentAdapter:
    """Run one real DREAMLAND command-agent invocation per product command."""

    def __init__(
        self,
        agent: StreamingCommandAgent,
        *,
        publication_chunk_bytes: int = DEFAULT_PUBLICATION_CHUNK_BYTES,
    ) -> None:
        stream_respond = getattr(agent, "stream_respond", None)
        if not callable(stream_respond):
            raise TypeError("live product agent must support streaming responses")
        if (
            isinstance(publication_chunk_bytes, bool)
            or not isinstance(publication_chunk_bytes, int)
            or publication_chunk_bytes <= 0
        ):
            raise ValueError("publication chunk bytes must be a positive integer")
        self._agent = agent
        self._publication_chunk_bytes = publication_chunk_bytes

    def execute(
        self,
        context: CommandContext,
        control: ProductRuntimeControl,
        emit: Callable[[CommandOutputChunk], None],
    ) -> CommandResult:
        """Claim one attempt, bridge cancellation, and return one terminal result."""
        if control.claim_attempt() is None:
            return CommandResult(interrupted=True, signal="SIGINT", exit_status=130)

        agent_control = CommandExecutionControl()
        control.add_cancel_callback(agent_control.cancel)
        coalescer = _LineOutputCoalescer(
            emit,
            maximum_chunk_bytes=self._publication_chunk_bytes,
        )
        terminal: CommandResult | None = None
        try:
            for event in self._agent.stream_respond(context, control=agent_control):
                if isinstance(event, CommandOutputChunk):
                    if terminal is not None:
                        raise ValueError(
                            "live product agent emitted output after terminal"
                        )
                    agent_control.add_rendered(event.stream, event.text)
                    coalescer.append(event)
                    continue
                if isinstance(event, CommandStreamResult):
                    if terminal is not None:
                        raise ValueError(
                            "live product agent emitted multiple terminals"
                        )
                    coalescer.flush()
                    terminal = event.result
                    continue
                raise TypeError("live product agent emitted an invalid stream event")
        finally:
            coalescer.flush()
        if terminal is None:
            raise RuntimeError("live product agent omitted its terminal result")
        return terminal


class _LineOutputCoalescer:
    """Publish complete lines promptly and bound unterminated output by UTF-8 bytes."""

    def __init__(
        self,
        emit: Callable[[CommandOutputChunk], None],
        *,
        maximum_chunk_bytes: int,
    ) -> None:
        self._emit = emit
        self._maximum_chunk_bytes = maximum_chunk_bytes
        self._stream: str | None = None
        self._text = ""

    def append(self, chunk: CommandOutputChunk) -> None:
        if not chunk.text:
            return
        if self._stream is not None and chunk.stream != self._stream:
            self.flush()
        self._stream = chunk.stream
        self._text += chunk.text
        self._drain_complete_lines_and_full_chunks()

    def flush(self) -> None:
        while self._text:
            self._publish_prefix(self._maximum_chunk_bytes)
        self._stream = None

    def _drain_complete_lines_and_full_chunks(self) -> None:
        while self._text:
            newline = self._text.find("\n")
            if newline >= 0:
                line = self._text[: newline + 1]
                if len(line.encode("utf-8")) <= self._maximum_chunk_bytes:
                    self._publish_text(line)
                else:
                    self._publish_prefix(self._maximum_chunk_bytes)
                continue
            if len(self._text.encode("utf-8")) >= self._maximum_chunk_bytes:
                self._publish_prefix(self._maximum_chunk_bytes)
                continue
            return

    def _publish_prefix(self, maximum_bytes: int) -> None:
        encoded = self._text.encode("utf-8")
        prefix = encoded[:maximum_bytes].decode("utf-8", errors="ignore")
        if not prefix:
            raise ValueError("publication chunk size cannot contain one character")
        self._publish_text(prefix)

    def _publish_text(self, text: str) -> None:
        if self._stream is None:
            raise RuntimeError("publication stream is unavailable")
        self._text = self._text[len(text) :]
        self._emit(CommandOutputChunk(text, self._stream))


class ReservedMaximumUsageMeter:
    """Conservatively settle each development command at its full reservation."""

    def begin(
        self,
        *,
        command_id: str,
        checkpoint_namespace: str,
        limits: ProductRuntimeLimits,
    ) -> object:
        if not command_id or not checkpoint_namespace:
            raise ValueError("live usage measurement identity is invalid")
        return limits

    def finish(self, measurement: object) -> ProductRuntimeUsage:
        if not isinstance(measurement, ProductRuntimeLimits):
            raise TypeError("live usage measurement is invalid")
        return ProductRuntimeUsage(
            tokens=measurement.max_tokens,
            spend_units=measurement.max_spend_units,
        )


class ExactThreadCheckpointReaper:
    """Delete one exact LangGraph checkpoint thread after product execution."""

    def __init__(self, delete_thread: Callable[[str], None]) -> None:
        if not callable(delete_thread):
            raise TypeError("checkpoint delete collaborator must be callable")
        self._delete_thread = delete_thread

    def reap(self, checkpoint_namespace: str) -> None:
        if not isinstance(checkpoint_namespace, str) or not checkpoint_namespace:
            raise ValueError("checkpoint namespace is invalid")
        self._delete_thread(checkpoint_namespace)


__all__ = [
    "DEFAULT_PUBLICATION_CHUNK_BYTES",
    "ExactThreadCheckpointReaper",
    "ProductLiveAgentAdapter",
    "ReservedMaximumUsageMeter",
    "StreamingCommandAgent",
]
