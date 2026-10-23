"""Authentication policy for DREAMLAND's native SSH listener."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Protocol

SIMPLE_LOGIN_SUFFIX = "_simple"
FAST_LOGIN_SUFFIX = "_fast"


def is_fast_login(username: str) -> bool:
    """Identify the demo suffix on an already-authenticated lobby login."""
    return username.removesuffix(SIMPLE_LOGIN_SUFFIX).endswith(FAST_LOGIN_SUFFIX)


class SSHAuthenticator(Protocol):
    """Validate one SSH password attempt without retaining the attempt."""

    def authenticate_password(
        self,
        username: str,
        password: str | bytes,
    ) -> bool:
        """Return whether a non-empty username supplied the configured password."""

        raise NotImplementedError


@dataclass(frozen=True, slots=True, init=False)
class SharedPasswordAuthenticator:
    """Accept one static password, from a configured or an arbitrary username.

    A `required_username` closes the username field as a wildcard: without it,
    the username is only checked for non-emptiness, which lets a client vary it
    freely, defeats any detection that expects a stable value, and matches
    generic scanners that never guess a project-specific username. It provides
    no secrecy of its own; the required value must be documented for testers to
    connect and must not be treated as a second credential.

    Lobby composition may enable `allow_simple_login` to accept one appended
    `_simple` suffix with the same password. Direct entry keeps exact matching.
    With a fixed lobby username, `fast_password` enables `_fast` and optionally
    `_fast_simple` aliases using a distinct password. Both digest comparisons
    run for every attempt, including when demo admission is disabled.
    """

    _password_digest: bytes = field(repr=False)
    _fast_password_digest: bytes | None = field(repr=False)
    required_username: str | None = None
    allow_simple_login: bool = False

    def __init__(
        self,
        password: str,
        *,
        required_username: str | None = None,
        allow_simple_login: bool = False,
        fast_password: str | None = None,
    ) -> None:
        if not password or not password.strip():
            raise ValueError("DREAMLAND_SSH_PASSWORD must be set and non-empty.")
        normal_digest = _password_digest(password)
        fast_digest = None
        if fast_password is not None:
            if not fast_password.strip():
                raise ValueError("DREAMLAND_SSH_FAST_PASSWORD must not be blank.")
            if required_username is None:
                raise ValueError("Fast login requires a fixed lobby username.")
            fast_digest = _password_digest(fast_password)
            if hmac.compare_digest(normal_digest, fast_digest):
                raise ValueError(
                    "DREAMLAND_SSH_FAST_PASSWORD must differ from DREAMLAND_SSH_PASSWORD."
                )
        object.__setattr__(self, "_password_digest", normal_digest)
        object.__setattr__(self, "_fast_password_digest", fast_digest)
        object.__setattr__(self, "required_username", required_username)
        object.__setattr__(self, "allow_simple_login", allow_simple_login)

    def authenticate_password(
        self,
        username: str,
        password: str | bytes,
    ) -> bool:
        base_username = (
            username.removesuffix(SIMPLE_LOGIN_SUFFIX)
            if self.allow_simple_login
            else username
        )
        if self.required_username is not None:
            # Preserve an exact configured name, including one ending in the
            # suffix. Only one appended suffix is accepted as an alias.
            username_matches = (
                username == self.required_username
                or base_username == self.required_username
            )
        else:
            username_matches = bool(base_username)
        # Always hash the supplied password, even on a username mismatch, so
        # this method's own cost does not vary with which check failed. The
        # caller applies a uniform delay before this method runs at all; see
        # decision 0113 and ssh_transport.check_auth_password.
        supplied_digest = _password_digest(password)
        password_matches = hmac.compare_digest(
            self._password_digest,
            supplied_digest,
        )
        fast_password_matches = hmac.compare_digest(
            self._fast_password_digest or self._password_digest,
            supplied_digest,
        )
        fast_username_matches = (
            self._fast_password_digest is not None
            and base_username == f"{self.required_username}{FAST_LOGIN_SUFFIX}"
        )
        return (username_matches and password_matches) or (
            fast_username_matches and fast_password_matches
        )


def _password_digest(password: str | bytes) -> bytes:
    if isinstance(password, str):
        value = password.encode("utf-8", errors="surrogatepass")
    else:
        value = password
    return hashlib.sha256(value).digest()
