"""Owning checks for the private companion's identity and authority probe."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import multiprocessing
import os
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamland.deployment import companion, companion_http
from dreamland.deployment import companion_client as client

EPOCH = "a67c9430-d2f6-4593-a353-d48813ac3d04"
# Fault injection must reach DNS or response I/O even when the aggregate suite
# is importing a fresh spawned interpreter under load. Production deadlines are
# unchanged; these tests separately record entry and bound the final cleanup.
FAULT_WORKER_TIMEOUT_SECONDS = 8.0


def _mapped_dns_worker(sender, operation, arguments):
    port = arguments.pop("test_port")
    original = socket.getaddrinfo

    def local_address(_host, _port, *args, **kwargs):
        return original("127.0.0.1", port, *args, **kwargs)

    with patch.object(socket, "getaddrinfo", side_effect=local_address):
        client._worker(sender, operation, arguments)


def _stalled_dns_worker(sender, operation, arguments):
    # Prove escalation to kill as well as the normal terminate path. This is
    # confined to this test child, never the test runner's signal handlers.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def stalled_address(*_args, **_kwargs):
        (arguments["root"].parent / "dns-entered").write_text(str(time.monotonic()))
        time.sleep(30)
        raise AssertionError("the supervisor should have stopped this worker")

    with patch.object(socket, "getaddrinfo", side_effect=stalled_address):
        client._worker(sender, operation, arguments)


def _failed_private_worker(sender, operation, arguments):
    with patch.object(
        client, "_healthcheck", side_effect=ValueError("private-secret-never-send")
    ):
        client._worker(sender, operation, arguments)


class CompanionClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.descriptors = {}
        for role in ("attendee", "operator"):
            self.descriptors[role] = companion.initialize(
                self.directory / role,
                role=role,
                project="rehearsal",
                network="rehearsal-companion",
                image="sha256:" + "1" * 64,
                revision="2" * 40,
            )["handoff"]
        self.handoff = self.descriptors["attendee"]
        self.token_file = self.directory / "attendee.token"
        companion.export_token(self.directory / "attendee", self.token_file)
        self.token = self.token_file.read_text().removesuffix("\n")

    def responses(self, role="attendee"):
        return [
            (
                200,
                {
                    "handoff": copy.deepcopy(self.descriptors[role]),
                    "service_epoch": EPOCH,
                },
            ),
            (200, {"status": "healthy", "role": role, "service_epoch": EPOCH}),
            (
                503,
                {
                    "transport_ready": True,
                    "admission_ready": False,
                    "reason": "domain_services_unavailable",
                    "role": role,
                    "service_epoch": EPOCH,
                    "capabilities": dict(companion.CAPABILITIES),
                },
            ),
            (
                401,
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Private authority is required.",
                    }
                },
            ),
            (
                401,
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Private authority is required.",
                    }
                },
            ),
        ]

    def test_probe_checks_own_identity_and_rejects_cross_role_use(self):
        for role, opposite in (("attendee", "operator"), ("operator", "attendee")):
            with self.subTest(role=role):
                handoff = self.descriptors[role]
                deny_handoff = self.descriptors[opposite]
                token_file = self.directory / role / "transport.token"
                token = token_file.read_text().removesuffix("\n")
                with patch.object(
                    client, "_request", side_effect=self.responses(role)
                ) as request:
                    result = client._probe(
                        handoff, token_file, deny_handoff=deny_handoff
                    )
                self.assertTrue(result["transport_verified"])
                self.assertFalse(result["domain_ready"])
                self.assertTrue(result["missing_auth_denied"])
                self.assertTrue(result["wrong_role_denied"])
                self.assertEqual(result["role"], role)
                calls = request.call_args_list
                self.assertEqual(len(calls), 5)
                for index, operation in enumerate(("identity", "health", "readiness")):
                    self.assertEqual(calls[index].args, (handoff, operation, token))
                self.assertEqual(calls[3].args, (handoff, "identity", None))
                self.assertEqual(calls[4].args, (deny_handoff, "identity", token))
                self.assertNotIn(token, json.dumps(result))

    def test_identity_epoch_role_and_admission_mismatches_are_rejected(self):
        for mutation in (
            "identity",
            "epoch",
            "readiness_epoch",
            "role",
            "ready",
            "status",
            "redirect",
        ):
            with self.subTest(mutation=mutation):
                responses = self.responses()
                if mutation == "identity":
                    responses[0][1]["handoff"]["image"] = "sha256:" + "3" * 64
                elif mutation == "epoch":
                    responses[1][1]["service_epoch"] = (
                        "9454dd37-6b9a-4d86-a7fa-c1cb5816cbe4"
                    )
                elif mutation == "readiness_epoch":
                    responses[2][1]["service_epoch"] = (
                        "9454dd37-6b9a-4d86-a7fa-c1cb5816cbe4"
                    )
                elif mutation == "redirect":
                    responses[0] = (302, responses[0][1])
                elif mutation == "role":
                    responses[1][1]["role"] = "operator"
                elif mutation == "ready":
                    responses[2][1]["admission_ready"] = True
                else:
                    responses[2] = (200, responses[2][1])
                with patch.object(client, "_request", side_effect=responses):
                    with self.assertRaises(ValueError):
                        client._probe(self.handoff, self.token_file)

    def test_missing_or_wrong_role_auth_acceptance_fails_probe(self):
        for index in (3, 4):
            with self.subTest(index=index):
                responses = self.responses()
                responses[index] = (200, {"unexpected": "access"})
                with patch.object(client, "_request", side_effect=responses):
                    with self.assertRaises(ValueError):
                        client._probe(
                            self.handoff,
                            self.token_file,
                            deny_handoff=self.descriptors["operator"],
                        )

    def test_malformed_or_wrong_code_401_cannot_count_as_auth_denial(self):
        for index in (3, 4):
            for invalid in (
                {"error": "unauthorized"},
                {"error": {"code": "not_found", "message": "Unavailable."}},
                {"error": {"code": "unauthorized", "message": ""}},
                {"error": {"code": "unauthorized", "message": "x" * 161}},
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Denied.",
                        "extra": True,
                    }
                },
                {
                    "error": {"code": "unauthorized", "message": "Denied."},
                    "secret": True,
                },
            ):
                with self.subTest(index=index, invalid=invalid):
                    responses = self.responses()
                    responses[index] = (401, invalid)
                    with (
                        patch.object(client, "_request", side_effect=responses),
                        self.assertRaises(ValueError),
                    ):
                        client._probe(
                            self.handoff,
                            self.token_file,
                            deny_handoff=self.descriptors["operator"],
                        )

    def test_unsafe_or_wrong_role_token_fails_before_network(self):
        unsafe = self.directory / "unsafe.token"
        unsafe.write_bytes(self.token_file.read_bytes())
        unsafe.chmod(0o644)
        linked = self.directory / "linked.token"
        linked.symlink_to(self.token_file)
        wrong = self.directory / "operator" / "transport.token"
        for path in (unsafe, linked, wrong):
            with (
                self.subTest(path=path.name),
                patch.object(client, "_request") as request,
            ):
                with self.assertRaises(ValueError):
                    client._probe(self.handoff, path)
                request.assert_not_called()
        hardlink = self.directory / "hardlink.token"
        os.link(self.token_file, hardlink)
        with self.assertRaises(ValueError):
            client._token(hardlink, "attendee")

    def test_duplicate_nonfinite_and_oversize_inputs_are_rejected(self):
        for value in (b'{"role":"attendee","role":"operator"}', b'{"value":NaN}'):
            with self.assertRaises(ValueError):
                client._json(value)
        oversized = self.directory / "large.json"
        oversized.write_bytes(b"x" * (client._MAX_RESPONSE + 1))
        with self.assertRaises(ValueError):
            client._read_file(oversized, secret=False)

    def test_cli_domain_readiness_gate_fails_after_metadata_success(self):
        handoff_file = self.directory / "handoff.json"
        handoff_file.write_text(json.dumps(self.handoff))
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(client, "_request", side_effect=self.responses()),
            patch.object(client, "probe", side_effect=client._probe),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            status = client.main(
                [
                    "--handoff",
                    str(handoff_file),
                    "--token-file",
                    str(self.token_file),
                    "--require-domain-ready",
                ]
            )
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["domain_ready"])
        self.assertIn("Domain services", errors.getvalue())
        self.assertNotIn(self.token, output.getvalue() + errors.getvalue())

    def test_healthcheck_targets_local_listener_and_does_not_test_anonymous_auth(self):
        with patch.object(
            client, "_request", return_value=self.responses()[1]
        ) as request:
            status = client._healthcheck(self.directory / "attendee")
        self.assertEqual(status, 0)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]["transport"]["alias"], "127.0.0.1")
        self.assertEqual(request.call_args.args[1], "health")

    def test_supervised_probe_performs_real_http_and_reaps_child(self):
        server = companion_http.build_server(
            self.directory / "attendee", role="attendee", port=0
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        children_before = {child.pid for child in multiprocessing.active_children()}
        thread.start()
        try:
            with patch.object(client, "_worker", _mapped_dns_worker):
                result = client._supervise(
                    "probe",
                    {
                        "handoff": self.handoff,
                        "token_file": self.token_file,
                        "test_port": server.server_address[1],
                    },
                    timeout=5,
                )
            self.assertTrue(result["transport_verified"])
            self.assertFalse(result["domain_ready"])
            self.assertTrue(result["missing_auth_denied"])
            self.assertEqual(result["service_epoch"], server.service_epoch)
            self.assertNotIn(self.token, json.dumps(result))
            self.assertEqual(
                {child.pid for child in multiprocessing.active_children()},
                children_before,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_cli_healthcheck_contains_stalled_dns_and_kills_unresponsive_worker(self):
        children_before = {child.pid for child in multiprocessing.active_children()}
        errors = io.StringIO()
        started = time.monotonic()
        with (
            patch.object(client, "_worker", _stalled_dns_worker),
            patch.object(
                client, "HEALTHCHECK_TIMEOUT_SECONDS", FAULT_WORKER_TIMEOUT_SECONDS
            ),
            contextlib.redirect_stderr(errors),
        ):
            status = client.main(
                ["--healthcheck-root", str(self.directory / "attendee")]
            )
        self.assertEqual(status, 1)
        self.assertTrue((self.directory / "dns-entered").exists())
        dns_entered = float((self.directory / "dns-entered").read_text())
        finished = time.monotonic()
        self.assertGreaterEqual(dns_entered, started)
        self.assertGreaterEqual(finished - started, FAULT_WORKER_TIMEOUT_SECONDS * 0.9)
        self.assertLess(finished - started, FAULT_WORKER_TIMEOUT_SECONDS + 2)
        self.assertLess(finished - dns_entered, FAULT_WORKER_TIMEOUT_SECONDS + 2)
        self.assertNotIn(self.token, errors.getvalue())
        self.assertNotIn(str(self.directory), errors.getvalue())
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, children_before
        )

    def test_slow_dripping_response_cannot_extend_total_probe_deadline(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        accepted = threading.Event()
        stopped = threading.Event()
        response_entered = []

        def drip_response():
            try:
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
                with connection:
                    connection.settimeout(2)
                    connection.recv(16_384)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 30000\r\nConnection: close\r\n\r\n"
                    )
                    response_entered.append(time.monotonic())
                    accepted.set()
                    while not stopped.wait(0.05):
                        connection.sendall(b" ")
            except OSError:
                pass

        thread = threading.Thread(target=drip_response)
        thread.start()
        children_before = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()
        try:
            with (
                patch.object(client, "_worker", _mapped_dns_worker),
                self.assertRaises(client.ProbeError),
            ):
                client._supervise(
                    "probe",
                    {
                        "handoff": self.handoff,
                        "token_file": self.token_file,
                        "test_port": listener.getsockname()[1],
                    },
                    timeout=FAULT_WORKER_TIMEOUT_SECONDS,
                )
            self.assertTrue(accepted.is_set())
            finished = time.monotonic()
            self.assertGreaterEqual(response_entered[0], started)
            self.assertGreaterEqual(
                finished - started, FAULT_WORKER_TIMEOUT_SECONDS * 0.9
            )
            self.assertLess(finished - started, FAULT_WORKER_TIMEOUT_SECONDS + 2)
            self.assertLess(
                finished - response_entered[0], FAULT_WORKER_TIMEOUT_SECONDS + 2
            )
            self.assertEqual(
                {child.pid for child in multiprocessing.active_children()},
                children_before,
            )
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_worker_exception_is_redacted_and_worker_is_reaped(self):
        children_before = {child.pid for child in multiprocessing.active_children()}
        with (
            patch.object(client, "_worker", _failed_private_worker),
            self.assertRaises(client.ProbeError) as raised,
        ):
            client.healthcheck(self.directory / "attendee")
        self.assertNotIn("private-secret", str(raised.exception))
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, children_before
        )


if __name__ == "__main__":
    unittest.main()
