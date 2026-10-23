"""SSH command shape helpers for Inspect exploration runs."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SshCommandShape:
    """Classification of an SSH-looking command string."""

    is_ssh: bool
    is_standalone: bool
    has_batchmode: bool
    has_remote_command: bool
    parse_error: str | None = None


def classify_ssh_command(command: str) -> SshCommandShape:
    """Classify whether command is a standalone SSH pivot or remote execution."""

    stripped = command.strip()
    if not _starts_with_ssh(stripped):
        return SshCommandShape(
            is_ssh=False,
            is_standalone=False,
            has_batchmode=False,
            has_remote_command=False,
        )
    if _has_shell_chaining(stripped):
        return SshCommandShape(
            is_ssh=True,
            is_standalone=False,
            has_batchmode="batchmode=yes" in stripped.lower(),
            has_remote_command=True,
        )
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        return SshCommandShape(
            is_ssh=True,
            is_standalone=False,
            has_batchmode="batchmode=yes" in stripped.lower(),
            has_remote_command=True,
            parse_error=str(exc),
        )
    if not tokens or tokens[0] != "ssh":
        return SshCommandShape(
            is_ssh=True,
            is_standalone=False,
            has_batchmode="batchmode=yes" in stripped.lower(),
            has_remote_command=True,
        )

    operands = _ssh_operands(tokens)
    has_remote_command = len(operands) > 1
    has_batchmode = any(_is_batchmode_token(token) for token in tokens[1:])
    return SshCommandShape(
        is_ssh=True,
        is_standalone=len(operands) == 1 and not has_batchmode,
        has_batchmode=has_batchmode,
        has_remote_command=has_remote_command,
    )


def contains_ssh_command(command: str) -> bool:
    return bool(re.search(r"(^|[;&|]\s*)ssh(\s|$)", command.strip()))


def is_bare_ssh_pivot(command: str) -> bool:
    shape = classify_ssh_command(command)
    return shape.is_ssh and shape.is_standalone


def _starts_with_ssh(command: str) -> bool:
    return bool(re.match(r"^ssh(\s|$)", command))


def _has_shell_chaining(command: str) -> bool:
    return "\n" in command or bool(re.search(r"\s(&&|\|\||;|\|)\s*", command))


def _ssh_operands(tokens: list[str]) -> list[str]:
    operands: list[str] = []
    skip_next = False
    options_with_values = {
        "-b",
        "-c",
        "-D",
        "-E",
        "-e",
        "-F",
        "-I",
        "-i",
        "-J",
        "-L",
        "-l",
        "-m",
        "-O",
        "-o",
        "-p",
        "-Q",
        "-R",
        "-S",
        "-W",
        "-w",
    }
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if not operands and token in options_with_values:
            skip_next = True
            continue
        if not operands and _is_compact_option_with_value(token):
            continue
        if not operands and token.startswith("-"):
            continue
        operands.append(token)
    return operands


def _is_compact_option_with_value(token: str) -> bool:
    return bool(
        re.match(
            r"^-(?:b|c|D|E|e|F|I|i|J|L|l|m|O|o|p|Q|R|S|W|w).+",
            token,
        )
    )


def _is_batchmode_token(token: str) -> bool:
    normalized = token.lower()
    return normalized == "batchmode=yes" or normalized.startswith("-obatchmode=yes")
