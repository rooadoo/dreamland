#!/usr/bin/env python3
"""Run the explicit live World Forge-to-Presenter Stage development target.

The two private HTTP listeners are loopback-only and are created only by the
``serve`` command.  This tool is deliberately separate from normal DREAMLAND
composition: importing it creates no provider, worker, listener, credential,
or product capability.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import secrets
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

from dreamland.config import DreamlandSettings, load_settings
from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.forge import ForgeServiceError
from dreamland.product.forge_artifacts import SingleAttemptForge
from dreamland.product.forge_runtime import ForgeStateProfileCodec
from dreamland.product.forge_stage import (
    ForgeStageHandoffPolicy,
    ForgeStageHandoffService,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductRole,
)
from dreamland.product.stage import PresenterStageService
from dreamland.product.xray import StageXrayPolicy
from dreamland.runtime_logging import configure_console_logging

CONTRACT_REVISION = "0.6.0-rc.3"
FORGE_CONTRACT_REVISION = "0.1.0-rc.4"
LIVE_EXECUTOR = "development-live-forge-stage-xray-langgraph-v1"
LIVE_SCENARIO = "forge-stage-live-development"
PRESENTER_ACTOR_ID = "forge-live-presenter"
FORGE_HANDOFF_PATH = "/product/v1/stage/forge-handoffs"
FORGE_REPLACEMENT_SUFFIX = "/forge-replacement"
XRAY_SUFFIX = "/xray"
FORGE_EXPECTED_EPOCH_HEADER = "X-Dreamland-Forge-Expected-Epoch"
NOTICE_VERSION = "notice:forge-stage-live-development-v1"
MILLISECONDS_PER_MINUTE = 60_000
MAXIMUM_FORGE_JOBS = 16
SELECTED_HANDOFF_TTL_SECONDS = 3_600
WORLD_FORGE_DEPENDENCY = {
    "compatibility": "exact",
    "contract_name": "dreamland-private-world-forge-api",
    "contract_revision": FORGE_CONTRACT_REVISION,
    "manifest_path": "contracts/world-forge-api/v0.1.0-rc.4/manifest.json",
    "manifest_sha256": (
        "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09"
    ),
}
FORGE_EPOCH_PATTERN = re.compile(r"^forge-epoch:[A-Za-z0-9_-]{16,128}$")
FORGE_REFERENCE_PATTERN = re.compile(r"^forge-reference:[A-Za-z0-9_-]{16,128}$")
XRAY_ENTITY_PATTERN = re.compile(r"^(?:host|fact):[0-9a-f]{32}$")
XRAY_STREAM_PATTERN = re.compile(r"^xray-stream:[0-9a-f]{32}$")


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_combined_live",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_FORGE_LIVE: Any = _load_tool("product_forge_live")
_FORGE_TRANSPORT: Any = _FORGE_LIVE._TRANSPORT
_STAGE_LIVE: Any = _load_tool("product_stage_live")
_STAGE: Any = _STAGE_LIVE._STAGE
_CORE: Any = _STAGE_LIVE._CORE
_LIFECYCLE: Any = _STAGE_LIVE._LIFECYCLE


@dataclass(frozen=True, slots=True)
class ForgeStageBootstrap:
    """One presenter authority shared by the two private services."""

    actor_context: str


@dataclass(frozen=True, slots=True)
class LiveForgeStageComposition:
    """Explicit provider-backed collaborators and their safe announcement data."""

    actor_context: str
    forge_runner: SingleAttemptForge
    forge_model: str
    forge_max_output_tokens: int | None
    forge_deadline_seconds: int
    stage_executor: ProductCommandExecutor
    stage_model: str
    stage_max_output_tokens: int | None
    stage_command_reserved_tokens: int
    max_commands: int | None


class LiveForgeStageHarness(_STAGE.StagePreviewHarness):
    """Own a dynamic-only Stage using the reviewed live command executor."""

    def __init__(self, composition: LiveForgeStageComposition) -> None:
        expected_reservation = _STAGE_LIVE.resolve_live_command_reservation(
            composition.stage_max_output_tokens,
            max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
        )
        if composition.stage_command_reserved_tokens != expected_reservation:
            raise ValueError(
                "live Presenter command reservation does not match provider maximum"
            )
        self._composition = composition
        super().__init__(_STAGE.SCENARIOS[0])
        self.scenario = LIVE_SCENARIO

    def _build_service(self) -> PresenterStageService:
        composition = self._composition
        now_ms = _STAGE_LIVE._utc_ms()
        actor_expiry_ms = now_ms + 12 * 60 * MILLISECONDS_PER_MINUTE
        return PresenterStageService(
            revisions=(),
            actors=(
                ActorAuthoritySeed(
                    token=composition.actor_context,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=actor_expiry_ms,
                ),
            ),
            launch_grants=(),
            display_metadata=(),
            executor=composition.stage_executor,
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=2 * 60 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=128,
                max_launch_grants=32,
                max_revisions=16,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=composition.max_commands,
                command_deadline_ms=_STAGE_LIVE.COMMAND_DEADLINE_MS,
                cancellation_deadline_ms=5_000,
                max_output_bytes_per_command=(_STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND),
                max_output_chunks_per_command=(
                    _STAGE_LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND
                ),
                reserved_tokens_per_command=composition.stage_command_reserved_tokens,
                reserved_spend_units_per_command=(
                    _STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND
                ),
                max_attempts_per_command=1,
            ),
            admission=ProductAdmissionLedger(
                ProductAdmissionLimits(
                    max_active_sessions=2,
                    max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
                    max_physical_workers=2,
                    token_budget=_STAGE_LIVE.live_token_budget(
                        composition.stage_command_reserved_tokens,
                        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
                    ),
                    spend_budget_units=_STAGE_LIVE.SPEND_BUDGET_UNITS,
                    presenter_stage_reserved_sessions=1,
                    presenter_stage_reserved_logical_jobs=(
                        _STAGE_LIVE.MAX_LOGICAL_JOBS
                    ),
                    presenter_stage_reserved_physical_workers=2,
                    presenter_stage_reserved_token_budget=(
                        _STAGE_LIVE.live_token_budget(
                            composition.stage_command_reserved_tokens,
                            max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
                        )
                    ),
                    presenter_stage_reserved_spend_budget_units=(
                        _STAGE_LIVE.SPEND_BUDGET_UNITS
                    ),
                )
            ),
            clock_ms=_STAGE_LIVE._utc_ms,
            monotonic_ms=_STAGE_LIVE._monotonic_ms,
            token_factory=_STAGE_LIVE._live_token_factory,
            dynamic_selection_only=True,
            xray_policy=StageXrayPolicy(),
        )


class ForgeStagePreviewServer(_STAGE.ProductStagePreviewServer):
    """Stage transport with only the two additive handoff mutations."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        contract_root: Path,
        harness: LiveForgeStageHarness,
        handoff: ForgeStageHandoffService,
    ) -> None:
        self.handoff = handoff
        super().__init__(
            address,
            bearer=bearer,
            scenario=LIVE_SCENARIO,
            contract_root=contract_root,
            harness_factory=lambda _scenario: harness,
            handler_class=ForgeStagePreviewHandler,
        )


class ForgeStagePreviewHandler(_STAGE.ProductStagePreviewHandler):
    server: ForgeStagePreviewServer
    server_version = "DREAMLANDForgeStageLive/0.6"

    def _additional_get_operations(self) -> frozenset[str]:
        return frozenset({"xray", "xray-detail"})

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == FORGE_HANDOFF_PATH:
            return "create", None, None, parsed.query
        prefix = _STAGE.STAGE_SESSION_PREFIX
        if parsed.path.startswith(prefix):
            parts = parsed.path[len(prefix) :].split("/")
            raw_reference = parts[0] if parts else ""
            reference = unquote(raw_reference)
            valid_reference = (
                bool(raw_reference)
                and _CORE.SESSION_REFERENCE_PATTERN.fullmatch(reference) is not None
            )
            if len(parts) == 2 and parts[1] == "xray" and valid_reference:
                return "xray", reference, None, parsed.query
            if (
                len(parts) == 4
                and parts[1:3] == ["xray", "details"]
                and valid_reference
            ):
                entity_id = unquote(parts[3])
                if XRAY_ENTITY_PATTERN.fullmatch(entity_id):
                    return "xray-detail", reference, entity_id, parsed.query
                return "not-found", None, None, parsed.query
        if parsed.path.startswith(prefix) and parsed.path.endswith(
            FORGE_REPLACEMENT_SUFFIX
        ):
            raw_reference = parsed.path[len(prefix) : -len(FORGE_REPLACEMENT_SUFFIX)]
            reference = unquote(raw_reference)
            if (
                raw_reference
                and "/" not in raw_reference
                and _CORE.SESSION_REFERENCE_PATTERN.fullmatch(reference)
            ):
                return "switch", reference, None, parsed.query
            return "not-found", None, None, parsed.query
        return super()._route()

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation not in {"xray", "xray-detail"}:
            return super()._dispatch_get(
                operation,
                reference,
                command_id,
                raw_query,
                request_id,
            )
        if reference is None:
            raise _LIFECYCLE.PreviewTransportError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "Unavailable route.",
            )
        expected_epoch, actor_context, session_authority = self._session_context()
        query = _CORE._query_pairs(raw_query)
        if operation == "xray":
            if query:
                raise _LIFECYCLE.PreviewTransportError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "Unavailable route.",
                )
            result = self.server.preview.service.xray_snapshot(
                actor_authority=actor_context,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
        else:
            if command_id is None or set(query) != {
                "accepted_state_revision",
                "xray_stream_id",
                "xray_sequence",
            }:
                raise _LIFECYCLE.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The Stage X-ray detail precondition is invalid.",
                )
            try:
                accepted_revision = _xray_counter(
                    query["accepted_state_revision"], allow_zero=True
                )
                sequence = _xray_counter(query["xray_sequence"], allow_zero=False)
                stream_id = query["xray_stream_id"]
                if XRAY_STREAM_PATTERN.fullmatch(stream_id) is None:
                    raise ValueError("X-ray stream ID is invalid")
            except ValueError as error:
                raise _LIFECYCLE.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The Stage X-ray detail precondition is invalid.",
                ) from error
            result = self.server.preview.service.xray_detail(
                actor_authority=actor_context,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                entity_id=command_id,
                expected_accepted_state_revision=accepted_revision,
                expected_xray_stream_id=stream_id,
                expected_xray_sequence=sequence,
            )
        response = _CORE._decorate_success(result, request_id=request_id)
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
        parsed_path = urlsplit(self.path).path
        if parsed_path != FORGE_HANDOFF_PATH and not parsed_path.endswith(
            FORGE_REPLACEMENT_SUFFIX
        ):
            return super()._dispatch_post(
                operation,
                reference,
                command_id,
                raw_query,
                request_id,
            )
        if _CORE._query_pairs(raw_query):
            raise _LIFECYCLE.PreviewTransportError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "Unavailable route.",
            )
        stage_epoch, actor_context = self._read_context()
        forge_epoch = self._required_header(
            FORGE_EXPECTED_EPOCH_HEADER,
            FORGE_EPOCH_PATTERN,
        )
        idempotency_key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _CORE.IDEMPOTENCY_KEY_PATTERN,
        )
        try:
            if parsed_path == FORGE_HANDOFF_PATH and operation == "create":
                body = self._read_json_object(frozenset({"forge_reference"}))
                forge_reference = _require_forge_reference(body["forge_reference"])
                result = self.server.handoff.launch(
                    actor_authority=actor_context,
                    forge_expected_epoch=forge_epoch,
                    stage_expected_epoch=stage_epoch,
                    forge_reference=forge_reference,
                    idempotency_key=idempotency_key,
                )
            elif operation == "switch" and reference is not None:
                body = self._read_json_object(
                    frozenset({"forge_reference", "expected_control_revision"})
                )
                forge_reference = _require_forge_reference(body["forge_reference"])
                control_revision = _CORE._required_generation(
                    body["expected_control_revision"],
                    allow_zero=False,
                )
                session_authority = self._required_header(
                    _CORE.SESSION_CAPABILITY_HEADER,
                    _CORE.SESSION_CAPABILITY_PATTERN,
                )
                result = self.server.handoff.replace(
                    actor_authority=actor_context,
                    forge_expected_epoch=forge_epoch,
                    stage_expected_epoch=stage_epoch,
                    forge_reference=forge_reference,
                    idempotency_key=idempotency_key,
                    session_authority=session_authority,
                    stage_reference=reference,
                    expected_control_revision=control_revision,
                )
            else:
                raise _LIFECYCLE.PreviewTransportError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "Unavailable route.",
                )
        except ForgeServiceError as error:
            raise _forge_lifecycle_error(error) from None
        response = _CORE._decorate_success(result, request_id=request_id)
        # The immutable product response envelope intentionally excludes the
        # preview transport's internal request sequence.  Keep that sequence in
        # errors/logs without leaking an undeclared success-response property.
        response.pop("transport_request_id", None)
        return response


@dataclass(frozen=True, slots=True)
class LiveServerPair:
    forge: Any
    stage: ForgeStagePreviewServer


def initialize_private_files(
    *,
    forge_token_file: Path,
    stage_token_file: Path,
    bootstrap_file: Path,
) -> None:
    """Create distinct owner-only bearers and one shared presenter context."""
    paths = {
        path.expanduser().absolute()
        for path in (forge_token_file, stage_token_file, bootstrap_file)
    }
    if len(paths) != 3:
        raise ValueError("Forge, Stage, and bootstrap paths must differ")
    _STAGE_LIVE._exclusive_owner_file(
        forge_token_file,
        f"forge-preview-bearer:{secrets.token_urlsafe(32)}\n",
    )
    _STAGE_LIVE._exclusive_owner_file(
        stage_token_file,
        f"preview-bearer:{secrets.token_urlsafe(32)}\n",
    )
    _STAGE_LIVE._exclusive_owner_file(
        bootstrap_file,
        json.dumps(
            {"actor_context": f"actor:{secrets.token_urlsafe(32)}"},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def load_bootstrap(path: Path) -> ForgeStageBootstrap:
    try:
        value = json.loads(_STAGE_LIVE._read_owner_file(path, maximum_bytes=2_048))
    except json.JSONDecodeError as error:
        raise ValueError("Forge-to-Stage bootstrap is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"actor_context"}:
        raise ValueError("Forge-to-Stage bootstrap has invalid fields")
    actor_context = value["actor_context"]
    if not isinstance(actor_context, str) or not (
        _CORE.ACTOR_CONTEXT_PATTERN.fullmatch(actor_context)
    ):
        raise ValueError("Forge-to-Stage actor context is malformed")
    return ForgeStageBootstrap(actor_context)


def build_live_composition(
    *,
    settings: DreamlandSettings,
    actor_context: str,
    artifact_root: Path,
    deadline_seconds: int,
    max_commands: int | None,
) -> LiveForgeStageComposition:
    if deadline_seconds <= 0:
        raise ValueError("deadline seconds must be positive")
    _STAGE_LIVE._validate_max_commands(max_commands)
    stage_command_reserved_tokens = _STAGE_LIVE.resolve_live_command_reservation(
        settings.agent.max_output_tokens,
        max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
    )
    forge_runner, forge_model, forge_max_output_tokens = _FORGE_LIVE.build_runner(
        settings,
        artifact_root,
    )
    stage_executor, stage_model, stage_max_output_tokens = (
        _STAGE_LIVE.build_live_executor(
            settings=settings,
            state_codec=ForgeStateProfileCodec(),
        )
    )
    return LiveForgeStageComposition(
        actor_context=actor_context,
        forge_runner=forge_runner,
        forge_model=forge_model,
        forge_max_output_tokens=forge_max_output_tokens,
        forge_deadline_seconds=deadline_seconds,
        stage_executor=stage_executor,
        stage_model=stage_model,
        stage_max_output_tokens=stage_max_output_tokens,
        stage_command_reserved_tokens=stage_command_reserved_tokens,
        max_commands=max_commands,
    )


def build_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    forge_contract_root: Path,
    stage_contract_root: Path,
    composition: LiveForgeStageComposition,
) -> LiveServerPair:
    _validate_listener(host, forge_port, "Forge")
    _validate_listener(host, stage_port, "Stage")
    if forge_port and forge_port == stage_port:
        raise ValueError("Forge and Stage ports must differ")
    _LIFECYCLE._validate_preview_bearer(stage_bearer)
    _validate_forge_bearer(forge_bearer)
    validated_forge_root, validated_stage_root = validate_contract_dependency(
        forge_contract_root=forge_contract_root,
        stage_contract_root=stage_contract_root,
    )
    return _build_validated_server_pair(
        host=host,
        forge_port=forge_port,
        stage_port=stage_port,
        forge_bearer=forge_bearer,
        stage_bearer=stage_bearer,
        validated_forge_root=validated_forge_root,
        validated_stage_root=validated_stage_root,
        composition=composition,
    )


def _build_validated_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    validated_forge_root: Path,
    validated_stage_root: Path,
    composition: LiveForgeStageComposition,
) -> LiveServerPair:
    # Both roots and their exact dependency edge are checked before reaching
    # this constructor.  Keep the values live so a future refactor cannot
    # silently remove either side of the validation.
    if not (validated_forge_root.is_dir() and validated_stage_root.is_dir()):
        raise ValueError("validated Forge-to-Stage contract roots are unavailable")
    _configure_combined_transport()

    forge_harness = _FORGE_LIVE.LiveForgeHarness(
        actor_context=composition.actor_context,
        runner=composition.forge_runner,
        deadline_seconds=composition.forge_deadline_seconds,
        maximum_jobs=MAXIMUM_FORGE_JOBS,
        selected_handoff_ttl_seconds=SELECTED_HANDOFF_TTL_SECONDS,
    )
    stage_harness: LiveForgeStageHarness | None = None
    forge_server: Any | None = None
    try:
        stage_harness = LiveForgeStageHarness(composition)
        policy_limits = {
            "absolute_ttl_ms": 2 * 60 * MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": composition.stage_command_reserved_tokens,
            "command_reserved_spend_units": (
                _STAGE_LIVE.RESERVED_SPEND_UNITS_PER_COMMAND
            ),
            "command_max_attempts": 1,
            "command_deadline_ms": _STAGE_LIVE.COMMAND_DEADLINE_MS,
        }
        if composition.max_commands is not None:
            policy_limits["max_commands"] = composition.max_commands
        handoff = ForgeStageHandoffService(
            forge=forge_harness.service,
            artifacts=composition.forge_runner.store,
            stage=stage_harness.service,
            policy=ForgeStageHandoffPolicy(
                launch_limits=policy_limits,
                notice_version=NOTICE_VERSION,
                maximum_bindings=16,
                maximum_idempotency_records=128,
            ),
            token_factory=_handoff_token_factory,
        )
        forge_server = _FORGE_TRANSPORT.ForgePreviewServer(
            (host, forge_port),
            bearer=forge_bearer,
            harness=forge_harness,
            contract_revision=FORGE_CONTRACT_REVISION,
        )
        stage_server = ForgeStagePreviewServer(
            (host, stage_port),
            bearer=stage_bearer,
            contract_root=validated_stage_root,
            harness=stage_harness,
            handoff=handoff,
        )
        return LiveServerPair(forge_server, stage_server)
    except Exception:
        if forge_server is not None:
            forge_server.server_close()
        else:
            forge_harness.close()
        if stage_harness is not None:
            stage_harness.close()
        raise


def validate_forge_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    result = _FORGE_TRANSPORT._load_contract_tool().validate_contract(root)
    if result.get("contract_revision") != FORGE_CONTRACT_REVISION:
        raise ValueError("combined live target requires World Forge RC4")
    return root


def validate_contract_dependency(
    *,
    forge_contract_root: Path,
    stage_contract_root: Path,
) -> tuple[Path, Path]:
    """Validate RC5 and its byte-exact World Forge RC4 dependency."""
    forge_root = validate_forge_contract_root(forge_contract_root)
    stage_root = validate_contract_root(stage_contract_root)
    try:
        stage_manifest = json.loads(
            (stage_root / "manifest.json").read_text(encoding="utf-8")
        )
        dependency = stage_manifest["dependencies"]["world_forge"]
        forge_manifest_bytes = (forge_root / "manifest.json").read_bytes()
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("Forge-to-Stage contract dependency is unavailable") from error
    if dependency != WORLD_FORGE_DEPENDENCY:
        raise ValueError("Forge-to-Stage World Forge dependency is incompatible")
    observed_digest = hashlib.sha256(forge_manifest_bytes).hexdigest()
    if observed_digest != dependency["manifest_sha256"]:
        raise ValueError("Forge-to-Stage World Forge manifest digest is incompatible")
    return forge_root, stage_root


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_xray_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_xray_live_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("Stage X-ray contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("Stage X-ray contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("capabilities_enabled") != 0
    ):
        raise ValueError("Stage X-ray contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("Forge-to-Stage capabilities must remain disabled")
    return root


def startup_announcement(
    pair: LiveServerPair,
    composition: LiveForgeStageComposition,
) -> dict[str, object]:
    return {
        "capabilities_enabled": 0,
        "contract_revision": CONTRACT_REVISION,
        "development_only": True,
        "integration_only": True,
        "live_capability_evidence": False,
        "normal_runtime_composition": False,
        "provider_calls": True,
        "world_forge": {
            "contract_revision": FORGE_CONTRACT_REVISION,
            "deadline_seconds": composition.forge_deadline_seconds,
            "host": pair.forge.server_address[0],
            "maximum_jobs": MAXIMUM_FORGE_JOBS,
            "max_output_tokens": composition.forge_max_output_tokens,
            "model": composition.forge_model,
            "port": pair.forge.server_address[1],
            "selected_handoff_ttl_seconds": SELECTED_HANDOFF_TTL_SECONDS,
        },
        "presenter_stage": {
            "contract_revision": CONTRACT_REVISION,
            "effective_command_limit": (
                composition.max_commands
                if composition.max_commands is not None
                else "unlimited"
            ),
            "executor": LIVE_EXECUTOR,
            "host": pair.stage.server_address[0],
            "max_commands": composition.max_commands,
            "max_output_tokens": composition.stage_max_output_tokens,
            "command_reserved_tokens": composition.stage_command_reserved_tokens,
            "maximum_session_ttl_seconds": SELECTED_HANDOFF_TTL_SECONDS,
            "model": composition.stage_model,
            "output_publication": {
                "flush_bytes": _STAGE_LIVE.DEFAULT_PUBLICATION_CHUNK_BYTES,
                "flush_on_newline": True,
                "max_bytes_per_command": (_STAGE_LIVE.MAX_OUTPUT_BYTES_PER_COMMAND),
            },
            "port": pair.stage.server_address[1],
            "token_budget": _STAGE_LIVE.live_token_budget(
                composition.stage_command_reserved_tokens,
                max_logical_jobs=_STAGE_LIVE.MAX_LOGICAL_JOBS,
            ),
            "xray": {
                "available": True,
                "schema_revision": "stage-xray-v1",
                "authority": "presenter_session_only",
                "maximum_hosts": 64,
                "maximum_facts": 128,
            },
        },
    }


def serve_pair(pair: LiveServerPair) -> None:
    forge_thread = threading.Thread(
        target=pair.forge.serve_forever,
        name="dreamland-forge-stage-forge-http",
    )
    forge_thread.start()
    try:
        pair.stage.serve_forever()
    finally:
        pair.forge.shutdown()
        forge_thread.join(timeout=10)
        pair.stage.server_close()
        pair.forge.server_close()


def _configure_combined_transport() -> None:
    _STAGE.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.CONTRACT_REVISION = CONTRACT_REVISION
    _CORE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True
    _CORE.SAFE_ERROR_DETAILS.update(
        {
            "selected_record_unavailable": (
                404,
                "The selected World Forge revision is unavailable.",
                "none",
                None,
            ),
            "selected_artifact_unavailable": (
                409,
                "The selected World Forge artifact is unavailable.",
                "none",
                None,
            ),
            "idempotency_capacity_exhausted": (
                503,
                "Forge-to-Stage replay capacity is exhausted.",
                "none",
                None,
            ),
            "handoff_capacity_exhausted": (
                503,
                "Forge-to-Stage selection capacity is exhausted.",
                "none",
                None,
            ),
            "xray_unavailable": (
                404,
                "Stage X-ray is unavailable for this service.",
                "none",
                None,
            ),
            "xray_entity_unavailable": (
                404,
                "The Stage X-ray entity is unavailable; obtain a fresh snapshot.",
                "resume_xray_snapshot",
                None,
            ),
            "xray_resync_required": (
                409,
                "The Stage X-ray projection changed; obtain a fresh snapshot.",
                "resume_xray_snapshot",
                None,
            ),
        }
    )


def _validate_listener(host: str, port: int, label: str) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("combined live target may bind only to explicit loopback")
    if not 0 <= port <= 65_535:
        raise ValueError(f"{label} port must be between 0 and 65535")


def _validate_forge_bearer(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("forge-preview-bearer:")
        or not 48 <= len(value) <= 512
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError("live World Forge private bearer is malformed")
    return value


def _require_forge_reference(value: object) -> str:
    if not isinstance(value, str) or FORGE_REFERENCE_PATTERN.fullmatch(value) is None:
        raise _LIFECYCLE.PreviewTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "The World Forge reference is invalid.",
        )
    return value


def _xray_counter(value: str, *, allow_zero: bool) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("X-ray counter is invalid")
    parsed = int(value)
    minimum = 0 if allow_zero else 1
    if not minimum <= parsed <= _CORE.MAX_CONTROL_REVISION:
        raise ValueError("X-ray counter is invalid")
    return parsed


def _forge_lifecycle_error(error: ForgeServiceError) -> ProductLifecycleError:
    if error.code == "service_epoch_changed":
        return ProductLifecycleError(
            code="outcome_unknown",
            message="The World Forge service restarted before reconciliation.",
            status=410,
            retry="none",
        )
    retry = {
        "none": "none",
        "same_request": "same_request",
        "resume": "resume_snapshot",
    }.get(error.retry, "none")
    return ProductLifecycleError(
        code=error.code,
        message=error.message,
        status=error.status,
        retry=retry,
    )


def _handoff_token_factory(_purpose: str) -> str:
    return f"launch-grant:{secrets.token_urlsafe(32)}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit live World Forge-to-Stage target."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--forge-token-file", required=True, type=Path)
    initialize.add_argument("--stage-token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    serve = commands.add_parser("serve", help="start both loopback listeners")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--forge-port", type=int, default=0)
    serve.add_argument("--stage-port", type=int, default=0)
    serve.add_argument("--forge-token-file", required=True, type=Path)
    serve.add_argument("--stage-token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--forge-contract-root", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--artifact-root", required=True, type=Path)
    serve.add_argument(
        "--deadline-seconds",
        type=int,
        default=_FORGE_LIVE.DEFAULT_DEADLINE_SECONDS,
    )
    serve.add_argument(
        "--max-commands",
        default=_STAGE_LIVE.MAX_COMMANDS,
        type=_STAGE_LIVE._parse_max_commands,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            initialize_private_files(
                forge_token_file=arguments.forge_token_file,
                stage_token_file=arguments.stage_token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(
                json.dumps(
                    {
                        "bootstrap_file": str(arguments.bootstrap_file),
                        "created": True,
                        "forge_token_file": str(arguments.forge_token_file),
                        "stage_token_file": str(arguments.stage_token_file),
                    },
                    sort_keys=True,
                )
            )
            return 0
        _validate_listener(arguments.host, arguments.forge_port, "Forge")
        _validate_listener(arguments.host, arguments.stage_port, "Stage")
        if arguments.deadline_seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        bootstrap = load_bootstrap(arguments.bootstrap_file)
        forge_bearer = _FORGE_TRANSPORT.load_bearer(arguments.forge_token_file)
        stage_bearer = _LIFECYCLE.load_preview_bearer_file(arguments.stage_token_file)
        validated_forge_root, validated_stage_root = validate_contract_dependency(
            forge_contract_root=arguments.forge_contract_root,
            stage_contract_root=arguments.contract_root,
        )
        settings = load_settings()
        configure_console_logging(settings.logging, log_capacity=0)
        composition = build_live_composition(
            settings=settings,
            actor_context=bootstrap.actor_context,
            artifact_root=arguments.artifact_root,
            deadline_seconds=arguments.deadline_seconds,
            max_commands=arguments.max_commands,
        )
        pair = _build_validated_server_pair(
            host=arguments.host,
            forge_port=arguments.forge_port,
            stage_port=arguments.stage_port,
            forge_bearer=forge_bearer,
            stage_bearer=stage_bearer,
            validated_forge_root=validated_forge_root,
            validated_stage_root=validated_stage_root,
            composition=composition,
        )
        print(
            json.dumps(startup_announcement(pair, composition), sort_keys=True),
            flush=True,
        )
        serve_pair(pair)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"Live Forge-to-Stage error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
