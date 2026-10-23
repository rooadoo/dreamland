"""Deterministic command-to-surface hints shared by background agents."""

from __future__ import annotations

import shlex
from typing import Final

from dreamland.agent_shell import analyze_command

DEFAULT_SURFACE: Final[str] = "shell"
SUPPORTED_SURFACES: Final[frozenset[str]] = frozenset(
    {
        "database",
        "filesystem",
        "identity",
        "network",
        "packages",
        "process",
        "service",
        "shell",
    }
)
_SURFACE_COMMANDS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    (
        "network",
        frozenset(
            {
                "arp",
                "dig",
                "host",
                "ifconfig",
                "ip",
                "netstat",
                "nmap",
                "nslookup",
                "ping",
                "route",
                "ss",
                "traceroute",
            }
        ),
    ),
    (
        "process",
        frozenset({"htop", "kill", "pgrep", "pidof", "pkill", "ps", "pstree", "top"}),
    ),
    (
        "service",
        frozenset({"journalctl", "service", "systemctl"}),
    ),
    (
        "packages",
        frozenset(
            {"apk", "apt", "apt-get", "dnf", "dpkg", "pip", "pip3", "rpm", "yum"}
        ),
    ),
    (
        "identity",
        frozenset(
            {"groups", "hostname", "id", "last", "uname", "users", "who", "whoami"}
        ),
    ),
    (
        "filesystem",
        frozenset(
            {
                "cat",
                "cd",
                "cp",
                "df",
                "du",
                "file",
                "find",
                "head",
                "less",
                "ls",
                "mkdir",
                "mount",
                "mv",
                "pwd",
                "rm",
                "stat",
                "tail",
                "touch",
                "tree",
            }
        ),
    ),
    (
        "database",
        frozenset({"mongosh", "mysql", "psql", "redis-cli", "sqlite3"}),
    ),
)


def infer_command_surface(command: str) -> str:
    """Return a stable coarse surface without invoking a model."""
    # Aggregate output can span several surfaces and conditional branches.
    if analyze_command(command).kind != "simple":
        return DEFAULT_SURFACE
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    executables = {
        token.rsplit("/", maxsplit=1)[-1].lower()
        for token in tokens
        if token and not token.startswith("-")
    }
    for surface, commands in _SURFACE_COMMANDS:
        if executables & commands:
            return surface
    return DEFAULT_SURFACE
