from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal

from dreamland.agent_tracing import record_langfuse_session_start
from dreamland.console import (
    CONSOLE_PREFIX,
    DreamlandConsole,
)
from dreamland.lobby_input import BoundaryFilter
from dreamland.login_banner import initial_login_banner
from dreamland.models import (
    CommandExecutionControl,
    CommandResult,
    ConnectionMetadata,
    SessionState,
    ShellFrame,
)
from dreamland.runtime_logging import trace_input, trace_output
from dreamland.session_registry import RegisteredSession
from dreamland.ssh_session_state import (
    apply_nested_exit_fallback,
    pop_nested_shell_for_transport_exit,
    record_transport_result,
    should_close_shell_session,
)
from dreamland.ssh_streaming import (
    LineBufferedCommandWriter,
    execute_backend_stream,
)
from dreamland.ssh_types import CommandBackend
from dreamland.state_profiles import StateProfile
from dreamland.terminal import create_interactive_editor

COMMAND_LOGGER = logging.getLogger("dreamland.commands")
CONSOLE_LOGGER = logging.getLogger("dreamland.console")
LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


@dataclass(frozen=True, slots=True)
class ShellOutcome:
    reason: Literal["world_exit", "disconnect", "operator_close", "shutdown", "failure"]
    exit_status: int = 0


class ShellSession:
    def __init__(
        self,
        channel: Any,
        username: str,
        backend: CommandBackend,
        connection_metadata: ConnectionMetadata | None = None,
        *,
        console: DreamlandConsole | None = None,
        state_profile: StateProfile | None = None,
        session: SessionState | None = None,
        lobby_mode: bool = False,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.channel = channel
        self.backend = backend
        if session is not None and state_profile is not None:
            raise ValueError("Provide either a session or a state profile")
        self.session = session or (
            backend.create_session(
                username, connection_metadata, state_profile=state_profile
            )
            if state_profile is not None
            else backend.create_session(username, connection_metadata)
        )
        self.login_username = username
        self.console = console
        self.lobby_mode = lobby_mode
        self.stop_event = stop_event or threading.Event()
        self.boundary_input = b""
        self._boundary_prefix = BoundaryFilter()
        self._after_carriage_return = False
        self._close_requested = threading.Event()
        self._retired = threading.Event()
        self._workers_lock = threading.Lock()
        self._workers: dict[
            threading.Thread, tuple[CommandExecutionControl, LineBufferedCommandWriter]
        ] = {}
        self._send_lock = threading.Lock()
        self._resources_released = False
        self._registered_session = (
            console.session_registry.register(
                self.session,
                close_callback=self.request_close if lobby_mode else None,
            )
            if console is not None
            else None
        )
        try:
            _record_langfuse_session_start(console, self.session)
        except Exception:
            if lobby_mode:
                self.close()
            raise

    @property
    def cleanup_pending(self) -> bool:
        with self._workers_lock:
            return bool(self._workers)

    def request_close(self) -> None:
        """Fence output and command admission without joining provider owners."""
        self._close_requested.set()
        with self._send_lock:
            pass
        with self._workers_lock:
            workers = tuple(self._workers.values())
        for control, writer in workers:
            control.cancel()
            writer.close()

    def _release_if_idle(self) -> None:
        with self._workers_lock:
            if not self._retired.is_set() or self._workers or self._resources_released:
                return
            self._resources_released = True
        release = getattr(self.backend, "release_session", None)
        if release is not None:
            try:
                release(self.session)
            except Exception:
                LIFECYCLE_LOGGER.exception(
                    "SSH visit resource release failed session=%s",
                    self.session.langfuse_trace_id,
                )

    def run(self) -> ShellOutcome:
        try:
            if self.lobby_mode and hasattr(self.channel, "settimeout"):
                self.channel.settimeout(0.1)
            return self._run()
        except (EOFError, OSError):
            if not self.lobby_mode:
                raise
            return ShellOutcome("disconnect", 1)
        finally:
            self.close()

    def close(self) -> None:
        """Retire a constructed visit, including one whose opening send failed."""
        self._retired.set()
        self.request_close()
        if self._registered_session is not None and self.console is not None:
            self.console.session_registry.unregister(
                self._registered_session.session_id,
                session=self.session,
            )
        self._release_if_idle()

    def _interruption(self) -> ShellOutcome | None:
        if not self.lobby_mode:
            return None
        if self.stop_event.is_set():
            return ShellOutcome("shutdown", 0)
        if self._close_requested.is_set():
            return ShellOutcome("operator_close", 0)
        if getattr(self.channel, "closed", False) or getattr(
            self.channel, "eof_received", False
        ):
            return ShellOutcome("disconnect", 1)
        return None

    def _remember_input(self, data: bytes) -> None:
        if not self.lobby_mode:
            return
        # Retain receive order, including bytes drained by the command loop.
        # Compact old bytes into their finite pending-key state so arbitrarily
        # long world visits cannot grow the parent-boundary handoff in memory.
        combined = self.boundary_input + data
        if len(combined) > 4096:
            for byte in combined[:-4096]:
                self._boundary_prefix.consume(byte)
            prefix = {
                "escape": b"\x1b",
                "csi": b"\x1b[",
                "ss3": b"\x1bO",
                "osc": b"\x1b]",
                "osc_escape": b"\x1b]\x1b",
            }.get(self._boundary_prefix.sequence, b"")
            if self._boundary_prefix.utf8_remaining:
                prefix += {1: b"\xc2", 2: b"\xe0", 3: b"\xf0"}[
                    self._boundary_prefix.utf8_remaining
                ]
            if self._boundary_prefix.after_cr:
                prefix += b"\r"
            self.boundary_input = prefix + combined[-4096:]
            self._boundary_prefix = BoundaryFilter()
        else:
            self.boundary_input = combined

    def _run(self) -> ShellOutcome:
        interrupted = self._interruption()
        if interrupted is not None:
            return interrupted
        banner = initial_login_banner(self.session.world_state)
        display_banner = banner.replace("\r\n", "\n").replace("\n", "\r\n")
        self._send(display_banner)
        LIFECYCLE_LOGGER.info(
            "Initial login banner sent session=%s host=%s bytes=%s",
            self.session.langfuse_trace_id,
            self.session.host_identity.hostname,
            len(display_banner.encode("utf-8")),
        )
        prompt = self.backend.prompt(self.session)
        self._send(prompt)
        editor = create_interactive_editor(
            self.session.interactive_program,
            self._send_echo,
            prompt,
        )
        editors = [editor]

        while True:
            interrupted = self._interruption()
            if interrupted is not None:
                return interrupted
            try:
                data = self.channel.recv(1024)
            except TimeoutError:
                continue
            if not data:
                return ShellOutcome("disconnect", 1)
            self._remember_input(data)
            for byte in self._normalize_line_endings(data):
                interrupted = self._interruption()
                if interrupted is not None:
                    return interrupted
                if byte == 3 and self.session.interactive_program.name in {
                    "ssh",
                    "sudo",
                    "ssh-keygen",
                    "passwd",
                }:
                    if self.cleanup_pending:
                        # A prior in-flight interruption can return before its
                        # provider owner releases session state. Do not block
                        # on that owner or mutate its frame on a second Ctrl-C.
                        self._send("^C\r\n")
                        editor.start_prompt(prompt)
                        self._send(prompt)
                        continue
                    starting_shell_stack = tuple(self.session.shell_stack)
                    COMMAND_LOGGER.warning(
                        "user=%s cwd=%s program=%s interrupted=true signal=SIGINT",
                        self.session.username,
                        self.session.cwd,
                        self.session.interactive_program.name,
                    )
                    with _registered_session_lock(self._registered_session):
                        record_transport_result(
                            self.session,
                            "\x03",
                            self.session.cwd,
                            CommandResult(
                                exit_status=130,
                                interrupted=True,
                                signal="SIGINT",
                            ),
                        )
                        pop_nested_shell_for_transport_exit(
                            self.session, signal="SIGINT"
                        )
                    self._send("^C\r\n")
                    prompt = self.backend.prompt(self.session)
                    self._send(prompt)
                    editor = self._editor_after_stack_change(
                        editors, starting_shell_stack, prompt
                    )
                    continue
                event = editor.handle_byte(byte)
                if event is None:
                    continue
                if event.should_exit:
                    starting_shell_stack = tuple(self.session.shell_stack)
                    if pop_nested_shell_for_transport_exit(self.session):
                        prompt = self.backend.prompt(self.session)
                        self._send("\r\n")
                        self._send(prompt)
                        editor = self._editor_after_stack_change(
                            editors,
                            starting_shell_stack,
                            prompt,
                        )
                        continue
                    if not self.lobby_mode:
                        self.channel.send_exit_status(0)
                    return ShellOutcome("world_exit", 0)
                if event.command is not None:
                    if self._handle_console_request(event.command):
                        discard_history = getattr(
                            editor,
                            "discard_latest_history",
                            None,
                        )
                        if discard_history is not None:
                            discard_history(event.command)
                        if self.channel.closed:
                            return ShellOutcome("disconnect", 1)
                        prompt = self.backend.prompt(self.session)
                        editor.start_prompt(prompt)
                        self._send(prompt)
                        continue
                    trace_input(self.session.langfuse_trace_id, event.command)
                    starting_shell_stack = tuple(self.session.shell_stack)
                    result = self._execute_and_write(event.command)
                    interrupted = self._interruption()
                    if interrupted is not None:
                        return interrupted
                    apply_nested_exit_fallback(
                        self.session,
                        event.command,
                        result,
                        starting_shell_stack,
                    )
                    if should_close_shell_session(
                        event.command,
                        result,
                        starting_shell_stack,
                    ):
                        if not self.lobby_mode:
                            self.channel.send_exit_status(result.exit_status)
                        return ShellOutcome("world_exit", result.exit_status)
                    prompt = self.backend.prompt(self.session)
                    prompt_already_rendered = _result_ends_with_active_prompt(
                        self.session,
                        result,
                        prompt,
                    )
                    if not prompt_already_rendered:
                        self._send(prompt)
                    editor = self._editor_after_stack_change(
                        editors,
                        starting_shell_stack,
                        prompt,
                    )

    def _normalize_line_endings(self, data: bytes) -> bytes:
        # CRLF is one Enter even when its bytes cross receive, command, or
        # editor boundaries. Consume the whole received chunk here so data
        # drained during execution observes the same receive-order state.
        normalized = bytearray()
        for byte in data:
            follows_cr = self._after_carriage_return
            self._after_carriage_return = byte == 13
            if byte == 10 and follows_cr:
                continue
            normalized.append(byte)
        return bytes(normalized)

    def _handle_console_request(self, command: str) -> bool:
        if not _is_ssh_operator_command(command):
            return False
        CONSOLE_LOGGER.warning(
            "SSH operator command rejected user=%s",
            self.login_username,
        )
        self._send(_SSH_OPERATOR_COMMAND_DENIAL.replace("\n", "\r\n"))
        return True

    def _editor_after_stack_change(
        self,
        editors: list[Any],
        starting_shell_stack: tuple[ShellFrame, ...],
        prompt: str,
    ) -> Any:
        current_stack = tuple(self.session.shell_stack)
        starting_depth = len(starting_shell_stack)
        current_depth = len(current_stack)

        if current_depth > starting_depth:
            del editors[starting_depth:]
            for frame in current_stack[len(editors) :]:
                editors.append(
                    create_interactive_editor(
                        frame.interactive_program,
                        self._send_echo,
                        prompt,
                    )
                )
            return editors[-1]

        if current_depth < starting_depth:
            del editors[current_depth:]
            if not editors:
                editors.append(
                    create_interactive_editor(
                        self.session.interactive_program,
                        self._send_echo,
                        prompt,
                    )
                )
            editors[-1].start_prompt(prompt)
            return editors[-1]

        if (
            current_stack
            and starting_shell_stack
            and _editor_context_changed(
                starting_shell_stack[-1],
                current_stack[-1],
            )
        ):
            editors[-1] = create_interactive_editor(
                current_stack[-1].interactive_program,
                self._send_echo,
                prompt,
            )
            return editors[-1]

        editors[-1].start_prompt(prompt)
        return editors[-1]

    def _execute_and_write(self, command: str) -> CommandResult:
        # A cancelled owner may still be unwinding an upstream call. Do not
        # accumulate another worker for each Ctrl-C while that call is blocked.
        if self.lobby_mode:
            while self.cleanup_pending:
                if self._interruption() is not None:
                    return CommandResult(exit_status=130)
                self._close_requested.wait(0.01)
        starting_cwd = self.session.cwd
        control = CommandExecutionControl()
        writer = LineBufferedCommandWriter(
            stdout=lambda text: self._send(text.replace("\n", "\r\n")),
            stderr=lambda text: self._send(text.replace("\n", "\r\n")),
            control=control,
        )
        result_holder: dict[str, CommandResult] = {}
        error_holder: dict[str, Exception] = {}

        def execute() -> None:
            try:
                with _registered_session_lock(self._registered_session):
                    if self.lobby_mode and (
                        self._interruption() is not None
                        or (
                            self._registered_session is not None
                            and self._registered_session.closed.is_set()
                        )
                    ):
                        control.cancel()
                        return
                    result = execute_backend_stream(
                        self.backend,
                        self.session,
                        command,
                        writer,
                        control=control,
                    )
                if control.cancelled():
                    writer.discard_buffers()
                else:
                    prompt = self.backend.prompt(self.session)
                    prompt_already_rendered = _result_ends_with_active_prompt(
                        self.session,
                        result,
                        prompt,
                    )
                    writer.flush(ensure_trailing_newline=not prompt_already_rendered)
                result_holder["result"] = result
            except Exception as exc:
                error_holder["error"] = exc
            finally:
                writer.close()
                with self._workers_lock:
                    self._workers.pop(threading.current_thread(), None)
                self._release_if_idle()

        thread = threading.Thread(target=execute, daemon=True)
        with self._workers_lock:
            self._workers[thread] = (control, writer)
        thread.start()
        while thread.is_alive():
            if self._interruption() is not None:
                control.cancel()
                writer.close()
                return control.rendered_result()
            if self.channel.recv_ready():
                data = self.channel.recv(1024)
                if not data:
                    control.cancel()
                    writer.close()
                    return control.rendered_result()
                if self.lobby_mode:
                    # Preserve enough discarded bytes for the parent parser to
                    # consume a trailing split CRLF or control/key sequence.
                    self._remember_input(data)
                data = self._normalize_line_endings(data)
                if 3 in data and not control.cancelled():
                    COMMAND_LOGGER.warning(
                        "user=%s cwd=%s command=%r interrupted=true signal=SIGINT",
                        self.session.username,
                        starting_cwd,
                        command,
                    )
                    control.cancel()
                    writer.discard_buffers()
                    writer.close()
                    self._send("^C\r\n")
                    result = control.rendered_result()
                    if control.claim_recording():
                        record_transport_result(
                            self.session,
                            command,
                            starting_cwd,
                            result,
                        )
                    return result
            thread.join(timeout=0.01)
        if "error" in error_holder:
            raise error_holder["error"]
        thread.join()
        return result_holder.get("result", CommandResult())

    def _send(self, text: str) -> None:
        if text:
            with self._send_lock:
                if self.lobby_mode and self._close_requested.is_set():
                    return
                send = getattr(self.channel, "sendall", self.channel.send)
                send(text.encode("utf-8", errors="replace"))
                trace_output(self.session.langfuse_trace_id, text)

    def _send_echo(self, text: str) -> None:
        if text:
            with self._send_lock:
                if self.lobby_mode and self._close_requested.is_set():
                    return
                send = (
                    getattr(self.channel, "sendall", self.channel.send)
                    if self.lobby_mode
                    else self.channel.send
                )
                send(text.encode("utf-8", errors="replace"))


def _editor_context_changed(before: ShellFrame, after: ShellFrame) -> bool:
    return (
        before.username != after.username
        or before.interactive_program != after.interactive_program
    )


def _result_ends_with_active_prompt(
    session: SessionState,
    result: CommandResult,
    prompt: str,
) -> bool:
    if not prompt:
        return False
    program = session.interactive_program
    if not program.prompt_template:
        return False
    return result.stdout.endswith(prompt) or result.stderr.endswith(prompt)


class ExecSession:
    def __init__(
        self,
        channel: Any,
        username: str,
        backend: CommandBackend,
        command: str,
        connection_metadata: ConnectionMetadata | None = None,
        *,
        console: DreamlandConsole | None = None,
    ) -> None:
        self.channel = channel
        self.backend = backend
        self.session = backend.create_session(username, connection_metadata)
        self.login_username = username
        self.command = command
        self.console = console
        self._registered_session = (
            console.session_registry.register(self.session)
            if console is not None
            else None
        )
        _record_langfuse_session_start(console, self.session)

    def run(self) -> None:
        try:
            self._run()
        finally:
            if self._registered_session is not None and self.console is not None:
                self.console.session_registry.unregister(
                    self._registered_session.session_id,
                    session=self.session,
                )
            release = getattr(self.backend, "release_session", None)
            if release is not None:
                release(self.session)

    def _run(self) -> None:
        if self._run_console_command():
            return
        trace_input(self.session.langfuse_trace_id, self.command)
        writer = LineBufferedCommandWriter(
            stdout=self._send_stdout,
            stderr=self._send_stderr,
        )
        with _registered_session_lock(self._registered_session):
            result = execute_backend_stream(
                self.backend,
                self.session,
                self.command,
                writer,
            )
        writer.flush()
        self.channel.send_exit_status(result.exit_status)

    def _run_console_command(self) -> bool:
        if not _is_ssh_operator_command(self.command):
            return False
        CONSOLE_LOGGER.warning(
            "SSH operator command rejected user=%s",
            self.login_username,
        )
        self._send_stderr(_SSH_OPERATOR_COMMAND_DENIAL)
        self.channel.send_exit_status(126)
        return True

    def _send_stdout(self, text: str) -> None:
        if text:
            self.channel.send(text.encode("utf-8", errors="replace"))
            trace_output(self.session.langfuse_trace_id, text, stream="stdout")

    def _send_stderr(self, text: str) -> None:
        if text:
            self.channel.send_stderr(text.encode("utf-8", errors="replace"))
            trace_output(self.session.langfuse_trace_id, text, stream="stderr")


_SSH_OPERATOR_COMMAND_DENIAL = (
    "error: Dreamland operator commands are unavailable over SSH\n"
)


def _is_ssh_operator_command(command: str) -> bool:
    stripped = command.lstrip()
    if not stripped.startswith(CONSOLE_PREFIX):
        return False
    suffix = stripped[len(CONSOLE_PREFIX) :]
    return not suffix or suffix[0].isspace()


def _registered_session_lock(registration: RegisteredSession | None):
    if registration is None:
        return nullcontext()
    return registration.lock


def _record_langfuse_session_start(
    console: DreamlandConsole | None,
    session: SessionState,
) -> None:
    settings = getattr(console, "langfuse_settings", None)
    if settings is None:
        return
    record_langfuse_session_start(
        enabled=settings.enabled,
        public_key=settings.public_key,
        secret_key=settings.secret_key,
        host=settings.host,
        session=session,
    )
