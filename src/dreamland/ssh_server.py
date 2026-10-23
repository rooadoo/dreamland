"""Stable public facade for the SSH transport implementation."""

from __future__ import annotations

from typing import Any

from dreamland import ssh_transport as _transport
from dreamland.ssh_auth import SharedPasswordAuthenticator, SSHAuthenticator
from dreamland.ssh_session_state import (
    apply_nested_exit_fallback as _apply_nested_exit_fallback,
)
from dreamland.ssh_session_state import (
    pop_nested_shell_for_transport_exit as _pop_nested_shell_for_transport_exit,
)
from dreamland.ssh_session_state import (
    should_close_shell_session as _should_close_shell_session,
)
from dreamland.ssh_streaming import (
    LineBufferedCommandWriter as _LineBufferedCommandWriter,
)
from dreamland.ssh_streaming import execute_backend_stream as _execute_backend_stream
from dreamland.ssh_transport import (
    CommandBackend,
    DreamlandSSHServer,
    SSHServerConfig,
)

__all__ = [
    "CommandBackend",
    "DreamlandSSHServer",
    "SSHAuthenticator",
    "SSHServerConfig",
    "SharedPasswordAuthenticator",
    "_LineBufferedCommandWriter",
    "_apply_nested_exit_fallback",
    "_execute_backend_stream",
    "_pop_nested_shell_for_transport_exit",
    "_should_close_shell_session",
]


def __getattr__(name: str) -> Any:
    """Preserve legacy access to internal SSH helpers during decomposition."""
    try:
        return getattr(_transport, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
