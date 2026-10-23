from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dreamland.backend import AgentBackend
from dreamland.console import DreamlandConsole
from dreamland.lobby_presentation import normalize_terminal_type
from dreamland.profile import ServerProfile, server_profile
from dreamland.ssh_auth import SSHAuthenticator
from dreamland.ssh_handshake import HandshakeTransport
from dreamland.ssh_host_keys import (
    SSHHostKeyIdentity,
    default_ssh_host_key_path,
    load_or_create_ssh_host_key,
)
from dreamland.ssh_metadata import initial_ssh_connection_metadata
from dreamland.ssh_sessions import ExecSession as _ExecSession
from dreamland.ssh_sessions import ShellSession as _ShellSession
from dreamland.ssh_streaming import (
    LineBufferedCommandWriter,
    execute_backend_stream,
)
from dreamland.ssh_types import CommandBackend

try:
    import paramiko
except ImportError:  # pragma: no cover - exercised only without dependency.
    paramiko = None


LOGGER = logging.getLogger("dreamland.ssh_server")
LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
_PARAMIKO_LOGGER_NAME = "dreamland.ssh_server.paramiko.transport"
_PARAMIKO_DEFAULT_LOGGER_NAME = "paramiko.transport"
_ParamikoServerBase = paramiko.ServerInterface if paramiko is not None else object
_LineBufferedCommandWriter = LineBufferedCommandWriter
_execute_backend_stream = execute_backend_stream


@dataclass(slots=True)
class SSHServerConfig:
    host: str = "127.0.0.1"
    port: int = 2222
    profile: ServerProfile = field(default_factory=server_profile)
    host_key_path: Path = field(default_factory=default_ssh_host_key_path)
    socket_timeout_seconds: float = 1.0
    entry_mode: str = "direct"
    # Stopgap password-guessing mitigations pending real SSH failure bans in
    # Phase 3: a fixed delay before every password check, and a per-source-IP
    # cap on concurrently open connections. Neither remembers anything across
    # connections or reconnects.
    password_check_delay_seconds: float = 2.0
    max_connections_per_ip: int = 5

    def __post_init__(self) -> None:
        if self.entry_mode not in {"direct", "lobby"}:
            raise ValueError("SSH entry mode must be direct or lobby")
        if self.password_check_delay_seconds < 0:
            raise ValueError("SSH password check delay must not be negative")
        if self.max_connections_per_ip < 1:
            raise ValueError("SSH connections per IP must be at least one")


class DreamlandSSHServer:
    def __init__(
        self,
        config: SSHServerConfig | None = None,
        backend: CommandBackend | None = None,
        console: DreamlandConsole | None = None,
        *,
        authenticator: SSHAuthenticator,
        lobby: Any | None = None,
    ) -> None:
        if paramiko is None:
            raise RuntimeError(
                "Paramiko is required to run DreamlandSSHServer. "
                "Install the project dependencies first."
            )
        self.config = config or SSHServerConfig()
        if (self.config.entry_mode == "lobby") != (lobby is not None):
            raise ValueError(
                "SSH lobby mode and lobby controller must be configured together"
            )
        self.backend = backend or AgentBackend(self.config.profile)
        self.console = console
        self.authenticator = authenticator
        self.lobby = lobby
        self._host_key_identity = self._load_host_key()
        self._host_key = self._host_key_identity.key
        action = "generated" if self._host_key_identity.generated else "loaded"
        LIFECYCLE_LOGGER.info(
            "SSH host key %s path=%s algorithm=%s fingerprint=%s",
            action,
            self._host_key_identity.path,
            self._host_key_identity.algorithm,
            self._host_key_identity.fingerprint,
        )
        self._server_socket: socket.socket | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._serve_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._client_threads_lock = threading.Lock()
        # Live connection count per source address, guarded by the lock above.
        # Entries are removed at zero so an attacker cannot grow this map.
        self._connections_per_ip: dict[str, int] = {}

    @property
    def bound_port(self) -> int:
        if self._server_socket is None:
            return self.config.port
        return int(self._server_socket.getsockname()[1])

    @property
    def host_key_algorithm(self) -> str:
        return self._host_key_identity.algorithm

    @property
    def host_key_fingerprint(self) -> str:
        return self._host_key_identity.fingerprint

    def serve_forever(self) -> None:
        self._stop_event.clear()
        self._ready_event.clear()
        LIFECYCLE_LOGGER.info(
            "SSH server starting host=%s port=%s",
            self.config.host,
            self.config.port,
        )
        try:
            with self._listening_socket() as server_socket:
                bound_address = server_socket.getsockname()[:2]
                self._server_socket = server_socket
                self._ready_event.set()
                LIFECYCLE_LOGGER.info(
                    "SSH server listening host=%s port=%s",
                    *bound_address,
                )
                while not self._stop_event.is_set():
                    try:
                        client_socket, client_address = server_socket.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        if self._stop_event.is_set():
                            break
                        raise
                    # Reject over-limit sources before any SSH negotiation or
                    # worker thread, so a flood cannot consume those resources.
                    if not self._reserve_connection_slot(client_address):
                        _close_ssh_resource(client_socket)
                        continue
                    thread = threading.Thread(
                        target=self._run_client_handler,
                        args=(client_socket, client_address),
                        daemon=True,
                    )
                    with self._client_threads_lock:
                        self._client_threads.add(thread)
                    thread.start()
        finally:
            self._server_socket = None
            LIFECYCLE_LOGGER.info("SSH server stopped")

    def start_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        self._serve_thread = thread
        return thread

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        LIFECYCLE_LOGGER.info("SSH server stopping")
        self.request_stop()
        if self._serve_thread is not None:
            self._serve_thread.join(timeout=5)
        self._join_client_threads(timeout=5)

    def request_stop(self) -> None:
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass

    def _listening_socket(self) -> socket.socket:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.config.host, self.config.port))
        server_socket.listen(100)
        server_socket.settimeout(self.config.socket_timeout_seconds)
        return server_socket

    def _reserve_connection_slot(self, client_address: tuple[str, int]) -> bool:
        """Claim one concurrent-connection slot for this source address."""
        source = client_address[0]
        limit = self.config.max_connections_per_ip
        with self._client_threads_lock:
            active = self._connections_per_ip.get(source, 0)
            if active >= limit:
                LIFECYCLE_LOGGER.info(
                    "SSH connection refused peer=%s active=%d limit=%d",
                    source,
                    active,
                    limit,
                )
                return False
            self._connections_per_ip[source] = active + 1
        return True

    def _release_connection_slot(self, client_address: tuple[str, int]) -> None:
        source = client_address[0]
        with self._client_threads_lock:
            active = self._connections_per_ip.get(source, 0) - 1
            if active > 0:
                self._connections_per_ip[source] = active
            else:
                self._connections_per_ip.pop(source, None)

    def _run_client_handler(
        self,
        client_socket: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            self._handle_client(client_socket, client_address)
        finally:
            current_thread = threading.current_thread()
            with self._client_threads_lock:
                self._client_threads.discard(current_thread)
            self._release_connection_slot(client_address)

    def _join_client_threads(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._client_threads_lock:
                client_threads = list(self._client_threads)
            if not client_threads:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for thread in client_threads:
                if thread is threading.current_thread():
                    continue
                thread.join(timeout=min(remaining, 0.1))

    def _handle_client(
        self, client_socket: socket.socket, client_address: tuple[str, int]
    ) -> None:
        channel = None
        transport = None
        server = None
        started_at = time.monotonic()
        try:
            connection_metadata = initial_ssh_connection_metadata(
                client_socket,
                self.config.profile,
            )
            transport = HandshakeTransport(client_socket)
            transport.set_log_channel(_PARAMIKO_LOGGER_NAME)
            transport.local_version = self.config.profile.server_version
            transport.add_server_key(self._host_key)
            lobby = self.lobby
            server = _ParamikoServer(
                self.backend,
                self.authenticator,
                lobby_mode=lobby is not None,
                password_check_delay_seconds=self.config.password_check_delay_seconds,
            )
            try:
                transport.start_server(server=server)
            except Exception as error:
                transport.report_handshake_failure(
                    error,
                    peer=client_address,
                    connection_id=server.parent_connection_id or uuid.uuid4().hex,
                    started_at=started_at,
                )
                return
            channel = transport.accept(20)
            if channel is None:
                return
            request = server.wait_for_request(20)
            if request == "shell":
                if lobby is not None:
                    lobby.run(
                        channel,
                        server.ssh_username,
                        self.backend,
                        connection_metadata,
                        console=self.console,
                        stop_event=self._stop_event,
                        width=server.width,
                        height=server.height,
                        terminal_type=server.terminal_type,
                        dimensions=server.dimensions,
                        parent_connection_id=server.parent_connection_id,
                    )
                else:
                    _ShellSession(
                        channel,
                        server.username,
                        self.backend,
                        connection_metadata,
                        console=self.console,
                    ).run()
            elif request == "exec" and server.exec_command is not None:
                _ExecSession(
                    channel,
                    server.username,
                    self.backend,
                    server.exec_command,
                    connection_metadata,
                    console=self.console,
                ).run()
        except _client_disconnect_exceptions():
            LOGGER.debug("SSH client disconnected %s:%s", *client_address)
        except Exception:
            LOGGER.exception("Error handling SSH client %s:%s", *client_address)
        finally:
            if server is not None and server.ssh_username is not None:
                LIFECYCLE_LOGGER.info(
                    "SSH lobby connection closed parent=%s",
                    server.parent_connection_id,
                )
            _close_ssh_resource(channel)
            _close_ssh_resource(transport)
            try:
                client_socket.close()
            except OSError:
                pass

    def _load_host_key(self) -> SSHHostKeyIdentity:
        return load_or_create_ssh_host_key(self.config.host_key_path)


def _client_disconnect_exceptions() -> tuple[type[BaseException], ...]:
    exceptions: list[type[BaseException]] = [EOFError, OSError]
    ssh_exception = getattr(paramiko, "SSHException", None)
    if isinstance(ssh_exception, type):
        exceptions.append(ssh_exception)
    return tuple(exceptions)


def _close_ssh_resource(resource: object | None) -> None:
    if resource is None:
        return
    try:
        close = getattr(resource, "close", None)
        if close is not None:
            close()
    except _client_disconnect_exceptions():
        LOGGER.debug("SSH client disconnected during cleanup")


class _ParamikoDisconnectNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not message.startswith("Socket exception: Connection reset by peer")


class _ParamikoSensitiveAuthFilter(logging.Filter):
    """Block Paramiko's raw per-attempt auth line.

    Its username is attacker-controlled and unsanitized; logging or displaying
    it verbatim would allow log/terminal injection. See decision 0113.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Auth request (type=")


_PARAMIKO_DISCONNECT_NOISE_FILTER = _ParamikoDisconnectNoiseFilter()
_PARAMIKO_SENSITIVE_AUTH_FILTER = _ParamikoSensitiveAuthFilter()
logging.getLogger(_PARAMIKO_LOGGER_NAME).addFilter(_PARAMIKO_DISCONNECT_NOISE_FILTER)
logging.getLogger(_PARAMIKO_DEFAULT_LOGGER_NAME).addFilter(
    _PARAMIKO_DISCONNECT_NOISE_FILTER
)
logging.getLogger(_PARAMIKO_LOGGER_NAME).addFilter(_PARAMIKO_SENSITIVE_AUTH_FILTER)
logging.getLogger(_PARAMIKO_DEFAULT_LOGGER_NAME).addFilter(
    _PARAMIKO_SENSITIVE_AUTH_FILTER
)


class _ParamikoServer(_ParamikoServerBase):  # type: ignore[misc, valid-type]
    def __init__(
        self,
        backend: CommandBackend,
        authenticator: SSHAuthenticator,
        *,
        lobby_mode: bool = False,
        password_check_delay_seconds: float = 0.0,
    ) -> None:
        self.backend = backend
        self.authenticator = authenticator
        self.lobby_mode = lobby_mode
        # Defaults to no delay so direct unit construction stays fast; the
        # transport passes the configured deployment value.
        self.password_check_delay_seconds = password_check_delay_seconds
        self.pty_requested = False
        self.width = 80
        self.height = 24
        self.terminal_type = ""
        self._terminal_lock = threading.Lock()
        self.username = "guest"
        # Transport audit identity never enters simulated state or model context.
        self.ssh_username: str | None = None
        self.parent_connection_id = uuid.uuid4().hex if lobby_mode else None
        self.exec_command: str | None = None
        self._session_channel_requested = False
        self._request_lock = threading.Lock()
        self._request_event = threading.Event()
        self._request_type: str | None = None

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind != "session" or self._session_channel_requested:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self._session_channel_requested = True
        return paramiko.OPEN_SUCCEEDED

    def check_channel_direct_tcpip_request(
        self,
        chanid: int,
        origin: tuple[str, int],
        destination: tuple[str, int],
    ) -> int:
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_port_forward_request(self, address: str, port: int) -> bool:
        return False

    def cancel_port_forward_request(self, address: str, port: int) -> None:
        return None

    def check_global_request(self, kind: str, msg) -> bool:
        return False

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_none(self, username: str) -> int:
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username: str, password: str | bytes) -> int:
        # Delay every attempt before checking it, so the cost does not depend
        # on whether the password was correct and cannot be used as an oracle.
        if self.password_check_delay_seconds > 0:
            time.sleep(self.password_check_delay_seconds)
        if not self.authenticator.authenticate_password(username, password):
            return paramiko.AUTH_FAILED
        if self.lobby_mode:
            self.ssh_username = username
            LIFECYCLE_LOGGER.info(
                "SSH lobby authenticated parent=%s ssh_username=%r",
                self.parent_connection_id,
                username,
            )
        self.username = _safe_username(username)
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username: str, key) -> int:
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username: str, submethods: str) -> int:
        return paramiko.AUTH_FAILED

    def check_auth_interactive_response(self, responses: list[str]) -> int:
        return paramiko.AUTH_FAILED

    def enable_auth_gssapi(self) -> bool:
        return False

    def check_auth_gssapi_keyex(
        self,
        username: str,
        gss_authenticated: int = 2,
        cc_file=None,
    ) -> int:
        return paramiko.AUTH_FAILED

    def check_auth_gssapi_with_mic(
        self,
        username: str,
        gss_authenticated: int = 2,
        cc_file=None,
    ) -> int:
        return paramiko.AUTH_FAILED

    def check_channel_subsystem_request(self, channel, name: str) -> bool:
        self._begin_request("rejected")
        return False

    def check_channel_forward_agent_request(self, channel) -> bool:
        return False

    def check_channel_x11_request(
        self,
        channel,
        single_connection: bool,
        auth_protocol: str,
        auth_cookie: bytes,
        screen_number: int,
    ) -> bool:
        return False

    def check_channel_env_request(self, channel, name: bytes, value: bytes) -> bool:
        return False

    def check_channel_pty_request(
        self,
        channel,
        term: bytes,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
        modes: bytes,
    ) -> bool:
        if self.lobby_mode and (self.pty_requested or self._request_type is not None):
            return False
        self.pty_requested = True
        self.terminal_type = normalize_terminal_type(term)
        self._update_dimensions(width, height)
        return True

    def check_channel_window_change_request(
        self,
        channel,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
    ) -> bool:
        if not self.pty_requested:
            return False
        self._update_dimensions(width, height)
        return True

    def dimensions(self) -> tuple[int, int]:
        """Snapshot the client viewport for the lobby's next render boundary."""
        with self._terminal_lock:
            return self.width, self.height

    def _update_dimensions(self, width: int, height: int) -> None:
        # SSH zero dimensions are unspecified. Preserve the preceding value;
        # positive tiny viewports must reach the renderer without a layout floor.
        with self._terminal_lock:
            if width > 0:
                self.width = min(width, 240)
            if height > 0:
                self.height = min(height, 100)

    def check_channel_shell_request(self, channel) -> bool:
        if self.lobby_mode and not self.pty_requested:
            return self._reject_lobby_request(channel)
        return self._begin_request("shell")

    def check_channel_exec_request(self, channel, command: bytes) -> bool:
        if self.lobby_mode:
            return self._reject_lobby_request(channel)
        decoded_command = command.decode("utf-8", errors="replace")
        return self._begin_request("exec", exec_command=decoded_command)

    def _reject_lobby_request(self, channel) -> bool:
        with self._request_lock:
            if self._request_type is not None:
                return False
            self._request_type = "rejected"
            try:
                channel.send_stderr(
                    b"DREAMLAND requires an interactive PTY shell. "
                    b"Reconnect with: ssh -t USER@HOST\r\n"
                )
                channel.send_exit_status(126)
            except _client_disconnect_exceptions():
                pass
            finally:
                self._request_event.set()
        return False

    def _begin_request(
        self,
        request_type: str,
        *,
        exec_command: str | None = None,
    ) -> bool:
        with self._request_lock:
            if self._request_type is not None:
                return False
            self.exec_command = exec_command
            self._request_type = request_type
            self._request_event.set()
            return True

    def wait_for_request(self, timeout: float) -> str | None:
        if not self._request_event.wait(timeout):
            return None
        return self._request_type


def _safe_username(username: str) -> str:
    cleaned = "".join(ch for ch in username if ch.isalnum() or ch in {"_", "-"})
    return cleaned[:64] or "guest"
