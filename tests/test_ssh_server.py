import importlib.util
import logging
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dreamland.models import (
    CommandOutputChunk,
    CommandResult,
    CommandStreamResult,
)

paramiko_available = importlib.util.find_spec("paramiko") is not None
if paramiko_available:
    import paramiko

from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings
from dreamland.console import DreamlandConsole
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer
from dreamland.ssh_server import (
    DreamlandSSHServer,
    SharedPasswordAuthenticator,
    SSHServerConfig,
    _ParamikoServer,
)

TEST_PASSWORD = "not-secret"


class AsyncAgent:
    async def respond(self, context):
        return CommandResult(stdout=f"async handled {context.command}\n")


class RecordingAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        return CommandResult(stdout="recorded\n")


class StreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield CommandOutputChunk("first")
        yield CommandOutputChunk(" line\nsecond")
        yield CommandOutputChunk(" line")
        yield CommandStreamResult(CommandResult(stdout="first line\nsecond line"))


class InterruptibleStreamingAgent:
    def __init__(self):
        self.cancel_seen = False

    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context, control=None):
        yield CommandOutputChunk("rendered line\n")
        yield CommandOutputChunk("buffered partial")
        while control is not None and not control.cancelled():
            time.sleep(0.01)
        self.cancel_seen = True
        if control is not None:
            yield CommandStreamResult(control.rendered_result())


class SlowCleanupInterruptibleAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context, control=None):
        yield CommandOutputChunk("rendered line\n")
        yield CommandOutputChunk("buffered partial")
        while control is not None and not control.cancelled():
            time.sleep(0.01)
        time.sleep(2)
        if control is not None:
            yield CommandStreamResult(control.rendered_result())


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class ParamikoServerPolicyTests(unittest.TestCase):
    def test_lobby_rejects_exec_and_non_pty_shell_before_session_creation(self):
        from unittest.mock import Mock

        for request in ("exec", "shell"):
            with self.subTest(request=request):
                backend = Mock()
                channel = Mock()
                server = _ParamikoServer(
                    backend,
                    SharedPasswordAuthenticator(TEST_PASSWORD),
                    lobby_mode=True,
                )
                if request == "exec":
                    accepted = server.check_channel_exec_request(channel, b"whoami")
                else:
                    accepted = server.check_channel_shell_request(channel)
                self.assertFalse(accepted)
                self.assertEqual(server.wait_for_request(0), "rejected")
                backend.create_session.assert_not_called()
                channel.send_stderr.assert_called_once()
                self.assertIn(
                    b"ssh -t USER@HOST", channel.send_stderr.call_args.args[0]
                )

    def test_lobby_requires_pty_before_shell_and_bounds_terminal_dimensions(self):
        server = _ParamikoServer(
            self.server.backend,
            SharedPasswordAuthenticator(TEST_PASSWORD),
            lobby_mode=True,
        )
        self.assertTrue(
            server.check_channel_pty_request(None, b"xterm", 999999, -1, 0, 0, b"")
        )
        self.assertEqual((server.width, server.height), (240, 24))
        self.assertTrue(server.check_channel_shell_request(None))
        self.assertFalse(
            server.check_channel_pty_request(None, b"xterm", 80, 24, 0, 0, b"")
        )
        self.assertFalse(server.check_channel_shell_request(None))

    def setUp(self):
        profile = server_profile()
        self.server = _ParamikoServer(
            AgentBackend(profile),
            SharedPasswordAuthenticator(TEST_PASSWORD),
        )

    def test_advertises_and_accepts_only_password_authentication(self):
        key = paramiko.RSAKey.generate(1024)

        self.assertEqual(self.server.get_allowed_auths("alice"), "password")
        self.assertEqual(
            self.server.check_auth_password("name@example.test", TEST_PASSWORD),
            paramiko.AUTH_SUCCESSFUL,
        )
        self.assertEqual(self.server.username, "nameexampletest")
        self.assertEqual(
            self.server.check_auth_password("alice", "wrong"),
            paramiko.AUTH_FAILED,
        )
        self.assertEqual(
            self.server.check_auth_publickey("alice", key),
            paramiko.AUTH_FAILED,
        )
        self.assertEqual(
            self.server.check_auth_none("alice"),
            paramiko.AUTH_FAILED,
        )
        self.assertEqual(
            self.server.check_auth_interactive("alice", ""),
            paramiko.AUTH_FAILED,
        )
        self.assertEqual(
            self.server.check_auth_interactive_response([TEST_PASSWORD]),
            paramiko.AUTH_FAILED,
        )
        self.assertFalse(self.server.enable_auth_gssapi())
        self.assertEqual(
            self.server.check_auth_gssapi_keyex("alice"),
            paramiko.AUTH_FAILED,
        )
        self.assertEqual(
            self.server.check_auth_gssapi_with_mic("alice"),
            paramiko.AUTH_FAILED,
        )

    def test_failed_password_does_not_replace_authenticated_username(self):
        self.assertEqual(
            self.server.check_auth_password("alice", TEST_PASSWORD),
            paramiko.AUTH_SUCCESSFUL,
        )

        self.server.check_auth_password("mallory", "wrong")

        self.assertEqual(self.server.username, "alice")

    def test_lobby_authentication_logs_exact_username_with_controls_escaped(self):
        for username in (
            "name@example.test",
            "Δ-user",
            "long" * 40,
            "unsafe\n\r\x1b[31m\t\u2028\u202euser",
        ):
            with self.subTest(username=username):
                server = _ParamikoServer(
                    self.server.backend,
                    SharedPasswordAuthenticator(TEST_PASSWORD),
                    lobby_mode=True,
                )
                with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
                    result = server.check_auth_password(username, TEST_PASSWORD)
                self.assertEqual(result, paramiko.AUTH_SUCCESSFUL)
                self.assertEqual(server.ssh_username, username)
                self.assertEqual(len(logs.records), 1)
                message = logs.records[0].getMessage()
                self.assertIn(f"ssh_username={username!r}", message)
                self.assertIn(f"parent={server.parent_connection_id}", message)
                for control in ("\n", "\r", "\x1b", "\t", "\u2028", "\u202e"):
                    self.assertNotIn(control, message)
                self.assertNotIn(TEST_PASSWORD, message)

                with self.assertNoLogs("dreamland.lifecycle", level="INFO"):
                    result = server.check_auth_password("different", "wrong")
                self.assertEqual(result, paramiko.AUTH_FAILED)
                self.assertEqual(server.ssh_username, username)

    def test_direct_authentication_keeps_existing_identity_and_logging(self):
        with self.assertNoLogs("dreamland.lifecycle", level="INFO"):
            self.server.check_auth_password("name@example.test", TEST_PASSWORD)
        self.assertEqual(self.server.username, "nameexampletest")
        self.assertIsNone(self.server.ssh_username)
        self.assertIsNone(self.server.parent_connection_id)

    def test_simple_lobby_authentication_preserves_delay_and_audit_identity(self):
        authenticator = Mock(
            wraps=SharedPasswordAuthenticator(
                TEST_PASSWORD, required_username="dreamland", allow_simple_login=True
            )
        )
        server = _ParamikoServer(
            self.server.backend,
            authenticator,
            lobby_mode=True,
            password_check_delay_seconds=2,
        )
        with patch("dreamland.ssh_transport.time.sleep") as delay:
            with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
                self.assertEqual(
                    server.check_auth_password("dreamland_simple", TEST_PASSWORD),
                    paramiko.AUTH_SUCCESSFUL,
                )
            self.assertEqual(server.ssh_username, "dreamland_simple")
            self.assertIn("ssh_username='dreamland_simple'", logs.output[0])
            self.assertNotIn(TEST_PASSWORD, logs.output[0])
            delay.assert_called_once_with(2)
            authenticator.authenticate_password.assert_called_once_with(
                "dreamland_simple", TEST_PASSWORD
            )
            for username, password in (
                ("other_simple", TEST_PASSWORD),
                ("dreamland_simple", "wrong"),
            ):
                delay.reset_mock()
                authenticator.reset_mock()
                with self.assertNoLogs("dreamland.lifecycle", level="INFO"):
                    self.assertEqual(
                        server.check_auth_password(username, password),
                        paramiko.AUTH_FAILED,
                    )
                self.assertEqual(server.ssh_username, "dreamland_simple")
                delay.assert_called_once_with(2)
                authenticator.authenticate_password.assert_called_once_with(
                    username, password
                )

    def test_direct_simple_suffix_remains_part_of_simulated_username(self):
        self.assertEqual(
            self.server.check_auth_password("alice_simple", TEST_PASSWORD),
            paramiko.AUTH_SUCCESSFUL,
        )
        self.assertEqual(self.server.username, "alice_simple")
        self.assertIsNone(self.server.ssh_username)

    def test_rejects_unsupported_channels_and_forwarding(self):
        self.assertEqual(
            self.server.check_channel_direct_tcpip_request(
                1,
                ("127.0.0.1", 50000),
                ("127.0.0.1", 80),
            ),
            paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
        )
        self.assertFalse(self.server.check_port_forward_request("127.0.0.1", 0))
        self.assertIsNone(self.server.cancel_port_forward_request("127.0.0.1", 2222))
        self.assertFalse(self.server.check_global_request("vendor-request", None))
        self.assertFalse(self.server.check_channel_subsystem_request(None, "sftp"))
        self.assertFalse(self.server.check_channel_forward_agent_request(None))
        self.assertFalse(
            self.server.check_channel_x11_request(
                None,
                False,
                "MIT-MAGIC-COOKIE-1",
                b"cookie",
                0,
            )
        )
        self.assertFalse(self.server.check_channel_env_request(None, b"PATH", b"/tmp"))

    def test_accepts_only_one_session_channel(self):
        self.assertEqual(
            self.server.check_channel_request("session", 1),
            paramiko.OPEN_SUCCEEDED,
        )
        self.assertEqual(
            self.server.check_channel_request("session", 2),
            paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
        )
        self.assertEqual(
            self.server.check_channel_request("forwarded-tcpip", 3),
            paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
        )

    def test_accepts_only_one_shell_or_exec_request(self):
        self.assertTrue(self.server.check_channel_shell_request(None))

        self.assertFalse(self.server.check_channel_exec_request(None, b"whoami"))
        self.assertFalse(self.server.check_channel_subsystem_request(None, "sftp"))
        self.assertEqual(self.server.wait_for_request(0), "shell")
        self.assertIsNone(self.server.exec_command)

    def test_paramiko_debug_auth_request_with_raw_username_is_filtered(self):
        logger = logging.getLogger("dreamland.ssh_server.paramiko.transport")

        with self.assertNoLogs(logger.name, level="DEBUG"):
            logger.debug(
                "Auth request (type=password) service=ssh-connection, "
                "username=unsafe\nusername"
            )


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class DreamlandSSHServerTests(unittest.TestCase):
    def test_lobby_mode_mismatch_fails_before_backend_or_listener(self):
        config = self._server_config(server_profile())
        config.entry_mode = "lobby"
        with self.assertRaisesRegex(ValueError, "configured together"):
            DreamlandSSHServer(config, authenticator=self.authenticator)

    def test_lobby_transport_only_dispatches_pty_shell_and_reuses_parent_channel(self):
        from unittest.mock import Mock

        self.server.stop()
        backend = Mock()

        class Lobby:
            calls = []

            def run(
                lobby_self, channel, username, received_backend, metadata, **kwargs
            ):
                lobby_self.calls.append((username, received_backend, kwargs))
                channel.send(b"picker one\r\n")
                self.assertEqual(channel.recv(1), b"1")
                channel.send(b"visit one ended\r\npicker two\r\n")
                self.assertEqual(channel.recv(1), b"q")
                channel.send_exit_status(0)

        lobby = Lobby()
        config = self._server_config(server_profile())
        config.entry_mode = "lobby"
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
            lobby=lobby,
        )
        self.server.start_in_thread()
        self._wait_for_port()
        for kind in ("exec", "non_pty_shell"):
            with self.subTest(kind=kind):
                client = self._connect(password=TEST_PASSWORD)
                try:
                    channel = client.get_transport().open_session()
                    with self.assertRaises((paramiko.SSHException, EOFError, OSError)):
                        if kind == "exec":
                            channel.exec_command("whoami")
                        else:
                            channel.invoke_shell()
                    self.assertIn(b"ssh -t USER@HOST", channel.recv_stderr(1024))
                finally:
                    client.close()
        backend.create_session.assert_not_called()
        self.assertEqual(lobby.calls, [])

        with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
            client = self._connect(password=TEST_PASSWORD, username="name!unsafe")
            try:
                channel = client.invoke_shell(width=95, height=40)
                self.assertIn("picker one", self._read_until(channel, "picker one"))
                channel.send(b"1")
                self.assertIn("picker two", self._read_until(channel, "picker two"))
                channel.send(b"q")
                self.assertEqual(channel.recv_exit_status(), 0)
            finally:
                client.close()
        self.assertEqual(len(lobby.calls), 1)
        self.assertEqual(lobby.calls[0][0], "name!unsafe")
        parent_id = lobby.calls[0][2]["parent_connection_id"]
        self.assertRegex(parent_id, r"^[0-9a-f]{32}$")
        self.assertTrue(
            any(
                f"parent={parent_id} ssh_username='name!unsafe'" in record.getMessage()
                for record in logs.records
            )
        )
        self.assertIs(lobby.calls[0][1], backend)
        self.assertEqual(
            (lobby.calls[0][2]["width"], lobby.calls[0][2]["height"]), (95, 40)
        )
        backend.create_session.assert_not_called()

    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.host_key_path = Path(self._temporary_directory.name) / "ssh_host_rsa_key"
        profile = server_profile()
        config = self._server_config(profile)
        self.authenticator = SharedPasswordAuthenticator(TEST_PASSWORD)
        self.server = DreamlandSSHServer(
            config,
            AgentBackend(profile),
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

    def tearDown(self):
        self.server.stop()
        self._temporary_directory.cleanup()

    def test_exec_command_routes_to_deterministic_backend(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="alice",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        _, stdout, stderr = client.exec_command("whoami")
        self.assertEqual(stdout.read().decode("utf-8"), "alice\n")
        self.assertEqual(stderr.read().decode("utf-8"), "")
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        client.close()

    def test_known_host_trust_survives_server_restart(self):
        port = self.server.bound_port
        original_fingerprint = self.server.host_key_fingerprint
        trusted_key = paramiko.RSAKey.from_private_key_file(str(self.host_key_path))
        original_bytes = self.host_key_path.read_bytes()
        self.server.stop()

        profile = server_profile()
        self.server = DreamlandSSHServer(
            self._server_config(profile, port=port),
            AgentBackend(profile),
            authenticator=self.authenticator,
        )
        self.assertEqual(self.server.host_key_algorithm, "ssh-rsa")
        self.assertEqual(self.server.host_key_fingerprint, original_fingerprint)
        self.assertEqual(self.host_key_path.read_bytes(), original_bytes)
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.get_host_keys().add(
            f"[127.0.0.1]:{port}",
            trusted_key.get_name(),
            trusted_key,
        )
        client.connect(
            "127.0.0.1",
            port=port,
            username="alice",
            password=TEST_PASSWORD,
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )
        _, stdout, _ = client.exec_command("whoami")
        self.assertEqual(stdout.read().decode(), "alice\n")
        client.close()

    def test_deliberate_rotation_is_rejected_by_old_known_host_entry(self):
        port = self.server.bound_port
        original_fingerprint = self.server.host_key_fingerprint
        trusted_key = paramiko.RSAKey.from_private_key_file(str(self.host_key_path))
        self.server.stop()
        self.host_key_path.unlink()

        profile = server_profile()
        self.server = DreamlandSSHServer(
            self._server_config(profile, port=port),
            AgentBackend(profile),
            authenticator=self.authenticator,
        )
        self.assertNotEqual(self.server.host_key_fingerprint, original_fingerprint)
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.get_host_keys().add(
            f"[127.0.0.1]:{port}",
            trusted_key.get_name(),
            trusted_key,
        )
        with self.assertRaises(paramiko.BadHostKeyException):
            client.connect(
                "127.0.0.1",
                port=port,
                username="alice",
                password=TEST_PASSWORD,
                look_for_keys=False,
                allow_agent=False,
                timeout=5,
            )
        client.close()

    def test_arbitrary_username_is_accepted_and_safely_normalized(self):
        client = self._connect(
            username="name@example.test",
            password=TEST_PASSWORD,
        )

        _, stdout, stderr = client.exec_command("whoami")

        self.assertEqual(stdout.read().decode(), "nameexampletest\n")
        self.assertEqual(stderr.read().decode(), "")
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        client.close()

    def test_wrong_and_empty_passwords_are_rejected(self):
        for password in ("wrong", ""):
            with self.subTest(password=password):
                transport = self._raw_transport()
                try:
                    with self.assertRaises(paramiko.AuthenticationException):
                        transport.auth_password("alice", password)
                    self.assertFalse(transport.is_authenticated())
                finally:
                    transport.close()

    def test_public_key_is_rejected_then_password_can_authenticate(self):
        transport = self._raw_transport()
        try:
            key = paramiko.RSAKey.generate(1024)
            with self.assertRaises(paramiko.AuthenticationException):
                transport.auth_publickey("alice", key)

            transport.auth_password("alice", TEST_PASSWORD)

            self.assertTrue(transport.is_authenticated())
        finally:
            transport.close()

    def test_none_and_keyboard_interactive_authentication_are_rejected(self):
        transport = self._raw_transport()
        try:
            with self.assertRaises(paramiko.AuthenticationException) as none_error:
                transport.auth_none("alice")
            self.assertEqual(none_error.exception.allowed_types, ["password"])

            with self.assertRaises(paramiko.AuthenticationException):
                transport.auth_interactive(
                    "alice", lambda title, instructions, prompts: []
                )
        finally:
            transport.close()

    def test_sftp_is_rejected_without_running_a_command(self):
        agent, _ = self._restart_with_console()
        client = self._connect(password=TEST_PASSWORD)

        # Paramiko may surface the server-side rejection directly or observe
        # the transport closing first; all are valid denial outcomes.
        with self.assertRaises((paramiko.SSHException, EOFError, OSError)):
            client.open_sftp()

        self.assertEqual(agent.contexts, [])
        client.close()
        followup = self._connect(password=TEST_PASSWORD)
        _, stdout, _ = followup.exec_command("whoami")
        self.assertEqual(stdout.read().decode(), "alice\n")
        followup.close()

    def test_local_and_remote_port_forwarding_are_rejected(self):
        client = self._connect(password=TEST_PASSWORD)
        transport = client.get_transport()
        self.assertIsNotNone(transport)
        assert transport is not None

        with self.assertRaises(paramiko.ChannelException) as direct_error:
            transport.open_channel(
                "direct-tcpip",
                ("127.0.0.1", 9),
                ("127.0.0.1", 50000),
                timeout=5,
            )
        self.assertEqual(
            direct_error.exception.code,
            paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
        )
        with self.assertRaises(paramiko.SSHException):
            transport.request_port_forward("127.0.0.1", 0)
        # Paramiko acknowledges the protocol's cancellation request even when
        # no forwarding was established; DREAMLAND keeps it as a safe no-op.
        transport.cancel_port_forward("127.0.0.1", 65535)

        _, stdout, _ = client.exec_command("whoami")
        self.assertEqual(stdout.read().decode(), "alice\n")
        client.close()

    def test_second_session_channel_on_one_transport_is_rejected(self):
        client = self._connect(password=TEST_PASSWORD)
        transport = client.get_transport()
        self.assertIsNotNone(transport)
        assert transport is not None
        first = transport.open_session(timeout=5)
        try:
            with self.assertRaises(paramiko.ChannelException):
                transport.open_session(timeout=5)
            first.exec_command("whoami")
            self.assertEqual(first.makefile("r").read().decode(), "alice\n")
        finally:
            client.close()

    def test_exec_operator_command_is_denied_without_reaching_agent(self):
        agent, console = self._restart_with_console()
        client = self._connect(password=TEST_PASSWORD)

        _, stdout, stderr = client.exec_command("!dreamland shutdown")

        self.assertEqual(stdout.read().decode(), "")
        self.assertEqual(
            stderr.read().decode(),
            "error: Dreamland operator commands are unavailable over SSH\n",
        )
        self.assertEqual(stderr.channel.recv_exit_status(), 126)
        self.assertEqual(agent.contexts, [])
        self.assertFalse(console.shutdown_requested)
        self.assertIsNotNone(self.server._serve_thread)
        assert self.server._serve_thread is not None
        self.assertTrue(self.server._serve_thread.is_alive())
        client.close()

    def test_shell_operator_command_is_denied_and_shell_remains_available(self):
        agent, _ = self._restart_with_console()
        client = self._connect(password=TEST_PASSWORD)
        channel = client.invoke_shell()
        self._read_until(channel, "alice@ubuntu:~$ ")

        channel.send("  !dreamland logging -n 1\n")
        denied = self._read_until(channel, "alice@ubuntu:~$ ")
        channel.send("uname -a\n")
        command_output = self._read_until(channel, "alice@ubuntu:~$ ")

        self.assertIn("operator commands are unavailable over SSH", denied)
        self.assertEqual([context.command for context in agent.contexts], ["uname -a"])
        self.assertIn("recorded", command_output)
        channel.send("exit\n")
        client.close()

    def test_client_disconnect_during_cleanup_is_not_logged_as_error(self):
        with self.assertNoLogs("dreamland.ssh_server", level="ERROR"):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                "127.0.0.1",
                port=self.server.bound_port,
                username="alice",
                password="not-secret",
                look_for_keys=False,
                allow_agent=False,
                timeout=5,
            )

            _, stdout, stderr = client.exec_command("whoami")
            self.assertEqual(stdout.read().decode("utf-8"), "alice\n")
            self.assertEqual(stderr.read().decode("utf-8"), "")
            self.assertEqual(stdout.channel.recv_exit_status(), 0)
            client.close()
            self.server.stop()

    def test_exec_command_supports_async_agent(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        backend = AgentBackend(profile, agent=AsyncAgent())
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="alice",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        _, stdout, stderr = client.exec_command("date")
        self.assertEqual(stdout.read().decode("utf-8"), "async handled date\n")
        self.assertEqual(stderr.read().decode("utf-8"), "")
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        client.close()

    def test_exec_session_records_generic_connection_metadata(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="alice",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        _, stdout, stderr = client.exec_command("netstat -ant")
        self.assertEqual(stdout.read().decode("utf-8"), "recorded\n")
        self.assertEqual(stderr.read().decode("utf-8"), "")
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        client.close()

        connection_metadata = agent.contexts[-1].environment_metadata.connection
        self.assertIsNotNone(connection_metadata)
        self.assertEqual(connection_metadata.protocol, "ssh")
        self.assertEqual(connection_metadata.source.ip, "198.51.100.23")
        self.assertIsInstance(connection_metadata.source.port, int)
        self.assertGreater(connection_metadata.source.port, 0)
        self.assertIsNotNone(connection_metadata.destination.ip)
        self.assertNotEqual(connection_metadata.destination.ip, "127.0.0.1")
        self.assertEqual(connection_metadata.destination.port, 22)
        observed = agent.contexts[-1].transport_connection.metadata["transport"]
        self.assertEqual(observed["destination"]["ip"], "127.0.0.1")
        self.assertEqual(observed["destination"]["port"], self.server.bound_port)

    def test_exec_command_streams_line_buffered_agent_output(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        backend = AgentBackend(profile, agent=StreamingAgent())
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="alice",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        _, stdout, stderr = client.exec_command("python manage.py shell")
        self.assertEqual(stdout.read().decode("utf-8"), "first line\nsecond line")
        self.assertEqual(stderr.read().decode("utf-8"), "")
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        client.close()

    def test_shell_accepts_interactive_commands(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="bob",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        channel = client.invoke_shell()
        self._read_until(channel, "bob@ubuntu:~$ ")
        channel.send("pwd\n")
        output = self._read_until(channel, "bob@ubuntu:~$ ")
        self.assertIn("/home/bob", output)
        channel.send("exit\n")
        client.close()

    def test_shell_ctrl_c_interrupts_running_stream(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        agent = InterruptibleStreamingAgent()
        backend = AgentBackend(profile, agent=agent)
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="bob",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        channel = client.invoke_shell()
        self._read_until(channel, "bob@ubuntu:~$ ")
        channel.send("slow command\n")
        first_output = self._read_until(channel, "rendered line\r\n")
        with self.assertLogs("dreamland.commands", level="WARNING") as captured:
            channel.send("\x03")
            interrupted_output = self._read_until(channel, "^C\r\nbob@ubuntu:~$ ")

        self.assertIn("rendered line\r\n", first_output)
        self.assertNotIn("buffered partial", first_output + interrupted_output)
        self.assertTrue(agent.cancel_seen)
        self.assertIn("command='slow command'", captured.output[0])
        self.assertIn("interrupted=true", captured.output[0])
        self.assertIn("signal=SIGINT", captured.output[0])
        channel.send("exit\n")
        client.close()

    def test_shell_ctrl_c_returns_prompt_before_stream_cleanup_finishes(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        backend = AgentBackend(profile, agent=SlowCleanupInterruptibleAgent())
        self.server = DreamlandSSHServer(
            config,
            backend,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username="bob",
            password="not-secret",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

        channel = client.invoke_shell()
        self._read_until(channel, "bob@ubuntu:~$ ")
        channel.send("slow command\n")
        self._read_until(channel, "rendered line\r\n")
        started = time.monotonic()
        channel.send("\x03")
        output = self._read_until(channel, "^C\r\nbob@ubuntu:~$ ")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertNotIn("buffered partial", output)
        client.close()

    def _wait_for_port(self):
        self.assertTrue(
            self.server.wait_until_ready(5), "server did not start listening"
        )

    def _restart_with_console(self):
        self.server.stop()
        profile = server_profile()
        config = self._server_config(profile)
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        settings = ConsoleSettings()
        logs = LogRecordBuffer(settings.log_capacity)
        logs.setFormatter(logging.Formatter("%(message)s"))
        console = DreamlandConsole(
            settings,
            logs,
            lambda: self.server.request_stop(),
        )
        self.server = DreamlandSSHServer(
            config,
            backend,
            console,
            authenticator=self.authenticator,
        )
        self.server.start_in_thread()
        self._wait_for_port()
        return agent, console

    def _server_config(self, profile, *, port=0):
        return SSHServerConfig(
            host="127.0.0.1",
            port=port,
            profile=profile,
            host_key_path=self.host_key_path,
            password_check_delay_seconds=0,
        )

    def _connect(self, *, password, username="alice"):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=self.server.bound_port,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )
        return client

    def _raw_transport(self):
        client_socket = socket.create_connection(
            ("127.0.0.1", self.server.bound_port),
            timeout=5,
        )
        transport = paramiko.Transport(client_socket)
        transport.start_client(timeout=5)
        return transport

    def _read_until(self, channel, needle):
        deadline = time.monotonic() + 5
        data = ""
        while time.monotonic() < deadline:
            if channel.recv_ready():
                data += channel.recv(4096).decode("utf-8", errors="replace")
                if needle in data:
                    return data
            time.sleep(0.01)
        self.fail(f"did not receive {needle!r}; received {data!r}")


if __name__ == "__main__":
    unittest.main()
