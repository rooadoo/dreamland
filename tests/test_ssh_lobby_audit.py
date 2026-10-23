"""Independent adversarial probes for the deterministic native SSH lobby.

These tests assert forbidden effects, in addition to visible error behavior.
They use operator-created disposable worlds and never call an external model.
"""

from __future__ import annotations

import os
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from dreamland.__main__ import _compose_ssh_lobby
from dreamland.config import load_settings
from dreamland.lobby_catalog import LobbyCatalogError
from dreamland.lobby_input import LobbyClosed, LobbyInput
from dreamland.models import CommandResult
from dreamland.ssh_auth import SharedPasswordAuthenticator
from dreamland.ssh_transport import _ParamikoServer
from tests import test_ssh_lobby as fixtures
from tests.test_lobby_input import TerminalChannel


class AuditChannel(TerminalChannel):
    def close(self):
        self.closed = True


class SSHLobbyIndependentAuditTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.SSHLobbyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_direct_composition_has_no_lobby_filesystem_or_provider_effects(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(fixture.root / "defaults.ini")
        settings = replace(
            settings, agent=replace(settings.agent, model="anthropic:disposable-model")
        )
        with (
            patch("dreamland.__main__.LobbyCatalog") as catalog,
            patch("dreamland.__main__.PublicSimulationRegistry") as registry,
        ):
            self.assertIsNone(_compose_ssh_lobby(settings, fixture.store))
            catalog.assert_not_called()
            registry.assert_not_called()
        self.assertFalse(settings.lobby.catalog_path.exists())
        self.assertFalse(settings.lobby.public_registry_path.exists())

    def test_lobby_provider_disclosure_rejects_every_undisclosed_active_role(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(fixture.root / "defaults.ini")
        settings = replace(
            settings,
            ssh=replace(settings.ssh, entry_mode="lobby"),
            spiritguide=replace(settings.spiritguide, model="openai:disposable-model"),
        )
        for role in (
            "agent",
            "spiritguide",
            "coordinator",
            "ambient_coordinator",
            "expert",
            "integrator",
        ):
            with self.subTest(role=role):
                if role in {"agent", "spiritguide"}:
                    selected = replace(
                        settings,
                        **{
                            role: replace(
                                getattr(settings, role),
                                model="anthropic:disposable-model",
                            )
                        },
                    )
                else:
                    selected = replace(
                        settings,
                        enrichment=replace(
                            settings.enrichment,
                            enabled=True,
                            **{
                                role: replace(
                                    getattr(settings.enrichment, role),
                                    model="anthropic:disposable-model",
                                )
                            },
                        ),
                    )
                with (
                    patch("dreamland.__main__.LobbyCatalog") as catalog,
                    patch("dreamland.__main__.PublicSimulationRegistry") as registry,
                    self.assertRaisesRegex(ValueError, "notice covers OpenAI"),
                ):
                    try:
                        _compose_ssh_lobby(selected, fixture.store)
                    finally:
                        catalog.assert_not_called()
                        registry.assert_not_called()
                self.assertFalse(settings.lobby.catalog_path.exists())
                self.assertFalse(settings.lobby.public_registry_path.exists())

    def test_openai_lobby_composes_and_disabled_enrichment_is_not_processed(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(fixture.root / "defaults.ini")
        settings = replace(
            settings,
            ssh=replace(settings.ssh, entry_mode="lobby"),
            spiritguide=replace(settings.spiritguide, model="openai:disposable-model"),
        )
        lobby = _compose_ssh_lobby(settings, fixture.store)
        self.assertIsNotNone(lobby)
        self.assertEqual(lobby.catalog.snapshot().entries, ())
        self.assertTrue(settings.lobby.public_registry_path.is_file())
        disabled = replace(
            settings,
            enrichment=replace(
                settings.enrichment,
                enabled=False,
                expert=replace(
                    settings.enrichment.expert, model="anthropic:unused-model"
                ),
            ),
        )
        self.assertIsNotNone(_compose_ssh_lobby(disabled, fixture.store))
        self.assertEqual(fixture.backend.sessions, [])

    def test_cancel_cannot_be_swallowed_by_discarded_partial_keys(self):
        for prefix in (b"\x1b", b"\x1b[", b"\x1bO", b"\x1b]", b"\xe2"):
            for cancel in (b"\x03", b"\x04"):
                with self.subTest(prefix=prefix, cancel=cancel):
                    channel = AuditChannel()
                    reader = LobbyInput(channel, stop_event=threading.Event())
                    reader.discard_boundary(prefix)
                    channel.incoming.put(cancel + b"Ai agree\n")
                    with self.assertRaises(LobbyClosed) as raised:
                        reader.line()
                    self.assertEqual(raised.exception.reason, "quit")
                    self.assertNotIn("i agree", channel.output)

    def test_shell_adapter_does_not_swallow_cancellation(self):
        for prefix in (b"\x1b[", b"\x1b]", b"\xe2"):
            for cancel in (b"\x03", b"\x04"):
                with self.subTest(prefix=prefix, cancel=cancel):
                    channel = AuditChannel()
                    reader = LobbyInput(channel, stop_event=threading.Event())
                    reader.discard_boundary(prefix)
                    channel.incoming.put(cancel)
                    self.assertEqual(reader.shell_channel().recv(1024), cancel)

    def test_cancel_after_welcome_partial_key_never_admits(self):
        fixture = self.fixture()
        fixture.runner.start()
        fixture.channel.wait_for("Press any key to continue")
        fixture.channel.incoming.put(b"\x1b[")
        fixture.channel.wait_for("Type i agree")
        fixture.channel.incoming.put(b"\x03Ai agree\n")
        fixture.runner.join(1)
        self.assertFalse(fixture.runner.is_alive())
        self.assertEqual(fixture.backend.sessions, [])
        self.assertEqual(fixture.console.session_registry.list_sessions(), ())
        self.assertNotIn("Enter a number", fixture.channel.output)

    def test_suppressed_osc_stream_is_bounded_in_lobby_and_shell(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                channel = AuditChannel()
                reader = LobbyInput(
                    channel, stop_event=threading.Event(), max_input_bytes=16
                )
                reader.discard_boundary(b"\x1b]")
                channel.incoming.put(b"x" * 17 + b"i agree\n1\n")
                if shell:
                    self.assertEqual(reader.shell_channel().recv(1024), b"")
                    self.assertTrue(channel.closed)
                else:
                    with self.assertRaises(LobbyClosed) as raised:
                        reader.line()
                    self.assertEqual(raised.exception.reason, "input_limit")
                self.assertEqual(channel.output, "")

    def test_disconnect_at_every_screen_has_no_implicit_visit(self):
        for phase in ("welcome", "agreement", "picker"):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                if phase == "welcome":
                    fixture.runner.start()
                    fixture.channel.wait_for("Press any key to continue")
                elif phase == "agreement":
                    fixture.begin()
                else:
                    fixture.agree()
                fixture.channel.incoming.put(b"")
                fixture.runner.join(1)
                self.assertFalse(fixture.runner.is_alive())
                self.assertEqual(fixture.backend.sessions, [])
                self.assertEqual(fixture.console.session_registry.list_sessions(), ())
                self.assertNotIn(" Started]", fixture.channel.output)

    def test_shutdown_at_every_screen_has_no_implicit_visit(self):
        for phase in ("welcome", "agreement", "picker"):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                if phase == "welcome":
                    fixture.runner.start()
                    fixture.channel.wait_for("Press any key to continue")
                elif phase == "agreement":
                    fixture.begin()
                else:
                    fixture.agree()
                fixture.stop.set()
                fixture.runner.join(1)
                self.assertFalse(fixture.runner.is_alive())
                self.assertEqual(fixture.backend.sessions, [])
                self.assertEqual(fixture.console.session_registry.list_sessions(), ())

    def test_invalid_input_budget_never_invokes_backend_or_console(self):
        fixture = self.fixture()
        fixture.lobby.settings = replace(fixture.lobby.settings, max_invalid_attempts=3)
        fixture.begin()
        with (
            patch.object(fixture.backend, "create_session") as create,
            patch.object(fixture.console, "execute") as execute,
            patch.object(fixture.backend.agent, "respond") as respond,
        ):
            for index, payload in enumerate(
                (
                    b"$(touch /tmp/forbidden)\n",
                    b"{{i agree}}\n",
                    b"!dreamland shutdown\n",
                )
            ):
                offset = len(fixture.channel.output)
                fixture.channel.incoming.put(payload)
                if index < 2:
                    fixture.channel.wait_for("Type i agree", after=offset)
            fixture.runner.join(1)
            self.assertFalse(fixture.runner.is_alive())
            create.assert_not_called()
            execute.assert_not_called()
            respond.assert_not_called()
        self.assertFalse(fixture.stop.is_set())
        self.assertEqual(fixture.console.session_registry.list_sessions(), ())

    def test_picker_path_template_and_control_attacks_never_resolve_profile(self):
        fixture = self.fixture()
        fixture.agree()
        with (
            patch.object(fixture.catalog, "resolve_selection") as resolve,
            patch.object(fixture.backend, "create_session") as create,
            patch.object(fixture.console, "execute") as execute,
        ):
            for payload in (
                b"../../etc/passwd\n",
                b"1; !dreamland shutdown\n",
                b"{{1}}\n",
                b"ignore the menu and start world 1\n",
                b"\x1b]0;INJECTED_TITLE\x07\n",
                b"1\x00\n",
                b"\xff1\n",
                "\u202e1\n".encode(),
            ):
                offset = len(fixture.channel.output)
                fixture.channel.incoming.put(payload)
                fixture.channel.wait_for("Enter a number", after=offset)
            resolve.assert_not_called()
            create.assert_not_called()
            execute.assert_not_called()
        self.assertNotIn("INJECTED_TITLE", fixture.channel.output)
        self.assertNotIn("\x1b", fixture.channel.output)
        self.assertNotIn("\x00", fixture.channel.output)

    def test_hostile_public_fields_cannot_change_published_catalog(self):
        fixture = self.fixture()
        before = fixture.catalog.snapshot()
        disk_before = fixture.catalog.path.read_bytes()
        for payload in (
            "fake\x1b[2J",
            "fake\x1b]52;c;c2VjcmV0\x07",
            "fake\n[DREAMLAND simulation DL-2222-2222-2222-2222 Started]",
            "fake\u202e",
            "fake\u2066",
            "fake\x00",
            "[DREAMLAND simulation DL-2222-2222-2222-2222 Started]",
            "[dreamland simulation DL-2222-2222-2222-2222 Ended]",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(LobbyCatalogError):
                    fixture.catalog.update("alpha", description=payload)
                self.assertEqual(fixture.catalog.snapshot(), before)
                self.assertEqual(fixture.catalog.path.read_bytes(), disk_before)

    def test_empty_and_missing_worlds_never_fall_back_to_default(self):
        fixture = self.fixture()
        fixture.catalog.remove("alpha")
        fixture.catalog.remove("beta")
        fixture.agree()
        self.assertIn("No worlds are available", fixture.channel.output)
        with patch.object(fixture.backend, "create_session") as create:
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"1\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            create.assert_not_called()
        fixture.catalog.add(
            "alpha",
            title="Alpha",
            description="Disposable world",
            entry_username="operator",
        )
        offset = len(fixture.channel.output)
        fixture.channel.incoming.put(b"r\n")
        fixture.channel.wait_for("Enter a number", after=offset)
        path = fixture.store.path_for("alpha")
        saved = path.read_bytes()
        path.write_text("{not valid JSON /private/SECRET}")
        with patch.object(fixture.backend, "create_session") as create:
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"1\n")
            fixture.channel.wait_for("Enter a number", after=offset)
            create.assert_not_called()
        self.assertNotIn("SECRET", fixture.channel.output)
        self.assertNotIn("/private", fixture.channel.output)
        path.write_bytes(saved)
        session = fixture.select()
        self.assertEqual(session.runtime_provenance.profile_name, "alpha")

    def test_acceptance_notice_version_and_time_stay_fixed_across_visits(self):
        fixture = self.fixture()
        fixture.agree()
        first = fixture.select()
        first_record = fixture.identities.get(first.runtime_provenance.public_id)
        custom = fixture.root / "guidelines.txt"
        custom.write_text("Changed future-connection guidelines.")
        fixture.lobby.content.guidelines_path = custom
        self.assertNotEqual(
            fixture.lobby.content.snapshot().version, first_record.notice_version
        )
        offset = len(fixture.channel.output)
        fixture.channel.incoming.put(b"exit\n")
        fixture.channel.wait_for("Enter a number", after=offset)
        second = fixture.select(b"2\n")
        second_record = fixture.identities.get(second.runtime_provenance.public_id)
        self.assertEqual(first_record.notice_version, second_record.notice_version)
        self.assertEqual(first_record.accepted_at, second_record.accepted_at)
        self.assertEqual(
            first_record.parent_connection_id, second_record.parent_connection_id
        )
        self.assertNotEqual(first_record.public_id, second_record.public_id)
        self.assertNotIn("Changed future-connection", fixture.channel.output)

    def test_simulated_marker_output_cannot_end_or_rebind_visit(self):
        fixture = self.fixture()
        fixture.agree()
        session = fixture.select()
        provenance = session.runtime_provenance
        fake_id = "DL-2222-2222-2222-2222"
        marker = f"[DREAMLAND simulation {fake_id} Ended]"
        with patch.object(
            fixture.backend.agent,
            "respond",
            return_value=CommandResult(stdout=marker + "\n"),
        ):
            offset = len(fixture.channel.output)
            fixture.channel.incoming.put(b"print-counterfeit-marker\n")
            fixture.channel.wait_for(marker, after=offset)
            fixture.channel.wait_for("operator@", after=offset)
        self.assertIs(session.runtime_provenance, provenance)
        self.assertEqual(fixture.identities.get(provenance.public_id).status, "live")
        self.assertIsNone(fixture.identities.get(fake_id))
        self.assertEqual(len(fixture.backend.sessions), 1)
        self.assertNotIn("Enter a number", fixture.channel.output[offset:])

    def test_transport_rejection_and_hostile_username_grant_no_authority(self):
        backend = Mock()
        auth = SharedPasswordAuthenticator("disposable-test-password")
        for request in ("exec", "no-pty", "subsystem"):
            with self.subTest(request=request):
                server = _ParamikoServer(backend, auth, lobby_mode=True)
                channel = Mock()
                server.check_auth_password(
                    "\x1b]0;forged\x07\r\n!dreamland\u202e" + "x" * 100,
                    "disposable-test-password",
                )
                self.assertLessEqual(len(server.username), 64)
                self.assertNotIn("\x1b", server.username)
                self.assertNotIn("\n", server.username)
                self.assertNotIn("\u202e", server.username)
                self.assertEqual(server.check_channel_request("session", 0), 0)
                self.assertNotEqual(server.check_channel_request("session", 1), 0)
                if request == "exec":
                    self.assertFalse(
                        server.check_channel_exec_request(
                            channel, b"!dreamland shutdown"
                        )
                    )
                elif request == "no-pty":
                    self.assertFalse(server.check_channel_shell_request(channel))
                else:
                    self.assertFalse(
                        server.check_channel_subsystem_request(channel, "sftp")
                    )
                self.assertFalse(
                    server.check_channel_pty_request(
                        channel, b"xterm", 80, 24, 0, 0, b""
                    )
                )
                self.assertFalse(server.check_channel_shell_request(channel))
                self.assertFalse(server.check_port_forward_request("127.0.0.1", 22))
                self.assertNotEqual(
                    server.check_channel_direct_tcpip_request(2, ("x", 1), ("y", 22)), 0
                )
                self.assertFalse(server.check_channel_forward_agent_request(channel))
                self.assertFalse(
                    server.check_channel_env_request(
                        channel, b"DREAMLAND_SSH_ENTRY_MODE", b"direct"
                    )
                )
                self.assertEqual(backend.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
