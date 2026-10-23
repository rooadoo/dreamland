from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from dreamland.terminal.events import EditorEvent

SendText = Callable[[str], None]


class InteractiveEditor(Protocol):
    def start_prompt(self, prompt: str) -> None:
        pass

    def handle_byte(self, byte: int) -> EditorEvent | None:
        pass
