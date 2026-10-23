"""Bounded framing shared by the private operator console and its client."""

from __future__ import annotations

import json
import select
import socket
import struct
import time
from typing import Any

PROTOCOL_VERSION = 1
REQUEST_MAX_BYTES = 64 * 1024
RESPONSE_MAX_BYTES = 4 * 1024 * 1024
FRAME_TIMEOUT_SECONDS = 5.0

_ERROR_MESSAGES = {
    "invalid_frame": "Invalid console message framing.",
    "message_too_large": "Console message exceeds the size limit.",
    "frame_timeout": "Timed out transferring a console message.",
    "connection_closed": "Console connection closed during a message.",
    "invalid_message": "Console messages must be valid JSON objects.",
}


class ConsoleProtocolError(Exception):
    """A safe protocol failure, without input bytes or arbitrary exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


def send_message(
    connection: socket.socket,
    message: dict[str, Any],
    *,
    max_bytes: int = RESPONSE_MAX_BYTES,
    timeout: float = FRAME_TIMEOUT_SECONDS,
) -> None:
    """Send one length-prefixed JSON object within a total transfer deadline."""

    try:
        body = json.dumps(
            message, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ConsoleProtocolError("invalid_message") from None
    if not isinstance(message, dict):
        raise ConsoleProtocolError("invalid_message")
    if not body or len(body) > max_bytes:
        raise ConsoleProtocolError("message_too_large")
    frame = memoryview(struct.pack("!I", len(body)) + body)
    deadline = time.monotonic() + timeout
    try:
        while frame:
            _wait_ready(connection, write=True, deadline=deadline)
            try:
                sent = connection.send(frame[:65536], socket.MSG_DONTWAIT)
            except BlockingIOError:
                continue
            if sent == 0:
                raise ConsoleProtocolError("connection_closed")
            frame = frame[sent:]
    except TimeoutError:
        raise ConsoleProtocolError("frame_timeout") from None
    except OSError:
        raise ConsoleProtocolError("connection_closed") from None


def receive_message(
    connection: socket.socket,
    *,
    max_bytes: int = REQUEST_MAX_BYTES,
    frame_timeout: float = FRAME_TIMEOUT_SECONDS,
    idle_timeout: float | None = None,
) -> dict[str, Any] | None:
    """Read a message; only the wait for its first byte may remain unbounded.

    A clean EOF before a frame returns ``None``. Once any header byte arrives,
    the whole remaining header and body share one finite deadline. This keeps
    idle operator prompts usable without admitting partial-frame slow readers.
    """

    try:
        first = _receive_chunk(
            connection,
            1,
            None if idle_timeout is None else time.monotonic() + idle_timeout,
        )
        if not first:
            return None
        deadline = time.monotonic() + frame_timeout
        header = first + _receive_exact(connection, 3, deadline)
        (length,) = struct.unpack("!I", header)
        if length == 0:
            raise ConsoleProtocolError("invalid_frame")
        if length > max_bytes:
            raise ConsoleProtocolError("message_too_large")
        body = _receive_exact(connection, length, deadline)
    except TimeoutError:
        raise ConsoleProtocolError("frame_timeout") from None
    except OSError:
        raise ConsoleProtocolError("connection_closed") from None
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ConsoleProtocolError("invalid_message") from None
    if not isinstance(decoded, dict):
        raise ConsoleProtocolError("invalid_message")
    return decoded


def _receive_exact(connection: socket.socket, length: int, deadline: float) -> bytes:
    received = bytearray()
    while len(received) < length:
        chunk = _receive_chunk(connection, min(length - len(received), 65536), deadline)
        if not chunk:
            raise ConsoleProtocolError("connection_closed")
        received.extend(chunk)
    return bytes(received)


def _receive_chunk(
    connection: socket.socket, length: int, deadline: float | None
) -> bytes:
    while True:
        _wait_ready(connection, write=False, deadline=deadline)
        try:
            return connection.recv(length, socket.MSG_DONTWAIT)
        except BlockingIOError:
            continue


def _wait_ready(
    connection: socket.socket, *, write: bool, deadline: float | None
) -> None:
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise ConsoleProtocolError("frame_timeout")
    try:
        readable, writable, _ = select.select(
            [] if write else [connection], [connection] if write else [], [], remaining
        )
    except (OSError, ValueError):
        raise ConsoleProtocolError("connection_closed") from None
    if not readable and not writable:
        raise ConsoleProtocolError("frame_timeout")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite number")
