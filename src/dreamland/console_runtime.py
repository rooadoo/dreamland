from __future__ import annotations

import logging
import os
import select
import sys
import termios
import threading
import time
import tty
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO, Protocol, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory

from dreamland.console import (
    CONSOLE_PROMPT,
    CONSOLE_REFRESH_ENTER_SCREEN,
    CONSOLE_REFRESH_EXIT_SCREEN,
    ConsoleContext,
    ConsoleRefresh,
    ConsoleResult,
    console_refresh_terminal_error,
    redraw_console_frame,
)
from dreamland.runtime_logging import BufferedLogRecord, SessionTraceRecord

LOGGER = logging.getLogger("dreamland.console")
_REFRESH_INPUT_POLL_SECONDS = 0.05
_REFRESH_INPUT_DRAIN_POLL_SECONDS = 0.002
_REFRESH_INPUT_SEQUENCE_IDLE_SECONDS = 0.075
_REFRESH_INPUT_SEQUENCE_MAX_SECONDS = 0.25
_REFRESH_INPUT_SEQUENCE_MAX_BYTES = 64


class ConsoleLogs(Protocol):
    @property
    def last_sequence(self) -> int:
        pass

    def after(
        self, sequence: int, *, timeout: float | None = None
    ) -> tuple[BufferedLogRecord, ...]:
        pass


class ConsoleTraces(Protocol):
    def last_sequence(self, session_id: str) -> int:
        pass

    def after(
        self, session_id: str, sequence: int, *, timeout: float | None = None
    ) -> tuple[SessionTraceRecord, ...]:
        pass


class ConsoleBackend(Protocol):
    """The terminal surface shared by the in-process and detachable consoles."""

    @property
    def shutdown_requested(self) -> bool:
        pass

    @property
    def logs(self) -> ConsoleLogs:
        pass

    @property
    def trace_logs(self) -> ConsoleTraces:
        pass

    def execute(
        self, command: str, *, allow_exit: bool, context: ConsoleContext | None = None
    ) -> ConsoleResult:
        pass

    def complete(self, line: str, *, allow_exit: bool) -> str | None:
        pass


class LocalConsole:
    def __init__(
        self,
        console: ConsoleBackend,
        *,
        input_stream: BinaryIO | None = None,
        output_stream: TextIO | None = None,
        prompt_session: Any | None = None,
        terminal_capable: bool | None = None,
    ) -> None:
        self.console = console
        self.input_stream = input_stream or sys.stdin.buffer
        self.output_stream = output_stream or sys.stdout
        self.terminal_capable = (
            _streams_support_terminal_refresh(
                self.input_stream,
                self.output_stream,
            )
            if terminal_capable is None
            else terminal_capable
        )
        self.context = ConsoleContext()
        self.prompt_session = prompt_session or PromptSession(
            history=InMemoryHistory(),
            completer=DreamlandCompleter(console),
            complete_while_typing=False,
        )

    def run(self) -> None:
        stop_monitor = threading.Event()
        monitor = threading.Thread(
            target=self._exit_prompt_on_shutdown,
            args=(stop_monitor,),
            daemon=True,
        )
        monitor.start()
        try:
            while not self.console.shutdown_requested:
                try:
                    command = self.prompt_session.prompt(self._prompt())
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    return
                if not command.strip():
                    continue
                result = self.console.execute(
                    command,
                    allow_exit=False,
                    context=self.context,
                )
                if result.refresh is not None and not self.terminal_capable:
                    LOGGER.info(
                        "Console refresh rejected target=%s transport=local "
                        "reason=interactive_terminal_required",
                        result.refresh.name,
                    )
                    self._send(console_refresh_terminal_error(result.refresh))
                    continue
                if result.refresh is not None:
                    self._send(CONSOLE_REFRESH_ENTER_SCREEN)
                self._send(result.output)
                if result.refresh is not None:
                    self._follow_refresh(
                        result.refresh,
                        initial_frame=result.output,
                    )
                elif result.follow:
                    if result.follow_trace_session_id is not None:
                        self._follow_trace(result.follow_trace_session_id)
                    else:
                        self._follow_logs()
                if result.shutdown:
                    return
        finally:
            stop_monitor.set()

    def _exit_prompt_on_shutdown(self, stop_monitor: threading.Event) -> None:
        while not stop_monitor.wait(0.1):
            if not self.console.shutdown_requested:
                continue
            app = getattr(self.prompt_session, "app", None)
            if app is None:
                continue
            try:
                app.exit(exception=EOFError)
            except Exception:
                continue
            return

    def _follow_logs(self) -> None:
        sequence = self.console.logs.last_sequence
        with _raw_terminal(self.input_stream):
            while not self.console.shutdown_requested:
                if _input_ready(self.input_stream):
                    self.input_stream.read(1)
                    self._send("\r\n")
                    return
                records = self.console.logs.after(sequence, timeout=0.1)
                for record in records:
                    self._send(f"{record.text}\r\n")
                    sequence = record.sequence

    def _follow_trace(self, session_id: str) -> None:
        sequence = self.console.trace_logs.last_sequence(session_id)
        with _raw_terminal(self.input_stream):
            while not self.console.shutdown_requested:
                if _input_ready(self.input_stream):
                    self.input_stream.read(1)
                    self._send("\r\n")
                    return
                records = self.console.trace_logs.after(
                    session_id,
                    sequence,
                    timeout=0.1,
                )
                for record in records:
                    self._send(f"{record.text}\r\n")
                    sequence = record.sequence

    def _follow_refresh(
        self,
        refresh: ConsoleRefresh,
        *,
        initial_frame: str,
    ) -> None:
        started = time.monotonic()
        updates = 0
        reason = "shutdown"
        final_frame = initial_frame
        next_refresh = started + refresh.interval_seconds
        LOGGER.info(
            "Console refresh started target=%s transport=local interval_ms=%s",
            refresh.name,
            round(refresh.interval_seconds * 1000),
        )
        try:
            with _raw_terminal(self.input_stream):
                while not self.console.shutdown_requested:
                    if _input_ready(self.input_stream):
                        key_received = _consume_refresh_input(self.input_stream)
                        reason = "keypress" if key_received else "input_closed"
                        return
                    now = time.monotonic()
                    if now < next_refresh:
                        time.sleep(
                            min(
                                _REFRESH_INPUT_POLL_SECONDS,
                                next_refresh - now,
                            )
                        )
                        continue
                    try:
                        next_frame = refresh.render()
                    except Exception as exc:
                        reason = "status_error"
                        LOGGER.warning(
                            "Console refresh read failed target=%s transport=local "
                            "error=%s",
                            refresh.name,
                            type(exc).__name__,
                        )
                        final_frame = (
                            "error: live console refresh failed "
                            f"[{type(exc).__name__}]\n"
                        )
                        self._send(
                            redraw_console_frame(final_frame).replace("\n", "\r\n")
                        )
                        return
                    final_frame = next_frame
                    self._send(redraw_console_frame(next_frame).replace("\n", "\r\n"))
                    updates += 1
                    next_refresh = time.monotonic() + refresh.interval_seconds
        finally:
            self._send(CONSOLE_REFRESH_EXIT_SCREEN)
            self._send(final_frame)
            duration_ms = round((time.monotonic() - started) * 1000)
            LOGGER.info(
                "Console refresh stopped target=%s transport=local reason=%s "
                "updates=%s duration_ms=%s",
                refresh.name,
                reason,
                updates,
                duration_ms,
            )

    def _send(self, text: str) -> None:
        if text:
            self.output_stream.write(text)
            self.output_stream.flush()

    def _prompt(self) -> str:
        session_id = self.context.selected_session_id()
        if session_id is None:
            return CONSOLE_PROMPT
        return f"dreamland[{session_id[:12]}]> "


class DreamlandCompleter(Completer):
    def __init__(self, console: ConsoleBackend) -> None:
        self.console = console

    def get_completions(
        self,
        document: Document,
        complete_event: object,
    ) -> Iterator[Completion]:
        line = document.current_line_before_cursor
        completed = self.console.complete(line, allow_exit=False)
        if completed is None:
            return
        yield Completion(completed, start_position=-len(line))


def _input_ready(stream: BinaryIO) -> bool:
    try:
        ready, _, _ = select.select([stream], [], [], 0)
    except (OSError, TypeError, ValueError):
        return False
    return bool(ready)


def _stream_is_terminal(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def _streams_support_terminal_refresh(
    input_stream: object,
    output_stream: object,
) -> bool:
    term = os.environ.get("TERM", "").strip().casefold()
    return (
        _stream_is_terminal(input_stream)
        and _stream_is_terminal(output_stream)
        and term != "dumb"
    )


def _consume_refresh_input(stream: BinaryIO) -> bool:
    """Consume one stop key and any delayed bytes in its terminal sequence."""

    first = stream.read(1)
    if not first:
        return False
    if not _refresh_input_may_continue(first):
        return True
    started = time.monotonic()
    consumed = len(first)
    maximum_deadline = started + _REFRESH_INPUT_SEQUENCE_MAX_SECONDS
    idle_deadline = started + _REFRESH_INPUT_SEQUENCE_IDLE_SECONDS
    while (
        consumed < _REFRESH_INPUT_SEQUENCE_MAX_BYTES
        and time.monotonic() < maximum_deadline
    ):
        if _input_ready(stream):
            next_byte = stream.read(1)
            if not next_byte:
                break
            consumed += len(next_byte)
            idle_deadline = min(
                maximum_deadline,
                time.monotonic() + _REFRESH_INPUT_SEQUENCE_IDLE_SECONDS,
            )
            continue
        if time.monotonic() >= idle_deadline:
            break
        time.sleep(_REFRESH_INPUT_DRAIN_POLL_SECONDS)
    return True


def _refresh_input_may_continue(first: bytes) -> bool:
    return first[0] >= 0x80 or first in {b"\x1b", b"\r", b"\n"}


@contextmanager
def _raw_terminal(stream: BinaryIO) -> Iterator[None]:
    try:
        file_descriptor = stream.fileno()
    except (AttributeError, OSError):
        yield
        return
    if not stream.isatty():
        yield
        return
    attributes = termios.tcgetattr(file_descriptor)
    try:
        tty.setraw(file_descriptor)
        yield
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, attributes)
