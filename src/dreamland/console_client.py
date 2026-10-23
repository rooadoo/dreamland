"""A terminal for an existing runtime; never creates a DREAMLAND runtime."""

from __future__ import annotations

import argparse
import math
import socket
import sys
import threading
from collections.abc import Sequence
from typing import Any

from dreamland.console import ConsoleContext, ConsoleRefresh, ConsoleResult
from dreamland.console_protocol import (
    PROTOCOL_VERSION,
    REQUEST_MAX_BYTES,
    RESPONSE_MAX_BYTES,
    ConsoleProtocolError,
    receive_message,
    send_message,
)
from dreamland.console_runtime import LocalConsole
from dreamland.runtime_logging import BufferedLogRecord, SessionTraceRecord

DEFAULT_SOCKET_PATH = "/run/dreamland/console.sock"
_UNCERTAIN_OUTCOME = (
    "The command may have completed in the backend. "
    "Reconnect and inspect its state before retrying; it was not replayed."
)


class ConsoleClientError(Exception):
    """A printable connection/protocol failure, without raw wire contents."""


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConsoleClientError("Invalid console response object.")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ConsoleClientError("Invalid console response text.")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ConsoleClientError("Invalid console response sequence.")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ConsoleClientError("Invalid console response flag.")
    return value


def _remote_error(message: dict[str, Any]) -> ConsoleClientError:
    error = _object(message.get("error"))
    code = _string(error.get("code"))
    # The server owns these fixed diagnostics, never exception/command text.
    detail = _string(error.get("message"))
    return ConsoleClientError(f"Console rejected the request ({code}): {detail}")


class ConsoleConnection:
    """One admitted connection, with one request in flight and no replay.

    A single reader also observes runtime EOF while the terminal is idle. It
    retains at most one bounded response; the request mutex serializes prompt
    completion, command execution, and log/refresh polling.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._socket = connection
        self._closed = threading.Event()
        self._condition = threading.Condition()
        self._requests = threading.Lock()
        self._next_id = 0
        self._pending_id: int | None = None
        self._response: dict[str, Any] | None = None
        self.failure: ConsoleClientError | None = None
        self.command_outcome_uncertain = False
        try:
            hello = receive_message(
                connection, max_bytes=RESPONSE_MAX_BYTES, idle_timeout=5.0
            )
            if hello is None:
                raise ConsoleClientError("Console closed before admission.")
            if hello.get("ok") is False:
                raise _remote_error(hello)
            if hello.get("type") != "hello":
                raise ConsoleClientError("Console did not send a versioned greeting.")
            if (
                type(hello.get("version")) is not int
                or hello["version"] != PROTOCOL_VERSION
            ):
                raise ConsoleClientError(
                    "Console protocol version mismatch; use the running backend's image."
                )
            self.runtime_id = _string(hello.get("runtime_id"))
            self.pid = _integer(hello.get("pid"))
            self.service = _string(hello.get("service"))
        except (OSError, ConsoleProtocolError) as exc:
            self.close()
            raise ConsoleClientError("Could not read the console greeting.") from exc
        except BaseException:
            self.close()
            raise
        self._reader = threading.Thread(
            target=self._receive_responses,
            name="dreamland-console-client-reader",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def connect(cls, path: str) -> ConsoleConnection:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(5.0)
            connection.connect(path)
            connection.settimeout(None)
        except OSError as exc:
            connection.close()
            raise ConsoleClientError(
                "Console unavailable; verify that the SSH service is running "
                "and that this client has access to its private socket."
            ) from exc
        return cls(connection)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        self._closed.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        with self._condition:
            self._condition.notify_all()

    def _fail(self, failure: ConsoleClientError) -> None:
        with self._condition:
            if not self.closed:
                self.failure = failure
            self.close()

    def _receive_responses(self) -> None:
        try:
            while not self.closed:
                message = receive_message(self._socket, max_bytes=RESPONSE_MAX_BYTES)
                if message is None:
                    raise ConsoleClientError(
                        "The backend console disconnected. Reconnect explicitly "
                        "to inspect the current runtime."
                    )
                with self._condition:
                    if (
                        self._pending_id is None
                        or type(message.get("id")) is not int
                        or message["id"] != self._pending_id
                        or self._response is not None
                    ):
                        raise ConsoleClientError("Unexpected console response ID.")
                    _boolean(message.get("ok"))
                    self._response = message
                    self._condition.notify_all()
        except ConsoleClientError as exc:
            self._fail(exc)
        except (OSError, ConsoleProtocolError):
            self._fail(ConsoleClientError("The console connection failed."))

    def request(self, operation: str, **arguments: object) -> dict[str, Any]:
        with self._requests:
            if self.closed:
                raise self.failure or ConsoleClientError("Console is disconnected.")
            with self._condition:
                self._next_id += 1
                request_id = self._next_id
                self._pending_id = request_id
                self._response = None
            if operation == "execute":
                self.command_outcome_uncertain = True
            try:
                send_message(
                    self._socket,
                    {"id": request_id, "op": operation, **arguments},
                    max_bytes=REQUEST_MAX_BYTES,
                )
                with self._condition:
                    while self._response is None and not self.closed:
                        self._condition.wait()
                    if self._response is None:
                        raise self.failure or ConsoleClientError(
                            "Console disconnected while waiting for a response."
                        )
                    response = self._response
                    self._pending_id = None
                    self._response = None
                if response["ok"] is False:
                    raise _remote_error(response)
                result = _object(response.get("result"))
                if operation == "execute":
                    self.command_outcome_uncertain = False
                return result
            except KeyboardInterrupt:
                self.close()
                raise
            except ConsoleClientError as exc:
                self._fail(exc)
                raise
            except (OSError, ConsoleProtocolError) as exc:
                failure = ConsoleClientError("The console request failed.")
                self._fail(failure)
                raise failure from exc


class _RemoteLogs:
    def __init__(self, connection: ConsoleConnection) -> None:
        self.connection = connection

    @property
    def last_sequence(self) -> int:
        return _integer(self.connection.request("log_cursor").get("sequence"))

    def after(
        self, sequence: int, *, timeout: float | None = None
    ) -> tuple[BufferedLogRecord, ...]:
        return tuple(
            BufferedLogRecord(
                _integer(record.get("sequence")), _string(record.get("text"))
            )
            for record in _records(
                self.connection.request("logs_after", sequence=sequence)
            )
        )


class _RemoteTraces:
    def __init__(self, connection: ConsoleConnection) -> None:
        self.connection = connection

    def last_sequence(self, session_id: str) -> int:
        return _integer(
            self.connection.request("trace_cursor", session_id=session_id).get(
                "sequence"
            )
        )

    def after(
        self, session_id: str, sequence: int, *, timeout: float | None = None
    ) -> tuple[SessionTraceRecord, ...]:
        return tuple(
            SessionTraceRecord(
                _integer(record.get("sequence")),
                session_id,
                _string(record.get("text")),
            )
            for record in _records(
                self.connection.request(
                    "traces_after", session_id=session_id, sequence=sequence
                )
            )
        )


def _records(result: dict[str, Any]) -> list[dict[str, Any]]:
    records = result.get("records")
    if not isinstance(records, list) or len(records) > 256:
        raise ConsoleClientError("Invalid console log records.")
    return [_object(record) for record in records]


class RemoteConsole:
    """Adapt wire values to the existing terminal's context and view callbacks."""

    def __init__(self, connection: ConsoleConnection) -> None:
        self.connection = connection
        self.logs = _RemoteLogs(connection)
        self.trace_logs = _RemoteTraces(connection)

    @property
    def shutdown_requested(self) -> bool:
        # LocalConsole polls this in its prompt monitor. No network polling.
        return self.connection.closed

    def execute(
        self, command: str, *, allow_exit: bool, context: ConsoleContext | None = None
    ) -> ConsoleResult:
        if command.strip().casefold() == "disconnect":
            self.connection.close()
            return ConsoleResult(
                output="Console disconnected; backend remains running.\n"
            )
        result = self.connection.request("execute", line=command)
        remote_context = _object(result.get("context"))
        active_id = _optional_string(remote_context.get("active_session_id"))
        default_id = _optional_string(remote_context.get("default_session_id"))
        recent_ids = remote_context.get("recent_session_ids")
        if not isinstance(recent_ids, list):
            raise ConsoleClientError("Invalid console session context.")
        recent = tuple(_string(session_id) for session_id in recent_ids)
        if context is not None:
            context.active_session_id = active_id
            context.default_session_id = default_id
            context.recent_session_ids = recent
        refresh = None
        if result.get("refresh") is not None:
            view = _object(result["refresh"])
            interval = view.get("interval_seconds")
            if (
                not isinstance(interval, (int, float))
                or isinstance(interval, bool)
                or not math.isfinite(interval)
                or interval <= 0
            ):
                raise ConsoleClientError("Invalid console refresh interval.")
            refresh = ConsoleRefresh(
                name=_string(view.get("name")),
                interval_seconds=float(interval),
                render=self._refresh,
            )
        return ConsoleResult(
            output=_string(result.get("output")),
            follow=_boolean(result.get("follow")),
            trace_session_id=_optional_string(result.get("trace_session_id")),
            follow_trace_session_id=_optional_string(
                result.get("follow_trace_session_id")
            ),
            refresh=refresh,
            # Only the client's connection controls its lifecycle.
            leave=False,
            shutdown=False,
        )

    def _refresh(self) -> str:
        return _string(self.connection.request("refresh").get("output"))

    def complete(self, line: str, *, allow_exit: bool) -> str | None:
        if line and "disconnect".startswith(line.strip().casefold()):
            return "disconnect "
        try:
            return _optional_string(
                self.connection.request("complete", line=line).get("completion")
            )
        except ConsoleClientError as exc:
            self.connection._fail(exc)
            return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect to the running SSH console.")
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET_PATH, help="Private Unix socket path"
    )
    parser.add_argument(
        "--command",
        help="Execute one command, print its initial output, then disconnect",
    )
    arguments = parser.parse_args(argv)
    connection: ConsoleConnection | None = None
    try:
        connection = ConsoleConnection.connect(arguments.socket)
        console = RemoteConsole(connection)
        identity = (
            f"Connected to DREAMLAND {connection.service} "
            f"runtime={connection.runtime_id} pid={connection.pid}."
        )
        if arguments.command is not None:
            print(identity, file=sys.stderr)
            result = console.execute(arguments.command, allow_exit=False)
            sys.stdout.write(result.output)
            sys.stdout.flush()
            return 0
        print(identity)
        print(
            "Ctrl-D or disconnect closes this console. Use dreamlandctl stop to stop the service."
        )
        LocalConsole(console).run()
        if connection.failure is not None:
            raise connection.failure
        return 0
    except KeyboardInterrupt:
        print("\nConsole interrupted; backend remains running.", file=sys.stderr)
        if connection is not None and connection.command_outcome_uncertain:
            print(_UNCERTAIN_OUTCOME, file=sys.stderr)
        return 130
    except ConsoleClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if connection is not None and connection.command_outcome_uncertain:
            print(_UNCERTAIN_OUTCOME, file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
