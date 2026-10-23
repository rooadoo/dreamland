"""Local transport proof: detaching an operator does not detach an SSH visit."""

from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings, LobbySettings
from dreamland.console import ConsoleContext, DreamlandConsole
from dreamland.console_client import (
    ConsoleClientError,
    ConsoleConnection,
    RemoteConsole,
)
from dreamland.console_socket import ConsoleSocketServer
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.models import CommandContext, CommandResult
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer
from dreamland.ssh_auth import SharedPasswordAuthenticator
from dreamland.ssh_lobby import SSHLobby
from dreamland.ssh_transport import DreamlandSSHServer, SSHServerConfig
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
)
from dreamland.structured_failures import StructuredFailureStore

paramiko_available = importlib.util.find_spec("paramiko") is not None
if paramiko_available:
    import paramiko


class StaticAgent:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def respond(self, context: CommandContext) -> CommandResult:
        self.commands.append(context.command)
        return CommandResult(stdout=f"stub handled: {context.command}\n")


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class ConsoleSSHIntegrationTests(unittest.TestCase):
    def test_public_lobby_visit_survives_operator_detach_and_reconnect(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dreamland-console-ssh-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profiles = StateProfileStore(root / "profiles")
        profiles.save(
            StateProfile(
                "operator-rehearsal",
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
                "2026-09-12T00:00:00Z",
            )
        )
        catalog = LobbyCatalog(profiles, root / "catalog.json")
        catalog.add(
            "operator-rehearsal",
            title="Operator Rehearsal",
            description="Provider-free local transport check.",
            entry_username="operator",
        )
        identities = PublicSimulationRegistry(root / "visits.sqlite3")
        shutdown = Mock()
        introspection = Mock()
        failures = StructuredFailureStore(root / "failures")
        console = DreamlandConsole(
            ConsoleSettings(),
            LogRecordBuffer(30),
            shutdown,
            introspection=introspection,
            state_profiles=profiles,
            lobby_catalog=catalog,
            public_simulations=identities,
            structured_failure_store=failures,
        )
        agent = StaticAgent()
        backend = AgentBackend(
            server_profile(),
            agent=agent,
            state_profile_runtime=StateProfileRuntime(store=profiles),
            structured_failure_store=failures,
        )
        ssh_server = DreamlandSSHServer(
            SSHServerConfig(
                host="127.0.0.1",
                port=0,
                host_key_path=root / "ssh_host_key",
                entry_mode="lobby",
                password_check_delay_seconds=0,
            ),
            backend,
            console,
            authenticator=SharedPasswordAuthenticator("local-rehearsal-password"),
            lobby=SSHLobby(
                catalog,
                identities,
                LobbyContent(),
                LobbySettings(intro_animation="off"),
            ),
        )
        self.addCleanup(ssh_server.stop)
        ssh_server.start_in_thread()
        self.assertTrue(
            ssh_server.wait_until_ready(5), "Local SSH listener did not start"
        )

        socket_path = root / "run" / "operator.sock"
        operator_server = ConsoleSocketServer(console, socket_path)
        self.addCleanup(operator_server.stop)
        operator_server.start()

        attendee = paramiko.SSHClient()
        self.addCleanup(attendee.close)
        attendee.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        attendee.connect(
            "127.0.0.1",
            port=ssh_server.bound_port,
            username="attendee",
            password="local-rehearsal-password",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
            auth_timeout=5,
            banner_timeout=5,
        )
        channel = attendee.invoke_shell(term="dumb", width=100, height=40)
        self._read_until(channel, "Press any key to continue")
        channel.sendall(b" ")
        self._read_until(channel, "Type i agree")
        channel.sendall(b"i agree\n")
        self._read_until(channel, "Enter a number")
        channel.sendall(b"1\n")
        entered = self._read_until(channel, "operator@ubuntu:~$ ")

        registrations = console.session_registry.list_sessions()
        self.assertEqual(len(registrations), 1)
        registration = registrations[0]
        session = registration.session
        public_id = session.runtime_provenance.public_id
        self.assertIsNotNone(public_id)
        self.assertIn(f"{public_id} Started", entered)
        self._run_attendee_command(channel, "console-rehearsal before-detach")

        first_connection = self._connect_operator(socket_path)
        first_console = RemoteConsole(first_connection)
        first_context = ConsoleContext()
        listing = first_console.execute(
            "sessions --live", allow_exit=False, context=first_context
        )
        self.assertIn(public_id, listing.output)
        self.assertIn("operator", listing.output)
        selected = first_console.execute(
            f"sessions --set {public_id}", allow_exit=False, context=first_context
        )
        self.assertIn(f"matched {public_id}", selected.output)
        self.assertEqual(first_context.active_session_id, registration.session_id)

        first_console.execute("disconnect", allow_exit=False, context=first_context)
        self.assertTrue(first_connection.closed)
        self.assertFalse(channel.closed)
        self.assertIs(
            console.session_registry.get(registration.session_id), registration
        )
        self._run_attendee_command(channel, "console-rehearsal after-detach")

        second_connection = self._connect_operator(socket_path)
        self.assertEqual(second_connection.runtime_id, first_connection.runtime_id)
        second_console = RemoteConsole(second_connection)
        second_context = ConsoleContext()
        second_console.execute("", allow_exit=False, context=second_context)
        self.assertIsNone(second_context.active_session_id)
        self.assertEqual(second_context.recent_session_ids, ())
        inspection = second_console.execute(
            f"sessions --close {public_id} --dry-run",
            allow_exit=False,
            context=second_context,
        )
        self.assertIn(
            f"Would close live session {registration.session_id}", inspection.output
        )
        self.assertEqual(identities.get(public_id).status, "live")
        self.assertIs(
            console.session_registry.get(registration.session_id), registration
        )
        self.assertIs(registration.session, session)
        second_connection.close()  # Also covers EOF without the disconnect command.

        self._run_attendee_command(channel, "console-rehearsal after-reconnect")
        self.assertEqual(
            agent.commands,
            [
                "console-rehearsal before-detach",
                "console-rehearsal after-detach",
                "console-rehearsal after-reconnect",
            ],
        )
        self.assertFalse(registration.closed.is_set())
        self.assertFalse(console.shutdown_requested)
        shutdown.assert_not_called()
        self.assertEqual(introspection.mock_calls, [])

        channel.sendall(b"exit\n")
        self._read_until(channel, "Enter a number")
        self.assertIsNone(console.session_registry.get(registration.session_id))
        self.assertEqual(identities.get(public_id).status, "ended")

    def _connect_operator(self, path: Path) -> ConsoleConnection:
        # EOF releases admission asynchronously. Retry admission only; never a
        # console command, so this cannot replay a backend operation.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                connection = ConsoleConnection.connect(str(path))
            except ConsoleClientError as exc:
                if "(busy)" not in str(exc):
                    raise
                time.sleep(0.01)
            else:
                self.addCleanup(connection.close)
                return connection
        self.fail("Operator admission was not released after disconnect")

    def _run_attendee_command(self, channel: paramiko.Channel, command: str) -> None:
        channel.sendall((command + "\n").encode())
        output = self._read_until(channel, "operator@ubuntu:~$ ")
        self.assertIn(f"stub handled: {command}\r\n", output)

    def _read_until(self, channel: paramiko.Channel, needle: str) -> str:
        deadline = time.monotonic() + 5
        output = ""
        channel.settimeout(0.1)
        while time.monotonic() < deadline:
            try:
                chunk = channel.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            output += chunk.decode("utf-8", errors="replace")
            if needle in output:
                return output
        self.fail(f"Did not receive {needle!r}; output was {output!r}")


if __name__ == "__main__":
    unittest.main()
