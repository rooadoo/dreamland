import json
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dreamland.config import ConsoleSettings
from dreamland.console import ConsoleRefresh, ConsoleResult, DreamlandConsole
from dreamland.console_protocol import (
    REQUEST_MAX_BYTES,
    RESPONSE_MAX_BYTES,
    ConsoleProtocolError,
    receive_message,
    send_message,
)
from dreamland.console_socket import ConsoleSocketError, ConsoleSocketServer
from dreamland.runtime_logging import LogRecordBuffer, SessionTraceLogBuffer
from dreamland.state_profiles import StateProfileStore
from dreamland.structured_failures import StructuredFailureStore


class ConsoleProtocolTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.writer = socket.socketpair()
        self.addCleanup(self.reader.close)
        self.addCleanup(self.writer.close)

    def test_unicode_roundtrip_and_adjacent_frames(self):
        first = {"id": 1, "op": "execute", "line": "spiritguide α\nβ"}
        second = {"id": 2, "op": "complete", "line": "lo"}
        send_message(self.writer, first)
        send_message(self.writer, second)
        self.assertEqual(receive_message(self.reader), first)
        self.assertEqual(receive_message(self.reader), second)

    def test_truncated_body_is_distinct_from_clean_eof(self):
        self.writer.sendall(struct.pack("!I", 20) + b"{")
        self.writer.shutdown(socket.SHUT_WR)
        with self.assertRaises(ConsoleProtocolError) as raised:
            receive_message(self.reader)
        self.assertEqual(raised.exception.code, "connection_closed")
        self.assertIsNone(receive_message(self.reader))

    def test_oversized_header_rejected_without_waiting_for_body(self):
        self.writer.sendall(struct.pack("!I", REQUEST_MAX_BYTES + 1))
        with self.assertRaises(ConsoleProtocolError) as raised:
            receive_message(self.reader)
        self.assertEqual(raised.exception.code, "message_too_large")

    def test_invalid_json_types_duplicates_and_nonfinite_numbers_rejected(self):
        for raw in (b"[]", b"null", b'{"id":1,"id":2}', b'{"n":NaN}', b"\xff"):
            with self.subTest(raw=raw):
                self.writer.sendall(struct.pack("!I", len(raw)) + raw)
                with self.assertRaises(ConsoleProtocolError) as raised:
                    receive_message(self.reader)
                self.assertEqual(raised.exception.code, "invalid_message")

    def test_partial_header_and_body_have_bounded_deadline(self):
        for raw in (b"\x00", struct.pack("!I", 20) + b"{"):
            with (
                self.subTest(raw=raw),
                _socket_pair() as pair,
            ):
                reader, writer = pair
                writer.sendall(raw)
                started = time.monotonic()
                with self.assertRaises(ConsoleProtocolError) as raised:
                    receive_message(reader, frame_timeout=0.05)
                self.assertEqual(raised.exception.code, "frame_timeout")
                self.assertLess(time.monotonic() - started, 0.5)

    def test_idle_prompt_does_not_expire_at_partial_frame_timeout(self):
        result = []
        thread = threading.Thread(
            target=lambda: result.append(
                receive_message(self.reader, frame_timeout=0.02)
            ),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        send_message(self.writer, {"op": "hello"})
        thread.join(1)
        self.assertEqual(result, [{"op": "hello"}])

    def test_nonreading_peer_cannot_block_sender_indefinitely(self):
        self.writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        started = time.monotonic()
        with self.assertRaises(ConsoleProtocolError) as raised:
            send_message(self.writer, {"output": "x" * 1000000}, timeout=0.05)
        self.assertEqual(raised.exception.code, "frame_timeout")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_full_duplex_helpers_preserve_socket_mode(self):
        self.reader.settimeout(1.2)
        result = []
        thread = threading.Thread(
            target=lambda: result.append(receive_message(self.reader)),
            daemon=True,
        )
        thread.start()
        send_message(self.reader, {"request": 1})
        self.assertEqual(receive_message(self.writer), {"request": 1})
        send_message(self.writer, {"response": 1})
        thread.join(1)
        self.assertEqual(result, [{"response": 1}])
        self.assertEqual(self.reader.gettimeout(), 1.2)


class ConsoleSocketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dreamland-console-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "run" / "operator.sock"
        self.logs = LogRecordBuffer(512)
        self.logs.setFormatter(logging.Formatter("%(message)s"))
        self.traces = SessionTraceLogBuffer(512)
        self.traces.setFormatter(logging.Formatter("%(message)s"))
        self.shutdown = Mock()
        self.console = DreamlandConsole(
            ConsoleSettings(),
            self.logs,
            self.shutdown,
            trace_logs=self.traces,
            introspection=Mock(),
            state_profiles=StateProfileStore(self.root / "profiles"),
            structured_failure_store=StructuredFailureStore(self.root / "failures"),
        )
        self.server = ConsoleSocketServer(self.console, self.path)
        self.addCleanup(self.server.stop)
        self.next_id = 0

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(str(self.path))
        hello = receive_message(
            connection, max_bytes=RESPONSE_MAX_BYTES, idle_timeout=1
        )
        return connection, hello

    def request(self, connection, operation, **values):
        self.next_id += 1
        send_message(connection, {"id": self.next_id, "op": operation, **values})
        response = receive_message(
            connection, max_bytes=RESPONSE_MAX_BYTES, idle_timeout=1
        )
        self.assertEqual(response["id"], self.next_id)
        return response

    def success(self, connection, operation, **values):
        response = self.request(connection, operation, **values)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def wait_until_available(self):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            connection, hello = self.connect()
            if hello.get("type") == "hello":
                return connection, hello
            self.assertEqual(hello["error"]["code"], "busy")
            connection.close()
            time.sleep(0.005)
        self.fail("console did not release its operator admission")

    def test_private_endpoint_hello_execute_completion_context_and_detach(self):
        self.server.start()
        self.assertTrue(self.server.running)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        connection, hello = self.connect()
        self.assertEqual(hello["version"], 1)
        self.assertEqual(hello["service"], "ssh")
        self.assertEqual(hello["pid"], os.getpid())
        self.assertEqual(hello["runtime_id"], self.server.runtime_id)
        self.assertEqual(
            self.success(connection, "complete", line="log")["completion"], "logging "
        )
        output = self.success(
            connection, "execute", line="sessions --set session-example"
        )
        self.assertEqual(output["context"]["active_session_id"], "session-example")
        self.assertIn("session-example", output["output"])
        connection.close()
        reconnected, next_hello = self.wait_until_available()
        self.assertEqual(next_hello["runtime_id"], hello["runtime_id"])
        output = self.success(reconnected, "execute", line="")
        self.assertIsNone(output["context"]["active_session_id"])
        self.shutdown.assert_not_called()
        self.assertFalse(self.console.shutdown_requested)

    def test_second_operator_is_busy_then_can_reconnect(self):
        self.server.start()
        connection, _ = self.connect()
        rejected, response = self.connect()
        self.assertEqual(response["error"]["code"], "busy")
        self.assertIsNone(receive_message(rejected))
        connection.close()
        accepted, hello = self.wait_until_available()
        self.assertEqual(hello["type"], "hello")
        self.success(accepted, "execute", line="help")

    def test_linux_peer_identity_denies_other_users_and_accepts_root(self):
        self.server.start()
        with patch(
            "dreamland.console_socket._peer_uid", return_value=os.geteuid() + 1000
        ):
            connection, denied = self.connect()
            self.assertEqual(denied["error"]["code"], "access_denied")
            self.assertIsNone(receive_message(connection))
        with patch("dreamland.console_socket._peer_uid", return_value=0):
            _, accepted = self.connect()
            self.assertEqual(accepted["type"], "hello")

    def test_deployed_shutdown_and_prefix_are_intercepted(self):
        self.server.start()
        connection, _ = self.connect()
        for command in (
            "shutdown",
            "!dreamland shutdown",
            "  '!dreamland' 'shutdown' ",
        ):
            with self.subTest(command=command):
                response = self.success(connection, "execute", line=command)
                self.assertIn("dreamlandctl stop", response["output"])
                self.assertFalse(response["shutdown"])
        self.shutdown.assert_not_called()
        self.assertFalse(self.console.shutdown_requested)

    def test_malformed_requests_and_duplicate_ids_never_execute(self):
        self.server.start()
        connection, _ = self.connect()
        with patch.object(self.console, "execute") as execute:
            for operation, values, expected in (
                ("unknown", {}, "unsupported_operation"),
                ("execute", {"line": 7}, "invalid_request"),
                ("execute", {"line": "help", "extra": True}, "invalid_request"),
                ("logs_after", {"sequence": True}, "invalid_request"),
                ("traces_after", {"session_id": "", "sequence": 0}, "invalid_request"),
            ):
                response = self.request(connection, operation, **values)
                self.assertEqual(response["error"]["code"], expected)
            send_message(
                connection, {"id": self.next_id, "op": "execute", "line": "help"}
            )
            duplicate = receive_message(connection)
            self.assertEqual(duplicate["error"]["code"], "invalid_request")
            execute.assert_not_called()

    def test_malformed_frame_is_rejected_and_disconnects_without_execution(self):
        self.server.start()
        connection, _ = self.connect()
        with patch.object(self.console, "execute") as execute:
            connection.sendall(struct.pack("!I", REQUEST_MAX_BYTES + 1))
            response = receive_message(connection)
            self.assertEqual(response["error"]["code"], "message_too_large")
            self.assertIsNone(receive_message(connection))
            execute.assert_not_called()

    def test_unexpected_failures_and_oversized_output_have_stable_errors(self):
        self.server.start()
        connection, _ = self.connect()
        with patch.object(
            self.console, "execute", side_effect=RuntimeError("secret-example-value")
        ):
            with self.assertLogs("dreamland.console", level="WARNING") as logs:
                response = self.request(connection, "execute", line="help")
            self.assertEqual(response["error"]["code"], "operation_failed")
            self.assertNotIn(
                "secret-example-value", json.dumps(response) + str(logs.output)
            )
        with patch.object(
            self.console,
            "execute",
            return_value=ConsoleResult(output="x" * RESPONSE_MAX_BYTES),
        ):
            response = self.request(connection, "execute", line="help")
            self.assertEqual(response["error"]["code"], "response_too_large")
        self.success(connection, "execute", line="help")

    def test_follow_uses_live_bounded_buffers_and_current_refresh_callback(self):
        for index in range(300):
            self.logs.handle(_record(f"log-{index}"))
            self.traces.handle(_record(f"trace-{index}", "session-a"))
        self.server.start()
        connection, _ = self.connect()
        self.assertEqual(self.success(connection, "log_cursor")["sequence"], 300)
        first = self.success(connection, "logs_after", sequence=0)
        self.assertEqual(len(first["records"]), 256)
        second = self.success(connection, "logs_after", sequence=first["sequence"])
        self.assertEqual(len(second["records"]), 44)
        self.assertEqual(second["records"][-1]["text"], "log-299")
        traces = self.success(
            connection, "traces_after", sequence=0, session_id="session-a"
        )
        self.assertEqual(len(traces["records"]), 256)
        self.assertEqual(
            self.success(connection, "trace_cursor", session_id="session-a")[
                "sequence"
            ],
            300,
        )
        with patch.object(self.logs, "after", wraps=self.logs.after) as after:
            self.success(connection, "logs_after", sequence=300)
            self.assertEqual(after.call_args.kwargs["timeout"], 0.1)
        frames = ["first", "second"]
        refresh = ConsoleRefresh("runtime_status", lambda: frames[-1], 1.0)
        with patch.object(
            self.console,
            "execute",
            return_value=ConsoleResult(output=frames[0], refresh=refresh),
        ):
            output = self.success(
                connection, "execute", line="enrichment status --follow"
            )
        self.assertEqual(
            output["refresh"], {"name": "runtime_status", "interval_seconds": 1.0}
        )
        self.assertEqual(self.success(connection, "refresh"), {"output": "second"})
        self.success(connection, "execute", line="help")
        self.assertEqual(
            self.request(connection, "refresh")["error"]["code"], "no_refresh"
        )

    def test_disconnected_mutation_retains_single_admission_until_finished(self):
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def execute(*args, **kwargs):
            entered.set()
            finish.wait(3)
            return ConsoleResult(output="completed")

        self.server.start()
        connection, _ = self.connect()
        with patch.object(self.console, "execute", side_effect=execute) as mocked:
            send_message(connection, {"id": 1, "op": "execute", "line": "mutation"})
            self.assertTrue(entered.wait(1))
            connection.close()
            for _ in range(3):
                rejected, response = self.connect()
                self.assertEqual(response["error"]["code"], "busy")
                rejected.close()
            self.assertEqual(mocked.call_count, 1)
            finish.set()
            accepted, _ = self.wait_until_available()
        self.success(accepted, "execute", line="help")

    def test_stop_closes_idle_transport_cleans_endpoint_and_allows_restart(self):
        self.server.start()
        connection, _ = self.connect()
        self.server.stop()
        self.assertFalse(self.server.running)
        self.assertFalse(self.path.exists())
        self.assertIsNone(receive_message(connection))
        self.assertIsNone(self.server._worker_thread)
        self.server.start()
        restarted, hello = self.connect()
        self.assertEqual(hello["runtime_id"], self.server.runtime_id)
        self.success(restarted, "execute", line="help")

    def test_stop_returns_bounded_with_pending_mutation_and_retains_its_lock(self):
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def execute(*args, **kwargs):
            entered.set()
            finish.wait(3)
            return ConsoleResult()

        self.server.start()
        connection, _ = self.connect()
        with patch.object(self.console, "execute", side_effect=execute):
            send_message(connection, {"id": 1, "op": "execute", "line": "mutation"})
            self.assertTrue(entered.wait(1))
            with patch("dreamland.console_socket.STOP_WAIT_SECONDS", 0.05):
                started = time.monotonic()
                self.server.stop()
                self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(self.path.exists())
            with self.assertRaises(ConsoleSocketError) as raised:
                self.server.start()
            self.assertEqual(raised.exception.code, "already_running")
            replacement = ConsoleSocketServer(self.console, self.path)
            with self.assertRaises(ConsoleSocketError) as raised:
                replacement.start()
            self.assertEqual(raised.exception.code, "endpoint_in_use")
            finish.set()
            self.server.stop()
        replacement.start()
        replacement.stop()

    def test_unsafe_parent_symlink_and_existing_file_are_preserved(self):
        for mode in (0o755, 0o770):
            self.path.parent.mkdir(mode=mode, exist_ok=True)
            self.path.parent.chmod(mode)
            with self.assertRaises(ConsoleSocketError) as raised:
                self.server.start()
            self.assertEqual(raised.exception.code, "unsafe_path")
        self.path.parent.chmod(0o700)
        self.path.write_text("keep this file")
        with self.assertRaises(ConsoleSocketError):
            self.server.start()
        self.assertEqual(self.path.read_text(), "keep this file")
        self.path.unlink()
        target = self.root / "target"
        target.write_text("preserve target")
        self.path.symlink_to(target)
        with self.assertRaises(ConsoleSocketError):
            self.server.start()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), "preserve target")
        alias = self.root / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        aliased = ConsoleSocketServer(self.console, alias / "other.sock")
        with self.assertRaises(ConsoleSocketError):
            aliased.start()

    def test_stale_socket_replaced_but_live_endpoint_and_replacement_file_preserved(
        self,
    ):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
            self.path.chmod(0o600)
        self.server.start()
        other = ConsoleSocketServer(self.console, self.path)
        with self.assertRaises(ConsoleSocketError) as raised:
            other.start()
        self.assertEqual(raised.exception.code, "endpoint_in_use")
        self.path.unlink()
        self.path.write_text("replacement is not our socket")
        self.server.stop()
        self.assertEqual(self.path.read_text(), "replacement is not our socket")

    def test_unmanaged_live_socket_is_not_removed(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(str(self.path))
            self.path.chmod(0o600)
            live.listen(1)
            identity = self.path.stat().st_ino
            with self.assertRaises(ConsoleSocketError) as raised:
                self.server.start()
            self.assertEqual(raised.exception.code, "endpoint_in_use")
            self.assertEqual(self.path.stat().st_ino, identity)

    def test_invalid_paths_and_listener_start_failure_leave_no_endpoint(self):
        for path in (
            Path("relative.sock"),
            self.root / "nul\x00.sock",
            self.root / ".." / "other.sock",
        ):
            with self.subTest(path=path):
                server = ConsoleSocketServer(self.console, path)
                with self.assertRaises(ConsoleSocketError) as raised:
                    server.start()
                self.assertEqual(raised.exception.code, "unsafe_path")
        with patch(
            "dreamland.console_socket.threading.Thread.start",
            side_effect=RuntimeError("no thread"),
        ):
            with self.assertRaises(ConsoleSocketError) as raised:
                self.server.start()
        self.assertEqual(raised.exception.code, "startup_failed")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.server.running)
        self.server.start()

    def test_worker_start_failure_does_not_hold_admission(self):
        self.server.start()
        with patch(
            "dreamland.console_socket.threading.Thread.start",
            side_effect=RuntimeError("no thread"),
        ):
            with self.assertLogs("dreamland.console", level="WARNING"):
                rejected, response = self.connect()
            self.assertEqual(response["error"]["code"], "operation_failed")
            self.assertIsNone(receive_message(rejected))
        _, hello = self.connect()
        self.assertEqual(hello["type"], "hello")

    def test_cleanup_failure_does_not_prevent_transport_shutdown(self):
        self.server.start()
        connection, _ = self.connect()
        with patch("dreamland.console_socket.os.unlink", side_effect=PermissionError):
            with self.assertLogs("dreamland.console", level="WARNING"):
                self.server.stop()
        self.assertIsNone(receive_message(connection))
        self.assertFalse(self.server.running)
        self.server.start()


def _record(message, session_id=None):
    record = logging.LogRecord("test", logging.INFO, "", 0, message, (), None)
    if session_id is not None:
        record.dreamland_session_id = session_id
    return record


class _socket_pair:
    def __enter__(self):
        self.pair = socket.socketpair()
        return self.pair

    def __exit__(self, *args):
        for connection in self.pair:
            connection.close()


if __name__ == "__main__":
    unittest.main()
