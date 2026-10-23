import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings, LobbySettings
from dreamland.console import DreamlandConsole
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer
from dreamland.ssh_auth import SharedPasswordAuthenticator
from dreamland.ssh_lobby import SSHLobby
from dreamland.ssh_transport import (
    DreamlandSSHServer,
    SSHServerConfig,
    _ParamikoServer,
)
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
)
from tests import test_ssh_lobby as fixtures
from tests.test_ssh_lobby_sessions import BlockingAgent
from tests.test_ssh_sessions import NestedSSHAgent

paramiko_available = importlib.util.find_spec("paramiko") is not None
if paramiko_available:
    import paramiko


class TrackingBackend(AgentBackend):
    def __init__(self, store):
        super().__init__(
            server_profile(),
            agent=NestedSSHAgent(),
            state_profile_runtime=StateProfileRuntime(store=store),
        )
        self.sessions = []

    def create_session(self, *args, **kwargs):
        session = super().create_session(*args, **kwargs)
        self.sessions.append(session)
        return session


class TerminalMetadataLobby:
    def run(self, channel, *args, terminal_type, dimensions, **kwargs):
        channel.sendall(b"metadata-ready\n")
        while True:
            command = channel.recv(1)
            if not command or command == b"q":
                channel.send_exit_status(0)
                return
            width, height = dimensions()
            channel.sendall(f"{terminal_type}|{width}|{height}\n".encode("ascii"))


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class SSHLobbyTransportTests(unittest.TestCase):
    def test_simple_login_reaches_static_monochrome_welcome_over_real_ssh(self):
        fixture = fixtures.SSHLobbyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.lobby.settings = LobbySettings()
        fixture.lobby.fast_backend = fixture.backend
        server = DreamlandSSHServer(
            SSHServerConfig(
                port=0,
                host_key_path=fixture.root / "host_key",
                entry_mode="lobby",
                password_check_delay_seconds=0,
            ),
            fixture.backend,
            authenticator=SharedPasswordAuthenticator(
                "test-password",
                required_username="dreamland",
                allow_simple_login=True,
                fast_password="demo-password",
            ),
            lobby=fixture.lobby,
        )
        self.addCleanup(server.stop)
        server.start_in_thread()
        self.assertTrue(server.wait_until_ready())
        for username, simple, password in (
            ("dreamland_simple", True, "test-password"),
            ("dreamland", False, "test-password"),
            ("dreamland_fast", False, "demo-password"),
            ("dreamland_fast_simple", True, "demo-password"),
        ):
            with self.subTest(username=username):
                client = paramiko.SSHClient()
                self.addCleanup(client.close)
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    "127.0.0.1",
                    port=server.bound_port,
                    username=username,
                    password=password,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=5,
                )
                channel = client.invoke_shell(
                    term="xterm-256color", width=100, height=100
                )
                output = self._read_until(channel, "Press any key to continue")
                self.assertIn("\x1b[2J", output)
                if simple:
                    self.assertNotRegex(output, r"\x1b\[[0-9;]*m")
                    self.assertNotRegex(output, r"\x1b\[[1-9][0-9]*;[1-9][0-9]*H")
                else:
                    self.assertRegex(output, r"\x1b\[(?:32|35|36)m")
                    self.assertRegex(output, r"\x1b\[[1-9][0-9]*;[1-9][0-9]*H")
                self.assertEqual(fixture.backend.sessions, [])
                channel.sendall(b"q")
                self.assertEqual(channel.recv_exit_status(), 0)
                client.close()

    def test_client_term_is_normalized_and_bounded_before_lobby_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._metadata_server(Path(directory))
            try:
                for term, expected in (
                    (b"XTERM-256COLOR", "xterm-256color"),
                    (b"PuTTY", "putty"),
                    (b"unknown-client", "unknown-client"),
                    (b"", ""),
                    (b"xterm\x1b[2J", ""),
                    (b"xterm\xff", ""),
                    (b"xterm" + b"x" * 100, ""),
                ):
                    with self.subTest(term=term):
                        client = self._connect(server)
                        try:
                            channel = client.invoke_shell(
                                term=term, width=91, height=37
                            )
                            self._read_until(channel, "metadata-ready\n")
                            channel.sendall(b"?")
                            self.assertEqual(
                                self._read_until(channel, "\n"),
                                f"{expected}|91|37\n",
                            )
                            channel.sendall(b"q")
                            self.assertEqual(channel.recv_exit_status(), 0)
                        finally:
                            client.close()
            finally:
                server.stop()

    def test_window_change_updates_bounded_geometry_and_preserves_tiny_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._metadata_server(Path(directory))
            client = self._connect(server)
            try:
                channel = client.invoke_shell(term="xterm", width=0, height=0)
                self._read_until(channel, "metadata-ready\n")
                channel.sendall(b"?")
                self.assertEqual(self._read_until(channel, "\n"), "xterm|80|24\n")
                for width, height, expected in (
                    (19, 4, "xterm|19|4\n"),
                    (1, 1, "xterm|1|1\n"),
                    (0, 0, "xterm|1|1\n"),
                    (2**32 - 1, 2**32 - 1, "xterm|240|100\n"),
                    (72, 18, "xterm|72|18\n"),
                    (0, 28, "xterm|72|28\n"),
                ):
                    with self.subTest(width=width, height=height):
                        channel.resize_pty(width=width, height=height)
                        # Requests and channel data arrive in order on one SSH
                        # transport, so this asks for a snapshot after the resize.
                        channel.sendall(b"?")
                        self.assertEqual(self._read_until(channel, "\n"), expected)
                channel.sendall(b"q")
                self.assertEqual(channel.recv_exit_status(), 0)
            finally:
                client.close()
                server.stop()

    def test_initial_tiny_and_oversized_dimensions_are_bounded_without_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._metadata_server(Path(directory))
            try:
                for width, height, expected in (
                    (9, 3, "dumb|9|3\n"),
                    (2**32 - 1, 2**32 - 1, "dumb|240|100\n"),
                ):
                    with self.subTest(width=width, height=height):
                        client = self._connect(server)
                        try:
                            channel = client.invoke_shell(
                                term="dumb", width=width, height=height
                            )
                            self._read_until(channel, "metadata-ready\n")
                            channel.sendall(b"?")
                            self.assertEqual(self._read_until(channel, "\n"), expected)
                            channel.sendall(b"q")
                            self.assertEqual(channel.recv_exit_status(), 0)
                        finally:
                            client.close()
            finally:
                server.stop()

    def test_resize_requires_pty_and_env_requests_remain_denied(self):
        server = _ParamikoServer(
            Mock(), SharedPasswordAuthenticator("test-password"), lobby_mode=True
        )
        self.assertFalse(server.check_channel_window_change_request(None, 12, 3, 0, 0))
        self.assertEqual(server.dimensions(), (80, 24))
        self.assertTrue(
            server.check_channel_pty_request(None, b"XTERM", 12, 3, 0, 0, b"")
        )
        self.assertEqual(server.dimensions(), (12, 3))
        self.assertEqual(server.terminal_type, "xterm")
        for name, value in ((b"TERM", b"dumb"), (b"NO_COLOR", b"1")):
            self.assertFalse(server.check_channel_env_request(None, name, value))
        self.assertEqual(server.terminal_type, "xterm")
        self.assertTrue(server.check_channel_window_change_request(None, 7, 2, 0, 0))
        self.assertEqual(server.dimensions(), (7, 2))

    def test_actual_lobby_nested_exits_and_idle_busy_public_close_reuse_one_channel(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateProfileStore(root / "profiles")
            for name in ("alpha", "beta"):
                store.save(
                    StateProfile(
                        name,
                        "world",
                        {
                            "world_state": {
                                "entry_host": "ubuntu",
                                "active_host": "ubuntu",
                                "hosts_by_name": {
                                    "ubuntu": {
                                        "hostname": "ubuntu",
                                        "login_banner": "Authored fixture host.\n",
                                    }
                                },
                            }
                        },
                        "2026-09-11T00:00:00Z",
                    )
                )
            catalog = LobbyCatalog(store, root / "catalog.json")
            catalog.add(
                "alpha",
                title="First World",
                description="Explore first.",
                entry_username="operator",
            )
            catalog.add(
                "beta",
                title="Second World",
                description="Explore second.",
                entry_username="reader",
            )
            identities = PublicSimulationRegistry(root / "visits.sqlite3")
            console = DreamlandConsole(
                ConsoleSettings(),
                LogRecordBuffer(20),
                lambda: None,
                state_profiles=store,
                lobby_catalog=catalog,
                public_simulations=identities,
            )
            backend = TrackingBackend(store)
            lobby = SSHLobby(
                catalog,
                identities,
                LobbyContent(),
                LobbySettings(intro_animation="off"),
            )
            server = DreamlandSSHServer(
                SSHServerConfig(
                    port=0,
                    host_key_path=root / "host_key",
                    entry_mode="lobby",
                    password_check_delay_seconds=0,
                ),
                backend,
                console,
                authenticator=SharedPasswordAuthenticator("test-password"),
                lobby=lobby,
            )
            server.start_in_thread()
            self.assertTrue(server.wait_until_ready())
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            blocked = BlockingAgent()
            logs = self.enterContext(
                self.assertLogs("dreamland.lifecycle", level="INFO")
            )
            try:
                client.connect(
                    "127.0.0.1",
                    port=server.bound_port,
                    username="real.ssh+visitor@example",
                    password="test-password",
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=5,
                )
                channel = client.invoke_shell(
                    term="xterm-256color", width=100, height=100
                )
                welcome = self._read_until(channel, "Press any key to continue")
                self.assertIn("\x1b[2J", welcome)
                self.assertEqual(backend.sessions, [])
                channel.send(b" ")
                self._read_until(channel, "Type i agree")
                channel.send(b"i agree; whoami\n")
                self._read_until(channel, "Type i agree")
                self.assertEqual(backend.sessions, [])
                channel.send(b"I AGREE\r\n1\nwhoami\n")
                self._read_until(channel, "Enter a number")
                self.assertEqual(backend.sessions, [])
                channel.send(b"1\n")
                first_output = self._read_until(channel, "operator@ubuntu:~$ ")
                first = backend.sessions[-1]
                first_id = first.runtime_provenance.public_id
                self.assertIn(f"{first_id} Started]", first_output)
                self.assertEqual(first_output.rsplit("\x1b", 1)[-1][:3], "[0m")
                self.assertNotIn("real.ssh+visitor@example", first_output)
                channel.send(b"whoami\n")
                identity_output = self._read_until(channel, "operator@ubuntu:~$ ")
                self.assertIn("\r\noperator\r\n", identity_output)

                channel.send(b"!dreamland shutdown\n")
                shell_output = self._read_until(channel, "operator@ubuntu:~$ ")
                self.assertIn("unavailable over SSH", shell_output)
                self.assertNotIn("\x1b", shell_output)
                self.assertEqual(backend.agent.contexts, [])
                channel.send(b"ssh swift-gw01.payments.meridianbank.local\n")
                self._read_until(channel, "bob@swift-gw01:~$ ")
                context = backend.agent.contexts[-1]
                self.assertEqual(context.username, "operator")
                self.assertEqual(context.login_username, "operator")
                self.assertNotIn("real.ssh+visitor@example", repr(context))
                channel.send(b"\x04")
                parent_output = self._read_until(channel, "operator@ubuntu:~$ ")
                self.assertNotIn("Ended]", parent_output)
                channel.send(b"exit\r\n1\nwhoami\n\x1b[")
                ended_output = self._read_until(channel, "Enter a number")
                self.assertIn(f"{first_id} Ended]", ended_output)
                self.assertEqual(len(backend.sessions), 1)
                self.assertIsNone(console.session_registry.get(first.langfuse_trace_id))

                channel.send(b"A1\n")
                self._read_until(channel, "operator@ubuntu:~$ ")
                second = backend.sessions[-1]
                second_id = second.runtime_provenance.public_id
                self.assertNotEqual(first_id, second_id)
                self.assertNotEqual(first.langfuse_trace_id, second.langfuse_trace_id)
                self.assertEqual(second.history, [])
                self.assertIn(
                    "Closed",
                    console.execute(
                        f"sessions --close {second_id}", allow_exit=False
                    ).output,
                )
                self._read_until(channel, "Enter a number")
                self.assertEqual(identities.get(second_id).end_reason, "operator_close")

                backend.agent = blocked
                channel.send(b"2\n")
                self._read_until(channel, "reader@ubuntu:~$ ")
                third = backend.sessions[-1]
                third_id = third.runtime_provenance.public_id
                channel.send(b"agent-check\n")
                self.assertTrue(blocked.started.wait(2))
                self._read_until(channel, "visible before close")
                self.assertIn(
                    "Closed",
                    console.execute(
                        f"sessions --close {third_id}", allow_exit=False
                    ).output,
                )
                self._read_until(channel, "Enter a number")
                self.assertTrue(blocked.control.cancelled())
                self.assertEqual(identities.get(third_id).end_reason, "operator_close")
                self.assertIsNone(console.session_registry.get(third.langfuse_trace_id))

                channel.send(b"1\n")
                fourth_output = self._read_until(channel, "operator@ubuntu:~$ ")
                fourth = backend.sessions[-1]
                self.assertNotEqual(third_id, fourth.runtime_provenance.public_id)
                self.assertEqual(fourth.history, [])
                blocked.finish.set()
                self.assertTrue(blocked.released.wait(2))
                channel.send(b"\x04")
                fourth_output += self._read_until(channel, "Enter a number")
                self.assertNotIn("FORBIDDEN", fourth_output)
                self.assertNotIn("Type i agree", fourth_output)
                self.assertEqual(console.session_registry.list_sessions(), ())
                channel.send(b"q\n")
                self.assertEqual(channel.recv_exit_status(), 0)
                self.assertEqual(len(backend.sessions), 4)
            finally:
                blocked.finish.set()
                client.close()
                server.stop()
            authenticated = next(
                record
                for record in logs.records
                if record.msg.startswith("SSH lobby authenticated ")
            )
            parent_id, ssh_username = authenticated.args
            self.assertEqual(ssh_username, "real.ssh+visitor@example")
            self.assertIn(
                "ssh_username='real.ssh+visitor@example'", authenticated.getMessage()
            )
            opened = next(
                record
                for record in logs.records
                if record.msg.startswith("SSH lobby opened ")
            )
            self.assertEqual(opened.args, authenticated.args)
            for session, expected in zip(
                backend.sessions,
                ("operator", "operator", "reader", "operator"),
                strict=True,
            ):
                self.assertEqual(session.login_username, expected)
                self.assertEqual(session.username, expected)
                self.assertEqual(session.cwd, f"/home/{expected}")
                public_id = session.runtime_provenance.public_id
                self.assertEqual(
                    identities.get(public_id).parent_connection_id, parent_id
                )
                for event in ("created", "ended"):
                    record = next(
                        record
                        for record in logs.records
                        if record.msg.startswith(f"SSH lobby visit {event} ")
                        and record.args[1] == public_id
                    )
                    self.assertEqual(
                        record.args[:3],
                        (parent_id, public_id, session.langfuse_trace_id),
                    )
                    self.assertEqual(record.args[4], expected)

    def _metadata_server(self, root):
        server = DreamlandSSHServer(
            SSHServerConfig(
                port=0,
                host_key_path=root / "host_key",
                entry_mode="lobby",
                password_check_delay_seconds=0,
            ),
            AgentBackend(server_profile(), agent=NestedSSHAgent()),
            authenticator=SharedPasswordAuthenticator("test-password"),
            lobby=TerminalMetadataLobby(),
        )
        server.start_in_thread()
        self.assertTrue(server.wait_until_ready())
        return server

    def _connect(self, server):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=server.bound_port,
            username="visitor",
            password="test-password",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )
        return client

    def _read_until(self, channel, needle):
        deadline = time.monotonic() + 5
        output = ""
        channel.settimeout(0.1)
        while time.monotonic() < deadline:
            try:
                data = channel.recv(4096)
            except TimeoutError:
                continue
            if not data:
                break
            output += data.decode("utf-8", errors="replace")
            if needle in output:
                return output
        self.fail(f"Did not receive {needle!r}; output was {output!r}")


if __name__ == "__main__":
    unittest.main()
