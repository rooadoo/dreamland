#!/usr/bin/env python3
"""Read-only preflight for the private companion metadata Docker network."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

_PROJECT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
_NETWORK = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_ROLES = ("attendee", "operator")
_PURPOSE = "companion-api-foundation"
_SAFE_ERROR = "Companion network validation failed; review Docker access and topology."


class NetworkError(ValueError):
    """The intended or current Docker topology is not the supported foundation."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, "Invalid companion network check arguments.\n")


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise NetworkError(_SAFE_ERROR)
        result[key] = value
    return result


def _json(value: str) -> dict:
    result = json.loads(value, object_pairs_hook=_object)
    if not isinstance(result, dict):
        raise NetworkError(_SAFE_ERROR)
    return result


def _docker(*arguments: str) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    # Docker errors and materialized configuration may include private values.
    # None of their stdout/stderr is included in an exception or report.
    if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
        raise NetworkError(_SAFE_ERROR)
    return result.stdout


def _rendered_intent(document: dict) -> tuple[str, str, dict[str, str]]:
    project = document.get("name")
    if not isinstance(project, str) or not _matches(_PROJECT, project):
        raise NetworkError(_SAFE_ERROR)
    networks = document.get("networks")
    if not isinstance(networks, dict) or set(networks) != {"companion"}:
        raise NetworkError(_SAFE_ERROR)
    network = networks["companion"]
    if (
        not isinstance(network, dict)
        or not _matches(_NETWORK, network.get("name"))
        or network.get("internal") is not True
        or network.get("driver", "bridge") != "bridge"
        or network.get("external", False) is not False
        or network.get("driver_opts")
        or not isinstance(network.get("labels"), dict)
        or network["labels"].get("org.dreamland.network-purpose") != _PURPOSE
    ):
        raise NetworkError(_SAFE_ERROR)
    services = document.get("services")
    if not isinstance(services, dict) or not {
        "attendee-api",
        "operator-api",
    }.issubset(services):
        raise NetworkError(_SAFE_ERROR)
    if set(services) - {
        "attendee-api",
        "operator-api",
        "attendee-init",
        "operator-init",
    }:
        raise NetworkError(_SAFE_ERROR)
    volumes = document.get("volumes")
    if not isinstance(volumes, dict):
        raise NetworkError(_SAFE_ERROR)
    images = set()
    actual_volumes = set()
    aliases = {}
    for role in _ROLES:
        service_name = role + "-api"
        alias = f"{project}-{role}-api"
        aliases[alias] = service_name
        source = role + "-api-data"
        volume = volumes.get(source)
        if (
            not isinstance(volume, dict)
            or volume.get("name") != f"{project}_{source}"
            or volume.get("external", False) is not False
        ):
            raise NetworkError(_SAFE_ERROR)
        actual_volumes.add(volume["name"])
        for name in (service_name, role + "-init"):
            if name not in services:
                continue
            service = services[name]
            if (
                not isinstance(service, dict)
                or not _matches(_IMAGE, service.get("image"))
                or service.get("ports")
                or service.get("secrets")
                or service.get("configs")
                or service.get("volumes_from")
            ):
                raise NetworkError(_SAFE_ERROR)
            images.add(service["image"])
            mounts = service.get("volumes")
            if not isinstance(mounts, list) or len(mounts) != 1:
                raise NetworkError(_SAFE_ERROR)
            mount = mounts[0]
            if (
                not isinstance(mount, dict)
                or mount.get("type") != "volume"
                or mount.get("source") != source
                or mount.get("target") != "/var/lib/dreamland/api"
            ):
                raise NetworkError(_SAFE_ERROR)
            if name == service_name:
                connections = service.get("networks")
                if (
                    service.get("network_mode")
                    or not isinstance(connections, dict)
                    or set(connections) != {"companion"}
                    or not isinstance(connections["companion"], dict)
                    or connections["companion"].get("aliases") != [alias]
                    or mount.get("read_only") is not True
                ):
                    raise NetworkError(_SAFE_ERROR)
            elif service.get("network_mode") != "none" or service.get("networks"):
                raise NetworkError(_SAFE_ERROR)
    if len(images) != 1 or len(actual_volumes) != 2:
        raise NetworkError(_SAFE_ERROR)
    return project, network["name"], aliases


def check_network(env_file: Path) -> dict:
    """Check intended ports/mounts and current network ownership without mutation."""
    compose = Path(__file__).resolve().with_name("compose.api.yaml")
    config = _json(
        _docker(
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(compose),
            "--profile",
            "attendee",
            "--profile",
            "operator",
            "config",
            "--format",
            "json",
        )
    )
    project, network, aliases = _rendered_intent(config)
    names = _docker(
        "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"
    ).splitlines()
    matches = names.count(network)
    if matches > 1:
        raise NetworkError(_SAFE_ERROR)
    if matches == 0:
        return {
            "validation": "passed",
            "project": project,
            "network": network,
            "network_state": "absent",
            "connected_containers": 0,
        }
    network_format = (
        '{"name":{{json .Name}},"driver":{{json .Driver}},"internal":{{json .Internal}},'
        '"project":{{json (index .Labels "com.docker.compose.project")}},'
        '"network":{{json (index .Labels "com.docker.compose.network")}},'
        '"purpose":{{json (index .Labels "org.dreamland.network-purpose")}},'
        '"containers":{{json .Containers}}}'
    )
    state = _json(_docker("network", "inspect", "--format", network_format, network))
    if (
        state.get("name") != network
        or state.get("driver") != "bridge"
        or state.get("internal") is not True
        or state.get("project") != project
        or state.get("network") != "companion"
        or state.get("purpose") != _PURPOSE
        or not isinstance(state.get("containers"), dict)
    ):
        raise NetworkError(_SAFE_ERROR)
    container_format = (
        '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
        '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
        '"aliases":{{json (index .NetworkSettings.Networks "'
        + network
        + '").Aliases}}}'
    )
    claimed = set()
    for container_id in state["containers"]:
        if not _matches(_CONTAINER_ID, container_id):
            raise NetworkError(_SAFE_ERROR)
        container = _json(
            _docker("inspect", "--format", container_format, container_id)
        )
        declared = container.get("aliases")
        if declared is None:
            declared = []
        if not isinstance(declared, list) or any(
            not isinstance(value, str) for value in declared
        ):
            raise NetworkError(_SAFE_ERROR)
        for alias in set(declared) & set(aliases):
            if (
                container.get("project") != project
                or container.get("service") != aliases[alias]
                or container.get("oneoff") != "False"
                or alias in claimed
            ):
                raise NetworkError(_SAFE_ERROR)
            claimed.add(alias)
    return {
        "validation": "passed",
        "project": project,
        "network": network,
        "network_state": "present",
        "connected_containers": len(state["containers"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/dev/null"))
    arguments = parser.parse_args(argv)
    try:
        result = check_network(arguments.env_file)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        print(_SAFE_ERROR, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
