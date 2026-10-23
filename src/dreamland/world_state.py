from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files

from dreamland.models import InteractiveProgram

DEFAULT_ENTRY_HOST = "ubuntu"
DEFAULT_STATE_PROFILE_NAME = "default"


def default_world_state() -> dict[str, object]:
    resource = files("dreamland.default_state_profiles").joinpath(
        f"{DEFAULT_STATE_PROFILE_NAME}.json"
    )
    document = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return {}
    state = document.get("state")
    if not isinstance(state, dict):
        return {}
    world_state = state.get("world_state")
    return deepcopy(world_state) if isinstance(world_state, dict) else {}


def format_world_state_for_prompt(world_state: dict[str, object]) -> str:
    return json.dumps(world_state, indent=2, sort_keys=False)


def host_metadata_for_ref(
    world_state: dict[str, object],
    host_ref: str | None,
) -> dict[str, object]:
    hosts = world_state.get("hosts_by_name")
    if not isinstance(hosts, dict):
        return {}
    if isinstance(host_ref, str) and host_ref in hosts:
        host = hosts.get(host_ref)
        return deepcopy(host) if isinstance(host, dict) else {}
    return {}


def active_host_ref(world_state: dict[str, object]) -> str:
    value = world_state.get("active_host")
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = world_state.get("entry_host")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_ENTRY_HOST


def active_host_metadata(world_state: dict[str, object]) -> dict[str, object]:
    return host_metadata_for_ref(world_state, active_host_ref(world_state))


def active_host_metadata_for_context(
    world_state: dict[str, object],
) -> dict[str, object]:
    metadata = active_host_metadata(world_state)
    metadata.pop("command_history", None)
    metadata.pop("login_banner", None)
    return metadata


def hostname_for_host_ref(world_state: dict[str, object], host_ref: str | None) -> str:
    metadata = host_metadata_for_ref(world_state, host_ref)
    hostname = metadata.get("hostname")
    if isinstance(hostname, str) and hostname.strip():
        return hostname.strip()
    if isinstance(host_ref, str) and host_ref.strip():
        return host_ref.strip()
    return DEFAULT_ENTRY_HOST


def sync_active_host(
    world_state: dict[str, object],
    hostname: str,
) -> dict[str, object]:
    """Return world state with active_host pointing at the current shell host."""
    cleaned_hostname = hostname.strip() or DEFAULT_ENTRY_HOST
    synced = deepcopy(world_state)
    host_ref = _host_ref_for_hostname(synced, cleaned_hostname)
    synced["active_host"] = host_ref
    hosts = synced.setdefault("hosts_by_name", {})
    if isinstance(hosts, dict):
        host = hosts.get(host_ref)
        if isinstance(host, dict):
            host.setdefault("hostname", cleaned_hostname)
        else:
            hosts[host_ref] = {"hostname": cleaned_hostname}
    return synced


def default_interactive_program_from_world_state(
    world_state: dict[str, object],
) -> InteractiveProgram:
    host = active_host_metadata(world_state)
    shell = host.get("shell")
    default = shell.get("default") if isinstance(shell, dict) else None
    if not isinstance(default, dict):
        return InteractiveProgram(
            name="bash",
            command="/bin/bash",
            argv=("bash",),
        )
    name = str(default.get("name") or "bash")
    command = str(default.get("command") or "/bin/bash")
    prompt_template = default.get("prompt_template")
    metadata = default.get("metadata")
    program_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    program_metadata.setdefault("host", active_host_ref(world_state))
    world_name = world_state.get("name")
    if isinstance(world_name, str) and world_name:
        program_metadata.setdefault("world_state", world_name)
    return InteractiveProgram(
        name=name,
        command=command,
        argv=_string_tuple(default.get("argv")) or (name,),
        prompt_template=str(prompt_template) if prompt_template else None,
        metadata=program_metadata,
    )


def _host_ref_for_hostname(world_state: dict[str, object], hostname: str) -> str:
    hosts = world_state.get("hosts_by_name")
    if isinstance(hosts, dict):
        for key, value in hosts.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            value_hostname = value.get("hostname")
            aliases = value.get("aliases")
            if value_hostname == hostname or key == hostname:
                return key
            if isinstance(aliases, list) and hostname in aliases:
                return key
    return hostname


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()
