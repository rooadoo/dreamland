#!/usr/bin/env python3
"""Build the additive provider-free Presenter launch-selection contract kit.

Phase 7A extends the published Phase 6 RC3 trust anchor with a private,
presenter-only discovery and exchange seam.  The browser receives only safe,
non-authorizing option projections.  The BFF exchanges one opaque selection
reference for a short-lived launch grant.  Importing this generator starts no
runtime, listener, provider, worker, or background task.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_xray_contract() -> ModuleType:
    path = Path(__file__).with_name("product_xray_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Presenter Stage X-ray contract generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_BASE: Any = _load_xray_contract()
_FORGE: Any = _BASE._BASE
_LIVE: Any = _BASE._LIVE
_STAGE: Any = _BASE._STAGE
_CORE: Any = _BASE._CORE

CONTRACT_NAME = _BASE.CONTRACT_NAME
CONTRACT_REVISION = "0.7.0-rc.1"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _BASE.CONTRACT_COMPATIBILITY
PREVIEW_EXECUTOR = "deterministic-launch-selection-no-provider-v1"

LAUNCH_OPTIONS_PATH = "/product/v1/stage/launch-options"
LAUNCH_SELECTIONS_PATH = "/product/v1/stage/launch-selections"
LAUNCH_SELECTION_OPERATIONS = {
    LAUNCH_OPTIONS_PATH: ("get", "getPresenterStageLaunchOptions"),
    LAUNCH_SELECTIONS_PATH: ("post", "exchangePresenterStageLaunchSelection"),
}
EXPECTED_OPERATIONS = {
    **_BASE.EXPECTED_OPERATIONS,
    **LAUNCH_SELECTION_OPERATIONS,
}
MUTATION_OPERATIONS = {
    *_FORGE.MUTATION_OPERATIONS,
    "exchangePresenterStageLaunchSelection",
}
SESSION_OPERATIONS = set(_BASE.SESSION_OPERATIONS)

LAUNCH_SELECTION_SAFE_ERROR_CASES = {
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
        1000,
    ),
}
SAFE_ERROR_CASES = {
    **_BASE.SAFE_ERROR_CASES,
    **LAUNCH_SELECTION_SAFE_ERROR_CASES,
}

OWN_CLIENT_SMOKE_REJECTED_MUTATIONS = 25
CLIENT_SMOKE_VALIDATED_FIXTURES = _BASE.CLIENT_SMOKE_VALIDATED_FIXTURES + 5
CLIENT_SMOKE_REJECTED_MUTATIONS = (
    _BASE.CLIENT_SMOKE_REJECTED_MUTATIONS + OWN_CLIENT_SMOKE_REJECTED_MUTATIONS
)

for module in (_BASE, _FORGE, _LIVE, _STAGE, _CORE):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    module.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
    module.MUTATION_OPERATIONS = MUTATION_OPERATIONS
    module.SESSION_OPERATIONS = SESSION_OPERATIONS
    module.SAFE_ERROR_CASES = SAFE_ERROR_CASES
for module in (_BASE, _FORGE, _LIVE):
    module.LIVE_EXECUTOR = PREVIEW_EXECUTOR

_base_build_openapi = _BASE.build_openapi
_base_build_manifest_schema = _BASE.build_manifest_schema
_base_fixture_definitions = _BASE.fixture_definitions
_base_manifest = _BASE._base_manifest
_base_generated_client_source = _BASE.generated_client_source
_base_validate_fixture_semantics = _BASE._validate_fixture_semantics
_base_smoke_import = _BASE._smoke_import
_base_refresh_contract = _BASE.refresh_contract
_base_client_smoke = _BASE.client_smoke


def build_openapi() -> dict[str, Any]:
    """Return the Phase 6 trust anchor plus two private Phase 7A operations."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Presenter Launch Selection Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, provider-free Phase 7A development contract for listing "
            "safe authored and Forge Presenter Stage options and exchanging one "
            "opaque selection reference for private launch authority. All "
            "production product capabilities remain disabled."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]["properties"]
    preview["executor"]["const"] = PREVIEW_EXECUTOR
    preview["provider_calls"]["const"] = False
    schemas = document["components"]["schemas"]
    schemas.update(_launch_selection_schemas())
    document["paths"].update(
        {
            LAUNCH_OPTIONS_PATH: {
                "get": _CORE._operation(
                    "getPresenterStageLaunchOptions",
                    "PresenterStageLaunchOptionsResponse",
                    (
                        "List bounded, display-safe, non-authorizing Presenter "
                        "Stage launch options for the validated actor."
                    ),
                    parameters=["ExpectedEpoch", "ActorContext"],
                )
            },
            LAUNCH_SELECTIONS_PATH: {
                "post": _CORE._operation(
                    "exchangePresenterStageLaunchSelection",
                    "StageLaunchSelectionResponse",
                    (
                        "Exchange one opaque Presenter launch selection for a "
                        "private, actor-bound Stage launch grant."
                    ),
                    parameters=[
                        "ExpectedEpoch",
                        "ActorContext",
                        "IdempotencyKey",
                    ],
                    request_schema="StageLaunchSelectionRequest",
                    idempotency_family="stage.launch_selection.exchange",
                )
            },
        }
    )
    exchange = document["paths"][LAUNCH_SELECTIONS_PATH]["post"]
    exchange["x-dreamland-idempotency"].update(
        {
            "reservation_point": (
                "after_bearer_current_epoch_presenter_actor_key_and_body_validation_"
                "before_selection_lookup"
            ),
            "semantic_request_evidence": {
                "canonical_body": True,
                "included_headers": [],
            },
            "claimed_deterministic_rejections": ["launch_selection_unavailable"],
            "unclaimed_retryable_rejections": ["launch_selection_capacity_exhausted"],
            "same_key_same_body": "replay_original_result",
            "same_key_changed_body": "idempotency_conflict",
            "old_epoch": "outcome_unknown_without_remint",
            "expired_grant_replay": "return_original_expired_grant",
        }
    )
    development = document.get("x-dreamland-development-live")
    if isinstance(development, dict):
        development["provider_calls"] = False
        development["usage_accounting"] = "provider_free_deterministic"
    document["x-dreamland-stage-launch-selection"] = {
        "authority": "private_presenter_bff_only",
        "browser_visible_fields": [
            "launch_selection_id",
            "offering_id",
            "display",
            "origin",
            "revision",
            "expires_at",
        ],
        "selection_reference_authorizes_launch": False,
        "offering_id_authorizes_launch": False,
        "profile_names_or_paths_accepted": False,
        "caller_supplied_revision_or_grant_accepted": False,
        "source_kinds": ["authored_profile_snapshot", "world_forge_selection"],
        "provider_dispatch": False,
        "normal_runtime_composition": False,
        "capability_evidence": False,
    }
    return document


def _launch_selection_schemas() -> dict[str, Any]:
    timestamp = {
        "type": "string",
        "format": "date-time",
        "minLength": 20,
        "maxLength": 64,
    }
    origin = _CORE._object(
        {
            "kind": {
                "type": "string",
                "enum": ["authored_profile_snapshot", "world_forge_selection"],
            }
        },
        ("kind",),
    )
    revision = _CORE._object(
        {
            "revision_id": _CORE._ref("StageLaunchRevisionId"),
            "sha256": _CORE._ref("Sha256"),
        },
        ("revision_id", "sha256"),
    )
    option = _CORE._object(
        {
            "launch_selection_id": _CORE._ref("LaunchSelectionId"),
            "offering_id": _CORE._ref("OfferingId"),
            "display": _CORE._ref("StageDisplayMetadata"),
            "origin": _CORE._ref("StageLaunchOrigin"),
            "revision": _CORE._ref("StageLaunchRevision"),
            "expires_at": {"oneOf": [timestamp, {"type": "null"}]},
        },
        (
            "launch_selection_id",
            "offering_id",
            "display",
            "origin",
            "revision",
            "expires_at",
        ),
    )
    recovery = _CORE._object(
        {
            "scenario": {
                "type": "string",
                "const": "launch_selection_exchange_response_lost_after_commit",
            },
            "operation_family": {
                "type": "string",
                "const": "stage.launch_selection.exchange",
            },
            "same_key_replay": {"type": "boolean", "const": True},
            "identical_body_required": {"type": "boolean", "const": True},
            "changed_body_outcome": {
                "type": "string",
                "const": "idempotency_conflict",
            },
            "old_epoch_outcome": {"type": "string", "const": "outcome_unknown"},
            "expired_grant_replay": {
                "type": "string",
                "const": "return_original_expired_grant",
            },
            "automatic_new_key_redispatch": {"type": "boolean", "const": False},
        },
        (
            "scenario",
            "operation_family",
            "same_key_replay",
            "identical_body_required",
            "changed_body_outcome",
            "old_epoch_outcome",
            "expired_grant_replay",
            "automatic_new_key_redispatch",
        ),
    )
    return {
        "LaunchSelectionId": _CORE._string(
            r"^launch-selection:[A-Za-z0-9_-]{24,128}$",
            minimum=41,
            maximum=145,
        ),
        "OfferingId": _CORE._string(
            r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
            minimum=10,
            maximum=137,
        ),
        "LaunchSelectionGrant": _CORE._string(
            r"^launch-grant:(?!0000000000000000_)[0-9a-f]{16}_[0-9a-f]{64}$",
            minimum=94,
            maximum=94,
        ),
        "StageLaunchRevisionId": _CORE._string(
            r"^world-revision:sha256:[0-9a-f]{64}$",
            minimum=86,
            maximum=86,
        ),
        "StageLaunchOrigin": origin,
        "StageLaunchRevision": revision,
        "StageLaunchOption": option,
        "PresenterStageLaunchOptionsResponse": _CORE._response_schema(
            {
                "kind": {
                    "type": "string",
                    "const": "presenter_stage_launch_options",
                },
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "options": {
                    "type": "array",
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": _CORE._ref("StageLaunchOption"),
                },
            },
            ("kind", "service_epoch", "options"),
        ),
        "StageLaunchSelectionRequest": _CORE._object(
            {"launch_selection_id": _CORE._ref("LaunchSelectionId")},
            ("launch_selection_id",),
        ),
        "StageLaunchSelectionResponse": _CORE._response_schema(
            {
                "kind": {
                    "type": "string",
                    "const": "presenter_stage_launch_selection_granted",
                },
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "replayed": {"type": "boolean"},
                "launch_grant": _CORE._ref("LaunchSelectionGrant"),
                "grant_expires_at": timestamp,
                "selection": _CORE._ref("StageLaunchOption"),
            },
            (
                "kind",
                "service_epoch",
                "replayed",
                "launch_grant",
                "grant_expires_at",
                "selection",
            ),
        ),
        "StageLaunchSelectionRecoveryFixture": recovery,
    }


def fixture_definitions() -> tuple[Any, ...]:
    fixtures: list[Any] = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        _replace_preview_context(payload)
        fixtures.append(
            _CORE.Fixture(
                fixture.case,
                fixture.classification,
                fixture.path,
                fixture.target,
                payload,
                fixture.client_action,
            )
        )

    authored = _launch_option("a", "authored_profile_snapshot")
    forged = _launch_option("b", "world_forge_selection")
    authored_response = _selection_response(authored, replayed=False)
    authored_replay = deepcopy(authored_response)
    authored_replay["replayed"] = True
    forged_response = _selection_response(forged, replayed=False)
    mixed_options = _success(
        {
            "kind": "presenter_stage_launch_options",
            "service_epoch": _CORE._EPOCH,
            "options": [authored, forged],
        }
    )
    empty_options = _success(
        {
            "kind": "presenter_stage_launch_options",
            "service_epoch": _CORE._EPOCH,
            "options": [],
        }
    )
    additive_options = deepcopy(mixed_options)
    additive_options["future_safe_top_level"] = {"revision": 1}

    additions = [
        _CORE.Fixture(
            "presenter_stage_launch_options_mixed",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/options-mixed.json",
            "openapi.json#/components/schemas/PresenterStageLaunchOptionsResponse",
            mixed_options,
            "render_non_authorizing_authored_and_forge_options",
        ),
        _CORE.Fixture(
            "presenter_stage_launch_options_empty",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/options-empty.json",
            "openapi.json#/components/schemas/PresenterStageLaunchOptionsResponse",
            empty_options,
            "render_no_available_presenter_options",
        ),
        _CORE.Fixture(
            "presenter_stage_authored_selection_request",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/authored-request.json",
            "openapi.json#/components/schemas/StageLaunchSelectionRequest",
            {"launch_selection_id": authored["launch_selection_id"]},
            "exchange_private_selection_reference",
        ),
        _CORE.Fixture(
            "presenter_stage_authored_selection_fresh",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/authored-fresh.json",
            "openapi.json#/components/schemas/StageLaunchSelectionResponse",
            authored_response,
            "store_private_launch_grant_then_launch_stage",
        ),
        _CORE.Fixture(
            "presenter_stage_authored_selection_replayed",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/authored-replayed.json",
            "openapi.json#/components/schemas/StageLaunchSelectionResponse",
            authored_replay,
            "reuse_original_private_launch_grant",
        ),
        _CORE.Fixture(
            "presenter_stage_forge_selection_request",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/forge-request.json",
            "openapi.json#/components/schemas/StageLaunchSelectionRequest",
            {"launch_selection_id": forged["launch_selection_id"]},
            "exchange_private_selection_reference",
        ),
        _CORE.Fixture(
            "presenter_stage_forge_selection_fresh",
            "canonical-valid",
            "fixtures/canonical-valid/launch-selection/forge-fresh.json",
            "openapi.json#/components/schemas/StageLaunchSelectionResponse",
            forged_response,
            "store_private_launch_grant_then_launch_stage",
        ),
        _CORE.Fixture(
            "presenter_stage_selection_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/launch-selection-response-lost.json",
            "openapi.json#/components/schemas/StageLaunchSelectionRecoveryFixture",
            {
                "scenario": "launch_selection_exchange_response_lost_after_commit",
                "operation_family": "stage.launch_selection.exchange",
                "same_key_replay": True,
                "identical_body_required": True,
                "changed_body_outcome": "idempotency_conflict",
                "old_epoch_outcome": "outcome_unknown",
                "expired_grant_replay": "return_original_expired_grant",
                "automatic_new_key_redispatch": False,
            },
            "retry_identical_exchange_without_remint",
        ),
        _CORE.Fixture(
            "presenter_stage_launch_options_additive_response",
            "forward-compat",
            "fixtures/forward-compat/launch-selection/options-top-level-addition.json",
            "openapi.json#/components/schemas/PresenterStageLaunchOptionsResponse",
            additive_options,
            "ignore_unknown_safe_top_level_fields",
        ),
    ]

    offering_as_selection = {
        "launch_selection_id": authored["offering_id"],
    }
    profile_name_request = {
        "launch_selection_id": authored["launch_selection_id"],
        "profile_name": "colonyship_a1",
    }
    profile_path_request = {
        "launch_selection_id": authored["launch_selection_id"],
        "profile_path": "/srv/dreamland/state_profiles/colonyship_a1.json",
    }
    caller_grant_request = {
        "launch_selection_id": authored["launch_selection_id"],
        "launch_grant": "launch-grant:caller_supplied_authority_0001",
    }
    unknown_origin = deepcopy(mixed_options)
    unknown_origin["options"][0]["origin"]["kind"] = "future_unreviewed_origin"
    leaked_projection = deepcopy(mixed_options)
    leaked_projection["options"][0]["profile_path"] = "/private/profile.json"
    duplicate_options = deepcopy(mixed_options)
    duplicate_options["options"] = [
        deepcopy(duplicate_options["options"][0]),
        deepcopy(duplicate_options["options"][0]),
    ]
    legacy_revision_id = deepcopy(mixed_options)
    legacy_revision_id["options"][0]["revision"]["revision_id"] = (
        "world-revision:legacy_revision_identifier_0001"
    )
    zero_serial_grant = deepcopy(authored_response)
    zero_serial_grant["launch_grant"] = "launch-grant:0000000000000000_" + "a" * 64
    for case, path, target, payload in (
        (
            "presenter_stage_offering_id_as_selection",
            "fixtures/invalid/launch-selection/offering-id-as-selection.json",
            "StageLaunchSelectionRequest",
            offering_as_selection,
        ),
        (
            "presenter_stage_profile_name_request",
            "fixtures/invalid/launch-selection/profile-name.json",
            "StageLaunchSelectionRequest",
            profile_name_request,
        ),
        (
            "presenter_stage_profile_path_request",
            "fixtures/invalid/launch-selection/profile-path.json",
            "StageLaunchSelectionRequest",
            profile_path_request,
        ),
        (
            "presenter_stage_caller_grant_request",
            "fixtures/invalid/launch-selection/caller-grant.json",
            "StageLaunchSelectionRequest",
            caller_grant_request,
        ),
        (
            "presenter_stage_unknown_launch_origin",
            "fixtures/invalid/launch-selection/unknown-origin.json",
            "PresenterStageLaunchOptionsResponse",
            unknown_origin,
        ),
        (
            "presenter_stage_profile_path_projection",
            "fixtures/invalid/launch-selection/profile-path-projection.json",
            "PresenterStageLaunchOptionsResponse",
            leaked_projection,
        ),
        (
            "presenter_stage_duplicate_launch_option",
            "fixtures/invalid/launch-selection/duplicate-option.json",
            "PresenterStageLaunchOptionsResponse",
            duplicate_options,
        ),
        (
            "presenter_stage_legacy_launch_revision_id",
            "fixtures/invalid/launch-selection/legacy-revision-id.json",
            "PresenterStageLaunchOptionsResponse",
            legacy_revision_id,
        ),
        (
            "presenter_stage_zero_serial_launch_grant",
            "fixtures/invalid/launch-selection/zero-serial-grant.json",
            "StageLaunchSelectionResponse",
            zero_serial_grant,
        ),
    ):
        additions.append(
            _CORE.Fixture(
                case,
                "invalid",
                path,
                f"openapi.json#/components/schemas/{target}",
                payload,
                "reject_fail_closed",
            )
        )
    fixtures.extend(additions)
    return tuple(fixtures)


def _launch_option(suffix: str, origin_kind: str) -> dict[str, Any]:
    sha256 = suffix * 64
    authored = origin_kind == "authored_profile_snapshot"
    return {
        "launch_selection_id": "launch-selection:" + suffix * 32,
        "offering_id": (
            "offering:authored-colonyship-a1"
            if authored
            else "offering:forge-harbor-control"
        ),
        "display": {
            "title": "Colony Ship A1" if authored else "Harbor Control",
            "source_label": "Authored" if authored else "World Forge",
            "revision_label": "Immutable A" if authored else "Selected B",
        },
        "origin": {"kind": origin_kind},
        "revision": {
            "revision_id": "world-revision:sha256:" + sha256,
            "sha256": sha256,
        },
        "expires_at": None if authored else "2026-08-27T14:00:00Z",
    }


def _selection_response(option: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    suffix = option["launch_selection_id"].removeprefix("launch-selection:")[0]
    serial = "0000000000000001" if suffix == "a" else "0000000000000002"
    return _success(
        {
            "kind": "presenter_stage_launch_selection_granted",
            "service_epoch": _CORE._EPOCH,
            "replayed": replayed,
            "launch_grant": f"launch-grant:{serial}_{suffix * 64}",
            "grant_expires_at": "2026-08-27T13:10:00Z",
            "selection": deepcopy(option),
        }
    )


def _success(value: dict[str, Any]) -> dict[str, Any]:
    value.update(_context())
    return value


def _context() -> dict[str, Any]:
    return {
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        },
        "preview": {
            "test_only": True,
            "integration_only": True,
            "executor": PREVIEW_EXECUTOR,
            "provider_calls": False,
            "live_capability_evidence": False,
        },
    }


def _replace_preview_context(value: Any) -> None:
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, dict) and {
            "test_only",
            "integration_only",
            "executor",
            "provider_calls",
            "live_capability_evidence",
        }.issubset(preview):
            preview["executor"] = PREVIEW_EXECUTOR
            preview["provider_calls"] = False
        for item in value.values():
            _replace_preview_context(item)
    elif isinstance(value, list):
        for item in value:
            _replace_preview_context(item)


_FORBIDDEN_OPTION_KEYS = {
    "admin",
    "artifact",
    "artifact_path",
    "authority",
    "backend_path",
    "credential",
    "forge_reference",
    "launch_grant",
    "profile",
    "profile_name",
    "profile_path",
    "raw_state",
    "runtime_document",
    "selected_record_id",
    "session_authority",
    "source_path",
    "state_profile",
    "world_state",
}


def _assert_no_option_leaks(value: Any, *, case: str) -> None:
    if isinstance(value, dict):
        forbidden = _FORBIDDEN_OPTION_KEYS.intersection(value)
        if forbidden:
            raise _CORE.ContractError(
                f"launch option exposes forbidden fields ({case}): "
                + ", ".join(sorted(forbidden))
            )
        for item in value.values():
            _assert_no_option_leaks(item, case=case)
    elif isinstance(value, list):
        for item in value:
            _assert_no_option_leaks(item, case=case)


def _validate_fixture_semantics(fixture: Any, payload: Any) -> None:
    _base_validate_fixture_semantics(fixture, payload)
    if fixture.classification == "invalid":
        return
    if fixture.target.endswith("PresenterStageLaunchOptionsResponse"):
        _assert_no_option_leaks(payload.get("options"), case=fixture.case)
    elif fixture.target.endswith("StageLaunchSelectionResponse"):
        _assert_no_option_leaks(payload.get("selection"), case=fixture.case)


def _base_manifest_document() -> dict[str, Any]:
    manifest = _base_manifest()
    prefix = (
        ".venv/bin/python tools/product_launch_selection_contract.py"
        f" {{verb}} contracts/product-api/{CONTRACT_DIRECTORY}"
    )
    manifest["generation"] = {
        "validate_command": prefix.format(verb="validate"),
        "refresh_command": prefix.format(verb="refresh"),
        "client_smoke_command": prefix.format(verb="client-smoke"),
        "publish_command": (
            prefix.format(verb="publish")
            + " --backend-commit COMMIT --published-at TIMESTAMP"
        ),
    }
    return manifest


def build_manifest_schema() -> dict[str, Any]:
    schema = _base_build_manifest_schema()
    _replace_generator_path(schema)
    return schema


def _replace_generator_path(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_xray_contract.py",
                    "tools/product_launch_selection_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_xray_contract.py",
                    "tools/product_launch_selection_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _base_generated_client_source(openapi)
    source = source.replace(
        "Generated by tools/product_xray_contract.py",
        "Generated by tools/product_launch_selection_contract.py",
        1,
    )
    source = source.replace(
        "development-live-forge-stage-xray-langgraph-v1",
        PREVIEW_EXECUTOR,
    )
    source = source.replace('"provider_calls": True', '"provider_calls": False')
    regex_marker = 'SHA256_RE = re.compile(r"^[0-9a-f]{64}$")\n'
    regexes = regex_marker + (
        'LAUNCH_SELECTION_RE = re.compile(r"^launch-selection:[A-Za-z0-9_-]{24,128}$")\n'
        'OFFERING_RE = re.compile(r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")\n'
    )
    if source.count(regex_marker) != 1:
        raise _CORE.ContractError(
            "generated launch-selection regex insertion point is missing"
        )
    source = source.replace(regex_marker, regexes, 1)

    validation_marker = "class ProductSessionPreviewClient:\n"
    validation = r"""
LAUNCH_OPTION_FORBIDDEN_KEYS = {
    "admin", "artifact", "artifact_path", "authority", "backend_path",
    "credential", "forge_reference", "launch_grant", "profile",
    "profile_name", "profile_path", "raw_state", "runtime_document",
    "selected_record_id", "session_authority", "source_path",
    "state_profile", "world_state",
}


def _launch_exact_object(value: Any, required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{name} violates its strict object schema")
    return value


def _launch_safe_text(name: str, value: Any, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _no_launch_option_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        if LAUNCH_OPTION_FORBIDDEN_KEYS.intersection(value):
            raise ValueError("private launch-selection field crossed the safe projection")
        for item in value.values():
            _no_launch_option_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _no_launch_option_forbidden_keys(item)


def _validate_stage_launch_option(value: Any) -> dict[str, Any]:
    required = {"launch_selection_id", "offering_id", "display", "origin", "revision", "expires_at"}
    value = _launch_exact_object(value, required, "Presenter Stage launch option")
    _no_launch_option_forbidden_keys(value)
    _forge_identifier("launch selection ID", value["launch_selection_id"], LAUNCH_SELECTION_RE)
    _forge_identifier("offering ID", value["offering_id"], OFFERING_RE)
    display = _launch_exact_object(value["display"], {"title", "source_label", "revision_label"}, "launch display metadata")
    _launch_safe_text("launch title", display["title"], 120)
    _launch_safe_text("launch source label", display["source_label"], 64)
    _launch_safe_text("launch revision label", display["revision_label"], 96)
    origin = _launch_exact_object(value["origin"], {"kind"}, "launch origin")
    if origin["kind"] not in {"authored_profile_snapshot", "world_forge_selection"}:
        raise ValueError("launch origin is unknown")
    revision = _launch_exact_object(value["revision"], {"revision_id", "sha256"}, "launch revision")
    revision_id = _forge_identifier("launch revision ID", revision["revision_id"], FORGE_REVISION_RE)
    sha256 = _forge_identifier("launch revision digest", revision["sha256"], SHA256_RE)
    if revision_id != "world-revision:sha256:" + sha256:
        raise ValueError("launch revision ID and digest disagree")
    if value["expires_at"] is not None:
        _forge_timestamp("launch selection expiry", value["expires_at"])
    return value


def validate_presenter_stage_launch_options(
    value: Any,
    *,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "presenter_stage_launch_options":
        raise ValueError("Presenter Stage launch-options response is invalid")
    _context(value)
    if expected_epoch is not None:
        expected = _forge_identifier(
            "expected service epoch", expected_epoch, EPOCH_RE
        )
        if value["service_epoch"] != expected:
            raise ValueError("returned Presenter Stage service epoch diverged")
    options = value.get("options")
    if not isinstance(options, list) or len(options) > 128:
        raise ValueError("Presenter Stage launch options are invalid")
    selection_ids: set[str] = set()
    offering_ids: set[str] = set()
    revision_bindings: dict[str, tuple[str, str, str, str]] = {}
    for option in options:
        _validate_stage_launch_option(option)
        selection_id = option["launch_selection_id"]
        offering_id = option["offering_id"]
        if selection_id in selection_ids or offering_id in offering_ids:
            raise ValueError("Presenter Stage launch option IDs are duplicated")
        selection_ids.add(selection_id)
        offering_ids.add(offering_id)
        revision_id = option["revision"]["revision_id"]
        binding = (
            option["origin"]["kind"],
            option["display"]["title"],
            option["display"]["source_label"],
            option["display"]["revision_label"],
        )
        prior = revision_bindings.get(revision_id)
        if prior is not None and prior != binding:
            raise ValueError(
                "Presenter Stage revision option metadata is inconsistent"
            )
        revision_bindings[revision_id] = binding
    return value


def validate_stage_launch_selection(
    value: Any,
    *,
    expected_launch_selection_id: str | None = None,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "presenter_stage_launch_selection_granted":
        raise ValueError("Presenter Stage launch-selection response is invalid")
    _context(value)
    if not isinstance(value.get("replayed"), bool):
        raise ValueError("Presenter Stage launch-selection replay truth is invalid")
    if expected_epoch is not None:
        expected = _forge_identifier(
            "expected service epoch", expected_epoch, EPOCH_RE
        )
        if value["service_epoch"] != expected:
            raise ValueError("returned Presenter Stage service epoch diverged")
    launch_grant = _bounded_header(
        "launch grant",
        value.get("launch_grant"),
        re.compile(r"^launch-grant:[0-9a-f]{16}_[0-9a-f]{64}$"),
    )
    serial = launch_grant.removeprefix("launch-grant:").split("_", 1)[0]
    if int(serial, 16) == 0:
        raise ValueError("launch grant serial must be positive")
    grant_expires_at = _forge_timestamp(
        "launch grant expiry", value.get("grant_expires_at")
    )
    selection = _validate_stage_launch_option(value.get("selection"))
    if selection["expires_at"] is not None and grant_expires_at > _forge_timestamp(
        "launch selection expiry", selection["expires_at"]
    ):
        raise ValueError("launch grant expiry exceeds its selection expiry")
    if expected_launch_selection_id is not None:
        expected = _forge_identifier("expected launch selection ID", expected_launch_selection_id, LAUNCH_SELECTION_RE)
        if selection["launch_selection_id"] != expected:
            raise ValueError("returned Presenter Stage launch selection diverged")
    return value


"""
    if source.count(validation_marker) != 1:
        raise _CORE.ContractError(
            "generated launch-selection validation insertion point is missing"
        )
    source = source.replace(validation_marker, validation + validation_marker, 1)

    method_marker = "    def _control_mutation("
    methods = r"""
    def presenter_stage_launch_options(self, *, epoch: str, actor: str) -> dict[str, Any]:
        payload = self._request("GET", "/product/v1/stage/launch-options", self._headers(epoch=epoch, actor=actor))
        return validate_presenter_stage_launch_options(
            payload,
            expected_epoch=epoch,
        )

    def exchange_presenter_stage_launch_selection(self, *, epoch: str, actor: str, key: str, launch_selection_id: str) -> dict[str, Any]:
        launch_selection_id = _forge_identifier("launch selection ID", launch_selection_id, LAUNCH_SELECTION_RE)
        payload = self._request(
            "POST",
            "/product/v1/stage/launch-selections",
            self._headers(epoch=epoch, actor=actor, idempotency_key=key),
            {"launch_selection_id": launch_selection_id},
        )
        return validate_stage_launch_selection(
            payload,
            expected_launch_selection_id=launch_selection_id,
            expected_epoch=epoch,
        )

"""
    if source.count(method_marker) != 1:
        raise _CORE.ContractError(
            "generated launch-selection client insertion point is missing"
        )
    return source.replace(method_marker, methods + method_marker, 1)


def _smoke_import(path: Path, root: Path) -> None:
    _base_smoke_import(path, root)
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_generated_smoke",
        path,
    )
    if specification is None or specification.loader is None:
        raise _CORE.ContractError("cannot import generated launch-selection client")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    options = _CORE.load_json(
        root / "fixtures/canonical-valid/launch-selection/options-mixed.json"
    )
    empty = _CORE.load_json(
        root / "fixtures/canonical-valid/launch-selection/options-empty.json"
    )
    authored = _CORE.load_json(
        root / "fixtures/canonical-valid/launch-selection/authored-fresh.json"
    )
    replayed = _CORE.load_json(
        root / "fixtures/canonical-valid/launch-selection/authored-replayed.json"
    )
    forged = _CORE.load_json(
        root / "fixtures/canonical-valid/launch-selection/forge-fresh.json"
    )
    module.validate_presenter_stage_launch_options(options)
    module.validate_presenter_stage_launch_options(empty)
    for payload in (authored, replayed, forged):
        module.validate_stage_launch_selection(
            payload,
            expected_launch_selection_id=payload["selection"]["launch_selection_id"],
        )

    additive = deepcopy(options)
    additive["future_safe_top_level"] = {"revision": 2}
    module.validate_presenter_stage_launch_options(additive)

    rejected_mutations = 0

    def reject(label: str, operation: Any) -> None:
        nonlocal rejected_mutations
        try:
            operation()
        except ValueError:
            rejected_mutations += 1
            return
        raise _CORE.ContractError(
            f"generated launch-selection client accepted {label} mutation"
        )

    mutations: list[tuple[str, dict[str, Any]]] = []
    changed = deepcopy(options)
    changed["options"][0]["origin"]["kind"] = "future_origin"
    mutations.append(("unknown origin", changed))
    changed = deepcopy(options)
    changed["options"].append(deepcopy(changed["options"][0]))
    mutations.append(("duplicate selection ID", changed))
    changed = deepcopy(options)
    changed["options"][1]["offering_id"] = changed["options"][0]["offering_id"]
    mutations.append(("duplicate offering ID", changed))
    changed = deepcopy(options)
    changed["options"][1]["revision"] = deepcopy(changed["options"][0]["revision"])
    changed["options"][1]["origin"] = deepcopy(changed["options"][0]["origin"])
    mutations.append(("inconsistent display for one revision", changed))
    changed = deepcopy(options)
    changed["options"][1]["revision"] = deepcopy(changed["options"][0]["revision"])
    changed["options"][1]["display"] = deepcopy(changed["options"][0]["display"])
    mutations.append(("inconsistent origin for one revision", changed))
    changed = deepcopy(options)
    changed["options"][0]["display"]["future"] = "unsafe"
    mutations.append(("nested additive field", changed))
    changed = deepcopy(options)
    changed["options"][0]["profile_path"] = "/private/profile.json"
    mutations.append(("profile path", changed))
    changed = deepcopy(options)
    changed["options"][0]["revision"]["sha256"] = "f" * 64
    mutations.append(("revision digest mismatch", changed))
    changed = deepcopy(options)
    changed["options"][0]["launch_selection_id"] = changed["options"][0]["offering_id"]
    mutations.append(("offering as selection", changed))
    changed = deepcopy(options)
    changed["options"][0]["expires_at"] = "not-a-timestamp"
    mutations.append(("invalid selection expiry", changed))
    changed = deepcopy(options)
    changed["options"][0]["display"]["title"] = "bad\ntext"
    mutations.append(("control-bearing title", changed))
    for label, changed in mutations:
        reject(
            label,
            lambda changed=changed: module.validate_presenter_stage_launch_options(
                changed
            ),
        )

    response_mutations: list[tuple[str, dict[str, Any], str | None]] = []
    changed = deepcopy(authored)
    changed["kind"] = "future_kind"
    response_mutations.append(("response kind", changed, None))
    changed = deepcopy(authored)
    changed["replayed"] = 1
    response_mutations.append(("nonboolean replay", changed, None))
    changed = deepcopy(authored)
    changed["launch_grant"] = "offering:not-authority"
    response_mutations.append(("invalid grant", changed, None))
    changed = deepcopy(authored)
    changed["launch_grant"] = "launch-grant:0000000000000000_" + "a" * 64
    response_mutations.append(("zero grant serial", changed, None))
    changed = deepcopy(authored)
    changed["grant_expires_at"] = "2026-08-27"
    response_mutations.append(("invalid grant expiry", changed, None))
    changed = deepcopy(authored)
    changed["selection"]["launch_grant"] = changed["launch_grant"]
    response_mutations.append(("nested grant leak", changed, None))
    changed = deepcopy(forged)
    changed["grant_expires_at"] = "2026-08-27T15:00:00Z"
    response_mutations.append(("grant outlives selection", changed, None))
    response_mutations.append(
        (
            "returned selection mismatch",
            deepcopy(authored),
            options["options"][1]["launch_selection_id"],
        )
    )
    for label, changed, expected in response_mutations:
        reject(
            label,
            lambda changed=changed, expected=expected: (
                module.validate_stage_launch_selection(
                    changed,
                    expected_launch_selection_id=expected,
                )
            ),
        )

    reject(
        "launch-options response epoch",
        lambda: module.validate_presenter_stage_launch_options(
            options,
            expected_epoch="epoch:wrong_launch_selection_0001",
        ),
    )
    reject(
        "launch-selection response epoch",
        lambda: module.validate_stage_launch_selection(
            authored,
            expected_epoch="epoch:wrong_launch_selection_0001",
        ),
    )

    class NoTransport:
        calls = 0

        def open(self, *_args: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("invalid launch selection reached transport")

    client = module.ProductSessionPreviewClient(
        "http://127.0.0.1",
        "private-launch-selection-smoke-bearer",
    )
    no_transport = NoTransport()
    client._opener = no_transport
    invalid_identifiers = (
        options["options"][0]["offering_id"],
        "launch-selection:short",
        "launch-selection:" + "a" * 129,
        7,
    )
    for invalid in invalid_identifiers:
        reject(
            "pre-transport invalid selection ID",
            lambda invalid=invalid: client.exchange_presenter_stage_launch_selection(
                epoch=options["service_epoch"],
                actor="actor:launch_selection_smoke_0001",
                key="launch-selection-smoke-key-0001",
                launch_selection_id=invalid,
            ),
        )
    if no_transport.calls != 0:
        raise _CORE.ContractError(
            "generated launch-selection client dispatched invalid input"
        )

    if rejected_mutations != OWN_CLIENT_SMOKE_REJECTED_MUTATIONS:
        raise _CORE.ContractError(
            f"generated launch-selection mutation count drifted: {rejected_mutations}"
        )


def refresh_contract(root: Path | str) -> dict[str, Any]:
    root_path = Path(root).resolve()
    manifest_path = root_path / "manifest.json"
    if manifest_path.is_file():
        manifest = _CORE.load_json(manifest_path)
        if (
            isinstance(manifest, dict)
            and manifest.get("publication_status") == "published"
        ):
            raise _CORE.ContractError(
                "published Presenter launch-selection artifacts are immutable"
            )
    return _base_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _base_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


for module in (_BASE, _FORGE, _LIVE, _STAGE, _CORE):
    module.build_openapi = build_openapi
    module.build_manifest_schema = build_manifest_schema
    module.fixture_definitions = fixture_definitions
    module._base_manifest = _base_manifest_document
    module.generated_client_source = generated_client_source
_CORE._validate_fixture_semantics = _validate_fixture_semantics
_CORE._smoke_import = _smoke_import
_CORE.refresh_contract = refresh_contract
_CORE.client_smoke = client_smoke

build_events_schema = _CORE.build_events_schema
validate_contract = _CORE.validate_contract
publish_contract = _CORE.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
