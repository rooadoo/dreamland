"""Explicit, bounded private transport for the deployment handoff foundation.

This listener has no Product, administrative, provider, or world authority. Its
authenticated readiness response deliberately denies domain admission until a
later supported deployment composition supplies those services.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import signal
import socket
import socketserver
import threading
import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from types import FrameType
from typing import Any

from dreamland.deployment.companion import CompanionError, load_service

LOGGER = logging.getLogger("dreamland.deployment.companion")
MAX_CONNECTIONS = 8
MAX_HEADER_BYTES = 16_384
MAX_LINE_BYTES = 4_096
MAX_HEADERS = 32
READ_TIMEOUT_SECONDS = 2.0
CONNECTION_DEADLINE_SECONDS = 5.0
_TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ERRORS = {
    "unauthorized": (HTTPStatus.UNAUTHORIZED, "Private service authority is required."),
    "invalid_request": (HTTPStatus.BAD_REQUEST, "The request is invalid."),
    "not_found": (HTTPStatus.NOT_FOUND, "The operation is unavailable."),
    "method_not_allowed": (HTTPStatus.METHOD_NOT_ALLOWED, "The method is unavailable."),
}


class CompanionHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One independently authenticated role, with bounded connection workers."""

    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(
        self,
        address: tuple[str, int],
        *,
        descriptor: Mapping[str, Any],
        bearer: str,
    ) -> None:
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.role = str(descriptor["role"])
        self.service_epoch = str(uuid.uuid4())
        # Keep a detached immutable serialization, never a caller-owned mapping.
        self._descriptor_bytes = json.dumps(
            descriptor, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self._authorization = ("Bearer " + bearer).encode("ascii")
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._closing = threading.Event()
        self._maximum_observed_connections = 0
        super().__init__(address, _CompanionHandler)

    @property
    def active_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    @property
    def maximum_observed_connections(self) -> int:
        with self._connections_lock:
            return self._maximum_observed_connections

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("Companion HTTP requires a TCP connection.")
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            LOGGER.debug("companion_connection_capacity_denied role=%s", self.role)
            return
        with self._connections_lock:
            if self._closing.is_set():
                self._slots.release()
                self.shutdown_request(request)
                return
            self._connections.add(request)
            self._maximum_observed_connections = max(
                self._maximum_observed_connections, len(self._connections)
            )
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("Companion HTTP requires a TCP connection.")
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(request)
        self._slots.release()

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        # Exceptions and request material can contain private authority. Log only
        # a fixed lifecycle event, without a traceback or request representation.
        LOGGER.error("companion_connection_failed role=%s", self.role)

    def server_close(self) -> None:
        self._closing.set()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().server_close()

    def response(self, path: bytes) -> tuple[HTTPStatus, dict[str, Any]] | None:
        common = {"role": self.role, "service_epoch": self.service_epoch}
        if path == b"/deployment/v1/identity":
            return HTTPStatus.OK, {
                "handoff": json.loads(self._descriptor_bytes),
                "service_epoch": self.service_epoch,
            }
        if path == b"/deployment/v1/health":
            return HTTPStatus.OK, {"status": "healthy", **common}
        if path == b"/deployment/v1/readiness":
            descriptor = json.loads(self._descriptor_bytes)
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "transport_ready": True,
                "admission_ready": False,
                "reason": "domain_services_unavailable",
                **common,
                "capabilities": descriptor["capabilities"],
            }
        return None


class _CompanionHandler(socketserver.BaseRequestHandler):
    server: CompanionHTTPServer
    request: socket.socket

    def handle(self) -> None:
        self._deadline = time.monotonic() + CONNECTION_DEADLINE_SECONDS
        self._head = False
        try:
            header = self._read_header()
            if header is None:
                return
            lines = header.split(b"\r\n")
            self._head = lines[0].startswith(b"HEAD ")
            # Even invalid methods, routes and framing must pass the exact
            # bearer gate before any service information is available.
            authorization = [
                line.partition(b":")[2].strip(b" \t")
                for line in lines[1:]
                if line.partition(b":")[0].lower() == b"authorization"
            ]
            supplied = authorization[0] if len(authorization) == 1 else b""
            if not hmac.compare_digest(supplied, self.server._authorization):
                LOGGER.warning(
                    "companion_authentication_denied role=%s", self.server.role
                )
                self._error("unauthorized")
                return
            parsed = self._parse(lines)
            if parsed is None:
                self._error("invalid_request")
                return
            method, path = parsed
            self._head = method == b"HEAD"
            if method != b"GET":
                self._error("method_not_allowed")
                return
            response = self.server.response(path)
            if response is None:
                self._error("not_found")
                return
            status, body = response
            self._send(status, body)
        except (OSError, TimeoutError):
            LOGGER.debug("companion_connection_ended role=%s", self.server.role)

    def _read_header(self) -> bytes | None:
        data = bytearray()
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.request.settimeout(min(READ_TIMEOUT_SECONDS, remaining))
            chunk = self.request.recv(min(4_096, MAX_HEADER_BYTES + 1 - len(data)))
            if not chunk:
                return None
            data.extend(chunk)
            end = data.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > MAX_HEADER_BYTES:
                    self._error("unauthorized")
                    return None
                return bytes(data[:end])
            if len(data) > MAX_HEADER_BYTES:
                # An incomplete/overlong envelope cannot authenticate safely.
                self._error("unauthorized")
                return None

    def _parse(self, lines: list[bytes]) -> tuple[bytes, bytes] | None:
        if len(lines) - 1 > MAX_HEADERS or any(
            len(line) > MAX_LINE_BYTES for line in lines
        ):
            return None
        request = lines[0].split(b" ")
        if len(request) != 3:
            return None
        method, path, protocol = request
        if (
            not _TOKEN.fullmatch(method)
            or protocol not in {b"HTTP/1.0", b"HTTP/1.1"}
            or not path.startswith(b"/")
            or path.startswith(b"//")
            or any(value <= 32 or value >= 127 for value in path)
            or any(marker in path for marker in (b"?", b"#", b"%", b"\\"))
        ):
            return None
        headers: dict[bytes, bytes] = {}
        for line in lines[1:]:
            name, colon, value = line.partition(b":")
            name = name.lower()
            if (
                not colon
                or not _TOKEN.fullmatch(name)
                or name in headers
                or any(
                    character < 32 and character != 9 or character >= 127
                    for character in value
                )
            ):
                return None
            headers[name] = value.strip(b" \t")
        if protocol == b"HTTP/1.1" and not headers.get(b"host"):
            return None
        if (
            b"transfer-encoding" in headers
            or b"expect" in headers
            or b"upgrade" in headers
            or headers.get(b"content-length", b"0") != b"0"
        ):
            return None
        return method, path

    def _error(self, code: str) -> None:
        status, message = _ERRORS[code]
        self._send(status, {"error": {"code": code, "message": message}})

    def _send(self, status: HTTPStatus, document: Mapping[str, Any]) -> None:
        body = json.dumps(
            document, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        headers = [
            f"HTTP/1.1 {status.value} {status.phrase}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
        ]
        if status == HTTPStatus.UNAUTHORIZED:
            headers.append("WWW-Authenticate: Bearer")
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            headers.append("Allow: GET")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return
        self.request.settimeout(min(READ_TIMEOUT_SECONDS, remaining))
        self.request.sendall(
            ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
            + (b"" if self._head else body)
        )


def build_server(
    root: Path,
    *,
    role: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    container_network: bool = False,
) -> CompanionHTTPServer:
    """Validate an explicit private binding and load only its role's files."""
    if not isinstance(host, str):
        raise CompanionError("Companion bind host must be numeric loopback.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise CompanionError("Companion bind host must be numeric loopback.") from None
    if not isinstance(container_network, bool) or not (
        address.is_loopback or (host == "0.0.0.0" and container_network)
    ):
        raise CompanionError("Companion binding requires explicit private topology.")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise CompanionError("Companion port must be between 0 and 65535.")
    descriptor, bearer = load_service(root, role=role)
    try:
        return CompanionHTTPServer((host, port), descriptor=descriptor, bearer=bearer)
    except OSError:
        raise CompanionError("Companion listener could not bind.") from None


def serve(server: CompanionHTTPServer) -> int:
    """Supervise the private listener with bounded SIGINT/SIGTERM cleanup."""
    stopped = threading.Event()
    failed = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stopped.set()

    def run() -> None:
        try:
            server.serve_forever(poll_interval=0.1)
        except Exception:
            failed.set()
            LOGGER.error("companion_listener_failed role=%s", server.role)
        finally:
            stopped.set()

    prior = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    thread = threading.Thread(target=run, name=f"dreamland-companion-{server.role}")
    try:
        for signum in prior:
            signal.signal(signum, request_stop)
        thread.start()
        LOGGER.info(
            "companion_listener_started role=%s service_epoch=%s",
            server.role,
            server.service_epoch,
        )
        stopped.wait()
    finally:
        if thread.ident is not None:
            server.shutdown()
        server.server_close()
        if thread.ident is not None:
            thread.join(timeout=1)
        for signum, handler in prior.items():
            signal.signal(signum, handler)
        LOGGER.info("companion_listener_stopped role=%s", server.role)
    return 1 if failed.is_set() else 0
