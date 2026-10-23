"""Private, detachable access to the console owned by the running SSH backend."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import shlex
import socket
import stat
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dreamland.console import (
    CONSOLE_PREFIX,
    ConsoleContext,
    ConsoleRefresh,
    ConsoleResult,
    DreamlandConsole,
)
from dreamland.console_protocol import (
    PROTOCOL_VERSION,
    ConsoleProtocolError,
    receive_message,
    send_message,
)

LOGGER = logging.getLogger("dreamland.console")
MAX_FOLLOW_RECORDS = 256
FOLLOW_WAIT_SECONDS = 0.1
STOP_WAIT_SECONDS = 2.0

_ERROR_MESSAGES = {
    "unsupported_platform": "The private console requires Linux peer credentials.",
    "unsafe_path": "Console socket requires an absolute path in an owned 0700 directory without symlinks.",
    "endpoint_in_use": "The console socket or its private runtime directory is in use.",
    "startup_failed": "The private console socket could not be started.",
    "already_running": "The private console is already running or finishing an operation.",
    "busy": "Another operator console is connected or finishing an operation.",
    "access_denied": "This local user cannot access the operator console.",
    "invalid_request": "Invalid console request.",
    "unsupported_operation": "Unsupported console operation.",
    "operation_failed": "Console operation failed; inspect backend logs.",
    "response_too_large": "Console response exceeds the size limit; the command may have completed.",
    "no_refresh": "No console refresh view is active on this connection.",
}


class ConsoleSocketError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class ConsoleSocketServer:
    """One admitted operator with serial operations and an ephemeral endpoint.

    Disconnecting does not cancel an in-flight console operation. Its admission
    remains reserved until it returns, preventing disconnected clients from
    accumulating workers. Shutdown closes transports and joins for a bounded
    time; it does not attempt unsafe asynchronous cancellation of Python work.
    """

    def __init__(self, console: DreamlandConsole, path: Path) -> None:
        self.console = console
        self.path = Path(path)
        self.runtime_id = str(uuid.uuid4())
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._listener: socket.socket | None = None
        self._client: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._directory_fd: int | None = None
        self._endpoint_identity: tuple[int, int] | None = None

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._listener is not None and not self._stop_event.is_set()

    def start(self) -> None:
        with self._state_lock:
            if (
                self._listener is not None
                or self._worker_thread is not None
                or (self._accept_thread is not None and self._accept_thread.is_alive())
            ):
                raise ConsoleSocketError("already_running")
            if not hasattr(socket, "SO_PEERCRED"):
                raise ConsoleSocketError("unsupported_platform")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self._directory_fd = _open_runtime_directory(self.path)
                _remove_stale_endpoint(self._directory_fd, self.path.name)
                # Bind through the opened directory so replacing a path ancestor
                # cannot redirect the endpoint into a different directory.
                listener.bind(_anchored_path(self._directory_fd, self.path.name))
                info = os.stat(
                    self.path.name, dir_fd=self._directory_fd, follow_symlinks=False
                )
                self._endpoint_identity = (info.st_dev, info.st_ino)
                os.chmod(
                    self.path.name,
                    0o600,
                    dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                listener.listen(4)
                listener.settimeout(0.2)
                self._stop_event.clear()
                self._listener = listener
                self._accept_thread = threading.Thread(
                    target=self._accept_loop,
                    args=(listener,),
                    name="dreamland-console-listener",
                    daemon=True,
                )
                self._accept_thread.start()
            except (OSError, RuntimeError, ConsoleSocketError) as exc:
                listener.close()
                self._listener = None
                self._remove_endpoint_locked()
                self._release_directory_locked()
                if isinstance(exc, ConsoleSocketError):
                    raise
                raise ConsoleSocketError("startup_failed") from None
        LOGGER.info(
            "Operator console socket started service=ssh runtime_id=%s", self.runtime_id
        )

    def stop(self) -> None:
        with self._state_lock:
            self._stop_event.set()
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            if self._client is not None:
                _close_connection(self._client)
            self._remove_endpoint_locked()
            threads = (self._accept_thread, self._worker_thread)
            if self._worker_thread is None:
                self._release_directory_locked()
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._state_lock:
            pending = self._worker_thread is not None
        LOGGER.info("Operator console socket stopped pending_operation=%s", pending)

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    LOGGER.warning("Operator console listener failed")
                    self.stop()
                return
            try:
                uid = _peer_uid(connection)
            except OSError:
                uid = -1
            if uid not in {0, os.geteuid()}:
                _reject_connection(connection, "access_denied")
                LOGGER.warning(
                    "Operator console connection rejected reason=access_denied"
                )
                continue
            with self._state_lock:
                if self._stop_event.is_set():
                    connection.close()
                    return
                if self._worker_thread is None:
                    self._client = connection
                    self._worker_thread = threading.Thread(
                        target=self._serve_connection,
                        args=(connection,),
                        name="dreamland-console-operator",
                        daemon=True,
                    )
                    try:
                        self._worker_thread.start()
                    except RuntimeError:
                        self._client = None
                        self._worker_thread = None
                        _reject_connection(connection, "operation_failed")
                        LOGGER.warning("Operator console worker could not start")
                    continue
            _reject_connection(connection, "busy")
            LOGGER.info("Operator console connection rejected reason=busy")

    def _serve_connection(self, connection: socket.socket) -> None:
        context = ConsoleContext()
        refresh: ConsoleRefresh | None = None
        last_request_id = 0
        LOGGER.info("Operator console connected transport=unix")
        try:
            send_message(
                connection,
                {
                    "type": "hello",
                    "version": PROTOCOL_VERSION,
                    "runtime_id": self.runtime_id,
                    "pid": os.getpid(),
                    "service": "ssh",
                },
            )
            while not self._stop_event.is_set():
                try:
                    request = receive_message(connection)
                except ConsoleProtocolError as exc:
                    _send_error(connection, None, exc.code, str(exc))
                    break
                if request is None:
                    break
                request_id = request.get("id")
                if type(request_id) is not int or not last_request_id < request_id <= (
                    2**53 - 1
                ):
                    _send_error(connection, None, "invalid_request")
                    continue
                last_request_id = request_id
                try:
                    result, refresh = self._dispatch(request, context, refresh)
                except ConsoleSocketError as exc:
                    _send_error(connection, request_id, exc.code)
                    continue
                except Exception as exc:
                    LOGGER.warning(
                        "Operator console operation failed error=%s", type(exc).__name__
                    )
                    _send_error(connection, request_id, "operation_failed")
                    continue
                try:
                    send_message(
                        connection, {"id": request_id, "ok": True, "result": result}
                    )
                except ConsoleProtocolError as exc:
                    if exc.code != "message_too_large":
                        raise
                    _send_error(connection, request_id, "response_too_large")
        except ConsoleProtocolError as exc:
            LOGGER.info("Operator console transport ended reason=%s", exc.code)
        finally:
            _close_connection(connection)
            with self._state_lock:
                self._client = None
                self._worker_thread = None
                if self._stop_event.is_set():
                    self._release_directory_locked()
            LOGGER.info("Operator console disconnected transport=unix")

    def _dispatch(
        self,
        request: dict[str, Any],
        context: ConsoleContext,
        refresh: ConsoleRefresh | None,
    ) -> tuple[dict[str, Any], ConsoleRefresh | None]:
        operation = request.get("op")
        fields = {
            "execute": {"line"},
            "complete": {"line"},
            "log_cursor": set(),
            "logs_after": {"sequence"},
            "trace_cursor": {"session_id"},
            "traces_after": {"session_id", "sequence"},
            "refresh": set(),
        }
        if not isinstance(operation, str) or operation not in fields:
            raise ConsoleSocketError("unsupported_operation")
        if set(request) != {"id", "op"} | fields[operation]:
            raise ConsoleSocketError("invalid_request")
        if operation in {"execute", "complete"}:
            line = request["line"]
            if not isinstance(line, str) or "\x00" in line:
                raise ConsoleSocketError("invalid_request")
            if operation == "complete":
                return {
                    "completion": self.console.complete(line, allow_exit=False)
                }, refresh
            LOGGER.info("Operator console command started transport=unix")
            started = time.monotonic()
            if _is_shutdown(line):
                result = ConsoleResult(
                    output="Use dreamlandctl stop to stop the service, or dreamlandctl restart to restart it.\n"
                )
            else:
                result = self.console.execute(line, allow_exit=False, context=context)
            LOGGER.info(
                "Operator console command completed transport=unix duration_ms=%s",
                round((time.monotonic() - started) * 1000),
            )
            return _console_result(result, context), result.refresh
        if operation == "refresh":
            if refresh is None:
                raise ConsoleSocketError("no_refresh")
            return {"output": refresh.render()}, refresh
        if operation == "log_cursor":
            return {"sequence": self.console.logs.last_sequence}, refresh
        if operation in {"trace_cursor", "traces_after"}:
            session_id = request["session_id"]
            if not isinstance(session_id, str) or not 0 < len(session_id) <= 256:
                raise ConsoleSocketError("invalid_request")
            if operation == "trace_cursor":
                return {
                    "sequence": self.console.trace_logs.last_sequence(session_id)
                }, refresh
        sequence = request["sequence"]
        if type(sequence) is not int or not 0 <= sequence <= 2**63 - 1:
            raise ConsoleSocketError("invalid_request")
        records = (
            self.console.logs.after(sequence, timeout=FOLLOW_WAIT_SECONDS)
            if operation == "logs_after"
            else self.console.trace_logs.after(
                request["session_id"], sequence, timeout=FOLLOW_WAIT_SECONDS
            )
        )[:MAX_FOLLOW_RECORDS]
        return {
            "records": [
                {"sequence": record.sequence, "text": record.text} for record in records
            ],
            "sequence": records[-1].sequence if records else sequence,
        }, refresh

    def _remove_endpoint_locked(self) -> None:
        if self._directory_fd is None or self._endpoint_identity is None:
            return
        try:
            info = os.stat(
                self.path.name, dir_fd=self._directory_fd, follow_symlinks=False
            )
            if (info.st_dev, info.st_ino) == self._endpoint_identity and stat.S_ISSOCK(
                info.st_mode
            ):
                os.unlink(self.path.name, dir_fd=self._directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.warning("Operator console endpoint cleanup failed")
        finally:
            self._endpoint_identity = None

    def _release_directory_locked(self) -> None:
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None


def _open_runtime_directory(path: Path) -> int:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or "\x00" in str(path)
        or len(os.fsencode(path)) >= 108
    ):
        raise ConsoleSocketError("unsafe_path")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parent.parts[1:]
        for index, part in enumerate(parts):
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
            except FileNotFoundError:
                if index != len(parts) - 1:
                    raise ConsoleSocketError("unsafe_path") from None
                os.mkdir(part, 0o700, dir_fd=current)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
            os.close(current)
            current = child
        info = os.fstat(current)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConsoleSocketError("unsafe_path")
        try:
            # The dedicated runtime directory is the lock, leaving no persistent
            # lock file and serializing stale-endpoint replacement at startup.
            fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConsoleSocketError("endpoint_in_use") from None
        return current
    except OSError:
        os.close(current)
        raise ConsoleSocketError("unsafe_path") from None
    except BaseException:
        os.close(current)
        raise


def _remove_stale_endpoint(directory_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ConsoleSocketError("unsafe_path")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(_anchored_path(directory_fd, name))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise ConsoleSocketError("endpoint_in_use") from None
        else:
            raise ConsoleSocketError("endpoint_in_use")
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ConsoleSocketError("endpoint_in_use")
    os.unlink(name, dir_fd=directory_fd)


def _anchored_path(directory_fd: int, name: str) -> str:
    return f"/proc/self/fd/{directory_fd}/{name}"


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _, uid, _ = struct.unpack("3i", credentials)
    return uid


def _is_shutdown(line: str) -> bool:
    try:
        arguments = shlex.split(line)
    except ValueError:
        return False
    if arguments and arguments[0] == CONSOLE_PREFIX:
        arguments = arguments[1:]
    return bool(arguments and arguments[0] == "shutdown")


def _console_result(result: ConsoleResult, context: ConsoleContext) -> dict[str, Any]:
    return {
        "output": result.output,
        "follow": result.follow,
        "trace_session_id": result.trace_session_id,
        "follow_trace_session_id": result.follow_trace_session_id,
        "refresh": None
        if result.refresh is None
        else {
            "name": result.refresh.name,
            "interval_seconds": result.refresh.interval_seconds,
        },
        "leave": False,
        "shutdown": False,
        "context": {
            "active_session_id": context.active_session_id,
            "default_session_id": context.default_session_id,
            "recent_session_ids": list(context.recent_session_ids),
        },
    }


def _send_error(
    connection: socket.socket,
    request_id: int | None,
    code: str,
    message: str | None = None,
    *,
    timeout: float = 5.0,
) -> None:
    send_message(
        connection,
        {
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message or _ERROR_MESSAGES[code]},
        },
        timeout=timeout,
    )


def _reject_connection(connection: socket.socket, code: str) -> None:
    try:
        _send_error(connection, None, code, timeout=0.2)
    except ConsoleProtocolError:
        pass
    finally:
        connection.close()


def _close_connection(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()
