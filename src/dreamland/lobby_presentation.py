"""Small, renderer-owned SSH lobby accents; client metadata is only a hint."""

from __future__ import annotations

import re
from dataclasses import dataclass

RESET = "\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"
COLORS = {
    "heading": "\x1b[36m",
    "rule": "\x1b[36m",
    "selection": "\x1b[36m",
    "action": "\x1b[32m",
    "reference": "\x1b[35m",
}

# Fixed ASCII artwork: spacing, including the first row's final space, is data.
WELCOME_BANNER = (
    "o---o   _   _   __                           _ ",
    "|      | \\ |_) |_   /\\  |\\/| |     /\\  |\\ | | \\",
    "o---o  |_/ | \\ |__ /--\\ |  | |___ /--\\ | \\| |_/",
)
WELCOME_BANNER_WIDTH = max(map(len, WELCOME_BANNER))
WELCOME_BANNER_HEIGHT = len(WELCOME_BANNER)


def normalize_terminal_type(value: object) -> str:
    if isinstance(value, bytes):
        if len(value) > 64:
            return ""
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return ""
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9+_.-]{0,63}", value
    ):
        return ""
    return value.lower()


@dataclass(frozen=True)
class TerminalCapabilities:
    color: bool = False
    clear: bool = False
    motion: bool = False


# Exact names only: unknown suffixes must not accidentally enable controls.
_COLOR_TERMINALS = frozenset(
    {
        "xterm",
        "xterm-color",
        "xterm-16color",
        "xterm-256color",
        "putty",
        "putty-256color",
        "rxvt",
        "rxvt-color",
        "rxvt-256color",
        "rxvt-unicode",
        "rxvt-unicode-256color",
        "linux",
        "screen",
        "screen-256color",
        "screen-bce",
        "screen-256color-bce",
        "tmux",
        "tmux-256color",
    }
)
_MONO_TERMINALS = frozenset({"vt100", "vt102", "vt220", "xterm-mono"})


def terminal_capabilities(value: object) -> TerminalCapabilities:
    name = normalize_terminal_type(value)
    if name in _COLOR_TERMINALS:
        return TerminalCapabilities(True, True, True)
    if name in _MONO_TERMINALS:
        return TerminalCapabilities(False, True, True)
    return TerminalCapabilities()


@dataclass(frozen=True)
class Text:
    text: str
    role: str = "body"
    continues_paragraph: bool = False


@dataclass(frozen=True)
class LobbyPresentation:
    color: bool = False
    clear: bool = False

    @classmethod
    def select(
        cls, terminal_type: object, *, color: str = "auto", clear_screen: str = "auto"
    ) -> LobbyPresentation:
        capabilities = terminal_capabilities(terminal_type)
        return cls(
            capabilities.color and color == "auto",
            capabilities.clear and clear_screen == "auto",
        )

    def style(self, text: str, role: str) -> str:
        accent = COLORS.get(role) if self.color else None
        return accent + text + RESET if accent and text else text

    @property
    def reset(self) -> str:
        return RESET if self.color else ""

    @property
    def start_screen(self) -> str:
        return self.reset + (CLEAR if self.clear else "")

    def for_size(self, width: int, height: int) -> LobbyPresentation:
        return self if width >= 20 and height >= 5 else LobbyPresentation()


def welcome_banner_lines(
    *,
    banner: str,
    presentation: LobbyPresentation,
    columns: int,
    rows: int,
    body_rows: int,
    footer_rows: int,
    leading_rows: int,
) -> tuple[str, ...]:
    """Use artwork only when all welcome copy and its action fit together."""
    if (
        banner == "auto"
        and (presentation.color or presentation.clear)
        and columns >= WELCOME_BANNER_WIDTH
        and body_rows > 0
        and WELCOME_BANNER_HEIGHT + body_rows + footer_rows + leading_rows <= rows
    ):
        return WELCOME_BANNER
    return ("DREAMLAND",)
