import copy
import importlib.util
import json
import select
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.1.0-rc.1"
TOOL_PATH = REPOSITORY_ROOT / "tools" / "product_contract.py"


def _load_contract_tool():
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_contract_tool",
        TOOL_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load product contract tool")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


CONTRACT_TOOL = _load_contract_tool()


class ProductContractTests(unittest.TestCase):
    def _generated_client_module(self, destination: Path):
        result = CONTRACT_TOOL.client_smoke(CONTRACT_ROOT, output=destination)
        specification = importlib.util.spec_from_file_location(
            "generated_product_client",
            destination,
        )
        if specification is None or specification.loader is None:
            self.fail("could not load generated product client")
        generated = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(generated)
        return generated, result

    def test_contract_validates(self):
        result = CONTRACT_TOOL.validate_contract(CONTRACT_ROOT)

        self.assertEqual(result["contract_revision"], "0.1.0-rc.1")
        self.assertEqual(result["capabilities"], 5)
        self.assertEqual(result["fixtures"], 13)
        self.assertIn(
            result["publication_status"],
            {"release_candidate", "published"},
        )

    def test_manifest_and_capability_fixture_fail_closed(self):
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        fixture = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "canonical-valid"
            / "capabilities"
            / "all-disabled.json"
        )

        expected = set(CONTRACT_TOOL.CAPABILITY_NAMES)
        self.assertEqual(set(manifest["capabilities"]), expected)
        self.assertEqual(set(fixture["capabilities"]), expected)
        for capability in expected:
            with self.subTest(capability=capability):
                self.assertFalse(manifest["capabilities"][capability]["enabled"])
                self.assertIsNone(
                    manifest["capabilities"][capability]["enabled_revision"]
                )
                self.assertFalse(fixture["capabilities"][capability]["enabled"])

    def test_safe_error_messages_are_phase0_allowlisted(self):
        safe = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "canonical-valid"
            / "errors"
            / "unauthorized.json"
        )
        unsafe = copy.deepcopy(safe)
        unsafe["error"]["message"] = (
            "Traceback /srv/dreamland token=private-service-bearer"
        )

        CONTRACT_TOOL._validate_safe_error(safe)
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "allowlist",
        ):
            CONTRACT_TOOL._validate_safe_error(unsafe)

    def test_openapi_claims_only_simulator_capability_discovery(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")

        self.assertEqual(
            set(openapi["paths"]),
            {"/product/v1/capabilities"},
        )
        operation = openapi["paths"]["/product/v1/capabilities"]["get"]
        self.assertEqual(operation["operationId"], "getProductCapabilities")
        self.assertEqual(
            operation["x-dreamland-availability"],
            "contract-simulator-only",
        )
        self.assertEqual(operation["security"], [{"ProductServiceBearer": []}])

    def test_openapi_security_claims_fail_closed_when_weakened(self):
        openapi = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "openapi.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        mutations = []
        live_server = copy.deepcopy(openapi)
        live_server["servers"] = [{"url": "https://product.example"}]
        mutations.append(("live-server", live_server))
        basic_auth = copy.deepcopy(openapi)
        basic_auth["components"]["securitySchemes"]["ProductServiceBearer"][
            "scheme"
        ] = "basic"
        mutations.append(("basic-auth", basic_auth))
        cacheable = copy.deepcopy(openapi)
        del cacheable["paths"]["/product/v1/capabilities"]["get"]["responses"]["200"][
            "headers"
        ]
        mutations.append(("cacheable", cacheable))
        unsafe_unauthorized = copy.deepcopy(openapi)
        unsafe_unauthorized["paths"]["/product/v1/capabilities"]["get"]["responses"][
            "401"
        ]["content"]["application/problem+json"]["schema"] = {"type": "object"}
        mutations.append(("unsafe-401", unsafe_unauthorized))
        sixth_capability = copy.deepcopy(openapi)
        sixth_capability["components"]["schemas"]["ProductCapabilitiesResponse"][
            "properties"
        ]["capabilities"]["properties"]["future_capability"] = {
            "$ref": "#/components/schemas/CapabilityState"
        }
        mutations.append(("sixth-capability", sixth_capability))

        for name, value in mutations:
            with self.subTest(name=name):
                with self.assertRaises(CONTRACT_TOOL.ContractError):
                    CONTRACT_TOOL._validate_openapi(value, manifest)

    def test_manifest_schema_enforces_publication_metadata_pairing(self):
        schema = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.schema.json")
        manifest = CONTRACT_TOOL.load_json(CONTRACT_ROOT / "manifest.json")
        published_without_metadata = copy.deepcopy(manifest)
        published_without_metadata["publication_status"] = "published"
        published_without_metadata["backend_commit"] = None
        published_without_metadata["published_at"] = None
        candidate_with_metadata = copy.deepcopy(manifest)
        candidate_with_metadata["publication_status"] = "release_candidate"
        candidate_with_metadata["backend_commit"] = "a" * 40
        candidate_with_metadata["published_at"] = "2026-08-22T00:00:00Z"

        self.assertTrue(
            CONTRACT_TOOL._validation_errors(
                schema,
                published_without_metadata,
            )
        )
        self.assertTrue(
            CONTRACT_TOOL._validation_errors(
                schema,
                candidate_with_metadata,
            )
        )

    def test_refresh_is_deterministic_and_invalidates_publication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            copied_root = Path(tmpdir) / "contract"
            shutil.copytree(CONTRACT_ROOT, copied_root)
            manifest_path = copied_root / "manifest.json"
            manifest = CONTRACT_TOOL.load_json(manifest_path)
            manifest["publication_status"] = "published"
            manifest["backend_commit"] = "a" * 40
            manifest["published_at"] = "2026-08-22T00:00:00Z"
            CONTRACT_TOOL.write_json(manifest_path, manifest)

            CONTRACT_TOOL.refresh_contract(copied_root)
            first = {
                path.relative_to(copied_root).as_posix(): path.read_bytes()
                for path in copied_root.rglob("*.json")
            }
            CONTRACT_TOOL.refresh_contract(copied_root)
            second = {
                path.relative_to(copied_root).as_posix(): path.read_bytes()
                for path in copied_root.rglob("*.json")
            }
            refreshed_manifest = CONTRACT_TOOL.load_json(manifest_path)

        self.assertEqual(first, second)
        self.assertEqual(refreshed_manifest["publication_status"], "release_candidate")
        self.assertIsNone(refreshed_manifest["backend_commit"])
        self.assertIsNone(refreshed_manifest["published_at"])

    def test_publish_records_external_implementation_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = Path(tmpdir) / "repository"
            repository.mkdir()
            copied_root = repository / "contract"
            shutil.copytree(CONTRACT_ROOT, copied_root)
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "add", "."],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Contract Test",
                    "-c",
                    "user.email=contract-test@dreamland.invalid",
                    "commit",
                    "-qm",
                    "contract implementation",
                ],
                check=True,
            )
            backend_commit = (
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                )
                .stdout.decode("ascii")
                .strip()
            )

            result = CONTRACT_TOOL.publish_contract(
                copied_root,
                backend_commit=backend_commit,
                published_at="2026-08-22T00:00:00Z",
            )
            manifest = CONTRACT_TOOL.load_json(copied_root / "manifest.json")

        self.assertEqual(result["publication_status"], "published")
        self.assertEqual(manifest["backend_commit"], backend_commit)
        self.assertEqual(manifest["published_at"], "2026-08-22T00:00:00Z")

    def test_publish_rejects_a_nonexistent_implementation_commit(self):
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "does not name a Git commit",
        ):
            CONTRACT_TOOL.publish_contract(
                CONTRACT_ROOT,
                backend_commit="b" * 40,
                published_at="2026-08-22T00:00:00Z",
            )

    def test_publish_rejects_non_rfc3339_timestamp(self):
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "RFC3339",
        ):
            CONTRACT_TOOL.publish_contract(
                CONTRACT_ROOT,
                backend_commit="b" * 40,
                published_at="2026-08-22 00:00:00+00:00",
            )

    def test_client_generation_smoke_compiles_and_calls_simulator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated_path = Path(tmpdir) / "product_client.py"
            generated, result = self._generated_client_module(generated_path)
            server = CONTRACT_TOOL.build_simulator_server(
                CONTRACT_ROOT,
                host="127.0.0.1",
                port=0,
                token="server-side-test-token",
                scenario="all-disabled",
                delay_ms=0,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                client = generated.ProductContractClient(
                    f"http://127.0.0.1:{port}",
                    "server-side-test-token",
                )
                payload = client.get_product_capabilities()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(result["operation_id"], "getProductCapabilities")
        self.assertEqual(
            set(payload["capabilities"]), set(CONTRACT_TOOL.CAPABILITY_NAMES)
        )

    def test_generated_client_accepts_additive_fields(self):
        fixture = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "forward-compat"
            / "capabilities"
            / "additive-fields.json"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client_module(
                Path(tmpdir) / "product_client.py"
            )

            result = generated.validate_product_capabilities(fixture)

        self.assertIs(result, fixture)

    def test_generated_client_rejects_incompatible_capability_evidence(self):
        canonical = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "canonical-valid"
            / "capabilities"
            / "all-disabled.json"
        )
        incompatible = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "forward-compat"
            / "capabilities"
            / "incompatible-revision.json"
        )
        missing = CONTRACT_TOOL.load_json(
            CONTRACT_ROOT
            / "fixtures"
            / "invalid"
            / "capabilities"
            / "missing-required-capability.json"
        )
        impossible = copy.deepcopy(canonical)
        impossible["capabilities"]["product_session_core"]["enabled_revision"] = (
            "99.0.0"
        )
        unknown = copy.deepcopy(canonical)
        unknown["capabilities"]["future_capability"] = copy.deepcopy(
            unknown["capabilities"]["stage_xray"]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client_module(
                Path(tmpdir) / "product_client.py"
            )

            for name, payload in (
                ("incompatible", incompatible),
                ("missing", missing),
                ("impossible", impossible),
                ("unknown", unknown),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        generated.validate_product_capabilities(payload)

    def test_generated_client_applies_fail_closed_rules_to_simulator_scenarios(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client_module(
                Path(tmpdir) / "product_client.py"
            )
            for scenario, should_accept in (
                ("additive-fields", True),
                ("incompatible-revision", False),
                ("missing-capability", False),
            ):
                with self.subTest(scenario=scenario):
                    server = CONTRACT_TOOL.build_simulator_server(
                        CONTRACT_ROOT,
                        host="127.0.0.1",
                        port=0,
                        token="server-side-test-token",
                        scenario=scenario,
                        delay_ms=0,
                    )
                    thread = threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    )
                    thread.start()
                    try:
                        client = generated.ProductContractClient(
                            f"http://127.0.0.1:{server.server_address[1]}",
                            "server-side-test-token",
                        )
                        if should_accept:
                            payload = client.get_product_capabilities()
                            self.assertEqual(
                                set(payload["capabilities"]),
                                set(CONTRACT_TOOL.CAPABILITY_NAMES),
                            )
                        else:
                            with self.assertRaises(ValueError):
                                client.get_product_capabilities()
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)

    def test_generated_client_does_not_follow_redirects_with_private_bearer(self):
        captured_authorization: list[str | None] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                captured_authorization.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.end_headers()

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        target_port = target.server_address[1]

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target_port}/capture",
                )
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(
            target=redirect.serve_forever,
            daemon=True,
        )
        redirect_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                generated, _ = self._generated_client_module(
                    Path(tmpdir) / "product_client.py"
                )
                client = generated.ProductContractClient(
                    f"http://127.0.0.1:{redirect.server_address[1]}",
                    "private-server-side-bearer",
                )

                with self.assertRaises(HTTPError) as caught:
                    client.get_product_capabilities()
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(captured_authorization, [])

    def test_generated_client_rejects_unsafe_base_urls_and_bearers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated, _ = self._generated_client_module(
                Path(tmpdir) / "product_client.py"
            )

            for base_url in (
                "http://product.internal.example",
                "ftp://127.0.0.1",
                "https://user:password@product.example",
                "https://product.example/path",
                "https://product.example?",
                "https://product.example#",
            ):
                with self.subTest(base_url=base_url):
                    with self.assertRaises(ValueError):
                        generated.ProductContractClient(base_url, "bearer")
            with self.assertRaises(ValueError):
                generated.ProductContractClient(
                    "https://product.example",
                    "bearer\ninjected",
                )
            client = generated.ProductContractClient(
                "https://product.example",
                "bearer",
            )
            proxy_handler = client._proxy_handler

        self.assertIsInstance(proxy_handler, generated.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})

    def test_simulator_requires_private_bearer_and_sets_no_store(self):
        server = CONTRACT_TOOL.build_simulator_server(
            CONTRACT_ROOT,
            host="127.0.0.1",
            port=0,
            token="server-side-test-token",
            scenario="all-disabled",
            delay_ms=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/product/v1/capabilities"
            with self.assertRaises(HTTPError) as caught:
                urlopen(url, timeout=2)
            unauthorized_content_type = caught.exception.headers.get("Content-Type")
            authenticate = caught.exception.headers.get("WWW-Authenticate")
            unauthorized_cache_control = caught.exception.headers.get("Cache-Control")
            request = Request(
                url,
                headers={"Authorization": "Bearer server-side-test-token"},
            )
            with urlopen(request, timeout=2) as response:
                payload = json.load(response)
                cache_control = response.headers.get("Cache-Control")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(unauthorized_content_type, "application/problem+json")
        self.assertEqual(authenticate, "Bearer")
        self.assertEqual(unauthorized_cache_control, "no-store")
        self.assertEqual(cache_control, "no-store")
        self.assertEqual(
            set(payload["capabilities"]), set(CONTRACT_TOOL.CAPABILITY_NAMES)
        )

    def test_simulator_once_waits_for_the_complete_delayed_response(self):
        command = [
            str(REPOSITORY_ROOT / ".venv/bin/python"),
            str(TOOL_PATH),
            "serve",
            str(CONTRACT_ROOT),
            "--token",
            "server-side-test-token",
            "--scenario",
            "delayed",
            "--delay-ms",
            "250",
            "--once",
        ]
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            if process.stdout is None:
                self.fail("contract simulator stdout was unavailable")
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable, "contract simulator did not report startup")
            startup = json.loads(process.stdout.readline())
            self.assertEqual(startup["scenario"], "delayed")

            request = Request(
                f"http://{startup['host']}:{startup['port']}"
                f"{CONTRACT_TOOL.CAPABILITIES_PATH}",
                headers={"Authorization": "Bearer server-side-test-token"},
            )
            with urlopen(request, timeout=5) as response:
                status = response.status
                headers = response.headers
                body = response.read()

            expected = CONTRACT_TOOL.load_json(
                CONTRACT_ROOT
                / "fixtures/canonical-valid/capabilities/all-disabled.json"
            )
            expected_body = json.dumps(
                expected,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertEqual(headers.get("Content-Type"), "application/json")
            self.assertEqual(headers.get("Content-Length"), str(len(body)))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(body, expected_body)

            remaining_stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(remaining_stdout, "")
            self.assertEqual(stderr, "")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_simulator_scenario_cannot_be_selected_by_request(self):
        server = CONTRACT_TOOL.build_simulator_server(
            CONTRACT_ROOT,
            host="127.0.0.1",
            port=0,
            token="server-side-test-token",
            scenario="missing-capability",
            delay_ms=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            url = (
                f"http://127.0.0.1:{port}/product/v1/capabilities?scenario=all-disabled"
            )
            request = Request(
                url,
                headers={
                    "Authorization": "Bearer server-side-test-token",
                    "X-Dreamland-Contract-Scenario": "all-disabled",
                },
            )
            with urlopen(request, timeout=2) as response:
                payload = json.load(response)
            post = Request(
                f"http://127.0.0.1:{port}/product/v1/sessions",
                data=b"{}",
                headers={"Authorization": "Bearer server-side-test-token"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(post, timeout=2)
            not_found_content_type = caught.exception.headers.get("Content-Type")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertNotIn("stage_xray", payload["capabilities"])
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(not_found_content_type, "application/problem+json")

    def test_simulator_rejects_nonloopback_bind(self):
        with self.assertRaisesRegex(
            CONTRACT_TOOL.ContractError,
            "only to loopback",
        ):
            CONTRACT_TOOL.build_simulator_server(
                CONTRACT_ROOT,
                host="0.0.0.0",
                port=0,
                token="server-side-test-token",
                scenario="all-disabled",
                delay_ms=0,
            )

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "duplicate.json"
            path.write_text('{"value": 1, "value": 2}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "duplicate JSON object key",
            ):
                CONTRACT_TOOL.load_json(path)

    def test_nonstandard_json_constants_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nonstandard.json"
            path.write_text('{"value": NaN}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                CONTRACT_TOOL.ContractError,
                "non-standard JSON constant",
            ):
                CONTRACT_TOOL.load_json(path)

    def test_contract_schema_target_cannot_escape_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "contract"
            root.mkdir()
            outside = Path(tmpdir) / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            (root / "linked.json").symlink_to(outside)

            for target in ("../outside.json#", "linked.json#"):
                with self.subTest(target=target):
                    with self.assertRaises(CONTRACT_TOOL.ContractError):
                        CONTRACT_TOOL._schema_for_target(root, target)

    def test_contract_tool_has_no_dreamland_runtime_import(self):
        source = TOOL_PATH.read_text(encoding="utf-8")

        self.assertNotIn("from dreamland", source)
        self.assertNotIn("import dreamland", source)


if __name__ == "__main__":
    unittest.main()
