from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from dreamland.deployment import companion_http as http
from dreamland.deployment.companion import CompanionError

ATTENDEE_BEARER = "dreamland-attendee-transport:test_attendee_secret"
OPERATOR_BEARER = "dreamland-operator-transport:test_operator_secret"
PROJECT = "companion-test"


def _descriptor(role: str = "attendee") -> dict:
    return {
        "kind": "dreamland.backend-handoff",
        "schema_version": "0.1.0-rc.1",
        "project": PROJECT,
        "role": role,
        "image": "sha256:" + "a" * 64,
        "revision": "b" * 40,
        "credential_id": "credential:" + str(uuid.UUID(int=1)),
        "transport": {
            "kind": "docker-network",
            "network": "companion-private",
            "alias": f"{PROJECT}-{role}-api",
            "port": 8080,
            "base_url": f"http://{PROJECT}-{role}-api:8080",
        },
        "contract": {
            "name": "dreamland.deployment-control",
            "version": "0.1.0-rc.1",
            "openapi_sha256": "c" * 64,
        },
        "paths": {
            "identity": "/deployment/v1/identity",
            "health": "/deployment/v1/health",
            "readiness": "/deployment/v1/readiness",
        },
        "capabilities": {
            "attendee_sessions": False,
            "presenter": False,
            "world_forge": False,
            "world_publication": False,
        },
        "product_contracts": [],
    }


def _build(role: str = "attendee") -> http.CompanionHTTPServer:
    bearer = ATTENDEE_BEARER if role == "attendee" else OPERATOR_BEARER
    with patch.object(http, "load_service", return_value=(_descriptor(role), bearer)):
        return http.build_server(Path("/not-read"), role=role, port=0)


@contextmanager
def _running(role: str = "attendee"):
    server = _build(role)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        if thread.is_alive():
            raise AssertionError("listener thread did not terminate")


def _wire(
    *,
    method: str = "GET",
    path: str = "/deployment/v1/identity",
    bearer: str | None = ATTENDEE_BEARER,
    extra: bytes = b"",
) -> bytes:
    value = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n".encode("ascii")
    if bearer is not None:
        value += f"Authorization: Bearer {bearer}\r\n".encode("ascii")
    return value + extra + b"\r\n"


def _exchange(server: http.CompanionHTTPServer, wire: bytes) -> bytes:
    with socket.create_connection(server.server_address[:2], timeout=2) as client:
        client.sendall(wire)
        chunks = []
        while True:
            chunk = client.recv(65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _parse(response: bytes) -> tuple[int, dict[str, str], object]:
    header, _, body = response.partition(b"\r\n\r\n")
    lines = header.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.lower()] = value.strip()
    return status, headers, json.loads(body) if body else None


def _wait_for(predicate, *, timeout: float = 1) -> None:
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            raise AssertionError("bounded condition did not become true")
        time.sleep(0.005)


class CompanionHTTPTests(unittest.TestCase):
    def test_identity_health_and_readiness_are_same_epoch_and_disabled(self) -> None:
        with _running() as server:
            status, headers, identity = _parse(_exchange(server, _wire()))
            self.assertEqual(status, 200)
            self.assertEqual(identity["handoff"], _descriptor())
            self.assertEqual(
                str(uuid.UUID(identity["service_epoch"])), server.service_epoch
            )
            self.assertEqual(headers["connection"], "close")
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            status, _, health = _parse(
                _exchange(server, _wire(path="/deployment/v1/health"))
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                health,
                {
                    "status": "healthy",
                    "role": "attendee",
                    "service_epoch": server.service_epoch,
                },
            )
            status, _, readiness = _parse(
                _exchange(server, _wire(path="/deployment/v1/readiness"))
            )
            self.assertEqual(status, 503)
            self.assertTrue(readiness["transport_ready"])
            self.assertFalse(readiness["admission_ready"])
            self.assertEqual(readiness["reason"], "domain_services_unavailable")
            self.assertEqual(readiness["service_epoch"], server.service_epoch)
            self.assertTrue(
                all(value is False for value in readiness["capabilities"].values())
            )

    def test_missing_wrong_and_wrong_role_credentials_reveal_no_metadata(self) -> None:
        with _running() as attendee, _running("operator") as operator:
            for server, bearer in (
                (attendee, None),
                (attendee, "wrong-secret"),
                (attendee, OPERATOR_BEARER),
                (operator, ATTENDEE_BEARER),
            ):
                with self.subTest(role=server.role, bearer=bearer):
                    raw = _exchange(server, _wire(bearer=bearer))
                    status, headers, body = _parse(raw)
                    self.assertEqual(status, 401)
                    self.assertEqual(headers["www-authenticate"], "Bearer")
                    self.assertEqual(body["error"]["code"], "unauthorized")
                    for private in (
                        PROJECT,
                        server.service_epoch,
                        ATTENDEE_BEARER,
                        OPERATOR_BEARER,
                    ):
                        self.assertNotIn(private.encode(), raw)

    def test_unauthorized_unknown_paths_methods_and_framing_share_same_error(
        self,
    ) -> None:
        with _running() as server:
            expected = _exchange(server, _wire(bearer=None))
            for wire in (
                _wire(path="/private-secret", bearer=None),
                _wire(method="DELETE", bearer=None),
                _wire(bearer=None, extra=b"Transfer-Encoding: chunked\r\n"),
                _wire(path="/?secret=never-echo", bearer=None),
                _wire(bearer=None, extra=b"Content-Length: 999999\r\n"),
            ):
                self.assertEqual(_exchange(server, wire), expected)

    def test_duplicate_authorization_even_identical_is_unauthorized(self) -> None:
        with _running() as server:
            extra = f"authorization: Bearer {ATTENDEE_BEARER}\r\n".encode()
            status, _, body = _parse(_exchange(server, _wire(extra=extra)))
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")

    def test_unauthorized_head_keeps_authentication_status_without_a_body(self) -> None:
        with _running() as server:
            status, headers, body = _parse(
                _exchange(server, _wire(method="HEAD", bearer=None))
            )
            self.assertEqual(status, 401)
            self.assertEqual(headers["www-authenticate"], "Bearer")
            self.assertIsNone(body)

    def test_auth_comparison_is_constant_time_and_non_ascii_is_denied(self) -> None:
        with (
            _running() as server,
            patch.object(
                http.hmac, "compare_digest", wraps=http.hmac.compare_digest
            ) as compare,
        ):
            raw = _wire().replace(ATTENDEE_BEARER.encode(), b"secret-\xff")
            self.assertEqual(_parse(_exchange(server, raw))[0], 401)
            compare.assert_called_once()
            supplied, expected = compare.call_args.args
            self.assertIsInstance(supplied, bytes)
            self.assertIsInstance(expected, bytes)

    def test_privileged_and_product_routes_are_unavailable_with_valid_authority(
        self,
    ) -> None:
        with _running() as server:
            for path in (
                "/product/v1/capabilities",
                "/product/v1/sessions",
                "/forge/v1/jobs",
                "/mcp",
                "/admin",
                "/deployment/v1/identity/",
            ):
                with self.subTest(path=path):
                    status, _, body = _parse(_exchange(server, _wire(path=path)))
                    self.assertEqual(status, 404)
                    self.assertEqual(body["error"]["code"], "not_found")

    def test_only_get_is_supported_and_head_sends_no_body(self) -> None:
        with _running() as server:
            for method in (
                "POST",
                "DELETE",
                "PATCH",
                "OPTIONS",
                "TRACE",
                "CONNECT",
                "UNKNOWN",
                "HEAD",
            ):
                with self.subTest(method=method):
                    status, headers, body = _parse(
                        _exchange(server, _wire(method=method))
                    )
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["allow"], "GET")
                    if method == "HEAD":
                        self.assertIsNone(body)
                    else:
                        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_queries_encoded_routes_absolute_targets_and_fragments_are_rejected(
        self,
    ) -> None:
        with _running() as server:
            for path in (
                "/deployment/v1/identity?token=secret",
                "/deployment/v1/identity?",
                "/deployment/v1/identity#secret",
                "/deployment/v1/%69dentity",
                "http://localhost/deployment/v1/identity",
                "//localhost/deployment/v1/identity",
                "/deployment\\v1\\identity",
            ):
                with self.subTest(path=path):
                    raw = _exchange(server, _wire(path=path))
                    self.assertEqual(_parse(raw)[0], 400)
                    self.assertNotIn(path.encode(), raw)

    def test_request_framing_duplicates_folding_and_control_bytes_fail_closed(
        self,
    ) -> None:
        with _running() as server:
            for extra in (
                b"Content-Length: 1\r\n",
                b"Content-Length: -1\r\n",
                b"Content-Length: +0\r\n",
                b"Content-Length: 00\r\n",
                b"Content-Length: 0\r\nContent-Length: 0\r\n",
                b"Transfer-Encoding: chunked\r\n",
                b"Expect: 100-continue\r\n",
                b"Upgrade: websocket\r\n",
                b"Host: duplicate\r\n",
                b"X-A: one\r\nx-a: two\r\n",
                b"X-A: one\r\n folded\r\n",
                b"Invalid Name: value\r\n",
                b"X-A: control-\x00\r\n",
                b"X-A: control-\x7f\r\n",
                b"X-A: nonascii-\xff\r\n",
            ):
                with self.subTest(extra=extra):
                    status, _, body = _parse(_exchange(server, _wire(extra=extra)))
                    self.assertEqual(status, 400)
                    self.assertEqual(body["error"]["code"], "invalid_request")

    def test_zero_content_length_and_http_10_are_supported(self) -> None:
        with _running() as server:
            self.assertEqual(
                _parse(_exchange(server, _wire(extra=b"Content-Length: 0\r\n")))[0], 200
            )
            self.assertEqual(
                _parse(_exchange(server, _wire().replace(b"HTTP/1.1", b"HTTP/1.0")))[0],
                200,
            )

    def test_invalid_request_line_version_and_missing_host_are_normalized(self) -> None:
        with _running() as server:
            for wire in (
                _wire().replace(b"HTTP/1.1", b"HTTP/0.9"),
                _wire().replace(b"HTTP/1.1", b"HTTP/2.0"),
                _wire().replace(b"GET ", b"GET  "),
                _wire().replace(b"Host: localhost\r\n", b""),
                _wire().replace(b"GET ", b"BAD\x00 "),
            ):
                self.assertEqual(_parse(_exchange(server, wire))[0], 400)

    def test_header_count_line_and_total_bytes_are_bounded(self) -> None:
        with _running() as server:
            extras = (
                b"".join(
                    f"X-{number}: value\r\n".encode()
                    for number in range(http.MAX_HEADERS)
                ),
                b"X-Large: " + b"a" * http.MAX_LINE_BYTES + b"\r\n",
            )
            for extra in extras:
                self.assertEqual(_parse(_exchange(server, _wire(extra=extra)))[0], 400)
            huge = _wire(extra=b"X-Huge: " + b"a" * http.MAX_HEADER_BYTES + b"\r\n")
            self.assertEqual(_parse(_exchange(server, huge))[0], 401)

    def test_connection_closes_after_one_request_and_never_dispatches_pipeline(
        self,
    ) -> None:
        with _running() as server:
            raw = _exchange(server, _wire() + _wire(path="/deployment/v1/health"))
            self.assertEqual(raw.count(b"HTTP/1.1 "), 1)
            status, headers, body = _parse(raw)
            self.assertEqual(status, 200)
            self.assertEqual(
                len(raw.partition(b"\r\n\r\n")[2]), int(headers["content-length"])
            )
            self.assertIn("handoff", body)

    def test_idle_and_absolute_deadlines_release_connections(self) -> None:
        with (
            patch.object(http, "READ_TIMEOUT_SECONDS", 0.1),
            patch.object(http, "CONNECTION_DEADLINE_SECONDS", 0.25),
            _running() as server,
        ):
            with socket.create_connection(server.server_address, timeout=1) as client:
                client.sendall(b"GET ")
                self.assertEqual(client.recv(1), b"")
            _wait_for(lambda: server.active_connections == 0)
            with socket.create_connection(server.server_address, timeout=1) as client:
                started = time.monotonic()
                while time.monotonic() - started < 0.4:
                    try:
                        client.sendall(b"x")
                    except OSError:
                        break
                    time.sleep(0.04)
                self.assertEqual(client.recv(1), b"")
            self.assertLess(time.monotonic() - started, 0.6)
            _wait_for(lambda: server.active_connections == 0)

    def test_connection_capacity_and_close_release_all_workers(self) -> None:
        with patch.object(http, "MAX_CONNECTIONS", 2), _running() as server:
            clients = [
                socket.create_connection(server.server_address, timeout=1)
                for _ in range(2)
            ]
            try:
                _wait_for(lambda: server.active_connections == 2)
                with socket.create_connection(
                    server.server_address, timeout=1
                ) as rejected:
                    self.assertEqual(rejected.recv(1), b"")
                self.assertEqual(server.maximum_observed_connections, 2)
                server.shutdown()
                started = time.monotonic()
                server.server_close()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(server.active_connections, 0)
                for client in clients:
                    self.assertEqual(client.recv(1), b"")
            finally:
                for client in clients:
                    client.close()

    def test_epoch_rotates_between_process_compositions(self) -> None:
        first = _build()
        second = _build()
        try:
            self.assertNotEqual(first.service_epoch, second.service_epoch)
        finally:
            first.server_close()
            second.server_close()

    def test_invalid_binding_rejected_before_credential_read(self) -> None:
        with patch.object(http, "load_service") as load:
            for kwargs in (
                {"host": "localhost"},
                {"host": 2_130_706_433},
                {"host": "example.com"},
                {"host": "0.0.0.0"},
                {"host": "::"},
                {"host": "192.0.2.1", "container_network": True},
                {"container_network": 1},
                {"port": -1},
                {"port": True},
                {"port": 65_536},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(CompanionError):
                    http.build_server(Path("/not-read"), role="attendee", **kwargs)
            load.assert_not_called()

    def test_explicit_container_binding_and_role_loader(self) -> None:
        with patch.object(
            http, "load_service", return_value=(_descriptor(), ATTENDEE_BEARER)
        ) as load:
            server = http.build_server(
                Path("/role-root"),
                role="attendee",
                host="0.0.0.0",
                port=0,
                container_network=True,
            )
            try:
                self.assertEqual(server.server_address[0], "0.0.0.0")
                load.assert_called_once_with(Path("/role-root"), role="attendee")
            finally:
                server.server_close()

    def test_wrong_role_storage_fails_before_listener_construction(self) -> None:
        with (
            patch.object(
                http, "load_service", side_effect=CompanionError("role mismatch")
            ),
            patch.object(http, "CompanionHTTPServer") as constructor,
        ):
            with self.assertRaises(CompanionError):
                http.build_server(Path("/operator-root"), role="attendee")
            constructor.assert_not_called()

    def test_errors_and_logs_never_echo_request_secrets(self) -> None:
        secret = "private-request-secret-never-log"
        with (
            _running() as server,
            self.assertLogs(http.LOGGER, level="WARNING") as logs,
        ):
            raw = _exchange(server, _wire(path=f"/{secret}", bearer=secret))
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertNotIn(secret.encode(), raw)
        self.assertTrue(
            any("companion_authentication_denied" in line for line in logs.output)
        )

    def test_import_is_inert_and_excludes_product_provider_and_admin_modules(
        self,
    ) -> None:
        script = """
import sys
import socket
import threading
from unittest.mock import patch
import dreamland
import dreamland.agent_core
import dreamland.spiritguide
before = set(sys.modules)
with patch.object(socket.socket, '__init__', side_effect=AssertionError('socket constructed')), patch.object(threading.Thread, 'start', side_effect=AssertionError('thread started')), patch.object(dreamland.agent_core.LangGraphCommandAgent, '__init__', side_effect=AssertionError('agent constructed')), patch.object(dreamland.spiritguide.ModelSpiritguide, '__init__', side_effect=AssertionError('Spiritguide constructed')):
    import dreamland.deployment.companion_http
for name in set(sys.modules) - before:
    assert not name.startswith(('dreamland.product', 'dreamland.agent', 'dreamland.admin_api', 'dreamland.spiritguide', 'langchain', 'langgraph', 'openai')), name
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_signal_supervision_closes_idle_worker_and_restores_handlers(self) -> None:
        script = """
import json
import signal
import sys
from pathlib import Path
from unittest.mock import patch
from dreamland.deployment import companion_http as h
descriptor = json.loads(sys.argv[1])
with patch.object(h, 'load_service', return_value=(descriptor, 'private-test-token')):
    server = h.build_server(Path('/not-read'), role='attendee', port=0)
prior = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
print(server.server_address[1], flush=True)
result = h.serve(server)
assert all(signal.getsignal(s) == value for s, value in prior.items())
assert server.active_connections == 0
raise SystemExit(result)
"""
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            with (
                self.subTest(signal=stop_signal),
                tempfile.TemporaryFile(mode="w+") as errors,
            ):
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", script, json.dumps(_descriptor())],
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                )
                try:
                    self.assertIsNotNone(process.stdout)
                    port = int(process.stdout.readline())
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=2
                    ) as client:
                        client.sendall(b"GET ")

                        # Successful accept proves the serving thread has begun;
                        # wait for a complete request to prove signal installation.
                        class Address:
                            server_address = ("127.0.0.1", port)

                        _exchange(Address(), _wire(bearer="private-test-token"))
                        os.kill(process.pid, stop_signal)
                        self.assertEqual(client.recv(1), b"")
                    self.assertEqual(process.wait(timeout=3), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    if process.stdout is not None:
                        process.stdout.close()


if __name__ == "__main__":
    unittest.main()
