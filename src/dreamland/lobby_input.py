"""Bounded terminal input for deterministic SSH lobby phases.

Only bytes already received are discarded at a boundary. Known incomplete keys
and CRLF are completed on subsequent reads; this is not an automation detector.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, overload


class LobbyClosed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BoundaryFilter:
    """Track a discarded key without replaying its continuation as new input."""

    def __init__(self) -> None:
        self.sequence = ""
        self.utf8_remaining = 0
        self.after_cr = False

    def consume(self, byte: int) -> None:
        if self.sequence == "osc":
            if byte == 7:
                self.sequence = ""
            elif byte == 27:
                self.sequence = "osc_escape"
            return
        if self.sequence == "osc_escape":
            self.sequence = "" if byte == 92 else "osc"
            return
        if self.sequence == "escape":
            self.sequence = {91: "csi", 93: "osc", 79: "ss3"}.get(byte, "")
            return
        if self.sequence in {"csi", "ss3"}:
            if 64 <= byte <= 126:
                self.sequence = ""
            return
        if self.utf8_remaining:
            if 128 <= byte <= 191:
                self.utf8_remaining -= 1
                return
            self.utf8_remaining = 0
        self.after_cr = byte == 13
        if byte == 27:
            self.sequence = "escape"
        elif 194 <= byte <= 223:
            self.utf8_remaining = 1
        elif 224 <= byte <= 239:
            self.utf8_remaining = 2
        elif 240 <= byte <= 244:
            self.utf8_remaining = 3

    def suppress(self, byte: int) -> bool:
        if self.sequence or self.utf8_remaining:
            self.consume(byte)
            return True
        if self.after_cr:
            self.after_cr = False
            if byte == 10:
                return True
        return False


class LobbyInput:
    def __init__(
        self,
        channel: Any,
        *,
        stop_event: threading.Event,
        idle_timeout_seconds: float = 300,
        max_input_bytes: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channel = channel
        self.stop_event = stop_event
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.boundary = BoundaryFilter()
        self._buffer = bytearray()
        self._suppressed_bytes = 0
        self._monotonic = monotonic
        self._last_activity = self.monotonic()
        self.channel.settimeout(0.1)

    def send(self, text: str) -> None:
        self.channel.sendall(text.replace("\n", "\r\n").encode("utf-8"))

    def monotonic(self) -> float:
        return self._monotonic()

    def resume_idle(self) -> None:
        """Start a fresh lobby idle period after returning from an active visit."""
        self._last_activity = self.monotonic()
        self.check()

    def check(self) -> None:
        """Check lifecycle and input inactivity without counting output as activity."""
        if self.stop_event.is_set():
            raise LobbyClosed("shutdown")
        if self.channel.closed:
            raise LobbyClosed("disconnect")
        if self.monotonic() - self._last_activity >= self.idle_timeout_seconds:
            raise LobbyClosed("timeout")

    @overload
    def _byte(self) -> int:
        pass

    @overload
    def _byte(self, deadline: float) -> int | None:
        pass

    def _byte(self, deadline: float | None = None) -> int | None:
        try:
            while True:
                self.check()
                if self._buffer:
                    byte = self._buffer.pop(0)
                    if byte in {3, 4}:
                        raise LobbyClosed("quit")
                    if not self.boundary.suppress(byte):
                        self._suppressed_bytes = 0
                        return byte
                    self._suppressed_bytes += 1
                    if self._suppressed_bytes > self.max_input_bytes:
                        raise LobbyClosed("input_limit")
                    continue
                now = self.monotonic()
                timeout = min(
                    0.1, self._last_activity + self.idle_timeout_seconds - now
                )
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0 and not self.channel.recv_ready():
                        return None
                    timeout = min(timeout, max(0.0, remaining))
                self.channel.settimeout(max(0.0, timeout))
                try:
                    data = self.channel.recv(1024)
                except TimeoutError:
                    self.check()
                    if deadline is not None and self.monotonic() >= deadline:
                        return None
                    continue
                if not data:
                    raise LobbyClosed("disconnect")
                self._last_activity = self.monotonic()
                self._buffer.extend(data)
        finally:
            self.channel.settimeout(0.1)

    def poll_key(self, deadline: float) -> int | None:
        """Read one eligible key, waiting only until an absolute monotonic deadline.

        Already available input is checked even at the deadline. The caller must
        discard a returned skip key and its buffered tail at the phase boundary.
        """
        byte = self._byte(deadline)
        if byte in {ord("q"), ord("Q")}:
            raise LobbyClosed("quit")
        return byte

    def discard_boundary(self, consumed_tail: bytes = b"") -> None:
        """Discard a finite snapshot of available bytes, retaining partial keys."""
        self.check()
        for byte in consumed_tail + bytes(self._buffer):
            self.boundary.consume(byte)
        self._buffer.clear()
        discarded = 0
        while self.channel.recv_ready():
            self.check()
            try:
                data = self.channel.recv(1024)
            except TimeoutError:
                break
            if not data:
                raise LobbyClosed("disconnect")
            self._last_activity = self.monotonic()
            discarded += len(data)
            if discarded > 65536:
                raise LobbyClosed("input_limit")
            for byte in data:
                self.boundary.consume(byte)
        self.check()

    def any_key(self) -> None:
        byte = self._byte()
        if byte in {3, 4, ord("q"), ord("Q")}:
            raise LobbyClosed("quit")
        self.discard_boundary(bytes([byte]))
        self.send("\n")

    def line(self) -> str | None:
        value = bytearray()
        invalid = False
        count = 0
        controls = BoundaryFilter()
        while True:
            byte = self._byte()
            count += 1
            if count > self.max_input_bytes:
                raise LobbyClosed("input_limit")
            if byte in {3, 4}:
                raise LobbyClosed("quit")
            if controls.sequence or controls.utf8_remaining:
                controls.consume(byte)
                invalid = True
                continue
            if byte in {10, 13}:
                self.discard_boundary(bytes([byte]))
                self.send("\n")
                return None if invalid else value.decode("ascii").strip(" \t").lower()
            if byte in {8, 127}:
                if value:
                    value.pop()
                    self.send("\b \b")
            elif byte == 9 or 32 <= byte <= 126:
                value.append(byte)
                self.send(" " if byte == 9 else chr(byte))
            else:
                invalid = True
                controls.consume(byte)

    def shell_channel(self) -> BoundaryChannel:
        return BoundaryChannel(self)


class BoundaryChannel:
    """Complete a selection's partial key before the new shell sees input."""

    def __init__(self, reader: LobbyInput) -> None:
        self.reader = reader

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader.channel, name)

    def recv(self, count: int) -> bytes:
        while True:
            data = self.reader.channel.recv(count)
            if not data:
                return b""
            filtered_bytes = bytearray()
            for byte in data:
                if byte in {3, 4}:
                    self.reader.boundary = BoundaryFilter()
                    self.reader._suppressed_bytes = 0
                    filtered_bytes.append(byte)
                    continue
                if self.reader.boundary.suppress(byte):
                    self.reader._suppressed_bytes += 1
                    if self.reader._suppressed_bytes > self.reader.max_input_bytes:
                        # EOF to the visit; the parent is also closed to avoid
                        # reframing an overlong key as another lobby response.
                        self.reader.channel.close()
                        return b""
                else:
                    self.reader._suppressed_bytes = 0
                    filtered_bytes.append(byte)
            filtered = bytes(filtered_bytes)
            if filtered:
                return filtered
