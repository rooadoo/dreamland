import re
import unittest

from dreamland.lobby_presentation import (
    LobbyPresentation,
    TerminalCapabilities,
    normalize_terminal_type,
    terminal_capabilities,
)


class TerminalCapabilityTests(unittest.TestCase):
    def test_normalization_accepts_only_bounded_ascii_terminal_names(self):
        for value, expected in (
            ("XTERM-256COLOR", "xterm-256color"),
            (b"PuTTY", "putty"),
            ("terminal+1.0_custom", "terminal+1.0_custom"),
            ("A" * 64, "a" * 64),
            (b"A" * 64, "a" * 64),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_terminal_type(value), expected)

        for value in (
            None,
            80,
            [],
            "",
            b"",
            "x" * 65,
            b"x" * 65,
            "xterm ",
            " xterm",
            "xterm\n",
            "xterm\r",
            "xterm\t",
            "xterm\x00",
            "xterm\x1b[31m",
            "xterm\x7f",
            "xtérm",
            b"xterm\xff",
            "../xterm",
            "/usr/share/terminfo/x/xterm",
            "-xterm",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_terminal_type(value), "")
                self.assertEqual(terminal_capabilities(value), TerminalCapabilities())

    def test_known_color_terminal_forms_enable_each_capability(self):
        for name in (
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
        ):
            for value in (name, name.upper().encode("ascii")):
                with self.subTest(value=value):
                    self.assertEqual(
                        terminal_capabilities(value),
                        TerminalCapabilities(color=True, clear=True, motion=True),
                    )

    def test_arbitrary_family_prefixes_and_unknown_terminals_remain_plain(self):
        for value in (
            "dumb",
            "unknown",
            "ansi",
            "xterm-unknown",
            "xterm256color",
            "xterm-256color-extra",
            "putty-private",
            "rxvt-unicode-future",
            "linux-color",
            "screen-custom",
            "tmux-direct",
            "vt100-color",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_terminal_type(value), value)
                self.assertEqual(terminal_capabilities(value), TerminalCapabilities())

    def test_monochrome_terminals_keep_clearing_and_motion_without_color(self):
        for value in ("vt100", "vt102", "vt220", "xterm-mono"):
            with self.subTest(value=value):
                self.assertEqual(
                    terminal_capabilities(value),
                    TerminalCapabilities(color=False, clear=True, motion=True),
                )
                presentation = LobbyPresentation.select(value)
                self.assertFalse(presentation.color)
                self.assertTrue(presentation.clear)
                self.assertEqual(
                    presentation.style("DREAMLAND", "heading"), "DREAMLAND"
                )
                self.assertEqual(presentation.reset, "")
                self.assertEqual(presentation.start_screen, "\x1b[2J\x1b[H")


class LobbyPresentationTests(unittest.TestCase):
    def test_color_and_clearing_switches_are_independent_and_capability_gated(self):
        for terminal, has_color, has_clear in (
            ("xterm", True, True),
            ("vt100", False, True),
            ("dumb", False, False),
            ("unknown", False, False),
            (None, False, False),
        ):
            for color in ("auto", "off"):
                for clear_screen in ("auto", "off"):
                    with self.subTest(
                        terminal=terminal, color=color, clear_screen=clear_screen
                    ):
                        presentation = LobbyPresentation.select(
                            terminal, color=color, clear_screen=clear_screen
                        )
                        self.assertEqual(
                            presentation.color, has_color and color == "auto"
                        )
                        self.assertEqual(
                            presentation.clear, has_clear and clear_screen == "auto"
                        )

    def test_plain_output_never_adds_presentation_escapes(self):
        for presentation in (
            LobbyPresentation(),
            LobbyPresentation.select("dumb"),
            LobbyPresentation.select("xterm-unknown"),
            LobbyPresentation.select("xterm", color="off", clear_screen="off"),
        ):
            with self.subTest(presentation=presentation):
                self.assertEqual(presentation.start_screen, "")
                self.assertEqual(presentation.reset, "")
                for role in (
                    "heading",
                    "rule",
                    "selection",
                    "action",
                    "reference",
                    "body",
                ):
                    self.assertEqual(
                        presentation.style("Visible text", role), "Visible text"
                    )

    def test_styles_use_basic_foreground_controls_and_reset_each_fragment(self):
        presentation = LobbyPresentation.select("xterm")
        foreground_codes = {
            "heading": "\x1b[36m",
            "rule": "\x1b[36m",
            "selection": "\x1b[36m",
            "action": "\x1b[32m",
            "reference": "\x1b[35m",
        }
        for role, foreground in foreground_codes.items():
            with self.subTest(role=role):
                self.assertEqual(
                    presentation.style("  Text  ", role),
                    foreground + "  Text  \x1b[0m",
                )
                self.assertEqual(presentation.style("", role), "")
                self.assertEqual(
                    presentation.style("Heading", role)
                    + presentation.style(" body", "body"),
                    foreground + "Heading\x1b[0m body",
                )

    def test_body_and_unknown_roles_preserve_layout_without_controls(self):
        presentation = LobbyPresentation.select("xterm")
        text = "  Body paragraph.\n\nNext paragraph.  "
        for role in ("body", "unknown", "\x1b[31m"):
            with self.subTest(role=role):
                self.assertEqual(presentation.style(text, role), text)

    def test_renderer_emits_only_approved_controls_and_clears_visible_screen(self):
        presentation = LobbyPresentation.select("xterm")
        self.assertEqual(presentation.start_screen, "\x1b[0m\x1b[2J\x1b[H")
        self.assertEqual(presentation.reset, "\x1b[0m")
        output = presentation.start_screen
        for role in ("heading", "rule", "selection", "action", "reference", "body"):
            output += presentation.style("Text\n", role)
        output += presentation.reset
        controls = re.findall(r"\x1b\[[0-9;]*[A-Za-z]", output)
        self.assertEqual(
            set(controls),
            {"\x1b[0m", "\x1b[2J", "\x1b[H", "\x1b[36m", "\x1b[32m", "\x1b[35m"},
        )
        self.assertNotIn("\x1b", re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output))

    def test_below_practical_floor_uses_plain_output_without_mutating_selection(self):
        presentation = LobbyPresentation.select("xterm")
        for width, height in ((19, 5), (20, 4), (19, 4), (1, 1), (80, 4), (19, 24)):
            with self.subTest(width=width, height=height):
                minimal = presentation.for_size(width, height)
                self.assertEqual(minimal, LobbyPresentation())
                self.assertEqual(minimal.start_screen, "")
                self.assertEqual(minimal.style("DREAMLAND", "heading"), "DREAMLAND")
                self.assertEqual(minimal.reset, "")
        self.assertTrue(presentation.color)
        self.assertTrue(presentation.clear)
        for width, height in ((20, 5), (21, 5), (20, 6), (80, 24)):
            with self.subTest(width=width, height=height):
                self.assertEqual(presentation.for_size(width, height), presentation)
