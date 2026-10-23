#!/usr/bin/env python3
"""Serve the provider-free World Forge browser-integration preview on loopback."""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
import socket
import stat
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from dreamland.product.forge import (
    ForgeActorSeed,
    ForgeJobState,
    ForgePolicy,
    ForgeServiceError,
    WorldForgeService,
    WorldSeedValidationError,
)

CONTRACT_NAME = "dreamland-private-world-forge-api"
CONTRACT_REVISION = "0.1.0-rc.2"
SCENARIOS = (
    "happy",
    "failure",
    "deadline",
    "capacity",
    "idempotency-capacity",
    "preparation-capacity",
    "preparation-expiry",
    "cursor-loss",
    "epoch-loss",
    "submit-response-lost",
    "withheld-event",
)
MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 131_072
MAX_CONNECTIONS = 16
ACTOR_HEADER = "X-Dreamland-Actor-Context"
EPOCH_HEADER = "X-Dreamland-Expected-Epoch"
IDEMPOTENCY_HEADER = "Idempotency-Key"


def _load_contract_tool() -> ModuleType:
    path = Path(__file__).with_name("product_forge_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_world_forge_contract_tool", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("World Forge contract validator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class ForgePreviewBootstrap:
    actor_context: str


def initialize_private_files(*, token_file: Path, bootstrap_file: Path) -> None:
    if token_file.expanduser().absolute() == bootstrap_file.expanduser().absolute():
        raise ValueError("token and bootstrap paths must differ")
    import secrets

    _exclusive_owner_file(
        token_file, f"forge-preview-bearer:{secrets.token_urlsafe(32)}\n"
    )
    _exclusive_owner_file(
        bootstrap_file,
        json.dumps(
            {"actor_context": f"actor:{secrets.token_urlsafe(32)}"},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def load_bootstrap(path: Path) -> ForgePreviewBootstrap:
    try:
        value = json.loads(_read_owner_file(path, maximum_bytes=2_048))
    except json.JSONDecodeError as error:
        raise ValueError("World Forge bootstrap is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"actor_context"}:
        raise ValueError("World Forge bootstrap has invalid fields")
    actor = value["actor_context"]
    if not isinstance(actor, str) or not 16 <= len(actor) <= 256:
        raise ValueError("World Forge actor context is malformed")
    return ForgePreviewBootstrap(actor)


def load_bearer(path: Path) -> str:
    value = _read_owner_file(path, maximum_bytes=512).strip()
    if not value.startswith("forge-preview-bearer:") or len(value) < 48:
        raise ValueError("World Forge private bearer is malformed")
    return value


class ForgePreviewHarness:
    def __init__(
        self,
        *,
        bootstrap: ForgePreviewBootstrap,
        scenario: str,
        step_delay_ms: int,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown World Forge preview scenario")
        if not 10 <= step_delay_ms <= 60_000:
            raise ValueError("step delay must be between 10 and 60000 ms")
        self.scenario = scenario
        self.step_delay_ms = step_delay_ms
        self.actor_authority = bootstrap.actor_context
        expiry = datetime.now(UTC) + timedelta(hours=12)
        maximum_jobs = 1 if scenario in {"capacity", "idempotency-capacity"} else 16
        self.service = WorldForgeService(
            actors=(
                ForgeActorSeed(
                    authority=bootstrap.actor_context,
                    actor_id="forge-preview-presenter",
                    expires_at=expiry.isoformat(),
                ),
            ),
            policy=ForgePolicy(
                maximum_jobs=maximum_jobs,
                maximum_idempotency_records=(
                    1 if scenario == "idempotency-capacity" else 128
                ),
                maximum_preparations=(1 if scenario == "preparation-capacity" else 32),
                preparation_ttl_seconds=(
                    1 if scenario == "preparation-expiry" else 300
                ),
                deadline_seconds=max(1, (step_delay_ms * 3 + 999) // 1_000),
            ),
        )
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="dreamland-forge-preview"
        )
        self._lock = threading.Lock()
        self._futures: set[Future[object]] = set()
        self._submit_response_lost = False

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def schedule(self, reference: str) -> None:
        future = self._executor.submit(self._run, reference)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._forget)

    def consume_submit_response_loss(self) -> bool:
        with self._lock:
            if self.scenario != "submit-response-lost" or self._submit_response_lost:
                return False
            self._submit_response_lost = True
            return True

    def _forget(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _run(self, reference: str) -> None:
        states = [ForgeJobState.QUEUED, ForgeJobState.BUILDING]
        if self.scenario == "deadline":
            states.append(ForgeJobState.DEADLINE_EXCEEDED)
        else:
            states.extend([ForgeJobState.GENERATED, ForgeJobState.VALIDATING])
            states.append(
                ForgeJobState.FAILED
                if self.scenario == "failure"
                else ForgeJobState.SELECTED
            )
        for state in states:
            if state is ForgeJobState.DEADLINE_EXCEEDED:
                projection = self.service.status(
                    actor_authority=self.actor_authority,
                    expected_epoch=self.service.service_epoch,
                    reference=reference,
                )
                deadline = datetime.fromisoformat(
                    str(projection["deadline_at"]).replace("Z", "+00:00")
                )
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    time.sleep(remaining)
            else:
                time.sleep(self.step_delay_ms / 1_000)
            try:
                self.service.preview_transition(reference=reference, state=state)
            except ForgeServiceError:
                return
            if self.scenario == "epoch-loss" and state is ForgeJobState.BUILDING:
                self.service.preview_rotate_epoch()
                return


class ForgePreviewServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        harness: ForgePreviewHarness,
        contract_revision: str = CONTRACT_REVISION,
    ) -> None:
        self.preview_bearer = bearer
        self.preview_harness = harness
        self.preview_contract_revision = contract_revision
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._synchronous_request = False
        super().__init__(address, ForgePreviewHandler)

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if self._synchronous_request:
            HTTPServer.process_request(self, request, client_address)
            return
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_request_synchronously(self) -> None:
        self._synchronous_request = True
        try:
            self.handle_request()
        finally:
            self._synchronous_request = False

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.preview_harness.close()


class ForgePreviewHandler(BaseHTTPRequestHandler):
    server: ForgePreviewServer
    server_version = "DREAMLANDWorldForgePreview/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _dispatch(self, method: str) -> None:
        request_id = f"request:{time.monotonic_ns():x}"
        try:
            self._require_bearer()
            parsed = urlsplit(self.path)
            path = parsed.path
            if method == "GET" and path == "/forge/v1/identity":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "kind": "forge_identity",
                        "contract_name": CONTRACT_NAME,
                        "contract_revision": self.server.preview_contract_revision,
                        "service_epoch": self.server.preview_harness.service.service_epoch,
                        "source": self.server.preview_harness.service.source,
                    },
                )
                return
            actor, epoch = self._authority_headers()
            service = self.server.preview_harness.service
            if method == "GET" and path == "/forge/v1/recipes":
                service.authorize(
                    actor_authority=actor,
                    expected_epoch=epoch,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "kind": "forge_recipes",
                        "service_epoch": epoch,
                        "recipes": [service.recipe()],
                        "source": service.source,
                    },
                )
                return
            if method == "POST" and path == "/forge/v1/preparations":
                body = self._read_json({"recipe_id", "recipe_revision", "answers"})
                result = service.prepare(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    recipe_id=body["recipe_id"],
                    recipe_revision=body["recipe_revision"],
                    answers=body["answers"],
                )
                self._send_json(HTTPStatus.CREATED, result)
                return
            if method == "POST" and path == "/forge/v1/builds":
                body = self._read_json({"preparation_reference"})
                result = service.confirm_and_submit(
                    actor_authority=actor,
                    expected_epoch=epoch,
                    idempotency_key=self._idempotency_key(),
                    preparation_reference=body["preparation_reference"],
                )
                if not result["replayed"]:
                    self.server.preview_harness.schedule(str(result["reference"]))
                if self.server.preview_harness.consume_submit_response_loss():
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self._send_json(HTTPStatus.CREATED, result)
                return
            reference, suffix = _build_route(path)
            if reference is not None and method == "GET" and suffix == "":
                self._send_json(
                    HTTPStatus.OK,
                    service.status(
                        actor_authority=actor,
                        expected_epoch=epoch,
                        reference=reference,
                    ),
                )
                return
            if reference is not None and method == "GET" and suffix == "/events":
                values = parse_qs(parsed.query, keep_blank_values=True)
                if set(values) != {"after"} or len(values["after"]) != 1:
                    raise ForgeServiceError(
                        "invalid_request",
                        "One opaque World Forge event cursor is required.",
                        status=400,
                    )
                if self.server.preview_harness.scenario in {
                    "cursor-loss",
                    "withheld-event",
                }:
                    raise ForgeServiceError(
                        "resync_required",
                        "World Forge event history must be resynchronized from the job snapshot.",
                        status=409,
                        retry="resume",
                    )
                self._send_json(
                    HTTPStatus.OK,
                    service.events(
                        actor_authority=actor,
                        expected_epoch=epoch,
                        reference=reference,
                        after_cursor=values["after"][0],
                    ),
                )
                return
            if reference is not None and method == "POST" and suffix == "/cancel":
                self._read_json(set())
                self._send_json(
                    HTTPStatus.OK,
                    service.cancel(
                        actor_authority=actor,
                        expected_epoch=epoch,
                        reference=reference,
                        idempotency_key=self._idempotency_key(),
                    ),
                )
                return
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "job_not_found",
                "The requested World Forge operation is unavailable.",
                "none",
                request_id,
            )
        except WorldSeedValidationError as error:
            issue_codes = {issue.code for issue in error.issues}
            if "recipe_unknown" in issue_codes:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "recipe_unknown",
                    "The World Forge recipe is unavailable.",
                    "none",
                    request_id,
                )
                return
            if "recipe_revision_mismatch" in issue_codes:
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "recipe_revision_mismatch",
                    "The World Forge recipe revision is no longer current.",
                    "none",
                    request_id,
                )
                return
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "validation_failed",
                "The World Forge seed contains invalid fields.",
                "none",
                request_id,
                issues=[issue.to_document() for issue in error.issues],
            )
        except ForgeServiceError as error:
            self._send_error(
                HTTPStatus(error.status),
                error.code,
                error.message,
                error.retry,
                request_id,
            )
        except (KeyError, TypeError, ValueError):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The World Forge request is invalid.",
                "none",
                request_id,
            )

    def _require_bearer(self) -> None:
        actual = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.preview_bearer
        if not hmac.compare_digest(actual, expected):
            raise ForgeServiceError(
                "authority_denied",
                "The requested World Forge resource is unavailable.",
                status=401,
            )

    def _authority_headers(self) -> tuple[str, str]:
        actor = self.headers.get(ACTOR_HEADER)
        epoch = self.headers.get(EPOCH_HEADER)
        if actor is None or epoch is None:
            raise ForgeServiceError(
                "authority_denied",
                "The requested World Forge resource is unavailable.",
                status=403,
            )
        return actor, epoch

    def _idempotency_key(self) -> str:
        value = self.headers.get(IDEMPOTENCY_HEADER)
        if value is None:
            raise ForgeServiceError(
                "invalid_request", "A valid idempotency key is required.", status=400
            )
        return value

    def _read_json(self, fields: set[str]) -> dict[str, Any]:
        media_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if media_type != "application/json":
            raise ForgeServiceError(
                "unsupported_media_type",
                "World Forge accepts application/json requests only.",
                status=415,
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as error:
            raise ForgeServiceError(
                "invalid_request", "A valid request length is required.", status=400
            ) from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ForgeServiceError(
                "request_too_large",
                "The World Forge request exceeds the private transport limit.",
                status=413,
            )
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ForgeServiceError(
                "invalid_request",
                "The World Forge request is not valid JSON.",
                status=400,
            ) from error
        if not isinstance(value, dict) or set(value) != fields:
            raise ForgeServiceError(
                "invalid_request",
                "The World Forge request shape is invalid.",
                status=400,
            )
        return value

    def _method_not_allowed(self) -> None:
        actual = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.preview_bearer
        if not hmac.compare_digest(actual, expected):
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "authority_denied",
                "The requested World Forge resource is unavailable.",
                "none",
                f"request:{time.monotonic_ns():x}",
            )
            return
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "invalid_request",
            "The requested World Forge method is unavailable.",
            "none",
            f"request:{time.monotonic_ns():x}",
        )

    def _send_json(
        self, status: HTTPStatus, payload: dict[str, Any], *, problem: bool = False
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if len(body) > MAX_RESPONSE_BYTES:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "response_too_large",
                "The World Forge response exceeded its safe transport limit.",
                "none",
                f"request:{time.monotonic_ns():x}",
            )
            return
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Type",
            "application/problem+json" if problem else "application/json",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        retry: str,
        request_id: str,
        *,
        issues: list[dict[str, str]] | None = None,
    ) -> None:
        error: dict[str, Any] = {
            "code": code,
            "message": message,
            "retry": retry,
            "retry_after_ms": 250 if code == "capacity_exhausted" else None,
        }
        if issues:
            error["issues"] = issues[:16]
        self._send_json(
            status,
            {"kind": "forge_error", "error": error, "request_id": request_id},
            problem=True,
        )


def _build_route(path: str) -> tuple[str | None, str]:
    prefix = "/forge/v1/builds/"
    if not path.startswith(prefix):
        return None, ""
    remainder = path[len(prefix) :]
    if remainder.endswith("/events"):
        raw, suffix = remainder[: -len("/events")], "/events"
    elif remainder.endswith("/cancel"):
        raw, suffix = remainder[: -len("/cancel")], "/cancel"
    else:
        raw, suffix = remainder, ""
    reference = unquote(raw)
    if not reference or "/" in reference or len(reference) > 256:
        return None, ""
    return reference, suffix


def _source() -> dict[str, Any]:
    return {
        "kind": "provider_free_integration_preview",
        "label": "Deterministic no-provider World Forge backend integration preview",
        "provider_calls": False,
        "live_forge_evidence": False,
    }


def build_server(
    *,
    host: str,
    port: int,
    bearer: str,
    bootstrap: ForgePreviewBootstrap,
    scenario: str,
    step_delay_ms: int,
    contract_root: Path,
) -> ForgePreviewServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("World Forge preview may bind only to explicit loopback")
    if not 0 <= port <= 65_535:
        raise ValueError("World Forge preview port must be between 0 and 65535")
    validated = _load_contract_tool().validate_contract(contract_root)
    if validated["contract_revision"] != CONTRACT_REVISION:
        raise ValueError("World Forge preview requires the exact current contract RC")
    harness = ForgePreviewHarness(
        bootstrap=bootstrap, scenario=scenario, step_delay_ms=step_delay_ms
    )
    try:
        return ForgePreviewServer(
            (host, port),
            bearer=bearer,
            harness=harness,
            contract_revision=CONTRACT_REVISION,
        )
    except Exception:
        harness.close()
        raise


def serve_preview(
    *,
    host: str,
    port: int,
    bearer: str,
    bootstrap: ForgePreviewBootstrap,
    scenario: str,
    step_delay_ms: int,
    contract_root: Path,
    once: bool,
) -> None:
    server = build_server(
        host=host,
        port=port,
        bearer=bearer,
        bootstrap=bootstrap,
        scenario=scenario,
        step_delay_ms=step_delay_ms,
        contract_root=contract_root,
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "contract_name": CONTRACT_NAME,
                "contract_revision": CONTRACT_REVISION,
                "host": address,
                "port": bound_port,
                "scenario": scenario,
                "source": _source(),
                "integration_only": True,
                "normal_runtime_composition": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        if once:
            server.handle_request_synchronously()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def _exclusive_owner_file(path: Path, text: str) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _read_owner_file(path: Path, *, maximum_bytes: int) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            "private World Forge file must be a regular non-symlink"
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("private World Forge file must be a regular non-symlink")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError(
                "private World Forge file must be owned by the current user"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("private World Forge file must be owner-only")
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError("private World Forge file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("private World Forge file must be UTF-8") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the provider-free DREAMLAND World Forge integration preview."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--scenario", choices=SCENARIOS, default="happy")
    serve.add_argument("--step-delay-ms", type=int, default=750)
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            initialize_private_files(
                token_file=arguments.token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(
                json.dumps(
                    {
                        "bootstrap_file": str(arguments.bootstrap_file),
                        "created": True,
                        "token_file": str(arguments.token_file),
                    },
                    sort_keys=True,
                )
            )
            return 0
        serve_preview(
            host=arguments.host,
            port=arguments.port,
            bearer=load_bearer(arguments.token_file),
            bootstrap=load_bootstrap(arguments.bootstrap_file),
            scenario=arguments.scenario,
            step_delay_ms=arguments.step_delay_ms,
            contract_root=arguments.contract_root,
            once=arguments.once,
        )
    except (OSError, ValueError) as error:
        print(f"World Forge preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
