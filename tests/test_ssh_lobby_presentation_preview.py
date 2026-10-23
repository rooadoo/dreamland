import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamland.agent import DeterministicCommandAgent
from dreamland.lobby_catalog import LobbySelectionChanged

TOOL_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "ssh_lobby_presentation_preview.py"
)
SPEC = importlib.util.spec_from_file_location(
    "ssh_lobby_presentation_preview", TOOL_PATH
)
assert SPEC is not None and SPEC.loader is not None
PREVIEW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREVIEW
SPEC.loader.exec_module(PREVIEW)


class SSHLobbyPresentationPreviewTests(unittest.TestCase):
    def test_fixture_is_isolated_and_uses_operator_copy_and_deterministic_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notice = root / "welcome.txt"
            notice.write_text("Current operator welcome.", encoding="utf-8")
            state = root / "preview"
            state.mkdir(mode=0o700)
            with patch("socket.socket.connect", side_effect=AssertionError("network")):
                instance = PREVIEW.build_preview(
                    state,
                    welcome_path=notice,
                    color="off",
                    clear_screen="off",
                    banner="simple",
                    intro_animation="auto",
                )
                self.assertEqual(
                    instance.content.snapshot().welcome, notice.read_text()
                )
                self.assertEqual(instance.settings.color, "off")
                self.assertEqual(instance.settings.clear_screen, "off")
                self.assertEqual(instance.settings.banner, "simple")
                self.assertEqual(instance.settings.intro_animation, "auto")
                self.assertEqual(instance.server.config.host, "127.0.0.1")
                self.assertIsInstance(instance.backend.agent, DeterministicCommandAgent)
                self.assertFalse(instance.console.introspection.available())
                snapshot = instance.catalog.snapshot()
                world, profile = instance.catalog.resolve_selection(snapshot, 1)
                self.assertEqual(world.entry_username, "operator")
                self.assertEqual(snapshot.entries[1].entry_username, "reader")
                world_state = profile.state["world_state"]
                self.assertEqual(world_state["entry_host"], "ubuntu")
                self.assertEqual(world_state["active_host"], "ubuntu")
                self.assertEqual(
                    world_state["hosts_by_name"]["ubuntu"]["login_banner"],
                    "Preview Alpha login console.\n",
                )
                session = instance.backend.create_session(
                    world.entry_username, state_profile=profile
                )
                try:
                    self.assertEqual(
                        instance.backend.execute(session, "whoami").stdout, "operator\n"
                    )
                    self.assertIn(
                        "deterministic-agent",
                        instance.backend.execute(session, "provider-probe").stdout,
                    )
                finally:
                    instance.backend.release_session(session)
            for path in (
                instance.catalog.path,
                instance.settings.public_registry_path,
                instance.server.config.host_key_path,
                instance.backend.state_profile_runtime.store.directory,
                instance.backend.structured_failure_store.directory,
                instance.console.structured_failures.directory,
            ):
                self.assertTrue(path.is_relative_to(state), path)
            self.assertEqual(notice.read_text(), "Current operator welcome.")

    def test_unavailable_fixture_rejects_beta_and_can_recover_through_console(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = PREVIEW.build_preview(
                Path(directory), catalog_case="unavailable"
            )
            self.assertEqual(instance.settings.banner, "auto")
            self.assertEqual(instance.settings.intro_animation, "auto")
            snapshot = instance.catalog.snapshot()
            with self.assertRaisesRegex(LobbySelectionChanged, "unavailable"):
                instance.catalog.resolve_selection(snapshot, 2)
            result = instance.console.execute(
                "lobby worlds remove beta", allow_exit=False
            )
            self.assertNotIn("error:", result.output)
            snapshot = instance.catalog.snapshot()
            self.assertEqual(len(snapshot.entries), 1)
            self.assertEqual(
                instance.catalog.resolve_selection(snapshot, 1)[0].profile, "alpha"
            )

    def test_empty_fixture_retains_saved_worlds_for_console_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = PREVIEW.build_preview(Path(directory), catalog_case="empty")
            self.assertEqual(instance.catalog.snapshot().entries, ())
            result = instance.console.execute(
                'lobby worlds add alpha --title "Preview Alpha" --description "Recovered test world." --entry-username operator',
                allow_exit=False,
            )
            self.assertNotIn("error:", result.output)
            self.assertEqual(len(instance.catalog.snapshot().entries), 1)

    def test_refuses_to_reuse_an_existing_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing-catalog.json"
            existing.write_text("retain me", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                PREVIEW.build_preview(root)
            self.assertEqual(existing.read_text(), "retain me")

    def test_intro_animation_cli_defaults_to_auto_and_allows_disable(self):
        parser = PREVIEW.parser()
        self.assertEqual(parser.parse_args([]).intro_animation, "auto")
        for mode in ("auto", "off"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    parser.parse_args(["--intro-animation", mode]).intro_animation,
                    mode,
                )


if __name__ == "__main__":
    unittest.main()
