from __future__ import annotations

import io
import os
import socket
import struct
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import patch

from prompt_toolkit.document import Document

from dreamland.console import CONSOLE_REFRESH_EXIT_SCREEN, ConsoleContext
from dreamland.console_client import (
    ConsoleClientError,
    ConsoleConnection,
    RemoteConsole,
    main,
)
from dreamland.console_protocol import (
    PROTOCOL_VERSION,
    RESPONSE_MAX_BYTES,
    receive_message,
    send_message,
)
from dreamland.console_runtime import DreamlandCompleter, LocalConsole


def _hello(**overrides: object) -> dict[str, Any]:
    return {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "runtime_id": "runtime-one",
        "pid": 123,
        "service": "ssh",
        **overrides,
    }


def _execution(**overrides: object) -> dict[str, Any]:
    return {
        "output": "sessions\n",
        "follow": False,
        "trace_session_id": None,
        "follow_trace_session_id": None,
        "refresh": None,
        "leave": False,
        "shutdown": False,
        "context": {
            "active_session_id": None,
            "default_session_id": None,
            "recent_session_ids": [],
        },
        **overrides,
    }


class FakePromptSession:
    def __init__(self, commands: list[str | BaseException]) -> None:
        self.commands = iter(commands)
        self.prompts: list[str] = []

    def prompt(self, prompt: str) -> str:
        self.prompts.append(prompt)
        command = next(self.commands, EOFError())
        if isinstance(command, BaseException):
            raise command
        return command


class FakePeer:
    def __init__(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        greeting: dict[str, Any] | None = None,
    ) -> None:
        self.client_socket, self.server_socket = socket.socketpair()
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.failure: BaseException | None = None
        self.done = threading.Event()
        send_message(self.server_socket, greeting or _hello())
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self) -> None:
        try:
            while not self.done.is_set():
                request = receive_message(self.server_socket)
                if request is None:
                    break
                self.requests.append(request)
                result = self.handler(request)
                if result is None:
                    break
                send_message(
                    self.server_socket,
                    {"id": request["id"], "ok": True, "result": result},
                )
        except BaseException as exc:
            if not self.done.is_set():
                self.failure = exc
        finally:
            self.server_socket.close()

    def close(self) -> None:
        self.done.set()
        for connection in (self.client_socket, self.server_socket):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.worker.join(2)


class ConsoleClientTests(unittest.TestCase):
    def peer(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any] | None],
        **kwargs: Any,
    ) -> FakePeer:
        peer = FakePeer(handler, **kwargs)
        self.addCleanup(peer.close)
        return peer

    def connect(self, peer: FakePeer) -> ConsoleConnection:
        connection = ConsoleConnection(peer.client_socket)
        self.addCleanup(connection.close)
        return connection

    def test_terminal_uses_remote_context_and_keeps_prompt_interrupt_local(
        self,
    ) -> None:
        peer = self.peer(
            lambda request: _execution(
                context={
                    "active_session_id": "visit-1234567890",
                    "default_session_id": None,
                    "recent_session_ids": ["visit-1234567890"],
                }
            )
        )
        connection = self.connect(peer)
        prompts = FakePromptSession([KeyboardInterrupt(), "sessions", "disconnect"])
        output = io.StringIO()
        terminal = LocalConsole(
            RemoteConsole(connection),
            input_stream=io.BytesIO(),
            output_stream=output,
            prompt_session=prompts,
        )
        terminal.run()
        self.assertEqual(
            prompts.prompts, ["dreamland> ", "dreamland> ", "dreamland[visit-123456]> "]
        )
        self.assertEqual(terminal.context.recent_session_ids, ("visit-1234567890",))
        self.assertEqual([request["line"] for request in peer.requests], ["sessions"])
        self.assertIn("backend remains running", output.getvalue())
        self.assertTrue(connection.closed)

    def test_completion_uses_native_terminal_completer_and_local_disconnect(
        self,
    ) -> None:
        peer = self.peer(lambda request: {"completion": "enrichment status "})
        console = RemoteConsole(self.connect(peer))
        completions = list(
            DreamlandCompleter(console).get_completions(
                Document("enrichment st"), object()
            )
        )
        self.assertEqual(completions[0].text, "enrichment status ")
        self.assertEqual(completions[0].start_position, -len("enrichment st"))
        self.assertEqual(console.complete("dis", allow_exit=False), "disconnect ")
        self.assertEqual([request["op"] for request in peer.requests], ["complete"])

    def test_log_follow_preserves_terminal_output_and_stops_on_key(self) -> None:
        received_record = threading.Event()

        def handler(request: dict[str, Any]) -> dict[str, Any]:
            if request["op"] == "log_cursor":
                return {"sequence": 3}
            if request["sequence"] == 3:
                received_record.set()
                return {
                    "records": [{"sequence": 4, "text": "new event"}],
                    "sequence": 4,
                }
            return {"records": [], "sequence": 4}

        peer = self.peer(handler)
        connection = self.connect(peer)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, "rb", buffering=0) as input_stream:
            output = io.StringIO()
            terminal = LocalConsole(
                RemoteConsole(connection),
                input_stream=input_stream,
                output_stream=output,
                prompt_session=FakePromptSession([]),
            )
            worker = threading.Thread(target=terminal._follow_logs, daemon=True)
            worker.start()
            self.assertTrue(received_record.wait(2))
            os.write(write_fd, b"x")
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertIn("new event\r\n", output.getvalue())
            self.assertFalse(connection.closed)

    def test_trace_adapter_preserves_session_and_sequence(self) -> None:
        def handler(request: dict[str, Any]) -> dict[str, Any]:
            if request["op"] == "trace_cursor":
                return {"sequence": 40}
            return {
                "records": [{"sequence": 42, "text": "trace event"}],
                "sequence": 42,
            }

        peer = self.peer(handler)
        console = RemoteConsole(self.connect(peer))
        self.assertEqual(console.trace_logs.last_sequence("visit-a"), 40)
        records = console.trace_logs.after("visit-a", 40, timeout=0.1)
        self.assertEqual(
            (records[0].session_id, records[0].sequence, records[0].text),
            ("visit-a", 42, "trace event"),
        )
        self.assertEqual(peer.requests[1]["session_id"], "visit-a")
        self.assertEqual(peer.requests[1]["sequence"], 40)

    def test_refresh_uses_server_callback_and_restores_terminal(self) -> None:
        refreshed = threading.Event()

        def handler(request: dict[str, Any]) -> dict[str, Any]:
            if request["op"] == "execute":
                return _execution(
                    output="initial status\n",
                    refresh={"name": "enrichment_status", "interval_seconds": 0.01},
                )
            refreshed.set()
            return {"output": "updated status\n"}

        peer = self.peer(handler)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, "rb", buffering=0) as input_stream:
            output = io.StringIO()
            terminal = LocalConsole(
                RemoteConsole(self.connect(peer)),
                input_stream=input_stream,
                output_stream=output,
                prompt_session=FakePromptSession(
                    ["enrichment status --follow", "disconnect"]
                ),
                terminal_capable=True,
            )
            worker = threading.Thread(target=terminal.run, daemon=True)
            worker.start()
            self.assertTrue(refreshed.wait(2))
            os.write(write_fd, b"x")
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertIn("updated status", output.getvalue())
            self.assertIn(CONSOLE_REFRESH_EXIT_SCREEN, output.getvalue())
            self.assertEqual(peer.requests[0]["op"], "execute")
            self.assertEqual(peer.requests[1]["op"], "refresh")

    def test_refresh_rejected_without_terminal(self) -> None:
        peer = self.peer(
            lambda request: _execution(
                refresh={"name": "enrichment_status", "interval_seconds": 1}
            )
        )
        output = io.StringIO()
        LocalConsole(
            RemoteConsole(self.connect(peer)),
            input_stream=io.BytesIO(),
            output_stream=output,
            prompt_session=FakePromptSession(["enrichment status --follow"]),
            terminal_capable=False,
        ).run()
        self.assertIn("requires an interactive terminal", output.getvalue())
        self.assertEqual(len(peer.requests), 1)

    def test_peer_eof_is_observed_while_prompt_is_idle_without_rpc_polling(
        self,
    ) -> None:
        peer = self.peer(lambda request: _execution())
        connection = self.connect(peer)
        console = RemoteConsole(connection)
        peer.close()
        self.assertTrue(connection._closed.wait(2))
        self.assertTrue(console.shutdown_requested)
        self.assertIsInstance(connection.failure, ConsoleClientError)
        self.assertEqual(peer.requests, [])

    def test_lost_mutation_reply_is_never_replayed_and_reports_uncertainty(
        self,
    ) -> None:
        peer = self.peer(lambda request: None)
        connection = self.connect(peer)
        stderr = io.StringIO()
        with (
            patch.object(ConsoleConnection, "connect", return_value=connection),
            redirect_stderr(stderr),
        ):
            status = main(["--command", "enrichment run --session visit-a"])
        self.assertEqual(status, 1)
        self.assertEqual(len(peer.requests), 1)
        self.assertTrue(connection.command_outcome_uncertain)
        self.assertIn("may have completed", stderr.getvalue())
        self.assertIn("not replayed", stderr.getvalue())

    def test_one_shot_announces_identity_and_does_not_follow(self) -> None:
        peer = self.peer(lambda request: _execution(output="recent log\n", follow=True))
        connection = self.connect(peer)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(ConsoleConnection, "connect", return_value=connection),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main(["--command", "logs --follow"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "recent log\n")
        self.assertIn("ssh runtime=runtime-one pid=123", stderr.getvalue())
        self.assertEqual(len(peer.requests), 1)
        self.assertTrue(connection.closed)

    def test_interrupt_during_mutation_disconnects_without_retry(self) -> None:
        accepted, release = threading.Event(), threading.Event()

        def handler(request: dict[str, Any]) -> None:
            accepted.set()
            release.wait(2)

        peer = self.peer(handler)
        connection = self.connect(peer)

        def interrupt_wait(timeout: float | None = None) -> None:
            self.assertTrue(accepted.wait(2))
            raise KeyboardInterrupt

        stderr = io.StringIO()
        try:
            with (
                patch.object(ConsoleConnection, "connect", return_value=connection),
                patch.object(connection._condition, "wait", side_effect=interrupt_wait),
                redirect_stderr(stderr),
            ):
                status = main(["--command", "enrichment run --session visit-a"])
        finally:
            release.set()
        self.assertEqual(status, 130)
        self.assertEqual(len(peer.requests), 1)
        self.assertTrue(connection.closed)
        self.assertIn("may have completed", stderr.getvalue())

    def test_command_and_completion_share_one_request_at_a_time(self) -> None:
        command_started = threading.Event()
        release = threading.Event()

        def handler(request: dict[str, Any]) -> dict[str, Any]:
            if request["op"] == "execute":
                command_started.set()
                release.wait(2)
                return _execution()
            return {"completion": "sessions "}

        peer = self.peer(handler)
        console = RemoteConsole(self.connect(peer))
        results: list[object] = []
        command = threading.Thread(
            target=lambda: results.append(console.execute("sessions", allow_exit=False))
        )
        completion = threading.Thread(
            target=lambda: results.append(console.complete("ses", allow_exit=False))
        )
        command.start()
        self.assertTrue(command_started.wait(2))
        completion.start()
        release.set()
        command.join(2)
        completion.join(2)
        self.assertFalse(command.is_alive())
        self.assertFalse(completion.is_alive())
        self.assertEqual(len(results), 2)
        self.assertIn("sessions ", results)
        self.assertEqual(
            [request["op"] for request in peer.requests], ["execute", "complete"]
        )

    def test_version_mismatch_and_busy_are_clear_errors(self) -> None:
        greetings = [
            (_hello(version=99), "version mismatch"),
            (
                {
                    "id": None,
                    "ok": False,
                    "error": {
                        "code": "busy",
                        "message": "Another operator is connected.",
                    },
                },
                "busy",
            ),
        ]
        for greeting, expected in greetings:
            with self.subTest(expected=expected):
                peer = self.peer(lambda request: {}, greeting=greeting)
                with self.assertRaisesRegex(ConsoleClientError, expected):
                    ConsoleConnection(peer.client_socket)

    def test_malformed_response_id_closes_connection(self) -> None:
        def handler(request: dict[str, Any]) -> None:
            send_message(
                peer.server_socket, {"id": request["id"] + 1, "ok": True, "result": {}}
            )

        peer = self.peer(handler)
        connection = self.connect(peer)
        with self.assertRaisesRegex(ConsoleClientError, "response ID"):
            connection.request("complete", line="ses")
        self.assertTrue(connection.closed)

    def test_oversized_response_is_rejected_before_body_allocation(self) -> None:
        def handler(request: dict[str, Any]) -> None:
            peer.server_socket.sendall(struct.pack("!I", RESPONSE_MAX_BYTES + 1))

        peer = self.peer(handler)
        connection = self.connect(peer)
        with self.assertRaisesRegex(ConsoleClientError, "connection failed"):
            connection.request("log_cursor")
        self.assertTrue(connection.closed)

    def test_invalid_context_and_refresh_are_rejected(self) -> None:
        for result in (
            _execution(context={"recent_session_ids": "not a list"}),
            _execution(refresh={"name": "status", "interval_seconds": 0}),
        ):
            with self.subTest(result=result):
                peer = self.peer(lambda request, response=result: response)
                console = RemoteConsole(self.connect(peer))
                with self.assertRaises(ConsoleClientError):
                    console.execute(
                        "sessions", allow_exit=False, context=ConsoleContext()
                    )


if __name__ == "__main__":
    unittest.main()
