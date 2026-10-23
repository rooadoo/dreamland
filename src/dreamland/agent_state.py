from __future__ import annotations

from dreamland.models import (
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    HostIdentity,
    InteractiveProgram,
    ShellEnvironment,
    ShellFrame,
    default_interactive_program,
    default_shell_environment,
    default_shell_frame,
)


def host_identity_to_state(identity: HostIdentity) -> dict[str, object]:
    return {"hostname": identity.hostname}


def host_identity_from_state(value: object, *, default_hostname: str) -> HostIdentity:
    if not isinstance(value, dict):
        return HostIdentity(hostname=default_hostname)
    hostname = value.get("hostname")
    return HostIdentity(
        hostname=str(hostname) if hostname is not None else default_hostname
    )


def optional_host_identity_from_state(value: object) -> HostIdentity | None:
    if not isinstance(value, dict):
        return None
    hostname = value.get("hostname")
    if hostname is None:
        return None
    return HostIdentity(hostname=str(hostname))


def interactive_program_to_state(
    program: InteractiveProgram,
) -> dict[str, object]:
    return {
        "name": program.name,
        "command": program.command,
        "argv": list(program.argv),
        "prompt_template": program.prompt_template,
        "metadata": dict(program.metadata),
    }


def interactive_program_from_state(value: object) -> InteractiveProgram:
    if not isinstance(value, dict):
        return default_interactive_program()
    name = str(value.get("name") or value.get("program name") or "bash")
    command = str(
        value.get("command")
        or value.get("program command")
        or _default_program_command(name)
    )
    argv = value.get("argv", value.get("program argv", ()))
    metadata = value.get("metadata", {})
    return InteractiveProgram(
        name=_normalize_program_name(name),
        command=command,
        argv=_coerce_string_tuple(argv),
        prompt_template=(
            str(value["prompt_template"])
            if value.get("prompt_template") is not None
            else _default_program_prompt(name)
        ),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def shell_frame_to_state(frame: ShellFrame) -> dict[str, object]:
    host_identity = _shell_frame_host_identity_for_state(frame)
    return {
        "username": frame.username,
        "cwd": frame.cwd,
        "interactive_program": interactive_program_to_state(frame.interactive_program),
        "shell_environment": shell_environment_to_state(frame.shell_environment),
        "host_identity": host_identity_to_state(host_identity),
        "metadata": dict(frame.metadata),
    }


def shell_frame_from_state(
    value: object,
    *,
    default_hostname: str = "ubuntu",
) -> ShellFrame:
    if not isinstance(value, dict):
        return default_shell_frame(hostname=default_hostname)
    metadata = value.get("metadata", {})
    shell_environment = shell_environment_from_state(value.get("shell_environment"))
    host_identity = host_identity_from_state(
        value.get("host_identity"),
        default_hostname=shell_environment.variables.get("HOSTNAME", default_hostname),
    )
    return ShellFrame(
        username=str(value.get("username", "guest")),
        cwd=str(value.get("cwd", "/home/guest")),
        interactive_program=interactive_program_from_state(
            value.get("interactive_program")
        ),
        shell_environment=shell_environment,
        host_identity=host_identity,
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def shell_stack_from_state(
    value: object,
    *,
    username: str,
    hostname: str,
    cwd: str,
    interactive_program: InteractiveProgram,
    shell_environment: ShellEnvironment,
) -> tuple[ShellFrame, ...]:
    if isinstance(value, (list, tuple)) and value:
        return tuple(
            shell_frame_from_state(item, default_hostname=hostname) for item in value
        )
    return (
        default_shell_frame(
            username=username,
            hostname=hostname,
            cwd=cwd,
            interactive_program=interactive_program,
            shell_environment=shell_environment,
        ),
    )


def _shell_frame_host_identity_for_state(frame: ShellFrame) -> HostIdentity:
    environment_hostname = frame.shell_environment.variables.get("HOSTNAME")
    if frame.host_identity == HostIdentity() and environment_hostname:
        return HostIdentity(hostname=environment_hostname)
    return frame.host_identity


def shell_environment_to_state(
    environment: ShellEnvironment,
) -> dict[str, object]:
    return {
        "variables": dict(environment.variables),
        "exported": sorted(environment.exported),
    }


def shell_environment_from_state(value: object) -> ShellEnvironment:
    if not isinstance(value, dict):
        return default_shell_environment()
    if "variables" in value or "exported" in value:
        variables = value.get("variables", {})
        exported = value.get("exported", [])
    else:
        variables = {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(item, (str, int, float, bool))
        }
        exported = list(variables)
    return ShellEnvironment(
        variables=(
            {str(key): str(item) for key, item in variables.items()}
            if isinstance(variables, dict)
            else {}
        ),
        exported=(
            frozenset(str(item) for item in exported)
            if isinstance(exported, (list, tuple, set, frozenset))
            else frozenset()
        ),
    )


def environment_metadata_to_state(
    environment_metadata: EnvironmentMetadata,
) -> dict[str, object]:
    return {
        "connection": connection_metadata_to_state(environment_metadata.connection),
        "metadata": dict(environment_metadata.metadata),
    }


def connection_metadata_to_state(
    connection_metadata: ConnectionMetadata | None,
) -> dict[str, object]:
    if connection_metadata is None:
        return {}
    return {
        "protocol": connection_metadata.protocol,
        "source": connection_endpoint_to_state(connection_metadata.source),
        "destination": connection_endpoint_to_state(connection_metadata.destination),
        "metadata": dict(connection_metadata.metadata),
    }


def environment_metadata_from_state(value: object) -> EnvironmentMetadata:
    if not isinstance(value, dict):
        return EnvironmentMetadata()
    metadata = value.get("metadata", {})
    return EnvironmentMetadata(
        connection=connection_metadata_from_state(value.get("connection")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def connection_endpoint_to_state(
    endpoint: ConnectionEndpoint,
) -> dict[str, object]:
    return {
        "ip": endpoint.ip,
        "port": endpoint.port,
    }


def connection_metadata_from_state(
    value: object,
) -> ConnectionMetadata | None:
    if not isinstance(value, dict) or not value:
        return None
    metadata = value.get("metadata", {})
    return ConnectionMetadata(
        protocol=optional_string(value.get("protocol")),
        source=connection_endpoint_from_state(value.get("source")),
        destination=connection_endpoint_from_state(value.get("destination")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def connection_endpoint_from_state(value: object) -> ConnectionEndpoint:
    if not isinstance(value, dict):
        return ConnectionEndpoint()
    return ConnectionEndpoint(
        ip=optional_string(value.get("ip")),
        port=optional_int(value.get("port")),
    )


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_int(value: object, default: int = 0) -> int:
    converted = optional_int(value)
    return default if converted is None else converted


def _coerce_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()


def _normalize_program_name(name: str) -> str:
    if name in {"python", "python3"}:
        return "python"
    return name


def _default_program_command(name: str) -> str:
    if name in {"python", "python3"}:
        return "/usr/bin/python3"
    if name == "bash":
        return "/bin/bash"
    return name


def _default_program_prompt(name: str) -> str | None:
    if name in {"python", "python3"}:
        return ">>> "
    if name == "redis" or name == "redis-cli":
        return "127.0.0.1:6379> "
    return None


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
