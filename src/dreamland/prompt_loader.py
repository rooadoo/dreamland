from __future__ import annotations

import re
from importlib.resources import files

DEFAULT_SYSTEM_PROMPT_MODULES = (
    "persona.md",
    "terminal_io.md",
    "state_contract.md",
    "environment_metadata.md",
    "world.md",
    "ambient_realism.md",
    "security.md",
    "environment.md",
)
INCLUDE_PATTERN = re.compile(r"^\{\{\s*include:([A-Za-z0-9_.-]+\.md)\s*\}\}$")


def load_prompt(name: str) -> str:
    return _load_prompt_file(name, seen=()).strip()


def compose_prompt(names: tuple[str, ...]) -> str:
    return "\n\n".join(
        _load_prompt_file(name, seen=()).strip()
        for name in names
    )


def _load_prompt_file(name: str, *, seen: tuple[str, ...]) -> str:
    if name in seen:
        chain = " -> ".join((*seen, name))
        raise ValueError(f"Prompt include cycle detected: {chain}")
    text = files("dreamland.prompts").joinpath(name).read_text(encoding="utf-8")
    return "\n".join(
        _expand_prompt_line(line, seen=(*seen, name)) for line in text.splitlines()
    )


def _expand_prompt_line(line: str, *, seen: tuple[str, ...]) -> str:
    match = INCLUDE_PATTERN.match(line.strip())
    if match is None:
        return line
    return _load_prompt_file(match.group(1), seen=seen).strip()
