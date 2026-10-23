#!/usr/bin/env python3
"""Serve the integration-only Phase 3 Presenter Stage facade on loopback.

This BFF compatibility harness is not DREAMLAND runtime composition.  It uses
the shared hardened preview transport and a deterministic no-provider executor,
and it never reports a live product capability as enabled.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.core import ProductCorePolicy
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)
from dreamland.product.stage import PresenterStageService, StageDisplayMetadataSeed


def _load_core_preview() -> ModuleType:
    path = Path(__file__).with_name("product_core_preview.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_core_preview_transport",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("product-core preview transport is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_CORE: Any = _load_core_preview()
_LIFECYCLE: Any = _CORE._LIFECYCLE_TRANSPORT

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.4.0-rc.1"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
PREVIEW_EXECUTOR = _CORE.PREVIEW_EXECUTOR

STAGE_LAUNCH_PATH = "/product/v1/stage/sessions"
STAGE_CURRENT_PATH = "/product/v1/stage/sessions/current"
STAGE_SESSION_PREFIX = "/product/v1/stage/sessions/"

SCENARIOS = (
    "stage-happy",
    "stage-launch-response-lost",
    "command-accept-response-lost",
    "promised-terminal-event-withheld",
)

PRESENTER_CONTEXT = "actor:stage_presenter_0001"
PRESENTER_ACTOR_ID = "stage-presenter-alpha"
STAGE_CREATE_GRANT = "launch-grant:stage_create_alpha_00000001"
STAGE_ALTERNATE_GRANT = "launch-grant:stage_create_alpha_00000002"
STAGE_REPLACE_GRANT = "launch-grant:stage_replace_alpha_00000001"
STAGE_REVISION_A = "world-revision:stage_alpha_revision_0001"
STAGE_REVISION_B = "world-revision:stage_bravo_revision_0001"
STAGE_SELECTION_A = "selected-record:stage_alpha_selection_0001"
STAGE_SELECTION_ALTERNATE = "selected-record:stage_alpha_selection_0002"
STAGE_SELECTION_B = "selected-record:stage_bravo_selection_0001"
STAGE_POLICY = "policy:stage-integration-preview-v1"
STAGE_NOTICE = "notice:stage-integration-preview-v1"

_CORE.CONTRACT_NAME = CONTRACT_NAME
_CORE.CONTRACT_REVISION = CONTRACT_REVISION
_CORE.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY


def _domain_problem(error: ProductLifecycleError, request_id: str) -> dict[str, object]:
    value = _CORE._problem(
        code=error.code,
        message=error.message,
        retry=error.retry,
        retry_after_ms=error.retry_after_ms,
        request_id=request_id,
    )
    if error.context:
        value["error"]["context"] = dict(error.context)
    return value


_CORE._domain_problem = _domain_problem


class StagePreviewHarness:
    """Own the explicit Stage service and bounded deterministic workers."""

    def __init__(self, scenario: str, *, clock: Any | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown Presenter Stage preview scenario")
        self.scenario = scenario
        self.clock = clock if clock is not None else _LIFECYCLE.MutablePreviewClock()
        self._workers = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="dreamland-stage-preview",
        )
        self._future_lock = threading.Lock()
        self._futures: set[Future[object]] = set()
        self._closed = False
        self._fault_lock = threading.Lock()
        self._stage_launch_response_lost = False
        self._command_accept_response_lost = False
        self._terminal_event_response_withheld = False
        try:
            self.service = self._build_service()
        except Exception:
            self._workers.shutdown(wait=True, cancel_futures=True)
            raise

    def _build_service(self) -> PresenterStageService:
        now_ms = self.clock.now_ms()
        seed_expiry = now_ms + 24 * 60 * _CORE.MILLISECONDS_PER_MINUTE
        launch_limits = {
            "absolute_ttl_ms": 30 * _CORE.MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 10 * _CORE.MILLISECONDS_PER_MINUTE,
            "max_commands": 8,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": 32,
            "command_reserved_spend_units": 32,
            "command_max_attempts": 1,
            "command_deadline_ms": 10_000,
        }
        return PresenterStageService(
            revisions=(
                ImmutableRevisionSeed(
                    revision_id=STAGE_REVISION_A,
                    document={"host": "stage-alpha", "private_seed": "alpha"},
                ),
                ImmutableRevisionSeed(
                    revision_id=STAGE_REVISION_B,
                    document={"host": "stage-bravo", "private_seed": "bravo"},
                ),
            ),
            actors=(
                ActorAuthoritySeed(
                    token=PRESENTER_CONTEXT,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=seed_expiry,
                ),
            ),
            launch_grants=(
                LaunchGrantSeed(
                    token=STAGE_CREATE_GRANT,
                    selected_record_id=STAGE_SELECTION_A,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    revision_id=STAGE_REVISION_A,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset({ProductOperation.CREATE}),
                    limits=launch_limits,
                    policy_id=STAGE_POLICY,
                    notice_version=STAGE_NOTICE,
                ),
                LaunchGrantSeed(
                    token=STAGE_ALTERNATE_GRANT,
                    selected_record_id=STAGE_SELECTION_ALTERNATE,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    revision_id=STAGE_REVISION_A,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset({ProductOperation.CREATE}),
                    limits=launch_limits,
                    policy_id="policy:stage-alternate-preview-v1",
                    notice_version=STAGE_NOTICE,
                ),
                LaunchGrantSeed(
                    token=STAGE_REPLACE_GRANT,
                    selected_record_id=STAGE_SELECTION_B,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    revision_id=STAGE_REVISION_B,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=launch_limits,
                    policy_id=STAGE_POLICY,
                    notice_version=STAGE_NOTICE,
                ),
            ),
            display_metadata=(
                StageDisplayMetadataSeed(
                    STAGE_REVISION_A,
                    "Presenter Demonstration World",
                    "Authored",
                    "Stage A",
                ),
                StageDisplayMetadataSeed(
                    STAGE_REVISION_B,
                    "Replacement Demonstration World",
                    "Authored",
                    "Stage B",
                ),
            ),
            executor=_CORE.DeterministicPreviewExecutor(),
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=30 * _CORE.MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * _CORE.MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * _CORE.MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * _CORE.MILLISECONDS_PER_MINUTE,
                max_active_sessions=4,
                max_tombstones=8,
                max_idempotency_records=64,
                max_launch_grants=8,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=16 * 1_024,
                max_output_chunks_per_command=16,
                reserved_tokens_per_command=32,
                reserved_spend_units_per_command=32,
                max_attempts_per_command=1,
            ),
            admission=ProductAdmissionLedger(
                ProductAdmissionLimits(
                    max_active_sessions=4,
                    max_logical_jobs=16,
                    max_physical_workers=4,
                    token_budget=4_096,
                    spend_budget_units=4_096,
                    presenter_stage_reserved_sessions=1,
                    presenter_stage_reserved_logical_jobs=8,
                    presenter_stage_reserved_physical_workers=2,
                    presenter_stage_reserved_token_budget=2_048,
                    presenter_stage_reserved_spend_budget_units=2_048,
                )
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_LIFECYCLE.PreviewTokens(1),
        )

    def submit_task(self, task: Any) -> None:
        if not callable(task):
            raise TypeError("Stage preview task must be callable")
        with self._future_lock:
            if self._closed:
                raise RuntimeError("Presenter Stage preview is closed")
            future = self._workers.submit(task)
            self._futures.add(future)
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future: Future[object]) -> None:
        with self._future_lock:
            self._futures.discard(future)

    def should_drop_stage_launch_response(self) -> bool:
        with self._fault_lock:
            if (
                self.scenario == "stage-launch-response-lost"
                and not self._stage_launch_response_lost
            ):
                self._stage_launch_response_lost = True
                return True
        return False

    def should_drop_command_accept_response(self) -> bool:
        with self._fault_lock:
            if (
                self.scenario == "command-accept-response-lost"
                and not self._command_accept_response_lost
            ):
                self._command_accept_response_lost = True
                return True
        return False

    def should_withhold_terminal_event_response(
        self,
        page: Mapping[str, object],
    ) -> bool:
        if self.scenario != "promised-terminal-event-withheld":
            return False
        events = page.get("events")
        if not isinstance(events, list) or not any(
            isinstance(event, Mapping)
            and event.get("type")
            in {"command.completed", "command.interrupted", "command.failed"}
            for event in events
        ):
            return False
        with self._fault_lock:
            if self._terminal_event_response_withheld:
                return False
            self._terminal_event_response_withheld = True
            return True

    def close(self) -> None:
        with self._future_lock:
            if self._closed:
                return
            self._closed = True
        self._workers.shutdown(wait=True, cancel_futures=True)


class StageHarness(Protocol):
    service: PresenterStageService

    def should_drop_stage_launch_response(self) -> bool: ...

    def should_drop_command_accept_response(self) -> bool: ...

    def should_withhold_terminal_event_response(
        self,
        page: Mapping[str, object],
    ) -> bool: ...

    def close(self) -> None: ...


class ProductStagePreviewServer(_CORE._LifecyclePreviewServerBase):
    """The hardened preview transport with an explicit Stage-only harness."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        scenario: str,
        contract_root: Path,
        harness_factory: Callable[[str], StageHarness] = StagePreviewHarness,
        handler_class: type[ProductStagePreviewHandler] | None = None,
    ) -> None:
        self.preview_bearer = bearer
        self.contract_root = contract_root
        self.preview: StageHarness = harness_factory(scenario)
        self._request_sequence = 0
        self._request_lock = threading.Lock()
        self._connection_slots = threading.BoundedSemaphore(
            _CORE.MAX_CONCURRENT_CONNECTIONS
        )
        self._connection_count_lock = threading.Lock()
        self._active_connections = 0
        self._maximum_observed_connections = 0
        self._synchronous_request = False
        try:
            ThreadingHTTPServer.__init__(
                self,
                address,
                handler_class or ProductStagePreviewHandler,
            )
        except Exception:
            self.preview.close()
            raise

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
        return f"request:stage-preview-{sequence:08d}"

    def server_close(self) -> None:
        try:
            HTTPServer.server_close(self)
        finally:
            self.preview.close()


class ProductStagePreviewHandler(_CORE.ProductCorePreviewHandler):
    server: ProductStagePreviewServer
    server_version = "DREAMLANDProductStagePreview/0.4"

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == STAGE_LAUNCH_PATH:
            return "create", None, None, parsed.query
        if parsed.path == STAGE_CURRENT_PATH:
            return "current", None, None, parsed.query
        if parsed.path.startswith(STAGE_SESSION_PREFIX):
            parts = parsed.path.removeprefix(STAGE_SESSION_PREFIX).split("/")
            if len(parts) == 2 and parts[1] == "replace":
                reference = unquote(parts[0])
                if _CORE.SESSION_REFERENCE_PATTERN.fullmatch(reference):
                    return "switch", reference, None, parsed.query
            return "not-found", None, None, parsed.query
        operation, reference, command_id, query = super()._route()
        if operation in {"create", "current", "switch"}:
            return "not-found", None, None, query
        return operation, reference, command_id, query

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        value = super()._dispatch_post(
            operation,
            reference,
            command_id,
            raw_query,
            request_id,
        )
        if (
            operation == "create"
            and self.server.preview.should_drop_stage_launch_response()
        ):
            self._drop_connection()
            return None
        return value


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_stage_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("Presenter Stage contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("Presenter Stage contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 15
    ):
        raise ValueError("Presenter Stage contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("Presenter Stage preview capabilities must remain disabled")
    return root


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
) -> ProductStagePreviewServer:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("Presenter Stage preview supports only loopback hosts")
    if port < 0 or port > 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown Presenter Stage preview scenario")
    return ProductStagePreviewServer(
        (host, port),
        bearer=bearer,
        scenario=scenario,
        contract_root=validate_contract_root(contract_root),
    )


def serve_preview(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
    once: bool,
) -> None:
    server = build_preview_server(
        host=host,
        port=port,
        bearer=bearer,
        scenario=scenario,
        contract_root=contract_root,
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "contract_revision": CONTRACT_REVISION,
                "executor": PREVIEW_EXECUTOR,
                "host": address,
                "integration_only": True,
                "live_capability_evidence": False,
                "port": bound_port,
                "scenario": scenario,
                "test_only": True,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the integration-only DREAMLAND Presenter Stage preview."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    bearer_source = parser.add_mutually_exclusive_group(required=True)
    bearer_source.add_argument("--token")
    bearer_source.add_argument("--token-file", type=Path)
    parser.add_argument("--contract-root", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bearer = (
            arguments.token
            if arguments.token is not None
            else _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        )
        serve_preview(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            scenario=arguments.scenario,
            contract_root=arguments.contract_root,
            once=arguments.once,
        )
    except (OSError, ValueError) as error:
        print(f"Presenter Stage preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
