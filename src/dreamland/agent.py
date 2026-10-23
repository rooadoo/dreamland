"""Stable public facade for command-agent implementations."""

from __future__ import annotations

from typing import Any

from dreamland import agent_core as _core
from dreamland.agent_core import (
    CommandAgent,
    DeterministicCommandAgent,
    LangGraphCommandAgent,
    MockCommandAgent,
    handle_builtin_command_context,
)

__all__ = [
    "CommandAgent",
    "DeterministicCommandAgent",
    "LangGraphCommandAgent",
    "MockCommandAgent",
    "handle_builtin_command_context",
]


def __getattr__(name: str) -> Any:
    """Preserve legacy access to internal helpers during module decomposition."""
    try:
        return getattr(_core, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
