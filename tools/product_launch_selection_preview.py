#!/usr/bin/env python3
"""Serve the provider-free Phase 7A Presenter launch-selection preview.

This loopback-only tool is explicit integration composition. Importing it
creates no provider, runtime, listener, worker, credential, or product
capability. The normal DREAMLAND SSH/runtime composition does not import it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.core import ProductCorePolicy
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.launch_selection_stage import (
    PresenterStageLaunchSelectionService,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)
from dreamland.product.stage import PresenterStageService
from dreamland.product.xray import StageXrayPolicy


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_launch_selection_preview",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_COMBINED: Any = _load_tool("product_forge_stage_live")
_STAGE: Any = _COMBINED._STAGE
_CORE: Any = _COMBINED._CORE
_LIFECYCLE: Any = _COMBINED._LIFECYCLE

CONTRACT_REVISION = "0.7.0-rc.1"
PREVIEW_EXECUTOR = "deterministic-launch-selection-no-provider-v1"
LAUNCH_OPTIONS_PATH = "/product/v1/stage/launch-options"
LAUNCH_SELECTIONS_PATH = "/product/v1/stage/launch-selections"
LAUNCH_SELECTION_ID_PATTERN = re.compile(r"^launch-selection:[A-Za-z0-9_-]{24,128}$")
PRESENTER_CONTEXT = "actor:launch_selection_presenter_0001"
PRESENTER_ACTOR_ID = "launch-selection-presenter"
AUTHORED_SELECTION_ID = "launch-selection:authored_preview_selection_00000001"
FORGED_SELECTION_ID = "launch-selection:forged_preview_selection_000000002"
SCENARIOS = (
    "launch-selection-happy",
    "launch-selection-empty",
    "launch-selection-exchange-response-lost",
)
MILLISECONDS_PER_MINUTE = 60_000


class _SelectionTokens:
    """Deterministic contract-compatible tokens under registry locking."""

    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch
        self._sequence = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose != "launch-grant":
            raise ValueError("unsupported launch-selection token purpose")
        self._sequence += 1
        return f"launch-grant:preview_grant_token_{self._sequence:08d}"


class LaunchSelectionPreviewHarness(_STAGE.StagePreviewHarness):
    """Own a dynamic Stage and source-neutral provider-free selection service."""

    def __init__(self, scenario: str, *, clock: Any | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown launch-selection preview scenario")
        self._selection_response_lock = threading.Lock()
        self._selection_response_lost = False
        super().__init__(_STAGE.SCENARIOS[0], clock=clock)
        self.scenario = scenario
        self.selection = self._build_selection_service(empty=scenario.endswith("empty"))

    def _build_service(self) -> PresenterStageService:
        now_ms = self.clock.now_ms()
        actor_expiry_ms = now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE
        return PresenterStageService(
            revisions=(),
            actors=(
                ActorAuthoritySeed(
                    token=PRESENTER_CONTEXT,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=actor_expiry_ms,
                ),
            ),
            launch_grants=(),
            display_metadata=(),
            executor=_CORE.DeterministicPreviewExecutor(),
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=4,
                max_tombstones=8,
                max_idempotency_records=128,
                max_launch_grants=32,
                max_actor_authorities=4,
                max_revisions=16,
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
            dynamic_selection_only=True,
            xray_policy=StageXrayPolicy(),
        )

    def _build_selection_service(
        self, *, empty: bool
    ) -> PresenterStageLaunchSelectionService:
        now_ms = self.clock.now_ms()
        presenter_stage_actors = tuple(
            actor
            for actor in self.service._actors.values()
            if actor.key == (PRESENTER_ACTOR_ID, ProductRole.PRESENTER)
        )
        if len(presenter_stage_actors) != 1:
            raise RuntimeError("preview Presenter Stage authority is unavailable")
        # A live-advancing clock may tick between building Stage and its adjacent
        # selection registry. Reuse Stage's exact authority boundary so the
        # registry can never advertise a grant even one millisecond longer.
        actor_expiry_ms = presenter_stage_actors[0].expires_at_ms
        authored = _revision_seed(_authored_document())
        forged = _revision_seed(_forged_document())
        revisions = () if empty else (authored, forged)
        offerings = (
            ()
            if empty
            else (
                _offering_seed(
                    authored,
                    offering_id="offering:authored-preview",
                    launch_selection_id=AUTHORED_SELECTION_ID,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title="Configured authored world",
                    source_label="Authored",
                    revision_label="Pinned preview snapshot",
                    expires_at_ms=now_ms + 60 * MILLISECONDS_PER_MINUTE,
                ),
                _offering_seed(
                    forged,
                    offering_id="offering:forged-preview",
                    launch_selection_id=FORGED_SELECTION_ID,
                    origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                    title="Selected World Forge revision",
                    source_label="World Forge",
                    revision_label="Immutable preview selection",
                    expires_at_ms=now_ms + 60 * MILLISECONDS_PER_MINUTE,
                ),
            )
        )
        registry = LaunchSelectionRegistry(
            revisions=revisions,
            offerings=offerings,
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_CONTEXT,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=actor_expiry_ms,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=16,
                max_offerings=16,
                max_actor_authorities=4,
                max_active_launch_grants=32,
                max_idempotency_records=128,
                max_token_history=128,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                idempotency_ttl_ms=40 * MILLISECONDS_PER_MINUTE,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_SelectionTokens(self.service.service_epoch),
        )
        return PresenterStageLaunchSelectionService(
            registry=registry, stage=self.service
        )

    def should_drop_launch_selection_exchange_response(self) -> bool:
        with self._selection_response_lock:
            if (
                self.scenario == "launch-selection-exchange-response-lost"
                and not self._selection_response_lost
            ):
                self._selection_response_lost = True
                return True
        return False


class _UnavailableLegacyForgeHandoff:
    """Provider-free preview must not synthesize authoritative Forge state."""

    def launch(self, **_arguments: object) -> dict[str, object]:
        raise _legacy_forge_unavailable()

    def replace(self, **_arguments: object) -> dict[str, object]:
        raise _legacy_forge_unavailable()


class LaunchSelectionPreviewServer(_STAGE.ProductStagePreviewServer):
    """Hardened Stage/X-ray transport plus two launch-selection routes."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        scenario: str,
        contract_root: Path,
        harness_factory: Callable[[str], LaunchSelectionPreviewHarness] = (
            LaunchSelectionPreviewHarness
        ),
    ) -> None:
        self.handoff = _UnavailableLegacyForgeHandoff()
        super().__init__(
            address,
            bearer=bearer,
            scenario=scenario,
            contract_root=contract_root,
            harness_factory=harness_factory,
            handler_class=LaunchSelectionPreviewHandler,
        )


class LaunchSelectionPreviewHandler(_COMBINED.ForgeStagePreviewHandler):
    server: LaunchSelectionPreviewServer
    server_version = "DREAMLANDLaunchSelectionPreview/0.7"

    def _additional_get_operations(self) -> frozenset[str]:
        return super()._additional_get_operations() | frozenset({"launch-options"})

    def _additional_post_operations(self) -> frozenset[str]:
        return super()._additional_post_operations() | frozenset({"launch-selection"})

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == LAUNCH_OPTIONS_PATH:
            return "launch-options", None, None, parsed.query
        if parsed.path == LAUNCH_SELECTIONS_PATH:
            return "launch-selection", None, None, parsed.query
        return super()._route()

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation != "launch-options":
            return super()._dispatch_get(
                operation, reference, command_id, raw_query, request_id
            )
        if _CORE._query_pairs(raw_query):
            raise _LIFECYCLE.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        expected_epoch, actor_context = self._read_context()
        try:
            domain = self.server.preview.selection.options(
                actor_authority=actor_context,
                expected_epoch=expected_epoch,
            )
        except LaunchSelectionError as error:
            raise _selection_lifecycle_error(error) from None
        response = _CORE._decorate_success(domain, request_id=request_id)
        response.pop("transport_request_id", None)
        return response

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation != "launch-selection":
            return super()._dispatch_post(
                operation, reference, command_id, raw_query, request_id
            )
        if _CORE._query_pairs(raw_query):
            raise _LIFECYCLE.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        expected_epoch, actor_context = self._read_context()
        idempotency_key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _CORE.IDEMPOTENCY_KEY_PATTERN,
        )
        body = self._read_json_object(frozenset({"launch_selection_id"}))
        launch_selection_id = body["launch_selection_id"]
        if (
            not isinstance(launch_selection_id, str)
            or LAUNCH_SELECTION_ID_PATTERN.fullmatch(launch_selection_id) is None
        ):
            raise _LIFECYCLE.PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The Presenter Stage launch selection is invalid.",
            )
        try:
            domain = self.server.preview.selection.exchange(
                actor_authority=actor_context,
                expected_epoch=expected_epoch,
                launch_selection_id=launch_selection_id,
                idempotency_key=idempotency_key,
            )
        except LaunchSelectionError as error:
            raise _selection_lifecycle_error(error) from None
        if self.server.preview.should_drop_launch_selection_exchange_response():
            self._drop_connection()
            return None
        response = _CORE._decorate_success(domain, request_id=request_id)
        response.pop("transport_request_id", None)
        return response


def _selection_lifecycle_error(error: LaunchSelectionError) -> ProductLifecycleError:
    translated = {
        "request_invalid": "invalid_request",
        "service_epoch_changed": "session_lost",
        "launch_selection_unavailable": "launch_selection_unavailable",
        "launch_selection_capacity_exhausted": ("launch_selection_capacity_exhausted"),
        "authority_denied": "authority_denied",
        "expected_epoch_required": "expected_epoch_required",
        "idempotency_conflict": "idempotency_conflict",
        "outcome_unknown": "outcome_unknown",
    }.get(error.code)
    if translated is None:
        raise RuntimeError("unexpected launch-selection domain error")
    status, message, retry, retry_after_ms = _CORE.SAFE_ERROR_DETAILS[translated]
    return ProductLifecycleError(
        code=translated,
        message=message,
        status=status,
        retry=retry,
        retry_after_ms=retry_after_ms,
    )


def _legacy_forge_unavailable() -> ProductLifecycleError:
    status, message, retry, retry_after_ms = _CORE.SAFE_ERROR_DETAILS[
        "selected_record_unavailable"
    ]
    return ProductLifecycleError(
        code="selected_record_unavailable",
        message=message,
        status=status,
        retry=retry,
        retry_after_ms=retry_after_ms,
    )


def _revision_seed(document: dict[str, object]) -> LaunchRevisionSeed:
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(
        revision_id=f"world-revision:sha256:{digest}",
        document=document,
    )


def _offering_seed(
    revision: LaunchRevisionSeed,
    *,
    offering_id: str,
    launch_selection_id: str,
    origin: LaunchSelectionOrigin,
    title: str,
    source_label: str,
    revision_label: str,
    expires_at_ms: int,
) -> LaunchOfferingSeed:
    return LaunchOfferingSeed(
        offering_id=offering_id,
        launch_selection_id=launch_selection_id,
        revision_id=revision.revision_id,
        origin=origin,
        title=title,
        source_label=source_label,
        revision_label=revision_label,
        role=ProductRole.PRESENTER,
        allowed_operations=frozenset(
            {ProductOperation.CREATE, ProductOperation.SWITCH}
        ),
        limits={
            "absolute_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
            "max_commands": 8,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": 32,
            "command_reserved_spend_units": 32,
            "command_max_attempts": 1,
            "command_deadline_ms": 10_000,
        },
        policy_id="policy:launch-selection-preview-v1",
        notice_version="notice:launch-selection-preview-v1",
        expires_at_ms=expires_at_ms,
        launch_grant_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
    )


def _authored_document() -> dict[str, object]:
    return {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "configured-authored-preview",
                    "entry_host": "authored-console",
                    "active_host": "authored-console",
                    "hosts_by_name": {
                        "authored-console": {
                            "hostname": "authored-console",
                            "login_banner": "Authored console ready.\n",
                            "purpose": {"theme": "Configured authored preview"},
                        }
                    },
                }
            },
        },
    }


def _forged_document() -> dict[str, object]:
    return {
        "kind": "dreamland.world_forge_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "selected-forge-preview",
                    "entry_host": "forge-console",
                    "active_host": "forge-console",
                    "hosts_by_name": {
                        "forge-console": {
                            "hostname": "forge-console",
                            "login_banner": "Forge console ready.\n",
                            "purpose": {"theme": "Selected Forge preview"},
                        }
                    },
                }
            },
        },
    }


def _configure_transport() -> None:
    """Configure only the private dynamically loaded preview module stack."""
    _COMBINED._configure_combined_transport()
    for module in (_COMBINED, _STAGE, _CORE):
        module.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = False
    _CORE.PREVIEW_TEST_ONLY = True
    additions = {
        "launch_selection_unavailable": (
            404,
            "The Presenter Stage launch selection is unavailable.",
            "none",
            None,
        ),
        "launch_selection_capacity_exhausted": (
            503,
            "Presenter Stage launch-selection capacity is exhausted.",
            "after_delay",
            1_000,
        ),
    }
    for code, detail in additions.items():
        existing = _CORE.SAFE_ERROR_DETAILS.get(code)
        if existing is not None and existing != detail:
            raise RuntimeError("launch-selection safe error configuration conflicts")
        _CORE.SAFE_ERROR_DETAILS[code] = detail


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_launch_selection_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_preview_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("launch-selection contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("launch-selection contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 21
    ):
        raise ValueError("launch-selection contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("launch-selection capabilities must remain disabled")
    return root


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
) -> LaunchSelectionPreviewServer:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("launch-selection preview supports only loopback hosts")
    if not 0 <= port <= 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown launch-selection preview scenario")
    root = validate_contract_root(contract_root)
    _configure_transport()
    return LaunchSelectionPreviewServer(
        (host, port),
        bearer=bearer,
        scenario=scenario,
        contract_root=root,
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
                "capabilities_enabled": 0,
                "contract_revision": CONTRACT_REVISION,
                "executor": PREVIEW_EXECUTOR,
                "host": address,
                "integration_only": True,
                "live_capability_evidence": False,
                "normal_runtime_composition": False,
                "port": bound_port,
                "provider_calls": False,
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
        description="Serve the provider-free Presenter launch-selection preview."
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
        print(f"Launch-selection preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
