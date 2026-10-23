"""Connection-local diagnostics for Paramiko's initial server handshake."""

from __future__ import annotations

import errno
import logging
import sys
import time
from dataclasses import dataclass

try:
    import paramiko
except ImportError:  # pragma: no cover - transport requires the optional dependency.
    paramiko = None


LOGGER = logging.getLogger("dreamland.lifecycle")
_TransportBase = paramiko.Transport if paramiko is not None else object
_PEER_ERRORS = {
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.EPIPE,
    errno.ENOTCONN,
}


@dataclass(frozen=True, slots=True)
class HandshakeFailure:
    reason: str
    level: int = logging.INFO


def classify_handshake_failure(
    error: BaseException, phase: str
) -> HandshakeFailure | None:
    """Allowlist expected failures; an unknown exception must stay an error."""
    if phase not in {"banner", "key_exchange"}:
        return None
    if isinstance(error, EOFError):
        return HandshakeFailure("peer_closed")
    if isinstance(error, TimeoutError):
        return HandshakeFailure("timeout")
    if isinstance(error, OSError) and error.errno in _PEER_ERRORS:
        return HandshakeFailure("peer_reset")
    if paramiko is None:
        return None
    if isinstance(error, paramiko.ssh_exception.IncompatiblePeer):
        return HandshakeFailure("incompatible_peer", logging.WARNING)
    if phase != "banner" or type(error) is not paramiko.SSHException:
        return None
    message = str(error)
    # Paramiko wraps readline failures. Do not mistake a wrapped internal bug
    # (e.g. ValueError or EBADF) for a harmless scan just because of its text.
    if message.startswith("Error reading SSH protocol banner"):
        cause = error.__cause__ or error.__context__
        if isinstance(cause, UnicodeDecodeError):
            return HandshakeFailure("invalid_banner")
        if isinstance(cause, (EOFError, OSError)):
            return classify_handshake_failure(cause, phase)
        return None
    if message == "Invalid SSH banner" or message.startswith(
        'Indecipherable protocol version "'
    ):
        return HandshakeFailure("invalid_banner")
    return None


class HandshakeTransport(_TransportBase):
    """Keep known handshake noise out of Paramiko's multiline ERROR output.

    The two private hooks are intentionally small: Paramiko still owns every
    byte, timeout, exception and cleanup operation. Revalidate them on upgrades.
    No shared logger level/filter or per-connection logger is installed.
    """

    def __init__(self, *args, **kwargs):
        self.handshake_phase = "banner"
        super().__init__(*args, **kwargs)

    def _check_banner(self):
        super()._check_banner()
        # remote_version alone is insufficient: Paramiko sets it *before*
        # checking the version and structure of the banner.
        self.handshake_phase = "key_exchange"

    def _log(self, level, msg, *args):
        if self.handshake_phase == "banner" and isinstance(msg, str):
            if msg.startswith(("Banner: ", "Remote version/idstring: ")):
                return  # Never echo untrusted preamble/banner bytes.
            if msg.startswith("Connected (version "):
                msg = "Connected (SSH version accepted)"
        error = sys.exc_info()[1]
        if (
            not self.initial_kex_done
            and error is not None
            and level == logging.ERROR
            and classify_handshake_failure(error, self.handshake_phase) is not None
            and (
                isinstance(msg, list)  # Paramiko's util.tb_strings().
                or (
                    isinstance(msg, str)
                    and msg.startswith(("Exception (server): ", "Socket exception: "))
                )
            )
        ):
            return
        super()._log(level, msg, *args)

    def report_handshake_failure(
        self,
        error: Exception,
        *,
        peer: tuple[str, int],
        connection_id: str,
        started_at: float,
    ) -> None:
        failure = classify_handshake_failure(error, self.handshake_phase)
        reason = failure.reason if failure is not None else "unexpected_error"
        level = failure.level if failure is not None else logging.ERROR
        fields = {
            "event": "ssh_handshake_failed",
            "connection_id": connection_id,
            "peer_ip": peer[0],
            "peer_port": peer[1],
            "phase": self.handshake_phase,
            "reason": reason,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        }
        LOGGER.log(
            level,
            "event=%(event)s connection_id=%(connection_id)s "
            "peer_ip=%(peer_ip)s peer_port=%(peer_port)s "
            "phase=%(phase)s reason=%(reason)s elapsed_ms=%(elapsed_ms)s",
            fields,
            extra=fields,
            exc_info=(type(error), error, error.__traceback__)
            if failure is None
            else None,
        )
