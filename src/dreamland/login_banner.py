"""Validate saved initial-login text without involving model context."""

from __future__ import annotations

import unicodedata

from dreamland.agent_state import host_identity_from_state, shell_frame_from_state

MAX_LOGIN_BANNER_BYTES = 16_384
_ALLOWED_CONTROLS = frozenset("\t\n\r")


def initial_login_banner(world_state: dict[str, object]) -> str:
    """Return the saved banner for the initial active host, including silence."""
    host_ref, host = _required_host(world_state, "active_host")
    return _validated_banner(host.get("login_banner"), host_ref)


def validate_profile_login_banners(state: dict[str, object]) -> None:
    """Require valid banners on both initial hosts and validate any others."""
    world_state = state.get("world_state")
    if not isinstance(world_state, dict):
        raise ValueError("state profile world_state must be an object")
    for field in ("entry_host", "active_host"):
        host_ref, host = _required_host(world_state, field)
        _validated_banner(host.get("login_banner"), host_ref)
    _validate_initial_shell_banner(state, world_state)
    hosts = world_state.get("hosts_by_name")
    if isinstance(hosts, dict):
        for host_ref, host in hosts.items():
            if isinstance(host, dict) and "login_banner" in host:
                _validated_banner(host["login_banner"], str(host_ref))


def _validate_initial_shell_banner(
    state: dict[str, object], world_state: dict[str, object]
) -> None:
    active_ref, active = _required_host(world_state, "active_host")
    hostname = active.get("hostname")
    default_hostname = (
        hostname.strip()
        if isinstance(hostname, str) and hostname.strip()
        else active_ref
    )
    identity = host_identity_from_state(
        state.get("host_identity"), default_hostname=default_hostname
    )
    frames = state.get("shell_stack")
    if isinstance(frames, list) and frames:
        identity = shell_frame_from_state(frames[-1]).host_identity
    # Match sync_active_host: the shell's hostname can identify a registry key,
    # a canonical hostname, or a saved host alias.
    initial_hostname = identity.hostname.strip() or "ubuntu"
    hosts = world_state.get("hosts_by_name")
    if isinstance(hosts, dict):
        for host_ref, host in hosts.items():
            if not isinstance(host_ref, str) or not isinstance(host, dict):
                continue
            aliases = host.get("aliases")
            if (
                host_ref == initial_hostname
                or host.get("hostname") == initial_hostname
                or isinstance(aliases, list)
                and initial_hostname in aliases
            ):
                _validated_banner(host.get("login_banner"), host_ref)
                return
    raise ValueError(
        f"initial shell host {initial_hostname!r} must reference a registered host "
        "with a login_banner"
    )


def _required_host(
    world_state: dict[str, object], field: str
) -> tuple[str, dict[str, object]]:
    reference = world_state.get(field)
    hosts = world_state.get("hosts_by_name")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError(f"state profile world_state.{field} is required")
    host = hosts.get(reference) if isinstance(hosts, dict) else None
    if not isinstance(host, dict):
        raise ValueError(
            f"state profile world_state.{field} must reference a registered host"
        )
    return reference, host


def _validated_banner(value: object, host_ref: str) -> str:
    label = f"host {host_ref!r} login_banner"
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string (use an empty string for silence)")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > MAX_LOGIN_BANNER_BYTES:
        raise ValueError(f"{label} exceeds {MAX_LOGIN_BANNER_BYTES} UTF-8 bytes")
    if any(
        unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS
        for character in value
    ):
        raise ValueError(f"{label} contains unsupported terminal control characters")
    return value
