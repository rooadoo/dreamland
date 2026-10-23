from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from dreamland.models import InteractiveProgram
from dreamland.world_state import (
    DEFAULT_ENTRY_HOST,
    default_interactive_program_from_world_state,
    default_world_state,
    hostname_for_host_ref,
)


@dataclass(slots=True)
class ServerProfile:
    """Transport and shell presentation settings for a world."""

    hostname: str
    server_version: str
    prompt_template: str
    default_interactive_program: InteractiveProgram
    world_state: dict[str, object]


def server_profile(
    world_state: dict[str, object] | None = None,
) -> ServerProfile:
    world = (
        deepcopy(world_state)
        if isinstance(world_state, dict)
        else default_world_state()
    )
    entry_host = world.get("entry_host")
    hostname = hostname_for_host_ref(
        world,
        entry_host if isinstance(entry_host, str) and entry_host.strip() else None,
    )
    return ServerProfile(
        hostname=hostname or DEFAULT_ENTRY_HOST,
        server_version="SSH-2.0-DREAMLAND",
        prompt_template="{username}@{hostname}:{cwd}$ ",
        default_interactive_program=default_interactive_program_from_world_state(world),
        world_state=world,
    )
