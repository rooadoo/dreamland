#!/usr/bin/env python3
"""Run the explicit Phase 8B live Forge-to-attendee development target.

The tool composes the reviewed live World Forge, Presenter Stage, attendee,
and World Access services only after an explicit ``serve`` command. Importing
it starts no provider, listener, worker, timer, reservation, or ordinary
DREAMLAND runtime work. Every production capability remains disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import secrets
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.config import load_settings
from dreamland.product.attendee_catalog import (
    GeneratedWorldCatalogPolicy,
    GeneratedWorldCatalogService,
)
from dreamland.product.forge import DEFAULT_SELECTED_HANDOFF_POLICY_ID
from dreamland.product.forge_stage import ForgeStageHandoffService
from dreamland.product.generated_access import (
    GeneratedAccessActorSeed,
    GeneratedAccessPolicy,
    GeneratedWorldAccessService,
    WorldForgeAccessSource,
)
from dreamland.product.launch_selection_forge import ForgeLaunchSelectionRegistrar
from dreamland.runtime_logging import configure_console_logging


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_world_access_live",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_ATTENDEE_LIVE: Any = _load_tool("product_attendee_live")
_WORLD: Any = _load_tool("product_world_access_preview")
_FORGE_LIVE: Any = _ATTENDEE_LIVE._FORGE_LIVE
_FORGE_TRANSPORT: Any = _ATTENDEE_LIVE._FORGE_TRANSPORT
_LIVE: Any = _ATTENDEE_LIVE._LIVE
_STAGE_LIVE: Any = _ATTENDEE_LIVE._STAGE_LIVE
_LIFECYCLE: Any = _ATTENDEE_LIVE._LIFECYCLE

WORLD_ACCESS_CONTRACT_REVISION = "0.1.0-rc.4"
PRODUCT_CONTRACT_REVISION = _ATTENDEE_LIVE.CONTRACT_REVISION
WORLD_FORGE_CONTRACT_REVISION = _ATTENDEE_LIVE.FORGE_CONTRACT_REVISION
LIVE_KIND = "live_generated_world_access_development"
LIVE_SCENARIO = "live-generated-world-access-development"
NOTICE_VERSION = "notice:phase8b-world-access-live-v1"
OPERATOR_ID = "operator:phase8b-world-access-live"
GENERATED_SOURCE_KIND = "single_attempt_live_vertical_slice"

MILLISECONDS_PER_MINUTE = 60_000
ACCESS_TTL_MS = 30 * MILLISECONDS_PER_MINUTE
GENERATED_PUBLICATION_CAPACITY = 16
ATTENDEE_REVISION_CAPACITY = 1 + GENERATED_PUBLICATION_CAPACITY
ATTENDEE_OFFERING_CAPACITY = 1 + GENERATED_PUBLICATION_CAPACITY
CATALOG_ITEM_CAPACITY = ATTENDEE_OFFERING_CAPACITY
ACCESS_RECORD_CAPACITY = 2 * GENERATED_PUBLICATION_CAPACITY
ACCESS_IDEMPOTENCY_CAPACITY = 4 * GENERATED_PUBLICATION_CAPACITY
ACCESS_TOKEN_HISTORY_CAPACITY = 1 + 3 * GENERATED_PUBLICATION_CAPACITY
PRODUCT_MANIFEST_SHA256 = (
    "1e90c8cbc2cd0465710bcddb931504de11e75d98c7a10e32ec84bc470de62588"
)
FORGE_MANIFEST_SHA256 = (
    "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09"
)

WORLD_ACCESS_RESULT_IDENTITY = _WORLD.WorldAccessResultIdentity(
    contract_name=_WORLD.WORLD_ACCESS_CONTRACT_NAME,
    contract_revision=WORLD_ACCESS_CONTRACT_REVISION,
    contract_compatibility=_WORLD.WORLD_ACCESS_CONTRACT_COMPATIBILITY,
    product_contract_revision=PRODUCT_CONTRACT_REVISION,
    world_forge_contract_revision=WORLD_FORGE_CONTRACT_REVISION,
    preview_kind=LIVE_KIND,
    provider_calls=True,
    live_forge_evidence=True,
    live_attendee_execution=True,
    live_capability_evidence=False,
)


@dataclass(frozen=True, slots=True)
class WorldAccessLiveBootstrap:
    """Owner-only Presenter and Operator authority for one process epoch."""

    presenter_actor_context: str = field(repr=False)
    operator_actor_context: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LiveServerPair:
    forge: Any
    stage: Any


class _AccessTokens:
    def __call__(self, purpose: str) -> str:
        return f"{purpose}:{secrets.token_urlsafe(32)}"


class WorldAccessLiveHandler(_WORLD.WorldAccessPreviewHandler):
    server_version = "DREAMLANDWorldAccessLive/0.1"


class WorldAccessLiveServer(_WORLD.WorldAccessPreviewServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        product_contract_root: Path,
        world_access_contract_root: Path,
        harness: _ATTENDEE_LIVE.LiveAttendeeHarness,
        handoff: ForgeStageHandoffService,
    ) -> None:
        super().__init__(
            address,
            bearer=bearer,
            scenario=_WORLD.SCENARIOS[0],
            product_contract_root=product_contract_root,
            world_access_contract_root=world_access_contract_root,
            harness_factory=lambda _scenario: harness,
            result_identity=WORLD_ACCESS_RESULT_IDENTITY,
        )
        self.handoff = handoff
        self.RequestHandlerClass = WorldAccessLiveHandler


def initialize_private_files(
    *,
    forge_token_file: Path,
    stage_token_file: Path,
    bootstrap_file: Path,
) -> None:
    """Create two bearers plus distinct Presenter and Operator authority."""
    paths = {
        path.expanduser().absolute()
        for path in (forge_token_file, stage_token_file, bootstrap_file)
    }
    if len(paths) != 3:
        raise ValueError("Forge, Product, and bootstrap paths must differ")
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
            {
                "operator_actor_context": f"actor:{secrets.token_urlsafe(32)}",
                "presenter_actor_context": f"actor:{secrets.token_urlsafe(32)}",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def load_bootstrap(path: Path) -> WorldAccessLiveBootstrap:
    try:
        value = json.loads(_STAGE_LIVE._read_owner_file(path, maximum_bytes=4_096))
    except json.JSONDecodeError as error:
        raise ValueError("live World Access bootstrap is not valid JSON") from error
    expected = {"presenter_actor_context", "operator_actor_context"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("live World Access bootstrap has invalid fields")
    presenter = value["presenter_actor_context"]
    operator = value["operator_actor_context"]
    if (
        not isinstance(presenter, str)
        or _ATTENDEE_LIVE._CORE.ACTOR_CONTEXT_PATTERN.fullmatch(presenter) is None
    ):
        raise ValueError("live World Access Presenter context is malformed")
    if (
        not isinstance(operator, str)
        or _WORLD._WORLD_ACTOR_CONTEXT_PATTERN.fullmatch(operator) is None
    ):
        raise ValueError("live World Access Operator context is malformed")
    if secrets.compare_digest(presenter, operator):
        raise ValueError("Presenter and Operator contexts must differ")
    return WorldAccessLiveBootstrap(presenter, operator)


def build_live_composition(
    *,
    settings,
    presenter_actor_context: str,
    eligibility_key: bytes,
    artifact_root: Path,
    authored_profile_path: Path,
    authored_profile_digest: str,
    authored_title: str,
    authored_revision_label: str,
    deadline_seconds: int,
    max_commands: int | None,
    max_contexts: int,
    context_ttl_minutes: int,
    admission_duration_minutes: int,
) -> _ATTENDEE_LIVE.LiveAttendeeComposition:
    return _ATTENDEE_LIVE.build_live_composition(
        settings=settings,
        actor_context=presenter_actor_context,
        artifact_root=artifact_root,
        authored_profile_path=authored_profile_path,
        authored_profile_digest=authored_profile_digest,
        authored_title=authored_title,
        authored_revision_label=authored_revision_label,
        deadline_seconds=deadline_seconds,
        max_commands=max_commands,
        max_contexts=max_contexts,
        context_ttl_minutes=context_ttl_minutes,
        admission_duration_minutes=admission_duration_minutes,
        generated_world_access=True,
        generated_eligibility_key=eligibility_key,
        attendee_notice_version=NOTICE_VERSION,
        max_revisions=ATTENDEE_REVISION_CAPACITY,
        max_offerings=ATTENDEE_OFFERING_CAPACITY,
        generated_publication_capacity=GENERATED_PUBLICATION_CAPACITY,
    )


def _attach_world_access(
    *,
    stage_harness: _ATTENDEE_LIVE.LiveAttendeeHarness,
    forge_harness: Any,
    composition: _ATTENDEE_LIVE.LiveAttendeeComposition,
    operator_actor_context: str,
    locator_key: bytes,
) -> None:
    eligibility_key = composition.generated_eligibility_key
    if eligibility_key is None:
        raise ValueError("live generated-world eligibility is unavailable")
    source = WorldForgeAccessSource(
        forge=forge_harness.service,
        artifacts=composition.presenter.forge_runner.store,
        forge_actor_authority=composition.presenter.actor_context,
    )
    access = GeneratedWorldAccessService(
        actors=(
            GeneratedAccessActorSeed(
                authority=operator_actor_context,
                actor_id=OPERATOR_ID,
                expires_at_ms=composition.admission_closes_at_ms,
            ),
        ),
        source=source,
        policy=GeneratedAccessPolicy(
            max_actor_authorities=1,
            max_active_promotions=GENERATED_PUBLICATION_CAPACITY,
            max_access_records=ACCESS_RECORD_CAPACITY,
            max_idempotency_records=ACCESS_IDEMPOTENCY_CAPACITY,
            max_token_history=ACCESS_TOKEN_HISTORY_CAPACITY,
            access_ttl_ms=ACCESS_TTL_MS,
            idempotency_ttl_ms=ACCESS_TTL_MS,
            failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
            accepted_selected_policy_ids=frozenset(
                {DEFAULT_SELECTED_HANDOFF_POLICY_ID}
            ),
            accepted_source_kinds=frozenset({GENERATED_SOURCE_KIND}),
        ),
        clock_ms=_STAGE_LIVE._utc_ms,
        monotonic_ms=_STAGE_LIVE._monotonic_ms,
        token_factory=_AccessTokens(),
        service_epoch=stage_harness.attendee_service.service_epoch,
    )
    catalog = GeneratedWorldCatalogService(
        access=access,
        registry=stage_harness.attendee_registry,
        attendee=stage_harness.attendee_service,
        policy=GeneratedWorldCatalogPolicy(
            launch_limits=_ATTENDEE_LIVE._attendee_launch_limits(
                composition.presenter.max_commands,
                composition.attendee_command_reserved_tokens,
            ),
            notice_version=NOTICE_VERSION,
            admission_closes_at_ms=(
                stage_harness.attendee_service._policy.admission_closes_at_ms
            ),
            policy_id="policy:phase8b-live-generated-world-v1",
            maximum_generated_offerings=GENERATED_PUBLICATION_CAPACITY,
            maximum_catalog_items=CATALOG_ITEM_CAPACITY,
            launch_grant_ttl_ms=min(
                10 * MILLISECONDS_PER_MINUTE,
                composition.context_ttl_ms,
            ),
        ),
        locator_key=locator_key,
        eligibility_key=eligibility_key,
        clock_ms=_STAGE_LIVE._utc_ms,
    )
    stage_harness.selected_source = source
    stage_harness.access = access
    stage_harness.world_catalog = catalog


def build_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    forge_contract_root: Path,
    product_contract_root: Path,
    world_access_contract_root: Path,
    composition: _ATTENDEE_LIVE.LiveAttendeeComposition,
    operator_actor_context: str,
) -> LiveServerPair:
    _LIVE._COMBINED._validate_listener(host, forge_port, "Forge")
    _LIVE._COMBINED._validate_listener(host, stage_port, "Product")
    if forge_port and forge_port == stage_port:
        raise ValueError("Forge and Product ports must differ")
    _LIFECYCLE._validate_preview_bearer(stage_bearer)
    _LIVE._COMBINED._validate_forge_bearer(forge_bearer)
    forge_root, product_root, world_root = validate_contract_dependencies(
        forge_contract_root=forge_contract_root,
        product_contract_root=product_contract_root,
        world_access_contract_root=world_access_contract_root,
    )
    return _build_validated_server_pair(
        host=host,
        forge_port=forge_port,
        stage_port=stage_port,
        forge_bearer=forge_bearer,
        stage_bearer=stage_bearer,
        validated_forge_root=forge_root,
        validated_product_root=product_root,
        validated_world_access_root=world_root,
        composition=composition,
        operator_actor_context=operator_actor_context,
    )


def _build_validated_server_pair(
    *,
    host: str,
    forge_port: int,
    stage_port: int,
    forge_bearer: str,
    stage_bearer: str,
    validated_forge_root: Path,
    validated_product_root: Path,
    validated_world_access_root: Path,
    composition: _ATTENDEE_LIVE.LiveAttendeeComposition,
    operator_actor_context: str,
) -> LiveServerPair:
    roots = (
        validated_forge_root,
        validated_product_root,
        validated_world_access_root,
    )
    if any(not root.is_dir() for root in roots):
        raise ValueError("validated live World Access contract roots are unavailable")
    if not composition.generated_world_access:
        raise ValueError("live World Access requires generated attendee eligibility")
    if secrets.compare_digest(
        composition.presenter.actor_context,
        operator_actor_context,
    ):
        raise ValueError("Presenter and Operator contexts must differ")
    _configure_live_transport()
    forge_harness: Any | None = None
    stage_harness: _ATTENDEE_LIVE.LiveAttendeeHarness | None = None
    forge_server: Any | None = None
    product_server: WorldAccessLiveServer | None = None
    try:
        registrar_slot: dict[str, ForgeLaunchSelectionRegistrar] = {}

        def register_selected(reference: str) -> object:
            registrar = registrar_slot.get("registrar")
            if registrar is None:
                raise RuntimeError("Forge launch-selection registrar is unavailable")
            return registrar.register_selected(reference)

        presenter = composition.presenter
        forge_harness = _FORGE_LIVE.LiveForgeHarness(
            actor_context=presenter.actor_context,
            runner=presenter.forge_runner,
            deadline_seconds=presenter.forge_deadline_seconds,
            maximum_jobs=_LIVE.MAXIMUM_FORGE_JOBS,
            selected_handoff_ttl_seconds=_LIVE.SELECTED_HANDOFF_TTL_SECONDS,
            actor_expires_at=_LIVE._actor_expiry(presenter.actor_expires_at_ms),
            on_selected=register_selected,
        )
        stage_harness = _ATTENDEE_LIVE.LiveAttendeeHarness(composition)
        registrar_slot["registrar"] = ForgeLaunchSelectionRegistrar(
            forge=forge_harness.service,
            artifacts=presenter.forge_runner.store,
            registry=stage_harness.selection_registry,
            actor_authority=presenter.actor_context,
            actor_id=_LIVE.PRESENTER_ACTOR_ID,
            stage_policy=stage_harness.forge_stage_policy,
            maximum_launch_grant_ttl_ms=_LIVE.LAUNCH_GRANT_TTL_MS,
        )
        _attach_world_access(
            stage_harness=stage_harness,
            forge_harness=forge_harness,
            composition=composition,
            operator_actor_context=operator_actor_context,
            locator_key=secrets.token_bytes(32),
        )
        handoff = ForgeStageHandoffService(
            forge=forge_harness.service,
            artifacts=presenter.forge_runner.store,
            stage=stage_harness.stage_service,
            policy=stage_harness.forge_stage_policy,
            token_factory=_LIVE._COMBINED._handoff_token_factory,
        )
        forge_server = _FORGE_TRANSPORT.ForgePreviewServer(
            (host, forge_port),
            bearer=forge_bearer,
            harness=forge_harness,
            contract_revision=WORLD_FORGE_CONTRACT_REVISION,
        )
        product_server = WorldAccessLiveServer(
            (host, stage_port),
            bearer=stage_bearer,
            product_contract_root=validated_product_root,
            world_access_contract_root=validated_world_access_root,
            harness=stage_harness,
            handoff=handoff,
        )
        return LiveServerPair(forge_server, product_server)
    except Exception:
        if product_server is not None:
            product_server.server_close()
        elif stage_harness is not None:
            stage_harness.close()
        if forge_server is not None:
            forge_server.server_close()
        elif forge_harness is not None:
            forge_harness.close()
        raise


def validate_world_access_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    validator_path = Path(__file__).with_name("product_world_access_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_world_access_live_contract_validator",
        validator_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("live World Access contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("live World Access contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != WORLD_ACCESS_CONTRACT_REVISION
        or result.get("valid") is not True
    ):
        raise ValueError("live World Access contract is incompatible")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        source = manifest["source"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("live World Access manifest is unavailable") from error
    if not isinstance(source, Mapping) or source != {
        "kind": LIVE_KIND,
        "provider_calls": True,
        "live_forge_evidence": True,
        "live_attendee_execution": True,
        "live_capability_evidence": False,
        "normal_runtime_integration": False,
        "production_capabilities_enabled": [],
    }:
        raise ValueError("live World Access identity is incompatible")
    return root


def validate_contract_dependencies(
    *,
    forge_contract_root: Path,
    product_contract_root: Path,
    world_access_contract_root: Path,
) -> tuple[Path, Path, Path]:
    forge_root, product_root = _ATTENDEE_LIVE.validate_contract_dependency(
        forge_contract_root=forge_contract_root,
        stage_contract_root=product_contract_root,
    )
    world_root = validate_world_access_contract_root(world_access_contract_root)
    try:
        world_manifest = json.loads(
            (world_root / "manifest.json").read_text(encoding="utf-8")
        )
        dependencies = world_manifest["dependencies"]
        product_manifest = (product_root / "manifest.json").read_bytes()
        forge_manifest = (forge_root / "manifest.json").read_bytes()
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("live World Access dependencies are unavailable") from error
    expected = {
        "product_api": {
            "contract": "dreamland-private-product-api",
            "revision": PRODUCT_CONTRACT_REVISION,
            "manifest_sha256": PRODUCT_MANIFEST_SHA256,
        },
        "world_forge_api": {
            "contract": "dreamland-private-world-forge-api",
            "revision": WORLD_FORGE_CONTRACT_REVISION,
            "manifest_sha256": FORGE_MANIFEST_SHA256,
        },
    }
    if dependencies != expected:
        raise ValueError("live World Access dependency pins are incompatible")
    if hashlib.sha256(product_manifest).hexdigest() != PRODUCT_MANIFEST_SHA256:
        raise ValueError("live World Access Product dependency changed")
    if hashlib.sha256(forge_manifest).hexdigest() != FORGE_MANIFEST_SHA256:
        raise ValueError("live World Access Forge dependency changed")
    return forge_root, product_root, world_root


def startup_announcement(
    pair: LiveServerPair,
    composition: _ATTENDEE_LIVE.LiveAttendeeComposition,
) -> dict[str, object]:
    base = _ATTENDEE_LIVE.startup_announcement(pair, composition)
    accounting = pair.stage.preview.world_catalog.publication_accounting()
    base.update(
        {
            "world_access_contract_revision": WORLD_ACCESS_CONTRACT_REVISION,
            "live_forge_evidence": True,
            "live_attendee_execution": True,
        }
    )
    base["world_access"] = {
        "contract_revision": WORLD_ACCESS_CONTRACT_REVISION,
        "host": pair.stage.server_address[0],
        "port": pair.stage.server_address[1],
        "kind": LIVE_KIND,
        "provider_calls": True,
        "live_forge_evidence": True,
        "live_attendee_execution": True,
        "live_capability_evidence": False,
        "operator_authority": "owner_bootstrap_only",
        "promotion": "explicit_operator_action",
        "eligible_source": "authoritative_live_selected_only",
        "notice_version": NOTICE_VERSION,
        "access_ttl_ms": ACCESS_TTL_MS,
        "publication_accounting": accounting,
        "restart_required_after_publication_exhaustion": True,
    }
    attendee = dict(base["attendee_sessions"])
    attendee.update(
        {
            "authored_options": 1,
            "generated_world_eligibility": True,
            "generated_publication_capacity": GENERATED_PUBLICATION_CAPACITY,
            "maximum_revisions": ATTENDEE_REVISION_CAPACITY,
            "maximum_offerings": ATTENDEE_OFFERING_CAPACITY,
            "notice_version": NOTICE_VERSION,
        }
    )
    base["attendee_sessions"] = attendee
    return base


def serve_pair(pair: LiveServerPair) -> None:
    _ATTENDEE_LIVE.serve_pair(pair)


def _configure_live_transport() -> None:
    _ATTENDEE_LIVE._configure_live_transport()
    _WORLD._ATTENDEE._configure_transport()
    stack = _WORLD._ATTENDEE
    for module in (
        stack,
        stack._SELECTION,
        stack._COMBINED,
        stack._STAGE,
        stack._CORE,
    ):
        module.CONTRACT_REVISION = PRODUCT_CONTRACT_REVISION
    stack._STAGE.PREVIEW_EXECUTOR = _ATTENDEE_LIVE.LIVE_EXECUTOR
    stack._CORE.PREVIEW_EXECUTOR = _ATTENDEE_LIVE.LIVE_EXECUTOR
    stack._CORE.PREVIEW_PROVIDER_CALLS = True
    stack._CORE.PREVIEW_TEST_ONLY = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit live generated-world attendee target."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--forge-token-file", required=True, type=Path)
    initialize.add_argument("--stage-token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    digest = commands.add_parser(
        "digest-authored",
        help="print the immutable digest pin for one trusted authored profile",
    )
    digest.add_argument("--profile-path", required=True, type=Path)
    serve = commands.add_parser("serve", help="start both loopback listeners")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--forge-port", type=int, default=0)
    serve.add_argument("--stage-port", type=int, default=0)
    serve.add_argument("--forge-token-file", required=True, type=Path)
    serve.add_argument("--stage-token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--forge-contract-root", required=True, type=Path)
    serve.add_argument("--product-contract-root", required=True, type=Path)
    serve.add_argument("--world-access-contract-root", required=True, type=Path)
    serve.add_argument("--artifact-root", required=True, type=Path)
    serve.add_argument("--authored-profile-path", required=True, type=Path)
    serve.add_argument("--authored-profile-digest", required=True)
    serve.add_argument(
        "--authored-title",
        default="Configured authored DREAMLAND world",
    )
    serve.add_argument(
        "--authored-revision-label",
        default="Pinned authored revision",
    )
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
    serve.add_argument(
        "--max-contexts",
        type=int,
        default=_ATTENDEE_LIVE.DEFAULT_MAX_CONTEXTS,
    )
    serve.add_argument(
        "--context-ttl-minutes",
        type=int,
        default=_ATTENDEE_LIVE.DEFAULT_CONTEXT_TTL_MINUTES,
    )
    serve.add_argument(
        "--admission-duration-minutes",
        type=int,
        default=_ATTENDEE_LIVE.DEFAULT_ADMISSION_DURATION_MINUTES,
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
        if arguments.command == "digest-authored":
            print(
                json.dumps(
                    {
                        "authored_revision_digest": _LIVE.authored_digest(
                            arguments.profile_path
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0

        _LIVE._COMBINED._validate_listener(
            arguments.host, arguments.forge_port, "Forge"
        )
        _LIVE._COMBINED._validate_listener(
            arguments.host, arguments.stage_port, "Product"
        )
        if arguments.forge_port and arguments.forge_port == arguments.stage_port:
            raise ValueError("Forge and Product ports must differ")
        _ATTENDEE_LIVE._validate_attendee_limits(
            max_contexts=arguments.max_contexts,
            context_ttl_minutes=arguments.context_ttl_minutes,
            admission_duration_minutes=arguments.admission_duration_minutes,
        )
        if arguments.deadline_seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        bootstrap = load_bootstrap(arguments.bootstrap_file)
        forge_bearer = _FORGE_TRANSPORT.load_bearer(arguments.forge_token_file)
        stage_bearer = _LIFECYCLE.load_preview_bearer_file(arguments.stage_token_file)
        forge_root, product_root, world_root = validate_contract_dependencies(
            forge_contract_root=arguments.forge_contract_root,
            product_contract_root=arguments.product_contract_root,
            world_access_contract_root=arguments.world_access_contract_root,
        )
        settings = load_settings()
        configure_console_logging(settings.logging, log_capacity=0)
        composition = build_live_composition(
            settings=settings,
            presenter_actor_context=bootstrap.presenter_actor_context,
            eligibility_key=secrets.token_bytes(32),
            artifact_root=arguments.artifact_root,
            authored_profile_path=arguments.authored_profile_path,
            authored_profile_digest=arguments.authored_profile_digest,
            authored_title=arguments.authored_title,
            authored_revision_label=arguments.authored_revision_label,
            deadline_seconds=arguments.deadline_seconds,
            max_commands=arguments.max_commands,
            max_contexts=arguments.max_contexts,
            context_ttl_minutes=arguments.context_ttl_minutes,
            admission_duration_minutes=arguments.admission_duration_minutes,
        )
        pair = _build_validated_server_pair(
            host=arguments.host,
            forge_port=arguments.forge_port,
            stage_port=arguments.stage_port,
            forge_bearer=forge_bearer,
            stage_bearer=stage_bearer,
            validated_forge_root=forge_root,
            validated_product_root=product_root,
            validated_world_access_root=world_root,
            composition=composition,
            operator_actor_context=bootstrap.operator_actor_context,
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
        print(f"Live World Access error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
