import errno
import importlib.util
import logging
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dreamland.ssh_auth import SharedPasswordAuthenticator
from dreamland.ssh_handshake import HandshakeTransport, classify_handshake_failure
from dreamland.ssh_transport import (
    DreamlandSSHServer,
    SSHServerConfig,
    _ParamikoServer,
)

paramiko_available = importlib.util.find_spec("paramiko") is not None
if paramiko_available:
    import paramiko


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class HandshakeClassificationTests(unittest.TestCase):
    def test_allowlist_does_not_hide_internal_or_post_handshake_errors(self):
        for error in (
            ValueError("broken invariant"),
            OSError(errno.EBADF, "bad descriptor"),
            paramiko.SSHException("unrecognized protocol error"),
            paramiko.SSHException("Error reading SSH protocol banner"),
        ):
            with self.subTest(error=repr(error)):
                self.assertIsNone(classify_handshake_failure(error, "banner"))
        self.assertIsNone(classify_handshake_failure(EOFError(), "established"))
        self.assertIsNone(
            classify_handshake_failure(
                paramiko.SSHException("Invalid SSH banner"), "key_exchange"
            )
        )

    def test_wrapped_banner_errors_use_typed_cause_not_message_alone(self):
        for cause, reason in (
            (EOFError(), "peer_closed"),
            (TimeoutError(), "timeout"),
            (ConnectionResetError(errno.ECONNRESET, "reset"), "peer_reset"),
            (UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid"), "invalid_banner"),
            (ValueError("internal error"), None),
            (OSError(errno.EBADF, "bad descriptor"), None),
        ):
            with self.subTest(cause=repr(cause)):
                error = paramiko.SSHException("Error reading SSH protocol banner")
                error.__context__ = cause
                result = classify_handshake_failure(error, "banner")
                self.assertEqual(result.reason if result else None, reason)

    def test_established_transport_retains_paramiko_error_output(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        transport = HandshakeTransport(left)
        self.addCleanup(transport.close)
        transport.initial_kex_done = True
        transport.logger = Mock()
        try:
            raise paramiko.ssh_exception.IncompatiblePeer("test rekey failure")
        except paramiko.SSHException:
            transport._log(logging.ERROR, "Exception (server): test rekey failure")
            transport._log(logging.ERROR, ["test traceback"])
        self.assertEqual(transport.logger.log.call_count, 2)


class _FastHandshakeTransport(HandshakeTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only test deadlines change; production retains Paramiko's defaults.
        self.banner_timeout = 0.3
        self.handshake_timeout = 0.5


@unittest.skipUnless(paramiko_available, "Paramiko is not installed")
class HandshakeSocketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.backend = Mock()
        self.server = DreamlandSSHServer(
            SSHServerConfig(
                port=0,
                host_key_path=Path(temporary.name) / "host_key",
                socket_timeout_seconds=0.05,
                password_check_delay_seconds=0,
            ),
            backend=self.backend,
            authenticator=SharedPasswordAuthenticator("test-password"),
        )
        patcher = patch(
            "dreamland.ssh_transport.HandshakeTransport", _FastHandshakeTransport
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.start_in_thread()
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_ready())

    def _connect(self, *, read_banner=True):
        peer = socket.create_connection(("127.0.0.1", self.server.bound_port), 3)
        self.addCleanup(peer.close)
        if read_banner:
            self.assertTrue(peer.recv(1024).startswith(b"SSH-"))
        return peer

    def _wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("SSH handler did not finish in time")

    @staticmethod
    def _events(captured):
        return [
            record
            for record in captured.records
            if getattr(record, "event", None) == "ssh_handshake_failed"
        ]

    def _finish(self, captured, count=1):
        self._wait(lambda: len(self._events(captured)) >= count)
        self._wait(
            lambda: (
                not self.server._client_threads and not self.server._connections_per_ip
            )
        )
        self.assertEqual(self.server._connections_per_ip, {})
        self.backend.create_session.assert_not_called()
        return self._events(captured)

    def _assert_expected(self, captured, reason, *, phase="banner", warning=False):
        events = self._finish(captured)
        self.assertEqual(len(events), 1)
        record = events[0]
        self.assertEqual(record.reason, reason)
        self.assertEqual(record.phase, phase)
        self.assertEqual(record.peer_ip, "127.0.0.1")
        self.assertGreater(record.peer_port, 0)
        self.assertRegex(record.connection_id, r"^[0-9a-f]{32}$")
        self.assertGreaterEqual(record.elapsed_ms, 0)
        self.assertIsNone(record.exc_info)
        self.assertEqual(record.levelno, logging.WARNING if warning else logging.INFO)
        self.assertFalse([r for r in captured.records if r.levelno >= logging.ERROR])
        self.assertNotIn("ATTACKER", "\n".join(captured.output))
        return record

    def test_connect_then_close_is_one_event_and_releases_slot(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            peer = self._connect()
            port = peer.getsockname()[1]
            peer.shutdown(socket.SHUT_WR)
            record = self._assert_expected(captured, "peer_closed")
            self.assertEqual(record.peer_port, port)
            self.assertEqual(peer.recv(1024), b"")

    def test_immediate_full_connect_scan_has_no_error_traceback(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            self._connect(read_banner=False).close()
            events = self._finish(captured)
            self.assertEqual(len(events), 1)
            # Whether the server's greeting reached TCP before the close is a race.
            self.assertIn(events[0].reason, {"peer_closed", "peer_reset"})
            self.assertFalse(
                [r for r in captured.records if r.levelno >= logging.ERROR]
            )

    def test_tcp_reset_is_one_event(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            peer = self._connect()
            peer.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            peer.close()
            self._assert_expected(captured, "peer_reset")

    def test_silent_client_times_out(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            self._connect()
            self._assert_expected(captured, "timeout")

    def test_incomplete_banner_times_out_without_echoing_bytes(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            self._connect().sendall(b"SSH-2.0-ATTACKER")
            self._assert_expected(captured, "timeout")

    def test_stalled_key_exchange_cleans_up_without_error_traceback(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            self._connect().sendall(b"SSH-2.0-ATTACKER\r\n")
            # Paramiko's packetizer reports expiry here as EOF, not TimeoutError.
            self._assert_expected(captured, "peer_closed", phase="key_exchange")

    def test_lobby_probe_reuses_parent_id_without_entering_lobby(self):
        self.server.config.entry_mode = "lobby"
        self.server.lobby = Mock()
        server_interfaces = []

        def create_server(*args, **kwargs):
            interface = _ParamikoServer(*args, **kwargs)
            server_interfaces.append(interface)
            return interface

        with patch("dreamland.ssh_transport._ParamikoServer", create_server):
            with self.assertLogs("dreamland", level="DEBUG") as captured:
                peer = self._connect()
                peer.shutdown(socket.SHUT_WR)
                record = self._assert_expected(captured, "peer_closed")
        self.assertEqual(len(server_interfaces), 1)
        self.assertEqual(
            record.connection_id, server_interfaces[0].parent_connection_id
        )
        self.assertIsNone(server_interfaces[0].ssh_username)
        self.server.lobby.run.assert_not_called()

    def test_successful_handshake_and_failed_auth_are_not_handshake_failures(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            peer = self._connect(read_banner=False)
            peer.sendall(b"ATTACKER preamble\x1b[31m\r\n")
            client = paramiko.Transport(peer)
            self.addCleanup(client.close)
            client.local_version = "SSH-2.0-ATTACKER\x1b[31m"
            client.start_client(timeout=3)
            with self.assertRaises(paramiko.AuthenticationException):
                client.auth_password("tester", "wrong-password")
            self.assertFalse(client.is_authenticated())
            client.auth_password("tester", "test-password")
            self.assertTrue(client.is_authenticated())
            client.close()
            self.assertEqual(self._finish(captured, count=0), [])
            self.assertFalse(
                [r for r in captured.records if r.levelno >= logging.ERROR]
            )
            self.assertNotIn("ATTACKER", "\n".join(captured.output))

    def test_wrong_protocol_and_malformed_banners_do_not_echo_client_bytes(self):
        for payload, reason in (
            (b"GET /ATTACKER HTTP/1.1\r\nHost: ATTACKER\r\n\r\n", "peer_closed"),
            (b"\xffATTACKER\n", "invalid_banner"),
            (b"ATTACKER\x1b[31m\n" * 100, "invalid_banner"),
            (b"SSH-ATTACKER\r\n", "invalid_banner"),
        ):
            with self.subTest(payload=payload[:40]):
                with self.assertLogs("dreamland", level="DEBUG") as captured:
                    peer = self._connect()
                    peer.sendall(payload)
                    peer.shutdown(socket.SHUT_WR)
                    self._assert_expected(captured, reason)
                    peer.close()

    def test_incompatible_protocol_is_warning_without_raw_banner(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            self._connect().sendall(b"SSH-1.5-ATTACKER\x1b[31m\r\n")
            self._assert_expected(captured, "incompatible_peer", warning=True)

    def test_close_after_valid_banner_is_key_exchange_failure(self):
        with self.assertLogs("dreamland", level="DEBUG") as captured:
            peer = self._connect()
            peer.sendall(b"SSH-2.0-ATTACKER\x1b[31m\r\n")
            peer.shutdown(socket.SHUT_WR)
            self._assert_expected(captured, "peer_closed", phase="key_exchange")

    def test_incompatible_algorithms_are_warning(self):
        class NoSharedKexTransport(_FastHandshakeTransport):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.get_security_options().kex = ("diffie-hellman-group14-sha256",)

        with patch("dreamland.ssh_transport.HandshakeTransport", NoSharedKexTransport):
            with self.assertLogs("dreamland", level="DEBUG") as captured:
                client = paramiko.Transport(self._connect(read_banner=False))
                self.addCleanup(client.close)
                client.get_security_options().kex = ("ecdh-sha2-nistp256",)
                # Capture the independent client's expected error output, too.
                with self.assertLogs("paramiko.transport", level="ERROR"):
                    with self.assertRaises(paramiko.SSHException):
                        client.start_client(timeout=3)
                self._assert_expected(
                    captured, "incompatible_peer", phase="key_exchange", warning=True
                )

    def test_wrapped_internal_error_still_has_traceback(self):
        with patch.object(
            paramiko.packet.Packetizer,
            "readline",
            side_effect=ValueError("internal sentinel"),
        ):
            with self.assertLogs("dreamland", level="DEBUG") as captured:
                self._connect()
                events = self._finish(captured)
                self.assertEqual(events[0].reason, "unexpected_error")
                self.assertEqual(events[0].levelno, logging.ERROR)
                self.assertIsNotNone(events[0].exc_info)
                self.assertIn(
                    "ValueError: internal sentinel", "\n".join(captured.output)
                )
                self.assertTrue(
                    any(
                        r.name == "dreamland.ssh_server.paramiko.transport"
                        and r.levelno == logging.ERROR
                        for r in captured.records
                    )
                )

    def test_concurrent_probe_does_not_hide_another_connections_error_or_auth(self):
        class BrokenKexTransport(_FastHandshakeTransport):
            def _check_banner(self):
                super()._check_banner()
                if self.remote_version == "SSH-2.0-internal-test":
                    raise RuntimeError("independent internal sentinel")

        with patch("dreamland.ssh_transport.HandshakeTransport", BrokenKexTransport):
            with self.assertLogs("dreamland", level="DEBUG") as captured:
                silent = self._connect()
                broken = self._connect()
                broken.sendall(b"SSH-2.0-internal-test\r\n")
                client = paramiko.Transport(self._connect(read_banner=False))
                self.addCleanup(client.close)
                client.start_client(timeout=3)
                client.auth_password("tester", "test-password")
                self.assertTrue(client.is_authenticated())
                client.close()
                events = self._finish(captured, count=2)
                self.assertEqual(
                    {r.reason for r in events}, {"timeout", "unexpected_error"}
                )
                self.assertEqual(len({r.connection_id for r in events}), 2)
                self.assertIn(
                    "RuntimeError: independent internal sentinel",
                    "\n".join(captured.output),
                )
                silent.close()
                broken.close()


if __name__ == "__main__":
    unittest.main()
