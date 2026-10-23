import os
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch

from dreamland.config import LobbySettings, SSHSettings, load_settings


class LobbyConfigTests(unittest.TestCase):
    def test_defaults_preserve_direct_entry_and_finite_lobby_bounds(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / "test.ini"
            settings = load_settings(path)
        self.assertEqual(SSHSettings().entry_mode, "direct")
        self.assertEqual(settings.ssh.entry_mode, "direct")
        self.assertEqual(
            settings.lobby.catalog_path, path.parent / "lobby/catalog.json"
        )
        self.assertEqual(
            settings.lobby.public_registry_path,
            path.parent / "lobby/public_visits.sqlite3",
        )
        self.assertIsNone(settings.lobby.welcome_path)
        self.assertEqual(settings.lobby.idle_timeout_seconds, 300)
        self.assertEqual(settings.lobby.max_input_bytes, 256)
        self.assertEqual(settings.lobby.max_invalid_attempts, 20)
        self.assertEqual(settings.lobby.retention_days, 30)
        self.assertEqual(settings.lobby.color, "auto")
        self.assertEqual(settings.lobby.clear_screen, "auto")
        self.assertEqual(settings.lobby.banner, "auto")
        self.assertEqual(settings.lobby.intro_animation, "auto")
        self.assertEqual(LobbySettings().color, "auto")
        self.assertEqual(LobbySettings().clear_screen, "auto")
        self.assertEqual(LobbySettings().banner, "auto")
        self.assertEqual(LobbySettings().intro_animation, "auto")

    def test_ini_relative_paths_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            path.write_text(
                "[ssh]\nentry_mode=lobby\n[lobby]\ncatalog_path=worlds.json\nwelcome_path=welcome.txt\nidle_timeout_seconds=120\n"
            )
            with patch.dict(os.environ, {}, clear=True):
                configured = load_settings(path)
            self.assertEqual(configured.ssh.entry_mode, "lobby")
            self.assertEqual(configured.lobby.catalog_path, path.parent / "worlds.json")
            self.assertEqual(configured.lobby.welcome_path, path.parent / "welcome.txt")
            self.assertEqual(configured.lobby.idle_timeout_seconds, 120)
            with patch.dict(
                os.environ,
                {
                    "DREAMLAND_SSH_ENTRY_MODE": "DIRECT",
                    "DREAMLAND_LOBBY_CATALOG_PATH": str(path.parent / "override.json"),
                    "DREAMLAND_LOBBY_IDLE_TIMEOUT_SECONDS": "60",
                },
                clear=True,
            ):
                overridden = load_settings(path)
            self.assertEqual(overridden.ssh.entry_mode, "direct")
            self.assertEqual(
                overridden.lobby.catalog_path, path.parent / "override.json"
            )
            self.assertEqual(overridden.lobby.idle_timeout_seconds, 60)

    def test_presentation_modes_are_independent_and_normalized(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / "test.ini"
            for color, clear_screen, banner, intro_animation in product(
                ("auto", "off"), ("auto", "off"), ("auto", "simple"), ("auto", "off")
            ):
                with self.subTest(
                    color=color,
                    clear_screen=clear_screen,
                    banner=banner,
                    intro_animation=intro_animation,
                ):
                    path.write_text(
                        f"[lobby]\ncolor = {color.upper()} \n"
                        f"clear_screen = {clear_screen.upper()} \n"
                        f"banner = {banner.upper()} \n"
                        f"intro_animation = {intro_animation.upper()} \n"
                    )
                    settings = load_settings(path)
                    self.assertEqual(settings.lobby.color, color)
                    self.assertEqual(settings.lobby.clear_screen, clear_screen)
                    self.assertEqual(settings.lobby.banner, banner)
                    self.assertEqual(settings.lobby.intro_animation, intro_animation)

    def test_presentation_environment_overrides_only_selected_ini_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            configured = {
                "color": "off",
                "clear_screen": "off",
                "banner": "simple",
                "intro_animation": "off",
            }
            path.write_text(
                "[lobby]\ncolor=off\nclear_screen=off\nbanner=simple\nintro_animation=off\n"
            )
            for name in configured:
                with (
                    self.subTest(name=name),
                    patch.dict(
                        os.environ,
                        {f"DREAMLAND_LOBBY_{name.upper()}": " AUTO "},
                        clear=True,
                    ),
                ):
                    settings = load_settings(path)
                    self.assertEqual(getattr(settings.lobby, name), "auto")
                    for other, mode in configured.items():
                        if other != name:
                            self.assertEqual(getattr(settings.lobby, other), mode)

    def test_simple_banner_environment_override_is_normalized(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"DREAMLAND_LOBBY_BANNER": " SIMPLE "}, clear=True),
        ):
            path = Path(directory) / "test.ini"
            path.write_text("[lobby]\nbanner=auto\n")
            self.assertEqual(load_settings(path).lobby.banner, "simple")

    def test_intro_animation_environment_can_disable_configured_animation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"DREAMLAND_LOBBY_INTRO_ANIMATION": " OFF "}, clear=True
            ),
        ):
            path = Path(directory) / "test.ini"
            path.write_text("[lobby]\nintro_animation=auto\n")
            self.assertEqual(load_settings(path).lobby.intro_animation, "off")

    def test_invalid_configuration_fails_instead_of_falling_back(self):
        values = {
            "DREAMLAND_SSH_ENTRY_MODE": ("", "unknown"),
            "DREAMLAND_LOBBY_CATALOG_PATH": ("", "relative.json"),
            "DREAMLAND_LOBBY_IDLE_TIMEOUT_SECONDS": ("", "0", "-1", "inf", "86401"),
            "DREAMLAND_LOBBY_MAX_INPUT_BYTES": ("0", "15", "4097"),
            "DREAMLAND_LOBBY_MAX_INVALID_ATTEMPTS": ("0", "1001"),
            "DREAMLAND_LOBBY_RETENTION_DAYS": ("0", "3651"),
            "DREAMLAND_LOBBY_COLOR": ("", " ", "on", "true", "simple", "unknown"),
            "DREAMLAND_LOBBY_CLEAR_SCREEN": ("", " ", "on", "true", "unknown"),
            "DREAMLAND_LOBBY_BANNER": ("", " ", "on", "off", "true", "unknown"),
            "DREAMLAND_LOBBY_INTRO_ANIMATION": (
                "",
                " ",
                "on",
                "true",
                "simple",
                "unknown",
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, candidates in values.items():
                for value in candidates:
                    with (
                        self.subTest(name=name, value=value),
                        patch.dict(os.environ, {name: value}, clear=True),
                        self.assertRaises(ValueError),
                    ):
                        load_settings(Path(directory) / "test.ini")

    def test_invalid_ini_presentation_mode_fails_explicitly(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / "test.ini"
            for name in ("color", "clear_screen", "banner", "intro_animation"):
                for value in ("", " ", "on", "true", "unknown"):
                    with self.subTest(name=name, value=value):
                        path.write_text(f"[lobby]\n{name}={value}\n")
                        with self.assertRaisesRegex(ValueError, name):
                            load_settings(path)
            path.write_text("[lobby]\nbanner=off\n")
            with self.assertRaisesRegex(ValueError, "banner must be auto or simple"):
                load_settings(path)

    def test_blank_ini_path_fails(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / "test.ini"
            path.write_text("[lobby]\nwelcome_path=\n")
            with self.assertRaisesRegex(ValueError, "welcome_path"):
                load_settings(path)
