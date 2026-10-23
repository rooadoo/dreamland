#!/usr/bin/env python3
"""Build the additive provider-free Phase 7C attendee contract kit.

The release candidate extends the published Phase 7B private product contract
with one bounded attendee-context bootstrap and an authored-only launch-option
exchange.  Attendee runtime work continues to use the existing shared session
routes.  Importing this generator creates no listener, worker, provider,
runtime, credential, or product capability.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_contract() -> ModuleType:
    path = Path(__file__).with_name("product_launch_selection_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published launch-selection contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_PUBLISHED: Any = _load_live_contract()
_SELECTION: Any = _PUBLISHED._SELECTION
_BASE: Any = _PUBLISHED._BASE
_FORGE: Any = _PUBLISHED._FORGE
_LIVE: Any = _PUBLISHED._LIVE
_STAGE: Any = _PUBLISHED._STAGE
_CORE: Any = _PUBLISHED._CORE

CONTRACT_NAME = _PUBLISHED.CONTRACT_NAME
CONTRACT_REVISION = "0.7.0-rc.3"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _PUBLISHED.CONTRACT_COMPATIBILITY
PREVIEW_EXECUTOR = "deterministic-attendee-no-provider-v1"

ATTENDEE_CONTEXTS_PATH = "/product/v1/attendee/contexts"
ATTENDEE_LAUNCH_OPTIONS_PATH = "/product/v1/attendee/launch-options"
ATTENDEE_LAUNCH_SELECTIONS_PATH = "/product/v1/attendee/launch-selections"
ATTENDEE_OPERATIONS = {
    ATTENDEE_CONTEXTS_PATH: ("post", "bootstrapAttendeeContext"),
    ATTENDEE_LAUNCH_OPTIONS_PATH: ("get", "getAttendeeLaunchOptions"),
    ATTENDEE_LAUNCH_SELECTIONS_PATH: (
        "post",
        "exchangeAttendeeLaunchSelection",
    ),
}
EXPECTED_OPERATIONS = {
    **_SELECTION.EXPECTED_OPERATIONS,
    **ATTENDEE_OPERATIONS,
}
MUTATION_OPERATIONS = {
    *_SELECTION.MUTATION_OPERATIONS,
    "bootstrapAttendeeContext",
    "exchangeAttendeeLaunchSelection",
}
SESSION_OPERATIONS = set(_SELECTION.SESSION_OPERATIONS)

ATTENDEE_SAFE_ERROR_CASES = {
    "attendee_context_capacity_exhausted": (
        503,
        "Attendee context capacity is exhausted for this service epoch.",
        "none",
        None,
    ),
    "attendee_context_idempotency_capacity_exhausted": (
        503,
        "Attendee context replay capacity is temporarily exhausted.",
        "after_delay",
        1_000,
    ),
    "notice_version_mismatch": (
        409,
        "The presented privacy notice version does not match this offering.",
        "none",
        None,
    ),
    "privacy_notice_unavailable": (
        503,
        "The attendee privacy notice is unavailable.",
        "none",
        None,
    ),
    "service_not_open": (
        503,
        "Attendee admission is not open yet.",
        "after_delay",
        1_000,
    ),
    "service_closed": (
        410,
        "Attendee admission is closed.",
        "none",
        None,
    ),
}
SAFE_ERROR_CASES = {
    **_SELECTION.SAFE_ERROR_CASES,
    **ATTENDEE_SAFE_ERROR_CASES,
}

OWN_CLIENT_SMOKE_VALIDATED_FIXTURES = 6
OWN_CLIENT_SMOKE_REJECTED_MUTATIONS = 26
CLIENT_SMOKE_VALIDATED_FIXTURES = (
    _SELECTION.CLIENT_SMOKE_VALIDATED_FIXTURES + OWN_CLIENT_SMOKE_VALIDATED_FIXTURES
)
CLIENT_SMOKE_REJECTED_MUTATIONS = (
    _SELECTION.CLIENT_SMOKE_REJECTED_MUTATIONS + OWN_CLIENT_SMOKE_REJECTED_MUTATIONS
)

_MODULES = (_PUBLISHED, _SELECTION, _BASE, _FORGE, _LIVE, _STAGE, _CORE)
for module in _MODULES:
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    module.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
    module.MUTATION_OPERATIONS = MUTATION_OPERATIONS
    module.SESSION_OPERATIONS = SESSION_OPERATIONS
    module.SAFE_ERROR_CASES = SAFE_ERROR_CASES
    if hasattr(module, "PREVIEW_EXECUTOR"):
        module.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    if hasattr(module, "LIVE_EXECUTOR"):
        module.LIVE_EXECUTOR = PREVIEW_EXECUTOR

_base_build_openapi = _PUBLISHED.build_openapi
_base_build_manifest_schema = _PUBLISHED.build_manifest_schema
_base_fixture_definitions = _PUBLISHED.fixture_definitions
_base_manifest = _PUBLISHED._base_manifest
_base_generated_client_source = _PUBLISHED.generated_client_source
_base_validate_fixture_semantics = _CORE._validate_fixture_semantics
_base_smoke_import = _CORE._smoke_import
_base_refresh_contract = _PUBLISHED.refresh_contract
_base_client_smoke = _PUBLISHED.client_smoke


def build_openapi() -> dict[str, Any]:
    """Return RC2 unchanged plus three private attendee integration routes."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Authored Attendee Integration Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, provider-free Phase 7C integration contract for bounded "
            "private attendee-context bootstrap, authored launch selection, "
            "and the existing shared product-session routes. All production "
            "product capabilities remain disabled."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]["properties"]
    preview["executor"]["const"] = PREVIEW_EXECUTOR
    preview["provider_calls"]["const"] = False

    schemas = document["components"]["schemas"]
    schemas.update(_attendee_schemas())
    document["paths"].update(
        {
            ATTENDEE_CONTEXTS_PATH: {
                "post": _CORE._operation(
                    "bootstrapAttendeeContext",
                    "AttendeeContextBootstrapResponse",
                    (
                        "Bind one server-side BFF gateway context to bounded "
                        "private attendee actor authority."
                    ),
                    parameters=["ExpectedEpoch", "IdempotencyKey"],
                    request_schema="AttendeeContextBootstrapRequest",
                    created=True,
                    idempotency_family="attendee.context.bootstrap",
                )
            },
            ATTENDEE_LAUNCH_OPTIONS_PATH: {
                "get": _CORE._operation(
                    "getAttendeeLaunchOptions",
                    "AttendeeLaunchOptionsResponse",
                    (
                        "List bounded, authored-only, non-authorizing attendee "
                        "launch options for the validated actor."
                    ),
                    parameters=["ExpectedEpoch", "ActorContext"],
                )
            },
            ATTENDEE_LAUNCH_SELECTIONS_PATH: {
                "post": _CORE._operation(
                    "exchangeAttendeeLaunchSelection",
                    "AttendeeLaunchSelectionResponse",
                    (
                        "Exchange one authored attendee selection and exact "
                        "privacy-notice delivery version for private launch "
                        "authority."
                    ),
                    parameters=[
                        "ExpectedEpoch",
                        "ActorContext",
                        "IdempotencyKey",
                    ],
                    request_schema="AttendeeLaunchSelectionRequest",
                    idempotency_family="attendee.launch_selection.exchange",
                )
            },
        }
    )

    bootstrap = document["paths"][ATTENDEE_CONTEXTS_PATH]["post"]
    bootstrap["x-dreamland-idempotency"].update(
        {
            "scope": [
                "service_epoch",
                "private_service_principal",
                "operation_family",
                "idempotency_key",
            ],
            "reservation_point": (
                "after_current_epoch_private_bff_authority_key_and_canonical_"
                "body_validation_replay_and_claim_before_context_lookup_and_capacity"
            ),
            "validation_order": [
                "current_service_epoch",
                "private_bff_authority",
                "idempotency_key_and_canonical_body",
                "exact_replay_or_claim",
                "gateway_context_lookup",
                "context_capacity",
            ],
            "semantic_request_evidence": {
                "canonical_body": True,
                "included_headers": [],
            },
            "claimed_deterministic_rejections": [
                "attendee_context_capacity_exhausted",
            ],
            "unclaimed_rejections": [
                "attendee_context_idempotency_capacity_exhausted",
            ],
            "same_key_same_body": "replay_original_result",
            "same_key_changed_body": "idempotency_conflict",
            "same_gateway_context_new_key": "return_existing_live_actor_context",
            "old_epoch": "outcome_unknown_without_remint",
            "expired_context_replay": "return_original_expired_actor_context",
            "automatic_new_key_redispatch": False,
        }
    )
    exchange = document["paths"][ATTENDEE_LAUNCH_SELECTIONS_PATH]["post"]
    exchange["x-dreamland-idempotency"].update(
        {
            "reservation_point": (
                "after_bearer_current_epoch_attendee_actor_key_and_canonical_"
                "body_validation_before_selection_and_notice_lookup"
            ),
            "semantic_request_evidence": {
                "canonical_body": True,
                "included_headers": [],
                "body_fields": [
                    "launch_selection_id",
                    "notice_version_presented",
                ],
            },
            "internal_registry_key": {
                "derivation": (
                    "outer_epoch_actor_operation_key_and_canonical_body_sha256"
                ),
                "selection_only_key_reused": False,
                "notice_changes_cannot_alias": True,
                "outer_retention_boundary_preserved": True,
            },
            "claimed_deterministic_rejections": [
                "launch_selection_unavailable",
                "notice_version_mismatch",
                "privacy_notice_unavailable",
                "service_closed",
            ],
            "unclaimed_retryable_rejections": [
                "service_not_open",
                "launch_selection_capacity_exhausted",
            ],
            "same_key_same_body": "replay_original_result",
            "same_key_changed_body": "idempotency_conflict",
            "old_epoch": "outcome_unknown_without_remint",
            "expired_grant_replay": "return_original_expired_grant",
            "automatic_new_key_redispatch": False,
        }
    )

    document["x-dreamland-development-live"] = {
        "explicit_start_only": True,
        "loopback_only": True,
        "provider_calls": False,
        "usage_accounting": "provider_free_deterministic",
        "capability_evidence": False,
        "normal_runtime_composition": False,
    }
    document["x-dreamland-attendee-integration-preview"] = {
        "authority": "private_attendee_bff_only",
        "authored_only": True,
        "browser_credentials_accepted": False,
        "gateway_context_is_browser_authority": False,
        "gateway_context_server_side_only": True,
        "profile_names_or_paths_accepted": False,
        "caller_supplied_actor_or_grant_accepted": False,
        "provider_dispatch": False,
        "normal_runtime_composition": False,
        "capability_evidence": False,
        "session_routes": "existing_product_v1_sessions",
        "generated_world_attendee_eligibility": False,
    }
    return document


def _attendee_schemas() -> dict[str, Any]:
    timestamp = {
        "type": "string",
        "format": "date-time",
        "minLength": 20,
        "maxLength": 64,
    }
    option = _CORE._object(
        {
            "launch_selection_id": _CORE._ref("LaunchSelectionId"),
            "offering_id": _CORE._ref("OfferingId"),
            "display": _CORE._ref("StageDisplayMetadata"),
            "origin": _CORE._ref("AttendeeLaunchOrigin"),
            "revision": _CORE._ref("StageLaunchRevision"),
            "notice_version": _CORE._ref("NoticeVersion"),
            "expires_at": {"oneOf": [timestamp, {"type": "null"}]},
        },
        (
            "launch_selection_id",
            "offering_id",
            "display",
            "origin",
            "revision",
            "notice_version",
            "expires_at",
        ),
    )
    bootstrap_recovery = _CORE._object(
        {
            "scenario": {
                "type": "string",
                "const": "attendee_context_bootstrap_response_lost_after_commit",
            },
            "operation_family": {
                "type": "string",
                "const": "attendee.context.bootstrap",
            },
            "same_key_replay": {"type": "boolean", "const": True},
            "identical_body_required": {"type": "boolean", "const": True},
            "changed_body_outcome": {
                "type": "string",
                "const": "idempotency_conflict",
            },
            "old_epoch_outcome": {"type": "string", "const": "outcome_unknown"},
            "expired_context_replay": {
                "type": "string",
                "const": "return_original_expired_actor_context",
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
            "expired_context_replay",
            "automatic_new_key_redispatch",
        ),
    )
    selection_recovery = _CORE._object(
        {
            "scenario": {
                "type": "string",
                "const": "attendee_launch_selection_response_lost_after_commit",
            },
            "operation_family": {
                "type": "string",
                "const": "attendee.launch_selection.exchange",
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
        "GatewayContextId": _CORE._string(
            r"^gateway-context:[A-Za-z0-9_-]{24,128}$",
            minimum=40,
            maximum=144,
        ),
        "NoticeVersion": _CORE._string(
            r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$",
            minimum=4,
            maximum=128,
        ),
        "AttendeeContextBootstrapRequest": _CORE._object(
            {"gateway_context_id": _CORE._ref("GatewayContextId")},
            ("gateway_context_id",),
        ),
        "AttendeeContextBootstrapResponse": _CORE._response_schema(
            {
                "kind": {
                    "type": "string",
                    "const": "attendee_context_bootstrapped",
                },
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "replayed": {"type": "boolean"},
                "authority_class": {"type": "string", "const": "attendee"},
                "actor_context": _CORE._ref("ActorContext"),
                "actor_expires_at": timestamp,
            },
            (
                "kind",
                "service_epoch",
                "replayed",
                "authority_class",
                "actor_context",
                "actor_expires_at",
            ),
        ),
        "AttendeeLaunchOrigin": _CORE._object(
            {
                "kind": {
                    "type": "string",
                    "const": "authored_profile_snapshot",
                }
            },
            ("kind",),
        ),
        "AttendeeLaunchOption": option,
        "AttendeeLaunchOptionsResponse": _CORE._response_schema(
            {
                "kind": {"type": "string", "const": "attendee_launch_options"},
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "options": {
                    "type": "array",
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": _CORE._ref("AttendeeLaunchOption"),
                },
            },
            ("kind", "service_epoch", "options"),
        ),
        "AttendeeLaunchSelectionRequest": _CORE._object(
            {
                "launch_selection_id": _CORE._ref("LaunchSelectionId"),
                "notice_version_presented": _CORE._ref("NoticeVersion"),
            },
            ("launch_selection_id", "notice_version_presented"),
        ),
        "AttendeeLaunchSelectionResponse": _CORE._response_schema(
            {
                "kind": {
                    "type": "string",
                    "const": "attendee_launch_selection_granted",
                },
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "replayed": {"type": "boolean"},
                "launch_grant": _CORE._ref("LaunchSelectionGrant"),
                "grant_expires_at": timestamp,
                "selection": _CORE._ref("AttendeeLaunchOption"),
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
        "AttendeeContextBootstrapRecoveryFixture": bootstrap_recovery,
        "AttendeeLaunchSelectionRecoveryFixture": selection_recovery,
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

    gateway_context_id = "gateway-context:attendee_browser_alpha_0001"
    actor_context = "actor:attendee_context_alpha_0001"
    notice_version = "notice:attendee-preview-v1"
    option = _attendee_option()
    context_fresh = _success(
        {
            "kind": "attendee_context_bootstrapped",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "authority_class": "attendee",
            "actor_context": actor_context,
            "actor_expires_at": "2026-08-28T14:00:00Z",
        }
    )
    context_replay = deepcopy(context_fresh)
    context_replay["replayed"] = True
    options = _success(
        {
            "kind": "attendee_launch_options",
            "service_epoch": _CORE._EPOCH,
            "options": [option],
        }
    )
    empty_options = _success(
        {
            "kind": "attendee_launch_options",
            "service_epoch": _CORE._EPOCH,
            "options": [],
        }
    )
    selection_request = {
        "launch_selection_id": option["launch_selection_id"],
        "notice_version_presented": notice_version,
    }
    selection_fresh = _attendee_selection_response(option, replayed=False)
    selection_replay = deepcopy(selection_fresh)
    selection_replay["replayed"] = True

    additions = [
        _CORE.Fixture(
            "attendee_context_bootstrap_request",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/context-request.json",
            "openapi.json#/components/schemas/AttendeeContextBootstrapRequest",
            {"gateway_context_id": gateway_context_id},
            "bootstrap_private_attendee_context",
        ),
        _CORE.Fixture(
            "attendee_context_bootstrap_fresh",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/context-fresh.json",
            "openapi.json#/components/schemas/AttendeeContextBootstrapResponse",
            context_fresh,
            "retain_private_actor_context",
        ),
        _CORE.Fixture(
            "attendee_context_bootstrap_replayed",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/context-replayed.json",
            "openapi.json#/components/schemas/AttendeeContextBootstrapResponse",
            context_replay,
            "reuse_original_private_actor_context",
        ),
        _CORE.Fixture(
            "attendee_authored_launch_options",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/options-authored.json",
            "openapi.json#/components/schemas/AttendeeLaunchOptionsResponse",
            options,
            "render_non_authorizing_authored_option",
        ),
        _CORE.Fixture(
            "attendee_launch_options_empty",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/options-empty.json",
            "openapi.json#/components/schemas/AttendeeLaunchOptionsResponse",
            empty_options,
            "render_no_eligible_attendee_options",
        ),
        _CORE.Fixture(
            "attendee_launch_selection_request",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/selection-request.json",
            "openapi.json#/components/schemas/AttendeeLaunchSelectionRequest",
            selection_request,
            "prove_notice_and_exchange_private_selection",
        ),
        _CORE.Fixture(
            "attendee_launch_selection_fresh",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/selection-fresh.json",
            "openapi.json#/components/schemas/AttendeeLaunchSelectionResponse",
            selection_fresh,
            "store_private_launch_grant_then_create_session",
        ),
        _CORE.Fixture(
            "attendee_launch_selection_replayed",
            "canonical-valid",
            "fixtures/canonical-valid/attendee/selection-replayed.json",
            "openapi.json#/components/schemas/AttendeeLaunchSelectionResponse",
            selection_replay,
            "reuse_original_private_launch_grant",
        ),
        _CORE.Fixture(
            "attendee_context_bootstrap_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/attendee-context-bootstrap-response-lost.json",
            "openapi.json#/components/schemas/AttendeeContextBootstrapRecoveryFixture",
            {
                "scenario": "attendee_context_bootstrap_response_lost_after_commit",
                "operation_family": "attendee.context.bootstrap",
                "same_key_replay": True,
                "identical_body_required": True,
                "changed_body_outcome": "idempotency_conflict",
                "old_epoch_outcome": "outcome_unknown",
                "expired_context_replay": "return_original_expired_actor_context",
                "automatic_new_key_redispatch": False,
            },
            "retry_identical_bootstrap_without_remint",
        ),
        _CORE.Fixture(
            "attendee_launch_selection_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/attendee-launch-selection-response-lost.json",
            "openapi.json#/components/schemas/AttendeeLaunchSelectionRecoveryFixture",
            {
                "scenario": "attendee_launch_selection_response_lost_after_commit",
                "operation_family": "attendee.launch_selection.exchange",
                "same_key_replay": True,
                "identical_body_required": True,
                "changed_body_outcome": "idempotency_conflict",
                "old_epoch_outcome": "outcome_unknown",
                "expired_grant_replay": "return_original_expired_grant",
                "automatic_new_key_redispatch": False,
            },
            "retry_identical_attendee_exchange_without_remint",
        ),
    ]

    additive_options = deepcopy(options)
    additive_options["future_safe_top_level"] = {"revision": 1}
    additions.append(
        _CORE.Fixture(
            "attendee_launch_options_additive_response",
            "forward-compat",
            "fixtures/forward-compat/attendee/options-top-level-addition.json",
            "openapi.json#/components/schemas/AttendeeLaunchOptionsResponse",
            additive_options,
            "ignore_unknown_safe_top_level_fields",
        )
    )

    invalid_values = (
        (
            "attendee_gateway_context_invalid",
            "fixtures/invalid/attendee/gateway-context.json",
            "AttendeeContextBootstrapRequest",
            {"gateway_context_id": "gateway-context:short"},
        ),
        (
            "attendee_bootstrap_caller_actor",
            "fixtures/invalid/attendee/bootstrap-caller-actor.json",
            "AttendeeContextBootstrapRequest",
            {
                "gateway_context_id": gateway_context_id,
                "actor_context": actor_context,
            },
        ),
        (
            "attendee_forge_origin",
            "fixtures/invalid/attendee/forge-origin.json",
            "AttendeeLaunchOptionsResponse",
            _changed(
                options, ("options", 0, "origin", "kind"), "world_forge_selection"
            ),
        ),
        (
            "attendee_option_missing_notice",
            "fixtures/invalid/attendee/option-missing-notice.json",
            "AttendeeLaunchOptionsResponse",
            _without(options, ("options", 0, "notice_version")),
        ),
        (
            "attendee_option_profile_path",
            "fixtures/invalid/attendee/option-profile-path.json",
            "AttendeeLaunchOptionsResponse",
            _changed(options, ("options", 0, "profile_path"), "/private/profile.json"),
        ),
        (
            "attendee_duplicate_option",
            "fixtures/invalid/attendee/duplicate-option.json",
            "AttendeeLaunchOptionsResponse",
            _duplicate_option(options),
        ),
        (
            "attendee_selection_missing_notice",
            "fixtures/invalid/attendee/selection-missing-notice.json",
            "AttendeeLaunchSelectionRequest",
            {"launch_selection_id": option["launch_selection_id"]},
        ),
        (
            "attendee_selection_consent_claim",
            "fixtures/invalid/attendee/selection-consent.json",
            "AttendeeLaunchSelectionRequest",
            {**selection_request, "consent": True},
        ),
        (
            "attendee_selection_caller_grant",
            "fixtures/invalid/attendee/selection-caller-grant.json",
            "AttendeeLaunchSelectionRequest",
            {
                **selection_request,
                "launch_grant": "launch-grant:caller_supplied_attendee_authority",
            },
        ),
    )
    for case, path, target, payload in invalid_values:
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


def _attendee_option(suffix: str = "c") -> dict[str, Any]:
    sha256 = suffix * 64
    return {
        "launch_selection_id": "launch-selection:" + suffix * 32,
        "offering_id": "offering:authored-colonyship-a1",
        "display": {
            "title": "Colony Ship A1",
            "source_label": "Authored",
            "revision_label": "Pinned attendee preview",
        },
        "origin": {"kind": "authored_profile_snapshot"},
        "revision": {
            "revision_id": "world-revision:sha256:" + sha256,
            "sha256": sha256,
        },
        "notice_version": "notice:attendee-preview-v1",
        "expires_at": "2026-08-28T14:00:00Z",
    }


def _attendee_selection_response(
    option: dict[str, Any], *, replayed: bool
) -> dict[str, Any]:
    suffix = option["launch_selection_id"].removeprefix("launch-selection:")[0]
    return _success(
        {
            "kind": "attendee_launch_selection_granted",
            "service_epoch": _CORE._EPOCH,
            "replayed": replayed,
            "launch_grant": f"launch-grant:0000000000000003_{suffix * 64}",
            "grant_expires_at": "2026-08-28T13:10:00Z",
            "selection": deepcopy(option),
        }
    )


def _success(value: dict[str, Any]) -> dict[str, Any]:
    value.update(
        {
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
    )
    return value


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


def _changed(value: Any, path: tuple[Any, ...], replacement: Any) -> Any:
    copied = deepcopy(value)
    current = copied
    for component in path[:-1]:
        current = current[component]
    current[path[-1]] = replacement
    return copied


def _without(value: Any, path: tuple[Any, ...]) -> Any:
    copied = deepcopy(value)
    current = copied
    for component in path[:-1]:
        current = current[component]
    del current[path[-1]]
    return copied


def _duplicate_option(options: dict[str, Any]) -> dict[str, Any]:
    copied = deepcopy(options)
    copied["options"].append(deepcopy(copied["options"][0]))
    return copied


_FORBIDDEN_ATTENDEE_OPTION_KEYS = {
    *_SELECTION._FORBIDDEN_OPTION_KEYS,
    "actor_context",
    "gateway_context_id",
    "notice_version_presented",
}


def _assert_no_attendee_option_leaks(value: Any, *, case: str) -> None:
    if isinstance(value, dict):
        forbidden = _FORBIDDEN_ATTENDEE_OPTION_KEYS.intersection(value)
        if forbidden:
            raise _CORE.ContractError(
                f"attendee option exposes forbidden fields ({case}): "
                + ", ".join(sorted(forbidden))
            )
        for item in value.values():
            _assert_no_attendee_option_leaks(item, case=case)
    elif isinstance(value, list):
        for item in value:
            _assert_no_attendee_option_leaks(item, case=case)


def _validate_fixture_semantics(fixture: Any, payload: Any) -> None:
    _base_validate_fixture_semantics(fixture, payload)
    if fixture.classification == "invalid":
        return
    if fixture.target.endswith("AttendeeLaunchOptionsResponse"):
        _assert_no_attendee_option_leaks(payload.get("options"), case=fixture.case)
    elif fixture.target.endswith("AttendeeLaunchSelectionResponse"):
        _assert_no_attendee_option_leaks(payload.get("selection"), case=fixture.case)
    elif fixture.target.endswith("AttendeeContextBootstrapResponse"):
        if "gateway_context_id" in payload:
            raise _CORE.ContractError(
                "attendee bootstrap response echoes gateway context"
            )


def _base_manifest_document() -> dict[str, Any]:
    manifest = _base_manifest()
    prefix = (
        ".venv/bin/python tools/product_attendee_contract.py"
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
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_launch_selection_live_contract.py",
                    "tools/product_attendee_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_launch_selection_live_contract.py",
                    "tools/product_attendee_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _base_generated_client_source(openapi)
    source = source.replace(
        "Generated by tools/product_launch_selection_live_contract.py",
        "Generated by tools/product_attendee_contract.py",
        1,
    )
    source = source.replace('"provider_calls": True', '"provider_calls": False')

    regex_marker = (
        'OFFERING_RE = re.compile(r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")\n'
    )
    regexes = regex_marker + (
        'GATEWAY_CONTEXT_RE = re.compile(r"^gateway-context:[A-Za-z0-9_-]{24,128}$")\n'
        'NOTICE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")\n'
    )
    if source.count(regex_marker) != 1:
        raise _CORE.ContractError("generated attendee regex insertion point is missing")
    source = source.replace(regex_marker, regexes, 1)

    validation_marker = "class ProductSessionPreviewClient:\n"
    validation = r"""

ATTENDEE_OPTION_FORBIDDEN_KEYS = {
    "admin", "artifact", "artifact_path", "authority", "backend_path",
    "credential", "forge_reference", "gateway_context_id", "launch_grant",
    "notice_version_presented", "profile", "profile_name", "profile_path",
    "raw_state", "runtime_document", "selected_record_id", "session_authority",
    "source_path", "state_profile", "world_state",
}


def _no_attendee_option_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        if ATTENDEE_OPTION_FORBIDDEN_KEYS.intersection(value):
            raise ValueError("private attendee field crossed the safe option projection")
        for item in value.values():
            _no_attendee_option_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _no_attendee_option_forbidden_keys(item)


def _validate_attendee_launch_option(value: Any) -> dict[str, Any]:
    required = {
        "launch_selection_id", "offering_id", "display", "origin",
        "revision", "notice_version", "expires_at",
    }
    value = _launch_exact_object(value, required, "attendee launch option")
    _no_attendee_option_forbidden_keys(value)
    _forge_identifier("attendee launch selection ID", value["launch_selection_id"], LAUNCH_SELECTION_RE)
    _forge_identifier("attendee offering ID", value["offering_id"], OFFERING_RE)
    display = _launch_exact_object(
        value["display"],
        {"title", "source_label", "revision_label"},
        "attendee launch display metadata",
    )
    _launch_safe_text("attendee launch title", display["title"], 120)
    _launch_safe_text("attendee launch source label", display["source_label"], 64)
    _launch_safe_text("attendee launch revision label", display["revision_label"], 96)
    origin = _launch_exact_object(value["origin"], {"kind"}, "attendee launch origin")
    if origin["kind"] != "authored_profile_snapshot":
        raise ValueError("attendee launch origin is not authored")
    revision = _launch_exact_object(value["revision"], {"revision_id", "sha256"}, "attendee launch revision")
    revision_id = _forge_identifier("attendee launch revision ID", revision["revision_id"], FORGE_REVISION_RE)
    sha256 = _forge_identifier("attendee launch revision digest", revision["sha256"], SHA256_RE)
    if revision_id != "world-revision:sha256:" + sha256:
        raise ValueError("attendee revision ID and digest disagree")
    _forge_identifier("attendee notice version", value["notice_version"], NOTICE_VERSION_RE)
    if value["expires_at"] is not None:
        _forge_timestamp("attendee launch option expiry", value["expires_at"])
    return value


def validate_attendee_context_bootstrap(
    value: Any,
    *,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "attendee_context_bootstrapped":
        raise ValueError("attendee context bootstrap response is invalid")
    _context(value)
    if not isinstance(value.get("replayed"), bool):
        raise ValueError("attendee context replay truth is invalid")
    if value.get("authority_class") != "attendee":
        raise ValueError("attendee context authority class is invalid")
    _forge_identifier("attendee actor context", value.get("actor_context"), ACTOR_RE)
    _forge_timestamp("attendee actor expiry", value.get("actor_expires_at"))
    if "gateway_context_id" in value:
        raise ValueError("gateway context crossed the bootstrap response")
    if expected_epoch is not None:
        expected = _forge_identifier("expected service epoch", expected_epoch, EPOCH_RE)
        if value.get("service_epoch") != expected:
            raise ValueError("returned attendee context epoch diverged")
    return value


def validate_attendee_launch_options(
    value: Any,
    *,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "attendee_launch_options":
        raise ValueError("attendee launch-options response is invalid")
    _context(value)
    if expected_epoch is not None:
        expected = _forge_identifier("expected service epoch", expected_epoch, EPOCH_RE)
        if value.get("service_epoch") != expected:
            raise ValueError("returned attendee launch-options epoch diverged")
    options = value.get("options")
    if not isinstance(options, list) or len(options) > 128:
        raise ValueError("attendee launch options are invalid")
    selection_ids: set[str] = set()
    offering_ids: set[str] = set()
    revision_bindings: dict[str, tuple[str, str, str, str]] = {}
    for option in options:
        _validate_attendee_launch_option(option)
        selection_id = option["launch_selection_id"]
        offering_id = option["offering_id"]
        if selection_id in selection_ids or offering_id in offering_ids:
            raise ValueError("attendee launch option IDs are duplicated")
        selection_ids.add(selection_id)
        offering_ids.add(offering_id)
        revision_id = option["revision"]["revision_id"]
        binding = (
            option["display"]["title"],
            option["display"]["source_label"],
            option["display"]["revision_label"],
            option["notice_version"],
        )
        prior = revision_bindings.get(revision_id)
        if prior is not None and prior != binding:
            raise ValueError("attendee revision option metadata is inconsistent")
        revision_bindings[revision_id] = binding
    return value


def validate_attendee_launch_selection(
    value: Any,
    *,
    expected_launch_selection_id: str | None = None,
    expected_notice_version: str | None = None,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "attendee_launch_selection_granted":
        raise ValueError("attendee launch-selection response is invalid")
    _context(value)
    if not isinstance(value.get("replayed"), bool):
        raise ValueError("attendee launch-selection replay truth is invalid")
    if expected_epoch is not None:
        expected = _forge_identifier("expected service epoch", expected_epoch, EPOCH_RE)
        if value.get("service_epoch") != expected:
            raise ValueError("returned attendee launch-selection epoch diverged")
    launch_grant = _bounded_header(
        "attendee launch grant",
        value.get("launch_grant"),
        re.compile(r"^launch-grant:[0-9a-f]{16}_[0-9a-f]{64}$"),
    )
    serial = launch_grant.removeprefix("launch-grant:").split("_", 1)[0]
    if int(serial, 16) == 0:
        raise ValueError("attendee launch grant serial must be positive")
    grant_expires_at = _forge_timestamp("attendee launch grant expiry", value.get("grant_expires_at"))
    selection = _validate_attendee_launch_option(value.get("selection"))
    if selection["expires_at"] is not None and grant_expires_at > _forge_timestamp(
        "attendee launch option expiry", selection["expires_at"]
    ):
        raise ValueError("attendee launch grant expiry exceeds its option expiry")
    if expected_launch_selection_id is not None:
        expected = _forge_identifier(
            "expected attendee launch selection ID",
            expected_launch_selection_id,
            LAUNCH_SELECTION_RE,
        )
        if selection["launch_selection_id"] != expected:
            raise ValueError("returned attendee launch selection diverged")
    if expected_notice_version is not None:
        expected_notice = _forge_identifier(
            "expected attendee notice version",
            expected_notice_version,
            NOTICE_VERSION_RE,
        )
        if selection["notice_version"] != expected_notice:
            raise ValueError("returned attendee notice version diverged")
    return value


"""
    if source.count(validation_marker) != 1:
        raise _CORE.ContractError(
            "generated attendee validation insertion point is missing"
        )
    source = source.replace(validation_marker, validation + validation_marker, 1)

    method_marker = "    def _control_mutation("
    methods = r"""
    def bootstrap_attendee_context(self, *, epoch: str, key: str, gateway_context_id: str) -> dict[str, Any]:
        gateway_context_id = _forge_identifier(
            "gateway context ID",
            gateway_context_id,
            GATEWAY_CONTEXT_RE,
        )
        payload = self._request(
            "POST",
            "/product/v1/attendee/contexts",
            self._headers(epoch=epoch, idempotency_key=key),
            {"gateway_context_id": gateway_context_id},
        )
        return validate_attendee_context_bootstrap(payload, expected_epoch=epoch)

    def attendee_launch_options(self, *, epoch: str, actor: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/product/v1/attendee/launch-options",
            self._headers(epoch=epoch, actor=actor),
        )
        return validate_attendee_launch_options(payload, expected_epoch=epoch)

    def exchange_attendee_launch_selection(self, *, epoch: str, actor: str, key: str, launch_selection_id: str, notice_version_presented: str) -> dict[str, Any]:
        launch_selection_id = _forge_identifier(
            "attendee launch selection ID",
            launch_selection_id,
            LAUNCH_SELECTION_RE,
        )
        notice_version_presented = _forge_identifier(
            "presented attendee notice version",
            notice_version_presented,
            NOTICE_VERSION_RE,
        )
        payload = self._request(
            "POST",
            "/product/v1/attendee/launch-selections",
            self._headers(epoch=epoch, actor=actor, idempotency_key=key),
            {
                "launch_selection_id": launch_selection_id,
                "notice_version_presented": notice_version_presented,
            },
        )
        return validate_attendee_launch_selection(
            payload,
            expected_launch_selection_id=launch_selection_id,
            expected_notice_version=notice_version_presented,
            expected_epoch=epoch,
        )

"""
    if source.count(method_marker) != 1:
        raise _CORE.ContractError(
            "generated attendee client insertion point is missing"
        )
    return source.replace(method_marker, methods + method_marker, 1)


def _smoke_import(path: Path, root: Path) -> None:
    _base_smoke_import(path, root)
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_generated_smoke",
        path,
    )
    if specification is None or specification.loader is None:
        raise _CORE.ContractError("cannot import generated attendee client")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    context_fresh = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/context-fresh.json"
    )
    context_replay = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/context-replayed.json"
    )
    options = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/options-authored.json"
    )
    empty = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/options-empty.json"
    )
    selection_fresh = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/selection-fresh.json"
    )
    selection_replay = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/selection-replayed.json"
    )
    module.validate_attendee_context_bootstrap(context_fresh)
    module.validate_attendee_context_bootstrap(context_replay)
    module.validate_attendee_launch_options(options)
    module.validate_attendee_launch_options(empty)
    for payload in (selection_fresh, selection_replay):
        module.validate_attendee_launch_selection(
            payload,
            expected_launch_selection_id=payload["selection"]["launch_selection_id"],
            expected_notice_version=payload["selection"]["notice_version"],
        )

    rejected_mutations = 0

    def reject(label: str, operation: Any) -> None:
        nonlocal rejected_mutations
        try:
            operation()
        except ValueError:
            rejected_mutations += 1
            return
        raise _CORE.ContractError(
            f"generated attendee client accepted {label} mutation"
        )

    changed = deepcopy(context_fresh)
    changed["kind"] = "future_kind"
    reject("context kind", lambda: module.validate_attendee_context_bootstrap(changed))
    changed = deepcopy(context_fresh)
    changed["replayed"] = 1
    reject(
        "context replay", lambda: module.validate_attendee_context_bootstrap(changed)
    )
    changed = deepcopy(context_fresh)
    changed["actor_context"] = "actor:short"
    reject("context actor", lambda: module.validate_attendee_context_bootstrap(changed))
    changed = deepcopy(context_fresh)
    changed["actor_expires_at"] = "not-a-timestamp"
    reject(
        "context expiry", lambda: module.validate_attendee_context_bootstrap(changed)
    )
    reject(
        "context epoch",
        lambda: module.validate_attendee_context_bootstrap(
            context_fresh,
            expected_epoch="epoch:wrong_attendee_context_0001",
        ),
    )

    changed = deepcopy(options)
    changed["options"][0]["origin"]["kind"] = "world_forge_selection"
    reject(
        "Forge attendee origin",
        lambda: module.validate_attendee_launch_options(changed),
    )
    changed = deepcopy(options)
    del changed["options"][0]["notice_version"]
    reject("missing notice", lambda: module.validate_attendee_launch_options(changed))
    changed = deepcopy(options)
    changed["options"].append(deepcopy(changed["options"][0]))
    reject("duplicate option", lambda: module.validate_attendee_launch_options(changed))
    changed = deepcopy(options)
    second = deepcopy(changed["options"][0])
    second["launch_selection_id"] = "launch-selection:" + "d" * 32
    changed["options"].append(second)
    reject(
        "duplicate offering", lambda: module.validate_attendee_launch_options(changed)
    )
    changed = deepcopy(options)
    changed["options"][0]["profile_path"] = "/private/profile.json"
    reject("profile path", lambda: module.validate_attendee_launch_options(changed))
    changed = deepcopy(options)
    changed["options"][0]["notice_version"] = "bad notice"
    reject("notice version", lambda: module.validate_attendee_launch_options(changed))
    changed = deepcopy(options)
    changed["options"][0]["revision"]["sha256"] = "d" * 64
    reject(
        "revision mismatch", lambda: module.validate_attendee_launch_options(changed)
    )
    changed = deepcopy(options)
    changed["options"][0]["display"]["future"] = "unsafe"
    reject(
        "nested additive display",
        lambda: module.validate_attendee_launch_options(changed),
    )
    reject(
        "options epoch",
        lambda: module.validate_attendee_launch_options(
            options,
            expected_epoch="epoch:wrong_attendee_options_0001",
        ),
    )

    changed = deepcopy(selection_fresh)
    changed["kind"] = "future_kind"
    reject("selection kind", lambda: module.validate_attendee_launch_selection(changed))
    changed = deepcopy(selection_fresh)
    changed["replayed"] = 1
    reject(
        "selection replay", lambda: module.validate_attendee_launch_selection(changed)
    )
    changed = deepcopy(selection_fresh)
    changed["launch_grant"] = "offering:not-authority"
    reject(
        "selection grant", lambda: module.validate_attendee_launch_selection(changed)
    )
    changed = deepcopy(selection_fresh)
    changed["launch_grant"] = "launch-grant:0000000000000000_" + "c" * 64
    reject(
        "zero grant serial", lambda: module.validate_attendee_launch_selection(changed)
    )
    changed = deepcopy(selection_fresh)
    changed["grant_expires_at"] = "not-a-timestamp"
    reject(
        "selection grant expiry",
        lambda: module.validate_attendee_launch_selection(changed),
    )
    changed = deepcopy(selection_fresh)
    changed["grant_expires_at"] = "2026-08-28T15:00:00Z"
    reject(
        "grant outlives option",
        lambda: module.validate_attendee_launch_selection(changed),
    )
    changed = deepcopy(selection_fresh)
    changed["selection"]["launch_grant"] = changed["launch_grant"]
    reject(
        "nested launch grant",
        lambda: module.validate_attendee_launch_selection(changed),
    )
    reject(
        "selection ID divergence",
        lambda: module.validate_attendee_launch_selection(
            selection_fresh,
            expected_launch_selection_id="launch-selection:" + "d" * 32,
        ),
    )
    reject(
        "selection notice divergence",
        lambda: module.validate_attendee_launch_selection(
            selection_fresh,
            expected_notice_version="notice:another-attendee-version",
        ),
    )

    class NoTransport:
        calls = 0

        def open(self, *_args: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("invalid attendee input reached transport")

    client = module.ProductSessionPreviewClient(
        "http://127.0.0.1",
        "private-attendee-contract-smoke-bearer",
    )
    no_transport = NoTransport()
    client._opener = no_transport
    reject(
        "invalid gateway context before transport",
        lambda: client.bootstrap_attendee_context(
            epoch=options["service_epoch"],
            key="attendee-bootstrap-smoke-key-0001",
            gateway_context_id="gateway-context:short",
        ),
    )
    reject(
        "invalid launch selection before transport",
        lambda: client.exchange_attendee_launch_selection(
            epoch=options["service_epoch"],
            actor="actor:attendee_smoke_context_0001",
            key="attendee-selection-smoke-key-0001",
            launch_selection_id="offering:not-a-selection",
            notice_version_presented=options["options"][0]["notice_version"],
        ),
    )
    reject(
        "invalid notice before transport",
        lambda: client.exchange_attendee_launch_selection(
            epoch=options["service_epoch"],
            actor="actor:attendee_smoke_context_0001",
            key="attendee-selection-smoke-key-0002",
            launch_selection_id=options["options"][0]["launch_selection_id"],
            notice_version_presented="bad notice",
        ),
    )
    if no_transport.calls != 0:
        raise _CORE.ContractError("generated attendee client dispatched invalid input")
    if rejected_mutations != OWN_CLIENT_SMOKE_REJECTED_MUTATIONS:
        raise _CORE.ContractError(
            f"generated attendee mutation count drifted: {rejected_mutations}"
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
            raise _CORE.ContractError("published attendee artifacts are immutable")
    return _base_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _base_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


for module in _MODULES:
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
