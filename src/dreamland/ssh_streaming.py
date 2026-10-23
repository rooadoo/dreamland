from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any, Literal, Protocol, cast

from dreamland.models import (
    CommandExecutionControl,
    CommandOutputChunk,
    CommandResult,
    CommandStreamEvent,
    CommandStreamResult,
    SessionState,
)

StreamName = Literal["stdout", "stderr"]


class ExecutableBackend(Protocol):
    def execute(
        self,
        session: SessionState,
        command: str,
    ) -> CommandResult | Awaitable[CommandResult]:
        pass


def execute_backend(
    backend: ExecutableBackend,
    session: SessionState,
    command: str,
) -> CommandResult:
    result = backend.execute(session, command)
    if inspect.isawaitable(result):
        return asyncio.run(cast(Coroutine[Any, Any, CommandResult], result))
    return result


def execute_backend_stream(
    backend: ExecutableBackend,
    session: SessionState,
    command: str,
    writer: LineBufferedCommandWriter,
    control: CommandExecutionControl | None = None,
) -> CommandResult:
    result: CommandResult | None = None
    execute_stream = getattr(backend, "execute_stream", None)
    if execute_stream is None:
        result = execute_backend(backend, session, command)
        writer.write(CommandOutputChunk(result.stdout, "stdout"))
        writer.write(CommandOutputChunk(result.stderr, "stderr"))
        return result

    for event in _execute_stream_events(execute_stream, session, command, control):
        if control is not None and control.cancelled():
            result = control.rendered_result()
            break
        if isinstance(event, CommandOutputChunk):
            writer.write(event)
        elif isinstance(event, CommandStreamResult):
            result = event.result
    if result is None:
        result = CommandResult()
    if control is not None and control.cancelled():
        return control.rendered_result()
    if not writer.received("stdout"):
        writer.write(CommandOutputChunk(result.stdout, "stdout"))
    if not writer.received("stderr"):
        writer.write(CommandOutputChunk(result.stderr, "stderr"))
    return result


def _execute_stream_events(
    execute_stream: Callable[..., Iterator[CommandStreamEvent]],
    session: SessionState,
    command: str,
    control: CommandExecutionControl | None,
) -> Iterator[CommandStreamEvent]:
    try:
        signature = inspect.signature(execute_stream)
    except (TypeError, ValueError):
        yield from execute_stream(session, command)
        return
    if "control" in signature.parameters:
        yield from execute_stream(session, command, control=control)
        return
    yield from execute_stream(session, command)


class LineBufferedCommandWriter:
    def __init__(
        self,
        stdout: Callable[[str], object],
        stderr: Callable[[str], object],
        control: CommandExecutionControl | None = None,
    ) -> None:
        self._writers = {"stdout": stdout, "stderr": stderr}
        self._buffers: dict[StreamName, str] = {"stdout": "", "stderr": ""}
        self._received: dict[StreamName, bool] = {"stdout": False, "stderr": False}
        self._wrote_any = False
        self._last_write_ended_newline = False
        self._control = control
        self._closed = False
        self._lock = threading.Lock()

    def write(self, chunk: CommandOutputChunk) -> None:
        if not chunk.text:
            return
        with self._lock:
            if self._closed:
                return
            self._received[chunk.stream] = True
            buffer = self._buffers[chunk.stream] + chunk.text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", maxsplit=1)
                self._write(chunk.stream, f"{line}\n")
            self._buffers[chunk.stream] = buffer

    def received(self, stream: StreamName) -> bool:
        return self._received[stream]

    def flush(self, *, ensure_trailing_newline: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            for stream, buffer in list(self._buffers.items()):
                if buffer:
                    self._write(stream, buffer)
                    self._buffers[stream] = ""
            if (
                ensure_trailing_newline
                and self._wrote_any
                and not self._last_write_ended_newline
            ):
                self._write("stdout", "\n", record=False)

    def discard_buffers(self) -> None:
        with self._lock:
            self._buffers = {"stdout": "", "stderr": ""}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._buffers = {"stdout": "", "stderr": ""}

    def _write(
        self,
        stream: StreamName,
        text: str,
        *,
        record: bool = True,
    ) -> None:
        self._wrote_any = True
        self._last_write_ended_newline = text.endswith("\n")
        self._writers[stream](text)
        if record and self._control is not None:
            self._control.add_rendered(stream, text)
