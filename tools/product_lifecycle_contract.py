#!/usr/bin/env python3
"""Validate and generate the unfrozen product lifecycle preview contract.

This development-only tool is deliberately independent from DREAMLAND runtime
modules and from the Phase 0 contract tool.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import py_compile
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.2.0-rc.2"
CONTRACT_COMPATIBILITY = "exact-release-candidate"
AVAILABILITY = "integration-preview-only"
CAPABILITY_NAMES = (
    "product_session_core",
    "presenter_stage_terminal",
    "public_attendee_sessions",
    "forge_to_stage_handoff",
    "stage_xray",
)
CAPABILITY_SET = frozenset(CAPABILITY_NAMES)
EXPECTED_OPERATIONS = {
    "/product/v1/capabilities": ("get", "getProductCapabilities"),
    "/product/v1/sessions": ("post", "createLifecyclePreview"),
    "/product/v1/sessions/current": ("get", "getCurrentLifecyclePreview"),
    "/product/v1/sessions/{reference}": (
        "get",
        "getLifecyclePreviewSnapshot",
    ),
    "/product/v1/sessions/{reference}/close": (
        "post",
        "closeLifecyclePreview",
    ),
}
EXPECTED_OPERATION_IDEMPOTENCY = {
    "createLifecyclePreview": {
        "operation_family": "create_lifecycle_preview",
        "policy": "#/x-dreamland-idempotency-policy",
        "resource_authority_kind": "launch_grant",
    },
    "closeLifecyclePreview": {
        "operation_family": "close_lifecycle_preview",
        "policy": "#/x-dreamland-idempotency-policy",
        "resource_authority_kind": "session_capability",
    },
}
EXPECTED_IDEMPOTENCY_POLICY = {
    "claim_boundary": {
        "claimed": {
            "deterministic_rejections": [
                {
                    "category": "launch_grant_rejected",
                    "operation_families": ["create_lifecycle_preview"],
                    "wire_codes": ["authority_denied"],
                },
                {
                    "category": "current_session_already_exists",
                    "operation_families": ["create_lifecycle_preview"],
                    "wire_codes": ["current_session_exists"],
                },
                {
                    "category": "session_reference_or_capability_rejected",
                    "operation_families": ["close_lifecycle_preview"],
                    "wire_codes": ["authority_denied"],
                },
                {
                    "category": "control_revision_changed",
                    "operation_families": ["close_lifecycle_preview"],
                    "wire_codes": ["control_revision_conflict"],
                },
                {
                    "category": "key_already_bound_to_another_body",
                    "operation_families": [
                        "close_lifecycle_preview",
                        "create_lifecycle_preview",
                    ],
                    "wire_codes": ["idempotency_conflict"],
                },
            ],
            "different_body": "return_idempotency_conflict_without_mutation",
            "fresh_metadata_may_differ": [
                "observed_at",
                "replayed",
                "request_id",
            ],
            "postclaim_terminal_default": "record_and_replay_before_response",
            "same_body": "replay_recorded_logical_outcome_and_status",
            "successful_results": ["closed", "created"],
        },
        "preclaim": {
            "binds_key": False,
            "reservation_point": (
                "after private service authentication, current expected epoch, "
                "authorized actor context, valid idempotency key, and canonical "
                "semantic body; before resource-authority and deterministic business "
                "checks"
            ),
            "same_key_changed_body": "permitted_because_no_binding_exists",
            "wire_error_categories": [
                {
                    "category": (
                        "private_service_authentication_or_route_availability"
                    ),
                    "wire_codes": ["unauthorized", "not_found"],
                },
                {
                    "category": "missing_malformed_or_stale_expected_epoch",
                    "wire_codes": [
                        "expected_epoch_required",
                        "invalid_request",
                        "outcome_unknown",
                    ],
                },
                {
                    "category": ("actor_context_rejected_before_resource_authority"),
                    "wire_codes": ["authority_denied", "invalid_request"],
                },
                {
                    "category": (
                        "malformed_key_headers_media_or_body_before_canonicalization"
                    ),
                    "wire_codes": [
                        "content_length_required",
                        "invalid_json",
                        "invalid_request",
                        "request_too_large",
                        "unsupported_media_type",
                    ],
                },
                {
                    "category": "internal_failure_before_reservation",
                    "wire_codes": ["internal_error"],
                },
            ],
        },
        "transient_unclaimed": [
            {
                "category": "capacity_check_before_work",
                "client_guidance": (
                    "retry_only_the_identical_key_and_body_after_retry_delay"
                ),
                "key_binding": "none",
                "same_key_changed_body": (
                    "permitted_because_no_binding_exists_but_a_fresh_key_is_recommended"
                ),
                "wire_codes": ["capacity_exhausted"],
            }
        ],
    },
    "epoch_namespace": {
        "deliberate_fresh_work_requires": [
            "current_epoch_evidence",
            "current_epoch_authority",
            "new_idempotency_key",
        ],
        "prior_epoch_records": "not_replayed_in_the_new_epoch",
        "rotation": "terminates_the_prior_in_memory_namespace",
        "stale_mutation_claims_current_epoch_key": False,
        "stale_mutation_wire_code": "outcome_unknown",
    },
    "retention": {
        "cleanup_rule": (
            "remove_only_after_the_applicable_retention_scope_expires; a removed "
            "failed-operation binding may be claimed again within the live epoch"
        ),
        "session_related_records": (
            "at_least_through_related_session_and_tombstone_cleanup"
        ),
        "unrelated_claimed_failures": {
            "scope": "bounded_failed_operation_retention",
            "ttl_configuration": "failed_operation_ttl_ms",
        },
    },
    "scope": {
        "authority_class_definition": (
            "stable_authenticated_actor_role_not_resource_authority_evidence"
        ),
        "coordinates": [
            "service_epoch",
            "actor_context",
            "authority_class",
            "operation_family",
            "idempotency_key",
        ],
        "first_body_binding": "canonical_semantic_request_body",
        "operation_families": [
            "close_lifecycle_preview",
            "create_lifecycle_preview",
        ],
        "request_authority_storage": {
            "comparison_material": "sha256_digest",
            "raw_request_authority_in_scope_body_or_failure_records": "forbidden",
            "successful_result_authority": (
                "a bounded generated session capability may be retained in active "
                "session state for current recovery and in its successful create "
                "replay result; terminalization removes the active-state copy and "
                "related tombstone cleanup removes the replay-result copy"
            ),
        },
    },
    "version": 1,
    "wire_code_resolution": {
        "close_resource_authority_rejection": "authority_denied",
        "create_resource_authority_rejection": "authority_denied",
        "grant_unavailable": ("declared_safe_error_code_not_emitted_by_rc2_mutations"),
        "not_found": (
            "preclaim_route_or_nonmutation_code_not_emitted_for_postclaim_close_"
            "resource_rejection"
        ),
    },
}
EXPECTED_PARAMETER_REFS = {
    "getProductCapabilities": (),
    "createLifecyclePreview": (
        "#/components/parameters/ExpectedEpoch",
        "#/components/parameters/ActorContext",
        "#/components/parameters/IdempotencyKey",
    ),
    "getCurrentLifecyclePreview": (
        "#/components/parameters/ExpectedEpoch",
        "#/components/parameters/ActorContext",
    ),
    "getLifecyclePreviewSnapshot": (
        "#/components/parameters/SessionReference",
        "#/components/parameters/ExpectedEpoch",
        "#/components/parameters/ActorContext",
        "#/components/parameters/SessionCapability",
    ),
    "closeLifecyclePreview": (
        "#/components/parameters/SessionReference",
        "#/components/parameters/ExpectedEpoch",
        "#/components/parameters/ActorContext",
        "#/components/parameters/SessionCapability",
        "#/components/parameters/IdempotencyKey",
    ),
}
EXPECTED_RESPONSES = {
    "getProductCapabilities": frozenset({"200", "401", "500"}),
    "createLifecyclePreview": frozenset(
        {"201", "400", "401", "403", "409", "410", "411", "413", "415", "500", "503"}
    ),
    "getCurrentLifecyclePreview": frozenset(
        {"200", "400", "401", "403", "404", "409", "410", "500"}
    ),
    "getLifecyclePreviewSnapshot": frozenset(
        {"200", "400", "401", "403", "404", "409", "410", "500"}
    ),
    "closeLifecyclePreview": frozenset(
        {"200", "400", "401", "403", "404", "409", "410", "411", "413", "415", "500"}
    ),
}
SUCCESS_SCHEMAS = {
    "getProductCapabilities": ("200", "ProductCapabilitiesResponse"),
    "createLifecyclePreview": ("201", "CreateLifecyclePreviewResponse"),
    "getCurrentLifecyclePreview": ("200", "CurrentLifecyclePreviewResponse"),
    "getLifecyclePreviewSnapshot": ("200", "LifecyclePreviewSnapshotResponse"),
    "closeLifecyclePreview": ("200", "CloseLifecyclePreviewResponse"),
}
REQUEST_SCHEMAS = {
    "createLifecyclePreview": "CreateLifecyclePreviewRequest",
    "closeLifecyclePreview": "CloseLifecyclePreviewRequest",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
DISPLAY_SAFE_PATTERN = r"^[^\u0000-\u001F\u007F-\u009F]{1,160}$"
MAX_CONTROL_REVISION = 9_007_199_254_740_991
# Exact-RC semantic trust anchor. An intentional OpenAPI change must update the
# validator, adapter/client assumptions, fixtures/tests, and this digest in the
# same review. In particular, refresh must never derive this value from the
# artifact it is being asked to bless.
EXPECTED_OPENAPI_SEMANTIC_SHA256 = (
    "d786fa12bcc0bbd803da872507f5c424c81a27d358c2ee43cae5884a95c34de8"
)
EXPECTED_PARAMETER_COMPONENTS = {
    "ActorContext": {
        "in": "header",
        "name": "X-Dreamland-Actor-Context",
        "required": True,
        "schema": {"$ref": "#/components/schemas/ActorContext"},
    },
    "ExpectedEpoch": {
        "description": (
            "Freshness evidence for the running service instance. Obtain this value "
            "from getProductCapabilities.service_epoch; never copy an epoch from an "
            "example payload or fixture."
        ),
        "in": "header",
        "name": "X-Dreamland-Expected-Epoch",
        "required": True,
        "schema": {"$ref": "#/components/schemas/ServiceEpoch"},
    },
    "IdempotencyKey": {
        "description": (
            "Mutation idempotency key governed by "
            "x-dreamland-idempotency-policy. Pre-claim and capacity failures leave "
            "it unbound; a claimed deterministic outcome binds its first canonical "
            "semantic body for replay and changed-body conflict detection."
        ),
        "in": "header",
        "name": "Idempotency-Key",
        "required": True,
        "schema": {"$ref": "#/components/schemas/IdempotencyKey"},
    },
    "SessionCapability": {
        "in": "header",
        "name": "X-Dreamland-Session-Capability",
        "required": True,
        "schema": {"$ref": "#/components/schemas/SessionCapability"},
    },
    "SessionReference": {
        "in": "path",
        "name": "reference",
        "required": True,
        "schema": {"$ref": "#/components/schemas/SessionReference"},
    },
}
EXPECTED_IDENTITY_AUTHORITY_SCHEMAS = {
    "ActorContext": {
        "maxLength": 134,
        "pattern": r"^actor:[A-Za-z0-9_-]{16,128}$",
        "type": "string",
    },
    "ServiceEpoch": {
        "maxLength": 134,
        "pattern": r"^epoch:[A-Za-z0-9_-]{16,128}$",
        "type": "string",
    },
    "SessionCapability": {
        "maxLength": 268,
        "pattern": r"^session-cap:[A-Za-z0-9_-]{24,256}$",
        "type": "string",
    },
    "IdempotencyKey": {
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
        "type": "string",
    },
    "LaunchGrant": {
        "maxLength": 493,
        "pattern": r"^launch-grant:[A-Za-z0-9_-]{24,480}$",
        "type": "string",
    },
    "SessionReference": {
        "maxLength": 140,
        "pattern": r"^session-ref:[A-Za-z0-9_-]{16,128}$",
        "type": "string",
    },
}
EXPECTED_REQUEST_SCHEMAS = {
    "CreateLifecyclePreviewRequest": {
        "additionalProperties": False,
        "properties": {"launch_grant": {"$ref": "#/components/schemas/LaunchGrant"}},
        "required": ["launch_grant"],
        "type": "object",
    },
    "CloseLifecyclePreviewRequest": {
        "additionalProperties": False,
        "properties": {
            "expected_control_revision": {
                "maximum": MAX_CONTROL_REVISION,
                "minimum": 1,
                "type": "integer",
            }
        },
        "required": ["expected_control_revision"],
        "type": "object",
    },
}
EXPECTED_RESPONSE_OBJECT_SHAPES = {
    "ProductCapabilitiesResponse": (
        frozenset(
            {"contract", "service_epoch", "request_id", "admission", "capabilities"}
        ),
        True,
    ),
    "CreateLifecyclePreviewResponse": (
        frozenset({"result", "replayed", "session_capability", "snapshot"}),
        True,
    ),
    "CurrentNoneResponse": (frozenset({"result"}), True),
    "CurrentActiveResponse": (
        frozenset({"result", "session_capability", "snapshot"}),
        True,
    ),
    "CurrentTerminalResponse": (frozenset({"result", "snapshot"}), True),
    "LifecyclePreviewSnapshotResponse": (
        frozenset({"result", "snapshot"}),
        True,
    ),
    "CloseLifecyclePreviewResponse": (
        frozenset({"result", "replayed", "snapshot"}),
        True,
    ),
    "SafeErrorEnvelope": (frozenset({"error"}), False),
    "SafeError": (
        frozenset({"code", "message", "retry", "retry_after_ms", "request_id"}),
        False,
    ),
}
ARTIFACT_PATHS = {
    "manifest_schema_sha256": "manifest.schema.json",
    "openapi_sha256": "openapi.json",
}
FORBIDDEN_SCHEMA_FRAGMENTS = (
    "accepted_state",
    "cancel",
    "command",
    "enrichment",
    "event",
    "guest_termination",
    "provider",
    "reset",
    "state_effect",
    "switch",
    "transcript",
)
FORBIDDEN_SNAPSHOT_FIELDS = frozenset(
    {
        "accepted_state_revision",
        "active_command",
        "command_slot_generation",
        "commands",
        "event_position",
        "events",
        "state_effect",
        "transcript",
        "transcript_position",
    }
)
SAFE_ERROR_DENYLIST = frozenset(
    {
        "capability",
        "credential",
        "detail",
        "environment",
        "exception",
        "path",
        "prompt",
        "provider_envelope",
        "raw",
        "reasoning",
        "secret",
        "stack",
        "token",
        "trace",
        "traceback",
    }
)
SAFE_ERRORS: dict[str, tuple[str, str, int | None]] = {
    "unauthorized": (
        "Missing or invalid private service authority.",
        "none",
        None,
    ),
    "not_found": (
        "The requested lifecycle preview resource is unavailable.",
        "none",
        None,
    ),
    "grant_unavailable": ("The launch grant is unavailable.", "none", None),
    "current_session_exists": (
        "A current lifecycle preview session already exists.",
        "resume_snapshot",
        None,
    ),
    "idempotency_conflict": (
        "The idempotency key was already used for another request.",
        "none",
        None,
    ),
    "control_revision_conflict": (
        "The lifecycle control revision has changed.",
        "resume_snapshot",
        None,
    ),
    "session_lost": (
        "The lifecycle preview session was lost after service restart.",
        "none",
        None,
    ),
    "outcome_unknown": (
        "The prior lifecycle mutation outcome is unknown after service restart.",
        "none",
        None,
    ),
    "invalid_request": (
        "The private lifecycle request is invalid.",
        "none",
        None,
    ),
    "invalid_json": ("The request body is not valid JSON.", "none", None),
    "request_too_large": (
        "The request body exceeds the lifecycle preview limit.",
        "none",
        None,
    ),
    "unsupported_media_type": (
        "The request content type is unsupported.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        "Expected product service epoch evidence is required.",
        "none",
        None,
    ),
    "authority_denied": (
        "The requested lifecycle preview resource is unavailable.",
        "none",
        None,
    ),
    "content_length_required": (
        "A Content-Length header is required.",
        "none",
        None,
    ),
    "capacity_exhausted": (
        "Lifecycle preview capacity is unavailable.",
        "after_delay",
        1000,
    ),
    "internal_error": (
        "The lifecycle preview request could not be completed.",
        "none",
        None,
    ),
}

# path -> (case, class, schema target, expected behavior)
FIXTURE_SPECS: dict[str, tuple[str, str, str, str]] = {
    "fixtures/canonical-valid/authority/actor-alpha.json": (
        "authority_actor_alpha",
        "canonical-valid",
        "openapi.json#/components/schemas/IntegrationAuthorityFixture",
        "valid",
    ),
    "fixtures/canonical-valid/authority/actor-bravo.json": (
        "authority_actor_bravo",
        "canonical-valid",
        "openapi.json#/components/schemas/IntegrationAuthorityFixture",
        "valid",
    ),
    "fixtures/canonical-valid/authority/simulator-secondary-alpha-grant.json": (
        "simulator_secondary_alpha_grant",
        "canonical-valid",
        "openapi.json#/components/schemas/IntegrationSimulatorGrantFixture",
        "valid",
    ),
    "fixtures/canonical-valid/authority/simulator-epoch-loss-current-alpha.json": (
        "simulator_epoch_loss_current_alpha",
        "canonical-valid",
        "openapi.json#/components/schemas/IntegrationSimulatorGrantFixture",
        "valid",
    ),
    "fixtures/canonical-valid/authority/cross-actor-grant-denied.json": (
        "cross_actor_grant_denied",
        "canonical-valid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "valid",
    ),
    "fixtures/canonical-valid/authority/cross-actor-session-capability-denied.json": (
        "cross_actor_session_capability_denied",
        "canonical-valid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "valid",
    ),
    "fixtures/canonical-valid/capabilities/all-disabled.json": (
        "capabilities_all_disabled",
        "canonical-valid",
        "openapi.json#/components/schemas/ProductCapabilitiesResponse",
        "valid",
    ),
    "fixtures/canonical-valid/close/replay.json": (
        "close_replay",
        "canonical-valid",
        "openapi.json#/components/schemas/CloseLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/close/request.json": (
        "close_request",
        "canonical-valid",
        "openapi.json#/components/schemas/CloseLifecyclePreviewRequest",
        "valid",
    ),
    "fixtures/canonical-valid/close/success.json": (
        "close_success",
        "canonical-valid",
        "openapi.json#/components/schemas/CloseLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/current/active.json": (
        "current_active",
        "canonical-valid",
        "openapi.json#/components/schemas/CurrentLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/current/authority-denied.json": (
        "current_authority_denied",
        "canonical-valid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "valid",
    ),
    "fixtures/canonical-valid/current/none.json": (
        "current_none",
        "canonical-valid",
        "openapi.json#/components/schemas/CurrentLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/current/terminal-closed.json": (
        "current_terminal_closed",
        "canonical-valid",
        "openapi.json#/components/schemas/CurrentLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/current/terminal-expired.json": (
        "current_terminal_expired",
        "canonical-valid",
        "openapi.json#/components/schemas/CurrentLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/sessions/create-request.json": (
        "create_request",
        "canonical-valid",
        "openapi.json#/components/schemas/CreateLifecyclePreviewRequest",
        "valid",
    ),
    "fixtures/canonical-valid/sessions/create-success.json": (
        "create_success",
        "canonical-valid",
        "openapi.json#/components/schemas/CreateLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/sessions/create-replay.json": (
        "create_replay",
        "canonical-valid",
        "openapi.json#/components/schemas/CreateLifecyclePreviewResponse",
        "valid",
    ),
    "fixtures/canonical-valid/snapshots/active.json": (
        "snapshot_active",
        "canonical-valid",
        "openapi.json#/components/schemas/LifecyclePreviewSnapshotResponse",
        "valid",
    ),
    "fixtures/forward-compat/capabilities/additive-fields.json": (
        "capabilities_additive_fields",
        "forward-compat",
        "openapi.json#/components/schemas/ProductCapabilitiesResponse",
        "consumer_accept_additive",
    ),
    "fixtures/forward-compat/errors/unknown-code.json": (
        "unknown_safe_error_code",
        "forward-compat",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "consumer_generic_error",
    ),
    "fixtures/forward-compat/snapshots/additive-fields.json": (
        "snapshot_additive_fields",
        "forward-compat",
        "openapi.json#/components/schemas/LifecyclePreviewSnapshotResponse",
        "consumer_accept_additive",
    ),
    "fixtures/invalid/capabilities/enabled-core.json": (
        "enabled_core_rejected",
        "invalid",
        "openapi.json#/components/schemas/CapabilityState",
        "reject",
    ),
    "fixtures/invalid/capabilities/incompatible-revision.json": (
        "incompatible_revision_rejected",
        "invalid",
        "openapi.json#/components/schemas/ContractIdentity",
        "reject",
    ),
    "fixtures/invalid/capabilities/missing-required-capability.json": (
        "missing_capability_rejected",
        "invalid",
        "openapi.json#/components/schemas/ProductCapabilitiesResponse",
        "reject",
    ),
    "fixtures/invalid/close/missing-control-revision.json": (
        "close_missing_control_revision",
        "invalid",
        "openapi.json#/components/schemas/CloseLifecyclePreviewRequest",
        "reject",
    ),
    "fixtures/invalid/close/excessive-control-revision.json": (
        "close_excessive_control_revision",
        "invalid",
        "openapi.json#/components/schemas/CloseLifecyclePreviewRequest",
        "reject",
    ),
    "fixtures/invalid/current/terminal-with-capability.json": (
        "terminal_current_with_capability",
        "invalid",
        "openapi.json#/components/schemas/CurrentTerminalResponse",
        "reject_policy",
    ),
    "fixtures/invalid/errors/unsafe-field.json": (
        "unsafe_error_field",
        "invalid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "reject",
    ),
    "fixtures/invalid/errors/control-message.json": (
        "safe_error_control_message",
        "invalid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "reject",
    ),
    "fixtures/invalid/lifecycle/control-safe-message.json": (
        "terminal_control_safe_message",
        "invalid",
        "openapi.json#/components/schemas/LifecycleState",
        "reject",
    ),
    "fixtures/invalid/lifecycle/malformed-closed.json": (
        "malformed_closed_lifecycle",
        "invalid",
        "openapi.json#/components/schemas/LifecycleState",
        "reject",
    ),
    "fixtures/invalid/lifecycle/unknown-state.json": (
        "unknown_lifecycle_state",
        "invalid",
        "openapi.json#/components/schemas/LifecycleState",
        "reject",
    ),
    "fixtures/invalid/sessions/offering-id-instead-of-grant.json": (
        "offering_id_is_not_grant",
        "invalid",
        "openapi.json#/components/schemas/CreateLifecyclePreviewRequest",
        "reject",
    ),
    "fixtures/invalid/sessions/selection-id-instead-of-grant.json": (
        "selection_id_is_not_grant",
        "invalid",
        "openapi.json#/components/schemas/CreateLifecyclePreviewRequest",
        "reject",
    ),
    "fixtures/invalid/snapshots/session-capability-inside.json": (
        "snapshot_must_not_contain_authority",
        "invalid",
        "openapi.json#/components/schemas/LifecycleSnapshot",
        "reject_policy",
    ),
    "fixtures/invalid/snapshots/zero-control-revision.json": (
        "zero_control_revision",
        "invalid",
        "openapi.json#/components/schemas/LifecycleSnapshot",
        "reject",
    ),
    "fixtures/invalid/snapshots/excessive-control-revision.json": (
        "snapshot_excessive_control_revision",
        "invalid",
        "openapi.json#/components/schemas/LifecycleSnapshot",
        "reject",
    ),
}

for _error_name in SAFE_ERRORS:
    _path_name = _error_name.replace("_", "-")
    FIXTURE_SPECS[f"fixtures/canonical-valid/errors/{_path_name}.json"] = (
        f"safe_error_{_error_name}",
        "canonical-valid",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        "valid",
    )


class ContractError(RuntimeError):
    """A deterministic lifecycle-contract failure."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ContractError(f"non-standard JSON constant: {value}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"could not read JSON {path}: {exc}") from exc


def _canonical_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.write_text(_canonical_json_text(value), encoding="utf-8")


def _contract_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError(f"unsafe contract-relative path: {relative_path!r}")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ContractError(f"contract path may not use symlinks: {relative_path}")
    resolved = (root / relative).resolve(strict=False)
    if root not in resolved.parents:
        raise ContractError(f"contract path escapes its root: {relative_path}")
    return root / relative


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer in {"", "#"}:
        return document
    if not pointer.startswith("#/"):
        raise ContractError(f"only local JSON pointers are supported: {pointer!r}")
    value = document
    for encoded_part in pointer[2:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
            continue
        if isinstance(value, list) and part.isdigit():
            try:
                value = value[int(part)]
                continue
            except IndexError as exc:
                raise ContractError(f"invalid JSON pointer: {pointer!r}") from exc
        raise ContractError(f"invalid JSON pointer: {pointer!r}")
    return value


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _dereference_local(value: Any, document: Any, stack: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [_dereference_local(item, document, stack) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ContractError(f"nonlocal schema reference: {reference!r}")
        if reference in stack:
            raise ContractError("cyclic local schema reference")
        resolved = _dereference_local(
            _json_pointer(document, reference), document, (*stack, reference)
        )
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        if not siblings:
            return resolved
        if not isinstance(resolved, dict):
            raise ContractError(f"cannot merge reference siblings: {reference}")
        return {
            **resolved,
            **{
                key: _dereference_local(item, document, stack)
                for key, item in siblings.items()
            },
        }
    return {
        key: _dereference_local(item, document, stack) for key, item in value.items()
    }


def _schema_for_target(root: Path, target: str) -> dict[str, Any]:
    document_name, separator, pointer = target.partition("#")
    if not separator:
        raise ContractError(f"fixture target must contain '#': {target!r}")
    document = load_json(_contract_path(root, document_name))
    if not isinstance(document, dict):
        raise ContractError("schema document must be an object")
    selected = _json_pointer(document, f"#{pointer}")
    schema = _dereference_local(selected, document)
    if not isinstance(schema, dict):
        raise ContractError(f"fixture schema is not an object: {target}")
    return schema


def _validation_errors(schema: dict[str, Any], instance: Any) -> list[str]:
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        error.message
        for error in sorted(
            validator.iter_errors(instance), key=lambda item: list(item.path)
        )
    ]


def _validate_contract_identity(identity: Any) -> None:
    if not isinstance(identity, dict):
        raise ContractError("contract identity must be an object")
    actual = (
        identity.get("name"),
        identity.get("revision"),
        identity.get("compatibility"),
    )
    expected = (CONTRACT_NAME, CONTRACT_REVISION, CONTRACT_COMPATIBILITY)
    if actual != expected:
        raise ContractError("incompatible lifecycle preview contract identity")


def _validate_capabilities(instance: Any) -> None:
    if not isinstance(instance, dict):
        raise ContractError("capability response must be an object")
    _validate_contract_identity(instance.get("contract"))
    admission = instance.get("admission")
    if admission != {"reason": "integration_preview_only", "state": "closed"}:
        raise ContractError("lifecycle preview admission must remain closed")
    capabilities = instance.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_SET:
        raise ContractError(
            "capability response must contain exactly five capabilities"
        )
    for name, state in capabilities.items():
        if not isinstance(state, dict):
            raise ContractError(f"capability state is malformed: {name}")
        if (
            state.get("declared_revision") != CONTRACT_REVISION
            or state.get("enabled") is not False
            or state.get("enabled_revision") is not None
            or state.get("reason") != "incomplete_integration_preview"
        ):
            raise ContractError(f"capability must remain disabled: {name}")


def _validate_safe_error(
    instance: Any, *, allow_unknown: bool = False
) -> dict[str, Any]:
    if not isinstance(instance, dict) or not isinstance(instance.get("error"), dict):
        raise ContractError("safe error envelope is malformed")
    error = instance["error"]
    unsafe = SAFE_ERROR_DENYLIST.intersection(error)
    if unsafe:
        raise ContractError(
            "safe error contains denied fields: " + ", ".join(sorted(unsafe))
        )
    if set(error) != {"code", "message", "retry", "retry_after_ms", "request_id"}:
        raise ContractError("safe error contains missing or unknown fields")
    message = error.get("message")
    if not isinstance(message, str) or not re.fullmatch(DISPLAY_SAFE_PATTERN, message):
        raise ContractError("safe error message contains display control characters")
    code = error.get("code")
    if code not in SAFE_ERRORS:
        if allow_unknown and isinstance(code, str) and code:
            return {
                "code": "unknown_safe_error",
                "message": "The lifecycle preview request could not be completed.",
                "retry": "none",
                "retry_after_ms": None,
                "request_id": error.get("request_id"),
            }
        raise ContractError("safe error code is outside the lifecycle allowlist")
    message, retry, retry_after = SAFE_ERRORS[code]
    if (
        error.get("message") != message
        or error.get("retry") != retry
        or error.get("retry_after_ms") != retry_after
    ):
        raise ContractError("safe error code/message/retry tuple is not canonical")
    return error


def _snapshot_from_fixture(instance: Any) -> dict[str, Any] | None:
    if not isinstance(instance, dict):
        return None
    if instance.get("model") == "product-session-lifecycle-preview":
        return instance
    snapshot = instance.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def _validate_snapshot_policy(snapshot: dict[str, Any]) -> None:
    forbidden = FORBIDDEN_SNAPSHOT_FIELDS.intersection(snapshot)
    if forbidden:
        raise ContractError(
            "lifecycle snapshot contains Phase 2 fields: "
            + ", ".join(sorted(forbidden))
        )
    if "session_capability" in snapshot:
        raise ContractError("session capability must remain outside snapshots")
    control_revision = snapshot.get("control_revision")
    if (
        isinstance(control_revision, bool)
        or not isinstance(control_revision, int)
        or control_revision < 1
        or control_revision > MAX_CONTROL_REVISION
    ):
        raise ContractError("snapshot control revision is outside the safe range")
    _validate_contract_identity(snapshot.get("contract"))
    launch_context = snapshot.get("launch_context")
    if not isinstance(launch_context, dict) or set(launch_context) != {
        "limits",
        "notice_version",
        "policy_id",
    }:
        raise ContractError("snapshot launch context is missing or not allowlisted")
    if not isinstance(launch_context.get("policy_id"), str) or not re.fullmatch(
        r"policy:[A-Za-z0-9._:-]{4,120}", launch_context["policy_id"]
    ):
        raise ContractError("snapshot policy identity is malformed")
    if not isinstance(launch_context.get("notice_version"), str) or not re.fullmatch(
        r"notice:[A-Za-z0-9._:-]{4,120}", launch_context["notice_version"]
    ):
        raise ContractError("snapshot notice version is malformed")
    limits = launch_context.get("limits")
    if not isinstance(limits, dict) or set(limits) != {
        "absolute_ttl_ms",
        "idle_ttl_ms",
    }:
        raise ContractError("snapshot lifecycle limits are not allowlisted")
    absolute_ttl = limits.get("absolute_ttl_ms")
    idle_ttl = limits.get("idle_ttl_ms")
    if (
        not isinstance(absolute_ttl, int)
        or isinstance(absolute_ttl, bool)
        or not isinstance(idle_ttl, int)
        or isinstance(idle_ttl, bool)
        or absolute_ttl < 1000
        or absolute_ttl > 86400000
        or idle_ttl < 1000
        or idle_ttl > absolute_ttl
    ):
        raise ContractError("snapshot lifecycle limits are invalid")
    lifecycle = snapshot.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise ContractError("snapshot lifecycle is missing")
    state = lifecycle.get("state")
    controls = snapshot.get("available_controls")
    if state == "active":
        if controls != ["snapshot", "close"]:
            raise ContractError("active lifecycle controls are not canonical")
    elif state in {"closed", "expired"}:
        if controls != []:
            raise ContractError("terminal lifecycle must expose no controls")
        safe_message = lifecycle.get("safe_message")
        if not isinstance(safe_message, str) or not re.fullmatch(
            DISPLAY_SAFE_PATTERN, safe_message
        ):
            raise ContractError(
                "terminal lifecycle message contains display control characters"
            )
    else:
        raise ContractError("unknown lifecycle state")


def _validate_fixture_semantics(instance: Any) -> None:
    if (
        isinstance(instance, dict)
        and instance.get("fixture_scope") == "integration-preview-simulator-only"
    ):
        purpose = instance.get("purpose")
        simulator_authority = {
            "exercise-current-session-exists": (
                "actor:preview_alpha_0001",
                "launch-grant:preview_alpha_grant_00000002",
            ),
            "exercise-epoch-loss-current-authority": (
                "actor:preview_alpha_rotated_0001",
                "launch-grant:preview_alpha_rotated_00000001",
            ),
        }.get(purpose)
        if simulator_authority is None:
            raise ContractError("unknown simulator-only authority fixture purpose")
        actor_context, launch_grant = simulator_authority
        expected_simulator_grant = {
            "actor_context": actor_context,
            "allowed_operations": ["create"],
            "authority_class": "attendee",
            "expected_epoch_source": "getProductCapabilities.service_epoch",
            "fixture_scope": "integration-preview-simulator-only",
            "immutable_revision": "world-revision:preview_alpha_revision_0001",
            "launch_context": {
                "limits": {
                    "absolute_ttl_ms": 1800000,
                    "idle_ttl_ms": 600000,
                },
                "notice_version": "notice:conference-preview-v1",
                "policy_id": "policy:conference-preview-v1",
            },
            "launch_grant": launch_grant,
            "purpose": purpose,
            "selected_record_id": "selected-record:preview_alpha_0001",
        }
        if instance != expected_simulator_grant:
            raise ContractError(
                "simulator-only secondary grant fixture is not canonical"
            )
    if isinstance(instance, dict) and "capabilities" in instance:
        _validate_capabilities(instance)
    snapshot = _snapshot_from_fixture(instance)
    if snapshot is not None:
        _validate_snapshot_policy(snapshot)
    if isinstance(instance, dict) and "error" in instance:
        _validate_safe_error(instance)
    if isinstance(instance, dict) and instance.get("result") == "current_terminal":
        if "session_capability" in instance:
            raise ContractError("terminal current response must not return authority")
    if isinstance(instance, dict) and instance.get("result") == "created":
        if "session_capability" not in instance:
            raise ContractError(
                "create response must return authority outside snapshot"
            )
    if isinstance(instance, dict) and instance.get("result") == "closed":
        if snapshot is None or snapshot.get("lifecycle", {}).get("state") != "closed":
            raise ContractError("close response must contain closed lifecycle truth")


def _validate_fixture_expectation(
    *, path: Path, fixture: Any, schema: dict[str, Any], expected: str
) -> None:
    errors = _validation_errors(schema, fixture)
    if expected in {"valid", "consumer_accept_additive"}:
        if errors:
            raise ContractError(f"fixture {path} should be valid: {'; '.join(errors)}")
        _validate_fixture_semantics(fixture)
        if expected == "consumer_accept_additive" and "additive" not in json.dumps(
            fixture
        ):
            raise ContractError(f"fixture {path} does not contain additive data")
        return
    if expected == "consumer_generic_error":
        if errors:
            raise ContractError(
                f"future safe error {path} should remain structurally valid"
            )
        normalized = _validate_safe_error(fixture, allow_unknown=True)
        if normalized["code"] != "unknown_safe_error" or normalized["retry"] != "none":
            raise ContractError(f"fixture {path} does not fail closed for the consumer")
        return
    if expected == "reject":
        if not errors:
            raise ContractError(f"fixture {path} should be rejected by its schema")
        return
    if expected == "reject_policy":
        if errors:
            raise ContractError(
                f"policy fixture {path} should satisfy its structural schema"
            )
        try:
            _validate_fixture_semantics(fixture)
        except ContractError:
            return
        raise ContractError(f"fixture {path} does not violate lifecycle preview policy")
    raise ContractError(f"unknown fixture expectation: {expected}")


def _validate_response_headers(response: dict[str, Any], *, unauthorized: bool) -> None:
    headers = response.get("headers")
    if not isinstance(headers, dict):
        raise ContractError("every response must declare security/cache headers")
    cache = headers.get("Cache-Control")
    no_sniff = headers.get("X-Content-Type-Options")
    if (
        not isinstance(cache, dict)
        or cache.get("schema", {}).get("const") != "no-store"
    ):
        raise ContractError("every response must declare Cache-Control: no-store")
    if (
        not isinstance(no_sniff, dict)
        or no_sniff.get("schema", {}).get("const") != "nosniff"
    ):
        raise ContractError("every response must declare nosniff")
    if unauthorized:
        challenge = headers.get("WWW-Authenticate")
        if (
            not isinstance(challenge, dict)
            or challenge.get("schema", {}).get("const") != "Bearer"
        ):
            raise ContractError("401 response must declare the bearer challenge")


def _validate_openapi(openapi: dict[str, Any], manifest: dict[str, Any]) -> None:
    if openapi.get("openapi") != "3.1.0":
        raise ContractError("OpenAPI document must declare version 3.1.0")
    if openapi.get("servers") != []:
        raise ContractError("lifecycle preview must not advertise a live server")
    info = openapi.get("info")
    if not isinstance(info, dict) or info.get("version") != CONTRACT_REVISION:
        raise ContractError("OpenAPI revision is not the dedicated Phase 1 RC")
    if manifest.get("contract_revision") != CONTRACT_REVISION:
        raise ContractError("manifest revision is not the dedicated Phase 1 RC")
    security = [{"ProductServiceBearer": []}]
    if openapi.get("security") != security:
        raise ContractError("OpenAPI must default to the private bearer")
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or set(paths) != set(EXPECTED_OPERATIONS):
        raise ContractError("OpenAPI lifecycle preview route set is not exact")
    for route, (method, operation_id) in EXPECTED_OPERATIONS.items():
        path_item = paths[route]
        if not isinstance(path_item, dict) or set(path_item) != {method}:
            raise ContractError(f"route has an unexpected method: {route}")
        operation = path_item[method]
        if operation.get("operationId") != operation_id:
            raise ContractError(f"operationId is not canonical: {route}")
        if operation.get("x-dreamland-availability") != AVAILABILITY:
            raise ContractError(f"operation is not labeled integration-only: {route}")
        expected_idempotency = EXPECTED_OPERATION_IDEMPOTENCY.get(operation_id)
        actual_idempotency = operation.get("x-dreamland-idempotency")
        if expected_idempotency is None:
            if actual_idempotency is not None:
                raise ContractError(
                    f"read operation unexpectedly declares idempotency: {route}"
                )
        elif actual_idempotency != expected_idempotency:
            raise ContractError(
                f"mutation idempotency operation family is not exact: {route}"
            )
        if operation.get("security") != security:
            raise ContractError(
                f"operation does not require the private bearer: {route}"
            )
        parameter_values = operation.get("parameters", [])
        if not isinstance(parameter_values, list):
            raise ContractError(f"operation parameters are not a list: {route}")
        actual_parameter_refs: list[str] = []
        for parameter_value in parameter_values:
            if not isinstance(parameter_value, dict) or set(parameter_value) != {
                "$ref"
            }:
                raise ContractError(
                    f"operation parameter is not a canonical component reference: {route}"
                )
            reference = parameter_value["$ref"]
            if not isinstance(reference, str):
                raise ContractError(
                    f"operation parameter reference is malformed: {route}"
                )
            actual_parameter_refs.append(reference)
            parameter = _dereference_local(parameter_value, openapi)
            if not isinstance(parameter, dict) or parameter.get("required") is not True:
                raise ContractError(
                    f"operation has an optional evidence parameter: {route}"
                )
        if tuple(actual_parameter_refs) != EXPECTED_PARAMETER_REFS[operation_id]:
            raise ContractError(f"operation evidence parameters are not exact: {route}")
        request_schema = REQUEST_SCHEMAS.get(operation_id)
        request_body = operation.get("requestBody")
        if request_schema is None:
            if "requestBody" in operation:
                raise ContractError(
                    f"read operation unexpectedly accepts a body: {route}"
                )
        else:
            expected_request_body = {
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{request_schema}"}
                    }
                },
                "required": True,
            }
            if request_body != expected_request_body:
                raise ContractError(f"mutation body is not canonical: {route}")
        responses = operation.get("responses")
        if (
            not isinstance(responses, dict)
            or set(responses) != EXPECTED_RESPONSES[operation_id]
        ):
            raise ContractError(f"operation response status set is not exact: {route}")
        success_status, success_schema = SUCCESS_SCHEMAS[operation_id]
        for status, response_value in responses.items():
            response = _dereference_local(response_value, openapi)
            if not isinstance(response, dict):
                raise ContractError(f"response is malformed: {route} {status}")
            _validate_response_headers(response, unauthorized=status == "401")
            content = response.get("content")
            if not isinstance(content, dict):
                raise ContractError(f"response content is missing: {route} {status}")
            if status == success_status:
                expected_media_type = "application/json"
                expected_schema = _dereference_local(
                    {"$ref": f"#/components/schemas/{success_schema}"}, openapi
                )
            else:
                expected_media_type = "application/problem+json"
                expected_schema = _dereference_local(
                    {"$ref": "#/components/schemas/SafeErrorEnvelope"}, openapi
                )
            if set(content) != {expected_media_type}:
                raise ContractError(
                    f"response media type set is not exact: {route} {status}"
                )
            media = content[expected_media_type]
            if media != {"schema": expected_schema}:
                raise ContractError(
                    f"response media/schema is not canonical: {route} {status}"
                )
    components = openapi.get("components")
    if not isinstance(components, dict):
        raise ContractError("OpenAPI components are missing")
    if openapi.get("x-dreamland-idempotency-policy") != EXPECTED_IDEMPOTENCY_POLICY:
        raise ContractError("OpenAPI idempotency claim policy is not exact")
    if components.get("parameters") != EXPECTED_PARAMETER_COMPONENTS:
        raise ContractError("OpenAPI evidence parameter components are not exact")
    expected_epoch_parameter = components.get("parameters", {}).get("ExpectedEpoch")
    if not isinstance(
        expected_epoch_parameter, dict
    ) or "getProductCapabilities.service_epoch" not in expected_epoch_parameter.get(
        "description", ""
    ):
        raise ContractError(
            "expected epoch instructions must point to capability discovery"
        )
    schemes = components.get("securitySchemes")
    bearer = schemes.get("ProductServiceBearer") if isinstance(schemes, dict) else None
    if (
        not isinstance(bearer, dict)
        or bearer.get("type") != "http"
        or bearer.get("scheme") != "bearer"
    ):
        raise ContractError("private authority must be an HTTP bearer")
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        raise ContractError("OpenAPI schemas are missing")
    for schema_name, expected_schema in EXPECTED_IDENTITY_AUTHORITY_SCHEMAS.items():
        if schemas.get(schema_name) != expected_schema:
            raise ContractError(
                f"identity/authority schema is not canonical: {schema_name}"
            )
    for schema_name, expected_schema in EXPECTED_REQUEST_SCHEMAS.items():
        if schemas.get(schema_name) != expected_schema:
            raise ContractError(f"request schema is not canonical: {schema_name}")
    for schema_name, (
        expected_required,
        expected_additional_properties,
    ) in EXPECTED_RESPONSE_OBJECT_SHAPES.items():
        schema = schemas.get(schema_name)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ContractError(f"response object schema is missing: {schema_name}")
        required = schema.get("required")
        if (
            not isinstance(required, list)
            or len(required) != len(expected_required)
            or frozenset(required) != expected_required
            or schema.get("additionalProperties") is not expected_additional_properties
        ):
            raise ContractError(
                f"response required/additionalProperties shape is not exact: "
                f"{schema_name}"
            )
    for fixture_schema_name in (
        "IntegrationAuthorityFixture",
        "IntegrationSimulatorGrantFixture",
    ):
        fixture_schema = schemas.get(fixture_schema_name, {})
        fixture_properties = fixture_schema.get("properties", {})
        if (
            not isinstance(fixture_properties, dict)
            or "expected_epoch" in fixture_properties
            or fixture_properties.get("expected_epoch_source")
            != {
                "const": "getProductCapabilities.service_epoch",
                "type": "string",
            }
            or "expected_epoch_source" not in fixture_schema.get("required", [])
        ):
            raise ContractError(
                "integration authority fixtures must resolve the live epoch "
                "through capability discovery"
            )
    for name, schema in schemas.items():
        lowered = name.lower()
        if any(fragment in lowered for fragment in FORBIDDEN_SCHEMA_FRAGMENTS):
            raise ContractError(f"Phase 2 schema is forbidden in this preview: {name}")
        if not isinstance(schema, dict):
            raise ContractError(f"schema is not an object: {name}")
        Draft202012Validator.check_schema(_dereference_local(schema, openapi))
        for item in _walk(schema):
            if not isinstance(item, dict):
                continue
            properties = item.get("properties")
            if not isinstance(properties, dict):
                continue
            for property_name in properties:
                lowered_property = property_name.lower()
                if any(
                    fragment in lowered_property
                    for fragment in FORBIDDEN_SCHEMA_FRAGMENTS
                ):
                    raise ContractError(
                        f"Phase 2 field is forbidden in this preview: {property_name}"
                    )
    date_time_schemas = [
        item
        for item in _walk(openapi)
        if isinstance(item, dict) and item.get("format") == "date-time"
    ]
    if len(date_time_schemas) != 6 or any(
        item.get("type") != "string" or item.get("maxLength") != 64
        for item in date_time_schemas
    ):
        raise ContractError(
            "all six lifecycle date-time schemas must be bounded to 64 characters"
        )
    lifecycle_snapshot = schemas.get("LifecycleSnapshot")
    if not isinstance(lifecycle_snapshot, dict):
        raise ContractError("lifecycle snapshot schema is missing")
    properties = lifecycle_snapshot.get("properties")
    if not isinstance(properties, dict):
        raise ContractError("lifecycle snapshot properties are missing")
    if "session_capability" in properties or FORBIDDEN_SNAPSHOT_FIELDS.intersection(
        properties
    ):
        raise ContractError(
            "lifecycle snapshot contains forbidden authority/Phase 2 fields"
        )
    expected_control_schema = {
        "maximum": MAX_CONTROL_REVISION,
        "minimum": 1,
        "type": "integer",
    }
    if properties.get("control_revision") != expected_control_schema:
        raise ContractError("snapshot control revision must be JS-safe and positive")
    close_control_schema = (
        schemas.get("CloseLifecyclePreviewRequest", {})
        .get("properties", {})
        .get("expected_control_revision")
    )
    if close_control_schema != expected_control_schema:
        raise ContractError("close control revision must be JS-safe and positive")
    required = lifecycle_snapshot.get("required")
    if not isinstance(required, list) or "available_controls" not in required:
        raise ContractError("lifecycle snapshot must declare available controls")
    direct_snapshot = schemas.get("LifecyclePreviewSnapshotResponse", {})
    direct_ref = direct_snapshot.get("properties", {}).get("snapshot")
    if direct_ref != {"$ref": "#/components/schemas/ActiveLifecycleSnapshot"}:
        raise ContractError("direct snapshot must require active session authority")
    terminal = schemas.get("CurrentTerminalResponse", {})
    if "session_capability" in terminal.get("properties", {}):
        raise ContractError("terminal current response must not contain authority")
    capability_schema = schemas.get("ProductCapabilitiesResponse")
    if not isinstance(capability_schema, dict):
        raise ContractError("capability response schema is missing")
    capability_projection = capability_schema.get("properties", {}).get(
        "capabilities", {}
    )
    if (
        set(capability_projection.get("properties", {})) != CAPABILITY_SET
        or set(capability_projection.get("required", [])) != CAPABILITY_SET
        or capability_projection.get("additionalProperties") is not False
    ):
        raise ContractError("OpenAPI must declare exactly five capabilities")
    safe_code_schema = (
        schemas.get("SafeError", {}).get("properties", {}).get("code", {})
    )
    if safe_code_schema != {
        "maxLength": 64,
        "pattern": "^[a-z][a-z0-9_]{0,63}$",
        "type": "string",
    }:
        raise ContractError(
            "safe error code must permit bounded forward-compatible values"
        )
    expected_display_schema = {
        "maxLength": 160,
        "minLength": 1,
        "pattern": DISPLAY_SAFE_PATTERN,
        "type": "string",
    }
    display_fields = (
        ("SafeError", "message"),
        ("ClosedLifecycle", "safe_message"),
        ("ExpiredLifecycle", "safe_message"),
    )
    for schema_name, property_name in display_fields:
        actual_display_schema = (
            schemas.get(schema_name, {}).get("properties", {}).get(property_name)
        )
        if actual_display_schema != expected_display_schema:
            raise ContractError(
                f"{schema_name}.{property_name} must exclude display controls"
            )
    for item in _walk(openapi):
        if not isinstance(item, dict) or "$ref" not in item:
            continue
        reference = item["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ContractError(f"OpenAPI reference must remain local: {reference!r}")
        _json_pointer(openapi, reference)
    semantic_digest = hashlib.sha256(
        _canonical_json_text(openapi).encode("utf-8")
    ).hexdigest()
    if semantic_digest != EXPECTED_OPENAPI_SEMANTIC_SHA256:
        raise ContractError(
            "OpenAPI semantic shape differs from the pinned exact release candidate"
        )


def _expected_contract_files() -> set[str]:
    return {
        "manifest.json",
        "manifest.schema.json",
        "openapi.json",
        "fixtures/index.json",
        *FIXTURE_SPECS,
    }


def _validate_exact_files(root: Path) -> None:
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.json")
        if path.is_file()
    }
    expected = _expected_contract_files()
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise ContractError(
            "contract file set is not exact"
            f"; missing={missing or 'none'}; unexpected={unexpected or 'none'}"
        )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if (
        manifest.get("contract_name") != CONTRACT_NAME
        or manifest.get("contract_revision") != CONTRACT_REVISION
        or manifest.get("compatibility") != CONTRACT_COMPATIBILITY
        or manifest.get("freeze_status") != "pending_ui_tracked_doc_acknowledgement"
        or manifest.get("implementation_status") != "integration_preview_only"
    ):
        raise ContractError(
            "manifest lifecycle preview identity/status is not canonical"
        )
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_SET:
        raise ContractError("manifest must declare exactly five capabilities")
    for name, state in capabilities.items():
        if not isinstance(state, dict) or (
            state.get("declared_revision") != CONTRACT_REVISION
            or state.get("enabled") is not False
            or state.get("enabled_revision") is not None
            or state.get("reason") != "incomplete_integration_preview"
        ):
            raise ContractError(f"manifest capability must remain disabled: {name}")


def _validate_manifest_schema_bounds(manifest_schema: dict[str, Any]) -> None:
    date_time_schemas = [
        item
        for item in _walk(manifest_schema)
        if isinstance(item, dict) and item.get("format") == "date-time"
    ]
    if len(date_time_schemas) != 2 or any(
        item.get("type") != "string" or item.get("maxLength") != 64
        for item in date_time_schemas
    ):
        raise ContractError(
            "manifest published_at schemas must be bounded to 64 characters"
        )


def _validate_fixture_index(root: Path, index: dict[str, Any]) -> None:
    if (
        index.get("schema_version") != 1
        or index.get("contract_revision") != CONTRACT_REVISION
    ):
        raise ContractError("fixture index identity is not canonical")
    entries = index.get("fixtures")
    if not isinstance(entries, list) or len(entries) != len(FIXTURE_SPECS):
        raise ContractError("fixture index does not contain the exact fixture set")
    actual_paths: set[str] = set()
    cases: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "case",
            "class",
            "expected",
            "path",
            "schema_target",
            "sha256",
        }:
            raise ContractError("fixture index entry shape is not canonical")
        path = entry["path"]
        if path not in FIXTURE_SPECS or path in actual_paths:
            raise ContractError(f"unknown or duplicate fixture index path: {path!r}")
        actual_paths.add(path)
        case, fixture_class, target, expected = FIXTURE_SPECS[path]
        if entry["case"] != case or case in cases:
            raise ContractError(
                f"fixture case is missing, duplicate, or incorrect: {path}"
            )
        cases.add(case)
        if (
            entry["class"] != fixture_class
            or entry["schema_target"] != target
            or entry["expected"] != expected
        ):
            raise ContractError(f"fixture index metadata differs from policy: {path}")
        digest = entry["sha256"]
        fixture_path = _contract_path(root, path)
        if not isinstance(digest, str) or digest != sha256_file(fixture_path):
            raise ContractError(f"fixture digest mismatch: {path}")
        fixture = load_json(fixture_path)
        schema = _schema_for_target(root, target)
        _validate_fixture_expectation(
            path=fixture_path, fixture=fixture, schema=schema, expected=expected
        )
    if actual_paths != set(FIXTURE_SPECS):
        raise ContractError("fixture index path set is incomplete")
    alpha = load_json(root / "fixtures/canonical-valid/authority/actor-alpha.json")
    bravo = load_json(root / "fixtures/canonical-valid/authority/actor-bravo.json")
    for key in (
        "actor_context",
        "launch_grant",
        "selected_record_id",
        "immutable_revision",
    ):
        if alpha.get(key) == bravo.get(key):
            raise ContractError(f"synthetic authorities are not isolated by {key}")


def validate_contract(root: Path) -> dict[str, Any]:
    root = root.resolve()
    _validate_exact_files(root)
    for relative in sorted(_expected_contract_files()):
        path = _contract_path(root, relative)
        if path.read_text(encoding="utf-8") != _canonical_json_text(load_json(path)):
            raise ContractError(f"contract JSON is not canonical: {relative}")
    openapi = load_json(root / "openapi.json")
    manifest_schema = load_json(root / "manifest.schema.json")
    manifest = load_json(root / "manifest.json")
    fixture_index = load_json(root / "fixtures/index.json")
    if not all(
        isinstance(value, dict)
        for value in (openapi, manifest_schema, manifest, fixture_index)
    ):
        raise ContractError("contract documents must be JSON objects")
    Draft202012Validator.check_schema(manifest_schema)
    _validate_manifest_schema_bounds(manifest_schema)
    manifest_errors = _validation_errors(manifest_schema, manifest)
    if manifest_errors:
        raise ContractError("manifest is invalid: " + "; ".join(manifest_errors))
    _validate_manifest(manifest)
    _validate_openapi(openapi, manifest)
    _validate_fixture_index(root, fixture_index)
    artifacts = manifest.get("artifacts")
    fixture_set = manifest.get("fixture_set")
    if not isinstance(artifacts, dict) or not isinstance(fixture_set, dict):
        raise ContractError("manifest digest sections are missing")
    for digest_name, relative in ARTIFACT_PATHS.items():
        digest = artifacts.get(digest_name)
        if digest != sha256_file(root / relative):
            raise ContractError(f"manifest artifact digest mismatch: {digest_name}")
    if fixture_set.get("path") != "fixtures/index.json":
        raise ContractError("fixture index path is not canonical")
    if fixture_set.get("index_sha256") != sha256_file(root / "fixtures/index.json"):
        raise ContractError("manifest fixture index digest mismatch")
    return {
        "capabilities": len(CAPABILITY_NAMES),
        "contract_revision": CONTRACT_REVISION,
        "fixtures": len(FIXTURE_SPECS),
        "implementation_status": manifest["implementation_status"],
        "publication_status": manifest["publication_status"],
        "routes": len(EXPECTED_OPERATIONS),
    }


def refresh_contract(root: Path) -> dict[str, Any]:
    root = root.resolve()
    _validate_exact_files(root)
    for relative in ("manifest.schema.json", "openapi.json", *sorted(FIXTURE_SPECS)):
        path = _contract_path(root, relative)
        write_json(path, load_json(path))
    entries = []
    for path in sorted(FIXTURE_SPECS):
        case, fixture_class, target, expected = FIXTURE_SPECS[path]
        entries.append(
            {
                "case": case,
                "class": fixture_class,
                "expected": expected,
                "path": path,
                "schema_target": target,
                "sha256": sha256_file(root / path),
            }
        )
    write_json(
        root / "fixtures/index.json",
        {
            "contract_revision": CONTRACT_REVISION,
            "fixtures": entries,
            "schema_version": 1,
        },
    )
    manifest = load_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise ContractError("manifest must be an object")
    artifacts = manifest.get("artifacts")
    fixture_set = manifest.get("fixture_set")
    if not isinstance(artifacts, dict) or not isinstance(fixture_set, dict):
        raise ContractError("manifest digest sections are missing")
    for digest_name, relative in ARTIFACT_PATHS.items():
        artifacts[digest_name] = sha256_file(root / relative)
    fixture_set["index_sha256"] = sha256_file(root / "fixtures/index.json")
    manifest["publication_status"] = "release_candidate"
    manifest["backend_commit"] = None
    manifest["published_at"] = None
    write_json(root / "manifest.json", manifest)
    return validate_contract(root)


def _parse_published_at(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not RFC3339_PATTERN.fullmatch(value)
    ):
        raise ContractError("published-at must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError("published-at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("published-at must include an explicit timezone")
    return value


def _verify_publication_commit(root: Path, backend_commit: str) -> None:
    repository_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if repository_result.returncode != 0:
        raise ContractError("contract publication requires a Git repository")
    repository = Path(repository_result.stdout.decode("utf-8").strip()).resolve()
    if repository != root and repository not in root.parents:
        raise ContractError("contract root is not contained by its Git repository")
    commit_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "cat-file",
            "-e",
            f"{backend_commit}^{{commit}}",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if commit_result.returncode != 0:
        raise ContractError("backend-commit does not name a Git commit")
    ancestor_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            backend_commit,
            "HEAD",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if ancestor_result.returncode != 0:
        raise ContractError("backend-commit is not reachable from the current HEAD")
    for relative in sorted(_expected_contract_files()):
        path = root / relative
        repository_path = path.relative_to(repository).as_posix()
        blob_result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show",
                f"{backend_commit}:{repository_path}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if blob_result.returncode != 0 or blob_result.stdout != path.read_bytes():
            raise ContractError(
                "backend-commit does not contain the exact lifecycle artifact: "
                + relative
            )


def publish_contract(
    root: Path, *, backend_commit: str, published_at: str
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(backend_commit):
        raise ContractError(
            "backend-commit must be exactly 40 lowercase hex characters"
        )
    published_at = _parse_published_at(published_at)
    validate_contract(root)
    _verify_publication_commit(root.resolve(), backend_commit)
    manifest_path = root.resolve() / "manifest.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ContractError("manifest must be an object")
    manifest["publication_status"] = "published"
    manifest["backend_commit"] = backend_commit
    manifest["published_at"] = published_at
    write_json(manifest_path, manifest)
    return validate_contract(root)


def _client_source() -> str:
    return r"""# Generated by tools/product_lifecycle_contract.py; do not edit.
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.2.0-rc.2"
CONTRACT_COMPATIBILITY = "exact-release-candidate"
CAPABILITY_NAMES = (
    "product_session_core",
    "presenter_stage_terminal",
    "public_attendee_sessions",
    "forge_to_stage_handoff",
    "stage_xray",
)
MAX_REQUEST_BODY = 4096
MAX_RESPONSE_BODY = 65536
MAX_CONTROL_REVISION = 9007199254740991
REQUEST_TIMEOUT_SECONDS = 5
EPOCH_PATTERN = re.compile(r"^epoch:[A-Za-z0-9_-]{16,128}$")
ACTOR_PATTERN = re.compile(r"^actor:[A-Za-z0-9_-]{16,128}$")
GRANT_PATTERN = re.compile(r"^launch-grant:[A-Za-z0-9_-]{24,480}$")
CAPABILITY_PATTERN = re.compile(r"^session-cap:[A-Za-z0-9_-]{24,256}$")
REFERENCE_PATTERN = re.compile(r"^session-ref:[A-Za-z0-9_-]{16,128}$")
SESSION_ID_PATTERN = re.compile(r"^session:[A-Za-z0-9_-]{16,128}$")
SELECTED_RECORD_PATTERN = re.compile(r"^selected-record:[A-Za-z0-9_-]{16,128}$")
IMMUTABLE_REVISION_PATTERN = re.compile(r"^world-revision:[A-Za-z0-9_-]{16,128}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
REQUEST_ID_PATTERN = re.compile(r"^request:[A-Za-z0-9_-]{8,128}$")
SAFE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
SAFE_ERRORS = {
    "unauthorized": ("Missing or invalid private service authority.", "none", None),
    "not_found": ("The requested lifecycle preview resource is unavailable.", "none", None),
    "grant_unavailable": ("The launch grant is unavailable.", "none", None),
    "current_session_exists": ("A current lifecycle preview session already exists.", "resume_snapshot", None),
    "idempotency_conflict": ("The idempotency key was already used for another request.", "none", None),
    "control_revision_conflict": ("The lifecycle control revision has changed.", "resume_snapshot", None),
    "session_lost": ("The lifecycle preview session was lost after service restart.", "none", None),
    "outcome_unknown": ("The prior lifecycle mutation outcome is unknown after service restart.", "none", None),
    "invalid_request": ("The private lifecycle request is invalid.", "none", None),
    "invalid_json": ("The request body is not valid JSON.", "none", None),
    "request_too_large": ("The request body exceeds the lifecycle preview limit.", "none", None),
    "unsupported_media_type": ("The request content type is unsupported.", "none", None),
    "expected_epoch_required": ("Expected product service epoch evidence is required.", "none", None),
    "authority_denied": ("The requested lifecycle preview resource is unavailable.", "none", None),
    "content_length_required": ("A Content-Length header is required.", "none", None),
    "capacity_exhausted": ("Lifecycle preview capacity is unavailable.", "after_delay", 1000),
    "internal_error": ("The lifecycle preview request could not be completed.", "none", None),
}
FORBIDDEN_SNAPSHOT_FIELDS = {
    "accepted_state_revision",
    "active_command",
    "command_slot_generation",
    "commands",
    "event_position",
    "events",
    "session_capability",
    "state_effect",
    "transcript",
    "transcript_position",
}


class ProductLifecycleError(RuntimeError):
    def __init__(self, status: int, error: dict[str, Any]) -> None:
        self.status = status
        self.error = error
        super().__init__(f"{error['code']} ({status})")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate response object key")
        value[key] = item
    return value


def _validated_base_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("product base URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("product base URL must not contain credentials")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise ValueError("product base URL must not contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("product base URL must not contain a path")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("plain HTTP product base URL must be loopback")
    return value


def _bounded_header(name: str, value: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\r" in value or "\n" in value:
        raise ValueError(f"{name} must be bounded and single-line")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{name} is malformed")
    return value


def _rfc3339(name: str, value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not RFC3339_PATTERN.fullmatch(value)
    ):
        raise ValueError(f"{name} must be a bounded RFC 3339 timestamp")
    normalized = value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be a bounded RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value


def _bounded_text(name: str, value: Any, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ValueError(f"{name} must be bounded text")
    return value


def _contract_identity(identity: Any) -> None:
    if not isinstance(identity, dict) or set(identity) != {
        "name",
        "revision",
        "compatibility",
    }:
        raise ValueError("missing lifecycle contract identity")
    if (
        identity.get("name"),
        identity.get("revision"),
        identity.get("compatibility"),
    ) != (CONTRACT_NAME, CONTRACT_REVISION, CONTRACT_COMPATIBILITY):
        raise ValueError("incompatible lifecycle contract identity")


def validate_product_capabilities(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("capability response must be an object")
    _contract_identity(payload.get("contract"))
    if payload.get("admission") != {"reason": "integration_preview_only", "state": "closed"}:
        raise ValueError("integration preview admission must remain closed")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != set(CAPABILITY_NAMES):
        raise ValueError("unknown or missing product capability")
    for name in CAPABILITY_NAMES:
        state = capabilities[name]
        if not isinstance(state, dict) or (
            state.get("declared_revision") != CONTRACT_REVISION
            or state.get("enabled") is not False
            or state.get("enabled_revision") is not None
            or state.get("reason") != "incomplete_integration_preview"
        ):
            raise ValueError(f"capability must remain disabled: {name}")
    epoch = payload.get("service_epoch")
    _bounded_header("service epoch", epoch, EPOCH_PATTERN)
    return payload


def validate_lifecycle_snapshot(snapshot: Any, *, require_active: bool = False) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("lifecycle snapshot must be an object")
    forbidden = FORBIDDEN_SNAPSHOT_FIELDS.intersection(snapshot)
    if forbidden:
        raise ValueError("lifecycle snapshot contains unavailable or private fields")
    required = {
        "model",
        "contract",
        "service_epoch",
        "session_id",
        "reference",
        "authority_class",
        "starting_world",
        "launch_context",
        "lifecycle",
        "available_controls",
        "control_revision",
        "created_at",
        "observed_at",
        "expires_at",
        "idle_expires_at",
    }
    if not required.issubset(snapshot):
        raise ValueError("lifecycle snapshot is incomplete")
    if snapshot.get("model") != "product-session-lifecycle-preview":
        raise ValueError("unknown lifecycle snapshot model")
    _contract_identity(snapshot.get("contract"))
    _bounded_header("service epoch", snapshot.get("service_epoch"), EPOCH_PATTERN)
    _bounded_header("session id", snapshot.get("session_id"), SESSION_ID_PATTERN)
    _bounded_header("session reference", snapshot.get("reference"), REFERENCE_PATTERN)
    if snapshot.get("authority_class") not in {"attendee", "presenter"}:
        raise ValueError("unknown lifecycle authority class")
    starting_world = snapshot.get("starting_world")
    if not isinstance(starting_world, dict) or not {
        "selected_record_id",
        "immutable_revision",
    }.issubset(starting_world):
        raise ValueError("lifecycle starting world is incomplete")
    _bounded_header(
        "selected record id",
        starting_world.get("selected_record_id"),
        SELECTED_RECORD_PATTERN,
    )
    _bounded_header(
        "immutable revision",
        starting_world.get("immutable_revision"),
        IMMUTABLE_REVISION_PATTERN,
    )
    launch_context = snapshot.get("launch_context")
    if not isinstance(launch_context, dict) or set(launch_context) != {
        "limits",
        "notice_version",
        "policy_id",
    }:
        raise ValueError("lifecycle launch context is not allowlisted")
    policy_id = launch_context.get("policy_id")
    notice_version = launch_context.get("notice_version")
    if (
        not isinstance(policy_id, str)
        or not re.fullmatch(r"policy:[A-Za-z0-9._:-]{4,120}", policy_id)
        or not isinstance(notice_version, str)
        or not re.fullmatch(r"notice:[A-Za-z0-9._:-]{4,120}", notice_version)
    ):
        raise ValueError("lifecycle policy/notice identity is malformed")
    limits = launch_context.get("limits")
    if not isinstance(limits, dict) or set(limits) != {
        "absolute_ttl_ms",
        "idle_ttl_ms",
    }:
        raise ValueError("lifecycle limits are not allowlisted")
    absolute_ttl = limits.get("absolute_ttl_ms")
    idle_ttl = limits.get("idle_ttl_ms")
    if (
        not isinstance(absolute_ttl, int)
        or isinstance(absolute_ttl, bool)
        or not isinstance(idle_ttl, int)
        or isinstance(idle_ttl, bool)
        or absolute_ttl < 1000
        or absolute_ttl > 86400000
        or idle_ttl < 1000
        or idle_ttl > absolute_ttl
    ):
        raise ValueError("lifecycle limits are invalid")
    lifecycle = snapshot.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise ValueError("lifecycle state is missing")
    state = lifecycle.get("state")
    controls = snapshot.get("available_controls")
    if state == "active":
        if set(lifecycle) != {"state"} or controls != ["snapshot", "close"]:
            raise ValueError("active lifecycle invariant failed")
    elif state in {"closed", "expired"}:
        terminal_required = {"state", "changed_at", "reason_code", "safe_message"}
        if set(lifecycle) != terminal_required or controls != []:
            raise ValueError("terminal lifecycle invariant failed")
        _rfc3339("lifecycle changed_at", lifecycle.get("changed_at"))
        allowed_reasons = (
            {"user_closed", "service_closed"}
            if state == "closed"
            else {"absolute_expired", "idle_expired", "service_window_ended"}
        )
        if lifecycle.get("reason_code") not in allowed_reasons:
            raise ValueError("unknown lifecycle terminal reason")
        _bounded_text("lifecycle safe message", lifecycle.get("safe_message"), maximum=160)
    else:
        raise ValueError("unknown lifecycle state")
    if require_active and state != "active":
        raise ValueError("direct snapshot authority is not valid for terminal state")
    control_revision = snapshot.get("control_revision")
    if (
        isinstance(control_revision, bool)
        or not isinstance(control_revision, int)
        or control_revision < 1
        or control_revision > MAX_CONTROL_REVISION
    ):
        raise ValueError("invalid lifecycle control revision")
    _rfc3339("created_at", snapshot.get("created_at"))
    _rfc3339("observed_at", snapshot.get("observed_at"))
    _rfc3339("expires_at", snapshot.get("expires_at"))
    _rfc3339("idle_expires_at", snapshot.get("idle_expires_at"), nullable=True)
    return snapshot


def normalize_safe_error(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        raise ValueError("private error envelope is malformed")
    error = payload["error"]
    if set(error) != {"code", "message", "retry", "retry_after_ms", "request_id"}:
        raise ValueError("private error contains missing or unknown fields")
    code = error.get("code")
    if not isinstance(code, str) or not SAFE_CODE_PATTERN.fullmatch(code):
        raise ValueError("private error code is malformed")
    _bounded_text("private error message", error.get("message"), maximum=160)
    request_id = _bounded_header(
        "private error request id", error.get("request_id"), REQUEST_ID_PATTERN
    )
    retry = error.get("retry")
    retry_after = error.get("retry_after_ms")
    if retry not in {"none", "same_request", "resume_snapshot", "after_delay"}:
        raise ValueError("private error retry category is malformed")
    if retry == "after_delay":
        if (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int)
            or retry_after < 1
            or retry_after > 60000
        ):
            raise ValueError("after_delay error requires a bounded delay")
    elif retry_after is not None:
        raise ValueError("retry delay is forbidden for this category")
    if code not in SAFE_ERRORS:
        return {
            "code": "unknown_safe_error",
            "message": "The lifecycle preview request could not be completed.",
            "retry": "none",
            "retry_after_ms": None,
            "request_id": request_id,
        }
    message, retry, retry_after = SAFE_ERRORS[code]
    if (
        error.get("message") != message
        or error.get("retry") != retry
        or error.get("retry_after_ms") != retry_after
    ):
        raise ValueError("private error tuple is not canonical")
    return error


class ProductLifecycleClient:
    def __init__(self, base_url: str, bearer: str) -> None:
        self.base_url = _validated_base_url(base_url)
        self.bearer = _bounded_header("private bearer", bearer)
        self._proxy_handler = ProxyHandler({})
        self._opener = build_opener(self._proxy_handler, _NoRedirectHandler())

    @staticmethod
    def _read_bounded(response: Any) -> Any:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise ValueError("invalid response Content-Length") from exc
            if declared < 0 or declared > MAX_RESPONSE_BODY:
                raise ValueError("lifecycle response exceeds the body limit")
        raw = response.read(MAX_RESPONSE_BODY + 1)
        if len(raw) > MAX_RESPONSE_BODY:
            raise ValueError("lifecycle response exceeds the body limit")
        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-standard JSON constant: {value}")
                ),
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("lifecycle response is not valid UTF-8 JSON") from exc

    @staticmethod
    def _validate_response_headers(response: Any, media_type: str) -> None:
        if response.headers.get_content_type() != media_type:
            raise ValueError("unexpected lifecycle response content type")
        if "no-store" not in response.headers.get("Cache-Control", "").lower():
            raise ValueError("lifecycle response must be non-cacheable")
        if response.headers.get("X-Content-Type-Options", "").lower() != "nosniff":
            raise ValueError("lifecycle response must declare nosniff")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        success_status: int,
    ) -> Any:
        request_headers = {
            "Accept": "application/json, application/problem+json",
            "Authorization": "Bearer " + self.bearer,
        }
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_REQUEST_BODY:
                raise ValueError("lifecycle request exceeds the body limit")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
        except HTTPError as error:
            if error.code < 400:
                raise
            self._validate_response_headers(error, "application/problem+json")
            payload = self._read_bounded(error)
            raise ProductLifecycleError(error.code, normalize_safe_error(payload)) from None
        with response:
            if response.status != success_status:
                raise ValueError("unexpected lifecycle response status")
            self._validate_response_headers(response, "application/json")
            return self._read_bounded(response)

    @staticmethod
    def _evidence(expected_epoch: str, actor_context: str) -> dict[str, str]:
        return {
            "X-Dreamland-Expected-Epoch": _bounded_header(
                "expected epoch", expected_epoch, EPOCH_PATTERN
            ),
            "X-Dreamland-Actor-Context": _bounded_header(
                "actor context", actor_context, ACTOR_PATTERN
            ),
        }

    def get_product_capabilities(self) -> dict[str, Any]:
        payload = self._request_json(
            "GET", "/product/v1/capabilities", success_status=200
        )
        return validate_product_capabilities(payload)

    def create_lifecycle_preview(
        self,
        expected_epoch: str,
        actor_context: str,
        launch_grant: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        headers = self._evidence(expected_epoch, actor_context)
        headers["Idempotency-Key"] = _bounded_header(
            "idempotency key", idempotency_key, IDEMPOTENCY_PATTERN
        )
        grant = _bounded_header("launch grant", launch_grant, GRANT_PATTERN)
        payload = self._request_json(
            "POST",
            "/product/v1/sessions",
            headers=headers,
            body={"launch_grant": grant},
            success_status=201,
        )
        if not isinstance(payload, dict) or payload.get("result") != "created":
            raise ValueError("unexpected create result")
        _bounded_header(
            "session capability", payload.get("session_capability"), CAPABILITY_PATTERN
        )
        validate_lifecycle_snapshot(payload.get("snapshot"), require_active=True)
        return payload

    def get_current_lifecycle_preview(
        self, expected_epoch: str, actor_context: str
    ) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            "/product/v1/sessions/current",
            headers=self._evidence(expected_epoch, actor_context),
            success_status=200,
        )
        if not isinstance(payload, dict):
            raise ValueError("unexpected current result")
        result = payload.get("result")
        if result == "none":
            return payload
        if result == "current_active":
            _bounded_header(
                "session capability", payload.get("session_capability"), CAPABILITY_PATTERN
            )
            validate_lifecycle_snapshot(payload.get("snapshot"), require_active=True)
            return payload
        if result == "current_terminal":
            if "session_capability" in payload:
                raise ValueError("terminal current result must not return authority")
            snapshot = validate_lifecycle_snapshot(payload.get("snapshot"))
            if snapshot["lifecycle"]["state"] not in {"closed", "expired"}:
                raise ValueError("terminal current result is not terminal")
            return payload
        raise ValueError("unknown current result")

    def get_lifecycle_preview_snapshot(
        self,
        expected_epoch: str,
        actor_context: str,
        reference: str,
        session_capability: str,
    ) -> dict[str, Any]:
        headers = self._evidence(expected_epoch, actor_context)
        headers["X-Dreamland-Session-Capability"] = _bounded_header(
            "session capability", session_capability, CAPABILITY_PATTERN
        )
        reference = _bounded_header("session reference", reference, REFERENCE_PATTERN)
        payload = self._request_json(
            "GET",
            "/product/v1/sessions/" + quote(reference, safe=""),
            headers=headers,
            success_status=200,
        )
        if not isinstance(payload, dict) or payload.get("result") != "snapshot":
            raise ValueError("unexpected snapshot result")
        validate_lifecycle_snapshot(payload.get("snapshot"), require_active=True)
        return payload

    def close_lifecycle_preview(
        self,
        expected_epoch: str,
        actor_context: str,
        reference: str,
        session_capability: str,
        idempotency_key: str,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_control_revision, bool)
            or not isinstance(expected_control_revision, int)
            or expected_control_revision < 1
            or expected_control_revision > MAX_CONTROL_REVISION
        ):
            raise ValueError("expected control revision is outside the safe range")
        headers = self._evidence(expected_epoch, actor_context)
        headers["X-Dreamland-Session-Capability"] = _bounded_header(
            "session capability", session_capability, CAPABILITY_PATTERN
        )
        headers["Idempotency-Key"] = _bounded_header(
            "idempotency key", idempotency_key, IDEMPOTENCY_PATTERN
        )
        reference = _bounded_header("session reference", reference, REFERENCE_PATTERN)
        payload = self._request_json(
            "POST",
            "/product/v1/sessions/" + quote(reference, safe="") + "/close",
            headers=headers,
            body={"expected_control_revision": expected_control_revision},
            success_status=200,
        )
        if not isinstance(payload, dict) or payload.get("result") != "closed":
            raise ValueError("unexpected close result")
        snapshot = validate_lifecycle_snapshot(payload.get("snapshot"))
        if snapshot["lifecycle"]["state"] != "closed":
            raise ValueError("close response does not contain closed truth")
        return payload
"""


def client_smoke(root: Path, *, output: Path | None = None) -> dict[str, Any]:
    validate_contract(root)
    source = _client_source()
    if output is not None:
        destination = output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        py_compile.compile(str(destination), doraise=True)
        generated_path = str(destination)
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "product_lifecycle_client.py"
            destination.write_text(source, encoding="utf-8")
            py_compile.compile(str(destination), doraise=True)
            specification = importlib.util.spec_from_file_location(
                "generated_product_lifecycle_client", destination
            )
            if specification is None or specification.loader is None:
                raise ContractError("could not load generated lifecycle client")
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
        generated_path = "temporary"
    return {
        "contract_revision": CONTRACT_REVISION,
        "generated_path": generated_path,
        "operations": len(EXPECTED_OPERATIONS),
        "server_side_only": True,
    }


def _json_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the DREAMLAND product lifecycle preview contract."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "refresh", "client-smoke"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "client-smoke":
            command.add_argument("--output", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("root", type=Path)
    publish.add_argument("--backend-commit", required=True)
    publish.add_argument("--published-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            _json_result(validate_contract(arguments.root))
        elif arguments.command == "refresh":
            _json_result(refresh_contract(arguments.root))
        elif arguments.command == "publish":
            _json_result(
                publish_contract(
                    arguments.root,
                    backend_commit=arguments.backend_commit,
                    published_at=arguments.published_at,
                )
            )
        elif arguments.command == "client-smoke":
            _json_result(client_smoke(arguments.root, output=arguments.output))
        else:
            raise ContractError(f"unsupported command: {arguments.command}")
    except ContractError as exc:
        print(f"product lifecycle contract error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
