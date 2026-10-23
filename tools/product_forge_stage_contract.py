#!/usr/bin/env python3
"""Build the capability-disabled live Forge-to-Stage handoff contract kit.

This Phase 5 contract is additive to the published Phase 3D Presenter Stage
contract.  It pins the adjacent World Forge contract used to authorize an
already-selected revision, while keeping every product capability disabled and
every route private.  Importing this generator starts no runtime, listener,
worker, provider, or background task.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_stage_contract() -> ModuleType:
    path = Path(__file__).with_name("product_stage_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_forge_stage_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("live Presenter Stage contract generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_LIVE: Any = _load_live_stage_contract()
_STAGE: Any = _LIVE._STAGE
_CORE: Any = _LIVE._CORE

CONTRACT_NAME = _LIVE.CONTRACT_NAME
CONTRACT_REVISION = "0.5.0-rc.2"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _LIVE.CONTRACT_COMPATIBILITY
LIVE_EXECUTOR = "development-live-forge-stage-langgraph-v1"
FIXTURE_EXPECTED_CONTROL_REVISION = 1
FIXTURE_INITIAL_EVENT_SEQUENCE = 1
CLIENT_SMOKE_VALIDATED_FIXTURES = 8
CLIENT_SMOKE_REJECTED_MUTATIONS = 11

WORLD_FORGE_CONTRACT_NAME = "dreamland-private-world-forge-api"
WORLD_FORGE_CONTRACT_REVISION = "0.1.0-rc.4"
WORLD_FORGE_MANIFEST_PATH = "contracts/world-forge-api/v0.1.0-rc.4/manifest.json"
WORLD_FORGE_MANIFEST_SHA256 = (
    "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09"
)

FORGE_STAGE_LAUNCH_PATH = "/product/v1/stage/forge-handoffs"
FORGE_STAGE_REPLACE_PATH = "/product/v1/stage/sessions/{reference}/forge-replacement"
FORGE_STAGE_OPERATIONS = {
    FORGE_STAGE_LAUNCH_PATH: ("post", "launchSelectedForgeRevision"),
    FORGE_STAGE_REPLACE_PATH: ("post", "replaceStageFromSelectedForgeRevision"),
}
EXPECTED_OPERATIONS = {**_STAGE.EXPECTED_OPERATIONS, **FORGE_STAGE_OPERATIONS}
MUTATION_OPERATIONS = {
    *_STAGE.MUTATION_OPERATIONS,
    "launchSelectedForgeRevision",
    "replaceStageFromSelectedForgeRevision",
}
SESSION_OPERATIONS = {
    *_STAGE.SESSION_OPERATIONS,
    FORGE_STAGE_REPLACE_PATH,
}

HANDOFF_SAFE_ERROR_CASES = {
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
    "handoff_capacity_exhausted": (
        503,
        "Forge-to-Stage selection capacity is exhausted.",
        "none",
        None,
    ),
    "idempotency_capacity_exhausted": (
        503,
        "Forge-to-Stage replay capacity is exhausted.",
        "none",
        None,
    ),
}
SAFE_ERROR_CASES = {
    **_STAGE.SAFE_ERROR_CASES,
    **HANDOFF_SAFE_ERROR_CASES,
}

for module in (_LIVE, _STAGE, _CORE):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
    module.MUTATION_OPERATIONS = MUTATION_OPERATIONS
    module.SESSION_OPERATIONS = SESSION_OPERATIONS
    module.SAFE_ERROR_CASES = SAFE_ERROR_CASES
_LIVE.LIVE_EXECUTOR = LIVE_EXECUTOR

_live_build_openapi = _LIVE.build_openapi
_live_build_manifest_schema = _LIVE.build_manifest_schema
_live_fixture_definitions = _LIVE.fixture_definitions
_live_base_manifest = _LIVE._base_manifest
_live_generated_client_source = _LIVE.generated_client_source
_core_validate_fixture_semantics = _CORE._validate_fixture_semantics
_core_refresh_contract = _CORE.refresh_contract
_core_client_smoke = _CORE.client_smoke
_core_smoke_import = _CORE._smoke_import


def build_openapi() -> dict[str, Any]:
    """Return Phase 3D unchanged plus the two private handoff mutations."""
    document = _live_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Live Forge-to-Stage Development Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, private, explicitly started Phase 5 development target. "
            "It launches only an authoritative selected World Forge revision "
            "into the Presenter Stage facade. All product capabilities remain "
            "disabled and this contract is not production capability evidence."
        ),
    }
    schemas = document["components"]["schemas"]
    schemas.update(_handoff_schemas())
    _harden_session_snapshot_schema(schemas["SessionSnapshot"])

    # Existing Stage fixtures and consumers remain valid.  A Forge source is
    # present only on snapshots provisioned through the new explicit seam.
    schemas["StageProjection"]["properties"]["source"] = _CORE._ref("ForgeStageSource")
    schemas["SelectedRecordId"] = {
        "oneOf": [
            schemas["SelectedRecordId"],
            _CORE._string(
                r"^forge-selected-record:[A-Za-z0-9_-]{16,128}$",
                maximum=150,
            ),
        ]
    }
    schemas["ImmutableRevisionId"] = {
        "oneOf": [
            schemas["ImmutableRevisionId"],
            _CORE._string(
                r"^world-revision:sha256:[0-9a-f]{64}$",
                minimum=86,
                maximum=86,
            ),
        ]
    }

    document["components"]["parameters"]["ForgeExpectedEpoch"] = {
        "name": "X-Dreamland-Forge-Expected-Epoch",
        "in": "header",
        "required": True,
        "description": (
            "Exact World Forge service epoch that authorized the selected record."
        ),
        "schema": _CORE._ref("ForgeServiceEpoch"),
    }
    document["paths"].update(
        {
            FORGE_STAGE_LAUNCH_PATH: {
                "post": _CORE._operation(
                    "launchSelectedForgeRevision",
                    "ForgeStageHandoffResponse",
                    (
                        "Resolve the presenter's authoritative selected Forge "
                        "record and create or resume its exact private Stage."
                    ),
                    parameters=[
                        "ExpectedEpoch",
                        "ForgeExpectedEpoch",
                        "ActorContext",
                        "IdempotencyKey",
                    ],
                    request_schema="ForgeStageHandoffRequest",
                    created=True,
                    idempotency_family="forge_stage.launch",
                )
            },
            FORGE_STAGE_REPLACE_PATH: {
                "post": _CORE._operation(
                    "replaceStageFromSelectedForgeRevision",
                    "ForgeStageReplacementResponse",
                    (
                        "Provision an authoritative selected Forge "
                        "revision before expected-control-CAS Stage cutover."
                    ),
                    parameters=[
                        "SessionReference",
                        "ExpectedEpoch",
                        "ForgeExpectedEpoch",
                        "ActorContext",
                        "SessionCapability",
                        "IdempotencyKey",
                    ],
                    request_schema="ForgeStageReplacementRequest",
                    idempotency_family="forge_stage.replace",
                )
            },
        }
    )
    for path in FORGE_STAGE_OPERATIONS:
        idempotency = document["paths"][path]["post"]["x-dreamland-idempotency"]
        idempotency["semantic_request_evidence"] = {
            "canonical_body": True,
            "included_headers": ["X-Dreamland-Forge-Expected-Epoch"],
        }
        idempotency["changed_semantic_evidence"] = (
            "idempotency_conflict_requires_fresh_key_after_reconciliation"
        )
    document["x-dreamland-forge-stage-handoff"] = {
        "world_forge_contract": {
            "name": WORLD_FORGE_CONTRACT_NAME,
            "revision": WORLD_FORGE_CONTRACT_REVISION,
            "compatibility": "exact",
            "manifest_sha256": WORLD_FORGE_MANIFEST_SHA256,
        },
        "authoritative_input": "selected_live_world_forge_record",
        "request_carries_authority": False,
        "request_artifact_paths_accepted": False,
        "exact_identity": [
            "selected_record_id",
            "revision_id",
            "immutable_digest",
            "forge_job_id",
            "forge_reference",
        ],
        "ordinary_resume": "same_selected_record_and_compatible_effective_policy",
        "different_selection": "explicit_forge_replacement",
        "xray_included": False,
        "browser_authentication_owner": "ui_bff",
        "normal_runtime_composition": False,
        "capability_evidence": False,
    }
    return document


def _harden_session_snapshot_schema(snapshot: dict[str, Any]) -> None:
    """Bind terminal lifecycle truth to its safe recovery projection in RC2."""

    terminal_lifecycle = {
        "properties": {
            "lifecycle": {
                "properties": {"state": {"enum": ["closed", "expired", "replaced"]}},
                "required": ["state"],
            }
        },
        "required": ["lifecycle"],
    }
    replaced_lifecycle = {
        "properties": {
            "lifecycle": {
                "properties": {"state": {"const": "replaced"}},
                "required": ["state"],
            }
        },
        "required": ["lifecycle"],
    }
    snapshot.setdefault("allOf", []).extend(
        (
            {
                "if": terminal_lifecycle,
                "then": {
                    "properties": {
                        "available_controls": {"maxItems": 0},
                        "command_slot": {
                            "properties": {
                                "active_command_id": {"type": "null"},
                                "state": {"const": "terminal"},
                            },
                            "required": ["state", "active_command_id"],
                        },
                        "recovery": {
                            "properties": {
                                "retain_until_ms": _CORE._integer(),
                                "tombstone": {"const": True},
                            },
                            "required": ["tombstone", "retain_until_ms"],
                        },
                    }
                },
            },
            {
                "if": replaced_lifecycle,
                "then": {
                    "properties": {
                        "lifecycle": {
                            "properties": {"terminal_reason": {"const": "replaced"}},
                            "required": ["terminal_reason"],
                        }
                    }
                },
            },
        )
    )


def _handoff_schemas() -> dict[str, Any]:
    policy_identifier = _CORE._string(
        r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
        maximum=128,
    )
    forge_revision = _CORE._object(
        {
            "revision_id": _CORE._ref("ForgeImmutableRevisionId"),
            "immutable_digest": _CORE._ref("ForgeImmutableDigest"),
        },
        ("revision_id", "immutable_digest"),
    )
    forge_selection = _CORE._object(
        {
            "service_epoch": _CORE._ref("ForgeServiceEpoch"),
            "selected_record_id": _CORE._ref("ForgeSelectedRecordId"),
            "job_id": _CORE._ref("ForgeJobId"),
            "reference": _CORE._ref("ForgeReference"),
            "selected_at": _timestamp(),
            "expires_at": _timestamp(),
            "policy_id": policy_identifier,
            "receipt_correlation_id": _CORE._ref("ForgeReceiptCorrelationId"),
            "revision": _CORE._ref("ForgeSelectedRevision"),
        },
        (
            "service_epoch",
            "selected_record_id",
            "job_id",
            "reference",
            "selected_at",
            "expires_at",
            "policy_id",
            "receipt_correlation_id",
            "revision",
        ),
    )
    source = _CORE._object(
        {
            "kind": {"type": "string", "const": "world_forge_selection"},
            "selected_record_id": _CORE._ref("ForgeSelectedRecordId"),
            "immutable_digest": _CORE._ref("ForgeImmutableDigest"),
            "forge_job_id": _CORE._ref("ForgeJobId"),
            "forge_reference": _CORE._ref("ForgeReference"),
        },
        (
            "kind",
            "selected_record_id",
            "immutable_digest",
            "forge_job_id",
            "forge_reference",
        ),
    )
    forge_stage_projection = {
        "allOf": [
            _CORE._ref("StageProjection"),
            _CORE._object(
                {"source": _CORE._ref("ForgeStageSource")},
                ("source",),
                additive=True,
            ),
        ]
    }
    forge_starting_revision = _CORE._object(
        {
            "revision_id": _CORE._ref("ForgeImmutableRevisionId"),
            "selected_record_id": _CORE._ref("ForgeSelectedRecordId"),
            "sha256": _CORE._string(
                r"^[0-9a-f]{64}$",
                minimum=64,
                maximum=64,
            ),
        },
        ("revision_id", "selected_record_id", "sha256"),
    )
    forge_snapshot = {
        "allOf": [
            _CORE._ref("PresenterStageSnapshot"),
            _CORE._object(
                {
                    "starting_revision": _CORE._ref("ForgeStartingRevision"),
                    "stage": _CORE._ref("ForgeStageProjection"),
                },
                ("starting_revision", "stage"),
                additive=True,
            ),
        ]
    }

    def launch_variant(kind: str) -> dict[str, Any]:
        return _CORE._response_schema(
            {
                "kind": {"type": "string", "const": kind},
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "replayed": {"type": "boolean"},
                "session_authority": _CORE._ref("SessionAuthority"),
                "snapshot": _CORE._ref("ForgePresenterStageSnapshot"),
                "forge_selection": _CORE._ref("ForgeSelectionProjection"),
            },
            (
                "kind",
                "service_epoch",
                "replayed",
                "session_authority",
                "snapshot",
                "forge_selection",
            ),
        )

    replacement = _CORE._response_schema(
        {
            "kind": {"type": "string", "const": "forge_stage_session_replaced"},
            "service_epoch": _CORE._ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "session_authority": _CORE._ref("SessionAuthority"),
            "previous_snapshot": _CORE._ref("ForgePresenterStageSnapshot"),
            "snapshot": _CORE._ref("ForgePresenterStageSnapshot"),
            "forge_selection": _CORE._ref("ForgeSelectionProjection"),
        },
        (
            "kind",
            "service_epoch",
            "replayed",
            "session_authority",
            "previous_snapshot",
            "snapshot",
            "forge_selection",
        ),
    )
    replay_fixture = _CORE._object(
        {
            "scenario": {
                "type": "string",
                "enum": [
                    "forge_stage_launch_response_lost_after_commit",
                    "forge_stage_replacement_response_lost_after_cutover",
                ],
            },
            "operation_family": {
                "type": "string",
                "enum": ["forge_stage.launch", "forge_stage.replace"],
            },
            "same_key_replay": {"type": "boolean", "const": True},
            "identical_body_required": {"type": "boolean", "const": True},
            "changed_body_outcome": {
                "type": "string",
                "const": "idempotency_conflict",
            },
            "automatic_new_key_redispatch": {"type": "boolean", "const": False},
            "expected_logical_outcome": {
                "type": "string",
                "enum": [
                    "forge_stage_session_created",
                    "forge_stage_session_replaced",
                ],
            },
        },
        (
            "scenario",
            "operation_family",
            "same_key_replay",
            "identical_body_required",
            "changed_body_outcome",
            "automatic_new_key_redispatch",
            "expected_logical_outcome",
        ),
    )
    return {
        "ForgeServiceEpoch": _CORE._string(
            r"^forge-epoch:[A-Za-z0-9_-]{16,128}$",
            minimum=28,
            maximum=140,
        ),
        "ForgeReference": _CORE._string(
            r"^forge-reference:[A-Za-z0-9_-]{16,128}$",
            minimum=32,
            maximum=144,
        ),
        "ForgeJobId": _CORE._string(
            r"^forge-job:[A-Za-z0-9_-]{16,128}$",
            minimum=26,
            maximum=138,
        ),
        "ForgeSelectedRecordId": _CORE._string(
            r"^forge-selected-record:[A-Za-z0-9_-]{16,128}$",
            minimum=38,
            maximum=150,
        ),
        "ForgeReceiptCorrelationId": _CORE._string(
            r"^forge-receipt:[A-Za-z0-9_-]{16,128}$",
            maximum=142,
        ),
        "ForgeImmutableDigest": _CORE._string(
            r"^sha256:[0-9a-f]{64}$",
            minimum=71,
            maximum=71,
        ),
        "ForgeImmutableRevisionId": _CORE._string(
            r"^world-revision:sha256:[0-9a-f]{64}$",
            minimum=86,
            maximum=86,
        ),
        "ForgeSelectedRevision": forge_revision,
        "ForgeSelectionProjection": forge_selection,
        "ForgeStageSource": source,
        "ForgeStageProjection": forge_stage_projection,
        "ForgeStartingRevision": forge_starting_revision,
        "ForgePresenterStageSnapshot": forge_snapshot,
        "ForgeStageHandoffRequest": _CORE._object(
            {"forge_reference": _CORE._ref("ForgeReference")},
            ("forge_reference",),
        ),
        "ForgeStageReplacementRequest": _CORE._object(
            {
                "forge_reference": _CORE._ref("ForgeReference"),
                "expected_control_revision": _CORE._integer(minimum=1),
            },
            ("forge_reference", "expected_control_revision"),
        ),
        "ForgeStageCreatedResponse": launch_variant("forge_stage_session_created"),
        "ForgeStageResumedResponse": launch_variant("forge_stage_session_resumed"),
        "ForgeStageHandoffResponse": {
            "oneOf": [
                _CORE._ref("ForgeStageCreatedResponse"),
                _CORE._ref("ForgeStageResumedResponse"),
            ]
        },
        "ForgeStageReplacementResponse": replacement,
        "ForgeStageReplayFixture": replay_fixture,
    }


def _timestamp() -> dict[str, Any]:
    return {
        "type": "string",
        "format": "date-time",
        "minLength": 20,
        "maxLength": 64,
    }


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = list(_live_fixture_definitions())
    selection_a = _forge_selection("a")
    selection_b = _forge_selection("b")
    snapshot_a = _forge_stage_snapshot("a")
    snapshot_b = _forge_stage_snapshot("b")
    created = _success(
        {
            "kind": "forge_stage_session_created",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "session_authority": "session-cap:forge_stage_alpha_capability_000001",
            "snapshot": snapshot_a,
            "forge_selection": selection_a,
        }
    )
    resumed = deepcopy(created)
    resumed["kind"] = "forge_stage_session_resumed"
    replayed = deepcopy(created)
    replayed["replayed"] = True
    previous = _forge_stage_snapshot(
        "a",
        state="replaced",
        control_revision=FIXTURE_EXPECTED_CONTROL_REVISION + 1,
        event_sequence=FIXTURE_INITIAL_EVENT_SEQUENCE + 1,
    )
    replacement = _success(
        {
            "kind": "forge_stage_session_replaced",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "session_authority": "session-cap:forge_stage_bravo_capability_000001",
            "previous_snapshot": previous,
            "snapshot": snapshot_b,
            "forge_selection": selection_b,
        }
    )
    missing_current_source = deepcopy(created)
    del missing_current_source["snapshot"]["stage"]["source"]
    missing_previous_source = deepcopy(replacement)
    del missing_previous_source["previous_snapshot"]["stage"]["source"]
    terminal_active_controls = deepcopy(replacement)
    terminal_active_controls["previous_snapshot"]["available_controls"] = ["submit"]
    terminal_available_slot = deepcopy(replacement)
    terminal_available_slot["previous_snapshot"]["command_slot"]["state"] = "available"
    terminal_occupied_slot = deepcopy(replacement)
    terminal_occupied_slot["previous_snapshot"]["command_slot"].update(
        {
            "state": "occupied",
            "generation": 1,
            "active_command_id": "command:phase5_terminal_invalid_0001",
        }
    )
    terminal_active_recovery = deepcopy(replacement)
    terminal_active_recovery["previous_snapshot"]["recovery"]["tombstone"] = False
    terminal_null_retention = deepcopy(replacement)
    terminal_null_retention["previous_snapshot"]["recovery"]["retain_until_ms"] = None
    terminal_reason_mismatch = deepcopy(replacement)
    terminal_reason_mismatch["previous_snapshot"]["lifecycle"]["terminal_reason"] = (
        "closed"
    )
    additions = (
        _CORE.Fixture(
            "forge_stage_handoff_request",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/handoff-request.json",
            "openapi.json#/components/schemas/ForgeStageHandoffRequest",
            {"forge_reference": selection_a["reference"]},
            "send_non_authorizing_selected_reference",
        ),
        _CORE.Fixture(
            "forge_stage_handoff_created",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/handoff-created.json",
            "openapi.json#/components/schemas/ForgeStageHandoffResponse",
            created,
            "store_private_authority_and_open_exact_stage",
        ),
        _CORE.Fixture(
            "forge_stage_handoff_resumed",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/handoff-resumed.json",
            "openapi.json#/components/schemas/ForgeStageHandoffResponse",
            resumed,
            "resume_exact_selected_stage_without_redispatch",
        ),
        _CORE.Fixture(
            "forge_stage_handoff_exact_retry",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/handoff-exact-retry.json",
            "openapi.json#/components/schemas/ForgeStageHandoffResponse",
            replayed,
            "accept_recorded_logical_outcome",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_request",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/replacement-request.json",
            "openapi.json#/components/schemas/ForgeStageReplacementRequest",
            {
                "forge_reference": selection_b["reference"],
                "expected_control_revision": FIXTURE_EXPECTED_CONTROL_REVISION,
            },
            "send_explicit_forge_replacement",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_success",
            "canonical-valid",
            "fixtures/canonical-valid/forge-stage/replacement-success.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            replacement,
            "rotate_private_authority_after_exact_cutover",
        ),
        _CORE.Fixture(
            "forge_stage_launch_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/forge-stage-launch-response-lost.json",
            "openapi.json#/components/schemas/ForgeStageReplayFixture",
            {
                "scenario": "forge_stage_launch_response_lost_after_commit",
                "operation_family": "forge_stage.launch",
                "same_key_replay": True,
                "identical_body_required": True,
                "changed_body_outcome": "idempotency_conflict",
                "automatic_new_key_redispatch": False,
                "expected_logical_outcome": "forge_stage_session_created",
            },
            "retry_identical_forge_stage_handoff",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/forge-stage-replacement-response-lost.json",
            "openapi.json#/components/schemas/ForgeStageReplayFixture",
            {
                "scenario": "forge_stage_replacement_response_lost_after_cutover",
                "operation_family": "forge_stage.replace",
                "same_key_replay": True,
                "identical_body_required": True,
                "changed_body_outcome": "idempotency_conflict",
                "automatic_new_key_redispatch": False,
                "expected_logical_outcome": "forge_stage_session_replaced",
            },
            "retry_identical_forge_stage_replacement",
        ),
        _CORE.Fixture(
            "forge_stage_request_forbidden_artifact_path",
            "invalid",
            "fixtures/invalid/forge-stage/artifact-path.json",
            "openapi.json#/components/schemas/ForgeStageHandoffRequest",
            {
                "forge_reference": selection_a["reference"],
                "artifact_path": "/tmp/untrusted-world.json",
            },
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_request_forbidden_launch_grant",
            "invalid",
            "fixtures/invalid/forge-stage/launch-grant.json",
            "openapi.json#/components/schemas/ForgeStageHandoffRequest",
            {
                "forge_reference": selection_a["reference"],
                "launch_grant": "launch-grant:browser_supplied_forbidden_000001",
            },
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_handoff_missing_source",
            "invalid",
            "fixtures/invalid/forge-stage/handoff-missing-source.json",
            "openapi.json#/components/schemas/ForgeStageHandoffResponse",
            missing_current_source,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_missing_previous_source",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-missing-previous-source.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            missing_previous_source,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_active_controls",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-active-controls.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_active_controls,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_available_slot",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-available-slot.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_available_slot,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_occupied_slot",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-occupied-slot.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_occupied_slot,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_active_recovery",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-active-recovery.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_active_recovery,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_null_retention",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-null-retention.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_null_retention,
            "reject",
        ),
        _CORE.Fixture(
            "forge_stage_replacement_terminal_reason_mismatch",
            "invalid",
            "fixtures/invalid/forge-stage/replacement-terminal-reason-mismatch.json",
            "openapi.json#/components/schemas/ForgeStageReplacementResponse",
            terminal_reason_mismatch,
            "reject",
        ),
    )
    fixtures.extend(additions)
    return tuple(fixtures)


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
            "executor": LIVE_EXECUTOR,
            "provider_calls": True,
            "live_capability_evidence": False,
        },
    }


def _forge_selection(suffix: str) -> dict[str, Any]:
    digest = suffix * 64
    return {
        "service_epoch": "forge-epoch:phase5_forge_service_0001",
        "selected_record_id": (
            f"forge-selected-record:phase5_selection_{suffix}_00000001"
        ),
        "job_id": f"forge-job:phase5_job_{suffix}_00000001",
        "reference": f"forge-reference:phase5_job_{suffix}_00000001",
        "selected_at": "2026-08-26T12:00:00Z",
        "expires_at": "2026-08-26T12:05:00Z",
        "policy_id": "policy:forge-stage-live-handoff-v1",
        "receipt_correlation_id": (f"forge-receipt:phase5_receipt_{suffix}_00000001"),
        "revision": {
            "revision_id": f"world-revision:sha256:{digest}",
            "immutable_digest": f"sha256:{digest}",
        },
    }


def _forge_stage_snapshot(suffix: str, **kwargs: Any) -> dict[str, Any]:
    selection = _forge_selection(suffix)
    reference = f"session-ref:forge_stage_{suffix}_00000001"
    snapshot = _STAGE._stage_snapshot(
        reference=reference,
        session_id=f"session:forge_stage_{suffix}_00000001",
        **kwargs,
    )
    snapshot["starting_revision"] = {
        "revision_id": selection["revision"]["revision_id"],
        "selected_record_id": selection["selected_record_id"],
        # This is the product-core canonical-document hash. The Forge artifact
        # digest below includes Forge's canonical file newline and is distinct.
        "sha256": ("c" if suffix == "a" else "d") * 64,
    }
    snapshot["stage"] = {
        "reference": reference,
        "display": {
            "title": "Audience-created DREAMLAND world",
            "source_label": "Live World Forge",
            "revision_label": f"World Forge {suffix * 12}",
        },
        "admission_class": "presenter_stage_reserved",
        "xray_available": False,
        "source": {
            "kind": "world_forge_selection",
            "selected_record_id": selection["selected_record_id"],
            "immutable_digest": selection["revision"]["immutable_digest"],
            "forge_job_id": selection["job_id"],
            "forge_reference": selection["reference"],
        },
    }
    return snapshot


def _fixture_forge_snapshot_identity(
    snapshot: Any,
    *,
    case: str,
) -> tuple[Any, Any, Any, Any, Any]:
    if not isinstance(snapshot, dict):
        raise _CORE.ContractError(f"Forge-to-Stage fixture lacks snapshot: {case}")
    stage = snapshot.get("stage")
    source = stage.get("source") if isinstance(stage, dict) else None
    starting = snapshot.get("starting_revision")
    if not isinstance(source, dict) or not isinstance(starting, dict):
        raise _CORE.ContractError(
            f"Forge-to-Stage fixture lacks exact identity: {case}"
        )
    observed = (
        source.get("selected_record_id"),
        starting.get("revision_id"),
        source.get("immutable_digest"),
        source.get("forge_job_id"),
        source.get("forge_reference"),
    )
    if (
        stage.get("reference") != snapshot.get("reference")
        or starting.get("selected_record_id") != source.get("selected_record_id")
        or starting.get("revision_id")
        != "world-revision:" + str(source.get("immutable_digest"))
    ):
        raise _CORE.ContractError(
            f"Forge-to-Stage fixture source projection diverges: {case}"
        )
    return observed


def _validate_terminal_fixture_snapshot(snapshot: Any, *, case: str) -> None:
    if not isinstance(snapshot, dict):
        raise _CORE.ContractError(f"terminal fixture snapshot is missing: {case}")
    lifecycle = snapshot.get("lifecycle")
    slot = snapshot.get("command_slot")
    recovery = snapshot.get("recovery")
    if not all(isinstance(item, dict) for item in (lifecycle, slot, recovery)):
        raise _CORE.ContractError(f"terminal fixture projection is incomplete: {case}")
    assert isinstance(lifecycle, dict)
    assert isinstance(slot, dict)
    assert isinstance(recovery, dict)
    terminal_at_ms = lifecycle.get("terminal_at_ms")
    retain_until_ms = recovery.get("retain_until_ms")
    if (
        lifecycle.get("state") != "replaced"
        or lifecycle.get("terminal_reason") != "replaced"
        or snapshot.get("available_controls") != []
        or slot.get("state") != "terminal"
        or slot.get("active_command_id") is not None
        or recovery.get("tombstone") is not True
        or isinstance(terminal_at_ms, bool)
        or not isinstance(terminal_at_ms, int)
        or isinstance(retain_until_ms, bool)
        or not isinstance(retain_until_ms, int)
        or retain_until_ms <= terminal_at_ms
    ):
        raise _CORE.ContractError(f"terminal fixture snapshot is incoherent: {case}")


def _validate_fixture_semantics(fixture: Any, payload: Any) -> None:
    _core_validate_fixture_semantics(fixture, payload)
    if fixture.case not in {
        "forge_stage_handoff_created",
        "forge_stage_handoff_resumed",
        "forge_stage_handoff_exact_retry",
        "forge_stage_replacement_success",
    }:
        return
    snapshot = payload.get("snapshot")
    selection = payload.get("forge_selection")
    if not isinstance(snapshot, dict) or not isinstance(selection, dict):
        raise _CORE.ContractError(
            f"Forge-to-Stage fixture lacks exact identity: {fixture.case}"
        )
    revision = selection.get("revision")
    if not isinstance(revision, dict):
        raise _CORE.ContractError(
            f"Forge-to-Stage fixture lacks exact identity: {fixture.case}"
        )
    expected = (
        selection.get("selected_record_id"),
        revision.get("revision_id"),
        revision.get("immutable_digest"),
        selection.get("job_id"),
        selection.get("reference"),
    )
    observed = _fixture_forge_snapshot_identity(snapshot, case=fixture.case)
    if observed != expected:
        raise _CORE.ContractError(
            f"Forge-to-Stage fixture identity diverges: {fixture.case}"
        )
    lifecycle = snapshot.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("state") != "active":
        raise _CORE.ContractError(
            f"Forge-to-Stage replacement is not active: {fixture.case}"
        )
    if fixture.case != "forge_stage_replacement_success":
        return
    previous = payload.get("previous_snapshot")
    _validate_terminal_fixture_snapshot(previous, case=fixture.case)
    old_selection = _forge_selection("a")
    old_revision = old_selection["revision"]
    expected_previous = (
        old_selection["selected_record_id"],
        old_revision["revision_id"],
        old_revision["immutable_digest"],
        old_selection["job_id"],
        old_selection["reference"],
    )
    observed_previous = _fixture_forge_snapshot_identity(
        previous,
        case=fixture.case,
    )
    if observed_previous != expected_previous:
        raise _CORE.ContractError(
            f"Forge-to-Stage previous identity diverges: {fixture.case}"
        )
    assert isinstance(previous, dict)
    coordinates = previous.get("coordinates")
    event = coordinates.get("event") if isinstance(coordinates, dict) else None
    if (
        not isinstance(coordinates, dict)
        or coordinates.get("control_revision") != FIXTURE_EXPECTED_CONTROL_REVISION + 1
        or not isinstance(event, dict)
        or event.get("sequence") != FIXTURE_INITIAL_EVENT_SEQUENCE + 1
        or previous.get("session_id") == snapshot.get("session_id")
        or previous.get("reference") == snapshot.get("reference")
    ):
        raise _CORE.ContractError(
            f"Forge-to-Stage replacement transition diverges: {fixture.case}"
        )


def _base_manifest() -> dict[str, Any]:
    manifest = _live_base_manifest()
    prefix = (
        ".venv/bin/python tools/product_forge_stage_contract.py"
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
    manifest["dependencies"] = {
        "world_forge": {
            "contract_name": WORLD_FORGE_CONTRACT_NAME,
            "contract_revision": WORLD_FORGE_CONTRACT_REVISION,
            "manifest_path": WORLD_FORGE_MANIFEST_PATH,
            "manifest_sha256": WORLD_FORGE_MANIFEST_SHA256,
            "compatibility": "exact",
        }
    }
    return manifest


def build_manifest_schema() -> dict[str, Any]:
    schema = _live_build_manifest_schema()
    dependency = _CORE._object(
        {
            "contract_name": {
                "type": "string",
                "const": WORLD_FORGE_CONTRACT_NAME,
            },
            "contract_revision": {
                "type": "string",
                "const": WORLD_FORGE_CONTRACT_REVISION,
            },
            "manifest_path": {
                "type": "string",
                "const": WORLD_FORGE_MANIFEST_PATH,
            },
            "manifest_sha256": {
                "type": "string",
                "const": WORLD_FORGE_MANIFEST_SHA256,
            },
            "compatibility": {"type": "string", "const": "exact"},
        },
        (
            "contract_name",
            "contract_revision",
            "manifest_path",
            "manifest_sha256",
            "compatibility",
        ),
    )
    schema["properties"]["dependencies"] = _CORE._object(
        {"world_forge": dependency},
        ("world_forge",),
    )
    schema["required"].append("dependencies")
    _replace_tool_path(schema)
    return schema


def _replace_tool_path(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_stage_live_contract.py",
                    "tools/product_forge_stage_contract.py",
                )
            else:
                _replace_tool_path(item)
    elif isinstance(value, list):
        for item in value:
            _replace_tool_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _live_generated_client_source(openapi)
    source = source.replace(
        "Generated by tools/product_stage_live_contract.py",
        "Generated by tools/product_forge_stage_contract.py",
        1,
    )
    source = source.replace(
        "import json\nimport re\n",
        "import json\nimport re\nfrom datetime import datetime\n",
        1,
    )
    regex_marker = 'EPOCH_RE = re.compile(r"^epoch:[A-Za-z0-9_-]{16,128}$")\n'
    regex_insertion = regex_marker + (
        'FORGE_EPOCH_RE = re.compile(r"^forge-epoch:[A-Za-z0-9_-]{16,128}$")\n'
        'FORGE_REFERENCE_RE = re.compile(r"^forge-reference:[A-Za-z0-9_-]{16,128}$")\n'
        'FORGE_JOB_RE = re.compile(r"^forge-job:[A-Za-z0-9_-]{16,128}$")\n'
        'FORGE_SELECTED_RECORD_RE = re.compile(r"^forge-selected-record:[A-Za-z0-9_-]{16,128}$")\n'
        'FORGE_RECEIPT_RE = re.compile(r"^forge-receipt:[A-Za-z0-9_-]{16,128}$")\n'
        'FORGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")\n'
        'FORGE_REVISION_RE = re.compile(r"^world-revision:sha256:[0-9a-f]{64}$")\n'
        'FORGE_POLICY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")\n'
        'SHA256_RE = re.compile(r"^[0-9a-f]{64}$")\n'
    )
    if regex_marker not in source:
        raise _CORE.ContractError(
            "generated client regex insertion point is unavailable"
        )
    source = source.replace(regex_marker, regex_insertion, 1)
    validation_marker = "class ProductSessionPreviewClient:\n"
    validation = r"""
def _forge_text(name: str, value: Any, *, minimum: int, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise ValueError(f"{name} must be bounded text")
    return value


def _forge_timestamp(name: str, value: Any) -> datetime:
    value = _forge_text(name, value, minimum=20, maximum=64)
    if "T" not in value:
        raise ValueError(f"{name} must be an offset-aware timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{name} must be an offset-aware timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be an offset-aware timestamp")
    return parsed


def _forge_identifier(name: str, value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is malformed")
    return value


_base_validate_snapshot = validate_snapshot
_TERMINAL_LIFECYCLE_STATES = frozenset({"closed", "expired", "replaced"})


def _snapshot_integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} is invalid")
    return value


def validate_snapshot(value: Any) -> dict[str, Any]:
    _base_validate_snapshot(value)
    lifecycle = value.get("lifecycle")
    slot = value.get("command_slot")
    recovery = value.get("recovery")
    if not all(isinstance(item, dict) for item in (lifecycle, slot, recovery)):
        raise ValueError("product snapshot lifecycle projection is incomplete")
    state = lifecycle.get("state")
    if state == "active":
        return value
    if state not in _TERMINAL_LIFECYCLE_STATES:
        raise ValueError("product snapshot lifecycle state is invalid")
    terminal_at_ms = _snapshot_integer(
        "terminal lifecycle time", lifecycle.get("terminal_at_ms")
    )
    retain_until_ms = _snapshot_integer(
        "terminal retention time", recovery.get("retain_until_ms")
    )
    if (
        value.get("available_controls") != []
        or slot.get("state") != "terminal"
        or slot.get("active_command_id") is not None
        or recovery.get("tombstone") is not True
        or retain_until_ms <= terminal_at_ms
    ):
        raise ValueError("terminal product snapshot is incoherent")
    if state == "replaced" and lifecycle.get("terminal_reason") != "replaced":
        raise ValueError("replaced product snapshot reason is inconsistent")
    return value


def _forge_snapshot_event_sequence(value: dict[str, Any]) -> int:
    coordinates = value.get("coordinates")
    event = coordinates.get("event") if isinstance(coordinates, dict) else None
    if not isinstance(event, dict):
        raise ValueError("Forge Stage event position is missing")
    sequence = _snapshot_integer(
        "Forge Stage event sequence", event.get("sequence"), minimum=1
    )
    _forge_identifier("Forge Stage event stream", event.get("stream_id"), STREAM_ID_RE)
    event_id = _forge_identifier(
        "Forge Stage event ID", event.get("event_id"), EVENT_ID_RE
    )
    if not event_id.endswith(f":{sequence}"):
        raise ValueError("Forge Stage event position diverged")
    return sequence


def _validate_forge_stage_snapshot(value: Any) -> tuple[str, str, str, str, str]:
    if not isinstance(value, dict):
        raise ValueError("Forge Stage snapshot must be an object")
    validate_snapshot(value)
    reference = _forge_identifier(
        "Forge Stage reference", value.get("reference"), REFERENCE_RE
    )
    stage = value.get("stage")
    starting = value.get("starting_revision")
    if not isinstance(stage, dict) or not isinstance(starting, dict):
        raise ValueError("Forge Stage snapshot lacks selected identity")
    source = stage.get("source")
    if not isinstance(source, dict) or set(source) != {
        "kind",
        "selected_record_id",
        "immutable_digest",
        "forge_job_id",
        "forge_reference",
    }:
        raise ValueError("Forge Stage snapshot lacks exact source identity")
    if source.get("kind") != "world_forge_selection":
        raise ValueError("Forge Stage source kind is invalid")
    selected_record_id = _forge_identifier(
        "Forge selected record ID",
        source.get("selected_record_id"),
        FORGE_SELECTED_RECORD_RE,
    )
    immutable_digest = _forge_identifier(
        "Forge immutable digest", source.get("immutable_digest"), FORGE_DIGEST_RE
    )
    forge_job_id = _forge_identifier(
        "Forge job ID", source.get("forge_job_id"), FORGE_JOB_RE
    )
    forge_reference = _forge_identifier(
        "Forge reference", source.get("forge_reference"), FORGE_REFERENCE_RE
    )
    if set(starting) != {"revision_id", "selected_record_id", "sha256"}:
        raise ValueError("Forge Stage starting revision is incomplete")
    revision_id = _forge_identifier(
        "Forge world revision ID", starting.get("revision_id"), FORGE_REVISION_RE
    )
    starting_selected_record_id = _forge_identifier(
        "Forge starting selected record ID",
        starting.get("selected_record_id"),
        FORGE_SELECTED_RECORD_RE,
    )
    runtime_document_digest = _forge_identifier(
        "Forge runtime document digest", starting.get("sha256"), SHA256_RE
    )
    if (
        stage.get("reference") != reference
        or starting_selected_record_id != selected_record_id
        or revision_id != "world-revision:" + immutable_digest
    ):
        raise ValueError("Forge Stage source projection diverged")
    if runtime_document_digest == immutable_digest.removeprefix("sha256:"):
        raise ValueError("Forge artifact and runtime document hash domains are conflated")
    _forge_snapshot_event_sequence(value)
    return (
        selected_record_id,
        revision_id,
        immutable_digest,
        forge_job_id,
        forge_reference,
    )


def validate_forge_stage_handoff(
    value: Any,
    *,
    expected_control_revision: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Forge-to-Stage response must be an object")
    _context(value)
    kind = value.get("kind")
    if kind not in {
        "forge_stage_session_created",
        "forge_stage_session_resumed",
        "forge_stage_session_replaced",
    }:
        raise ValueError("Forge-to-Stage response kind is invalid")
    required = {
        "kind",
        "service_epoch",
        "replayed",
        "session_authority",
        "snapshot",
        "forge_selection",
        "contract",
        "preview",
    }
    if kind == "forge_stage_session_replaced":
        required.add("previous_snapshot")
    if set(value) != required:
        raise ValueError("Forge-to-Stage response envelope is incomplete")
    _forge_identifier("product service epoch", value.get("service_epoch"), EPOCH_RE)
    _forge_identifier(
        "Forge Stage session authority", value.get("session_authority"), SESSION_CAP_RE
    )
    if not isinstance(value.get("replayed"), bool):
        raise ValueError("Forge-to-Stage replay truth is invalid")
    selection = value.get("forge_selection")
    if not isinstance(selection, dict) or set(selection) != {
        "service_epoch",
        "selected_record_id",
        "job_id",
        "reference",
        "selected_at",
        "expires_at",
        "policy_id",
        "receipt_correlation_id",
        "revision",
    }:
        raise ValueError("Forge-to-Stage response lacks selected identity")
    _forge_identifier(
        "Forge service epoch", selection.get("service_epoch"), FORGE_EPOCH_RE
    )
    selected_record_id = _forge_identifier(
        "Forge selected record ID",
        selection.get("selected_record_id"),
        FORGE_SELECTED_RECORD_RE,
    )
    forge_job_id = _forge_identifier(
        "Forge job ID", selection.get("job_id"), FORGE_JOB_RE
    )
    forge_reference = _forge_identifier(
        "Forge reference", selection.get("reference"), FORGE_REFERENCE_RE
    )
    _forge_identifier(
        "Forge receipt correlation ID",
        selection.get("receipt_correlation_id"),
        FORGE_RECEIPT_RE,
    )
    selected_at = _forge_timestamp("Forge selected_at", selection.get("selected_at"))
    expires_at = _forge_timestamp("Forge expires_at", selection.get("expires_at"))
    if expires_at <= selected_at:
        raise ValueError("Forge selection expiry must follow selection time")
    _forge_identifier("Forge policy ID", selection.get("policy_id"), FORGE_POLICY_RE)
    revision = selection.get("revision")
    if not isinstance(revision, dict) or set(revision) != {
        "revision_id",
        "immutable_digest",
    }:
        raise ValueError("Forge-to-Stage selected revision is incomplete")
    revision_id = _forge_identifier(
        "Forge world revision ID", revision.get("revision_id"), FORGE_REVISION_RE
    )
    immutable_digest = _forge_identifier(
        "Forge immutable digest", revision.get("immutable_digest"), FORGE_DIGEST_RE
    )
    if revision_id != "world-revision:" + immutable_digest:
        raise ValueError("Forge selected revision identity diverged")
    expected = (
        selected_record_id,
        revision_id,
        immutable_digest,
        forge_job_id,
        forge_reference,
    )
    observed = _validate_forge_stage_snapshot(value.get("snapshot"))
    if observed != expected:
        raise ValueError("Forge-to-Stage selected identity diverged")
    snapshot = value["snapshot"]
    if snapshot.get("lifecycle", {}).get("state") != "active":
        raise ValueError("Forge-to-Stage current snapshot is not active")
    if kind == "forge_stage_session_replaced":
        if (
            isinstance(expected_control_revision, bool)
            or not isinstance(expected_control_revision, int)
            or expected_control_revision < 1
        ):
            raise ValueError(
                "Forge-to-Stage replacement validation requires request CAS"
            )
        previous = value.get("previous_snapshot")
        _validate_forge_stage_snapshot(previous)
        if previous.get("lifecycle", {}).get("state") != "replaced":
            raise ValueError("Forge-to-Stage previous snapshot is not replaced")
        coordinates = previous.get("coordinates")
        if (
            not isinstance(coordinates, dict)
            or coordinates.get("control_revision")
            != expected_control_revision + 1
        ):
            raise ValueError("Forge-to-Stage replacement control CAS diverged")
        if _forge_snapshot_event_sequence(previous) < 2:
            raise ValueError("Forge-to-Stage replacement omitted terminal event position")
        if (
            previous.get("session_id") == snapshot.get("session_id")
            or previous.get("reference") == snapshot.get("reference")
        ):
            raise ValueError("Forge-to-Stage replacement identity did not rotate")
    return value


"""
    if validation_marker not in source:
        raise _CORE.ContractError(
            "generated client validation insertion point is unavailable"
        )
    source = source.replace(validation_marker, validation + validation_marker, 1)
    methods = r"""
    def launch_selected_forge_revision(self, *, epoch: str, forge_epoch: str, actor: str, key: str, forge_reference: str) -> dict[str, Any]:
        forge_epoch = _bounded_header("Forge expected epoch", forge_epoch, FORGE_EPOCH_RE)
        forge_reference = _bounded_header("Forge reference", forge_reference, FORGE_REFERENCE_RE)
        headers = self._headers(epoch=epoch, actor=actor, idempotency_key=key)
        headers["X-Dreamland-Forge-Expected-Epoch"] = forge_epoch
        payload = self._request("POST", "/product/v1/stage/forge-handoffs", headers, {"forge_reference": forge_reference})
        return validate_forge_stage_handoff(payload)

    def replace_stage_from_selected_forge_revision(self, *, epoch: str, forge_epoch: str, actor: str, capability: str, reference: str, key: str, expected_control_revision: int, forge_reference: str) -> dict[str, Any]:
        reference = _bounded_header("session reference", reference, REFERENCE_RE)
        forge_epoch = _bounded_header("Forge expected epoch", forge_epoch, FORGE_EPOCH_RE)
        forge_reference = _bounded_header("Forge reference", forge_reference, FORGE_REFERENCE_RE)
        path = "/product/v1/stage/sessions/" + quote(reference, safe="") + "/forge-replacement"
        headers = self._headers(epoch=epoch, actor=actor, session_capability=capability, idempotency_key=key)
        headers["X-Dreamland-Forge-Expected-Epoch"] = forge_epoch
        body = {"expected_control_revision": expected_control_revision, "forge_reference": forge_reference}
        payload = self._request("POST", path, headers, body)
        return validate_forge_stage_handoff(
            payload,
            expected_control_revision=expected_control_revision,
        )

"""
    marker = "    def _control_mutation("
    if marker not in source:
        raise _CORE.ContractError(
            "generated client method insertion point is unavailable"
        )
    return source.replace(marker, methods + marker, 1)


def _smoke_import(path: Path, root: Path) -> None:
    _core_smoke_import(path, root)
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_forge_stage_generated_smoke",
        path,
    )
    if specification is None or specification.loader is None:
        raise _CORE.ContractError("cannot import generated Forge-to-Stage client")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    created = _CORE.load_json(
        root / "fixtures/canonical-valid/forge-stage/handoff-created.json"
    )
    replacement = _CORE.load_json(
        root / "fixtures/canonical-valid/forge-stage/replacement-success.json"
    )
    module.validate_forge_stage_handoff(created)
    module.validate_forge_stage_handoff(
        replacement,
        expected_control_revision=FIXTURE_EXPECTED_CONTROL_REVISION,
    )

    mutations: list[tuple[str, dict[str, Any]]] = []

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["available_controls"] = ["submit"]
    mutations.append(("active controls", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["command_slot"]["state"] = "available"
    mutations.append(("available command slot", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["command_slot"].update(
        {
            "state": "occupied",
            "generation": 1,
            "active_command_id": "command:phase5_terminal_invalid_0001",
        }
    )
    mutations.append(("occupied command slot", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["recovery"]["tombstone"] = False
    mutations.append(("active recovery", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["recovery"]["retain_until_ms"] = None
    mutations.append(("null terminal retention", changed))

    changed = deepcopy(replacement)
    terminal_at_ms = changed["previous_snapshot"]["lifecycle"]["terminal_at_ms"]
    changed["previous_snapshot"]["recovery"]["retain_until_ms"] = terminal_at_ms
    mutations.append(("unbounded terminal retention", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["lifecycle"]["terminal_reason"] = "closed"
    mutations.append(("terminal reason", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["coordinates"]["control_revision"] = (
        FIXTURE_EXPECTED_CONTROL_REVISION
    )
    mutations.append(("replacement control CAS", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["coordinates"]["event"].update(
        {
            "event_id": "event:phase2alphaevent:1",
            "sequence": FIXTURE_INITIAL_EVENT_SEQUENCE,
        }
    )
    mutations.append(("terminal event position", changed))

    changed = deepcopy(replacement)
    changed["previous_snapshot"]["stage"]["source"]["immutable_digest"] = (
        "sha256:" + "f" * 64
    )
    mutations.append(("previous Forge source", changed))

    changed = deepcopy(replacement)
    changed["snapshot"]["stage"]["source"]["immutable_digest"] = "sha256:" + "f" * 64
    mutations.append(("replacement Forge source", changed))

    if len(mutations) != CLIENT_SMOKE_REJECTED_MUTATIONS:
        raise _CORE.ContractError("Forge-to-Stage client-smoke mutation count drifted")
    for label, mutation in mutations:
        try:
            module.validate_forge_stage_handoff(
                mutation,
                expected_control_revision=FIXTURE_EXPECTED_CONTROL_REVISION,
            )
        except ValueError:
            continue
        raise _CORE.ContractError(
            f"generated Forge-to-Stage client accepted {label} mutation"
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
                "published Forge-to-Stage contract artifacts are immutable"
            )
    return _core_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _core_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


_LIVE.build_openapi = build_openapi
_LIVE.build_manifest_schema = build_manifest_schema
_LIVE.fixture_definitions = fixture_definitions
_LIVE._base_manifest = _base_manifest
_LIVE.generated_client_source = generated_client_source

_STAGE.build_openapi = build_openapi
_STAGE.build_manifest_schema = build_manifest_schema
_STAGE.fixture_definitions = fixture_definitions
_STAGE._base_manifest = _base_manifest
_STAGE.generated_client_source = generated_client_source

_CORE.build_openapi = build_openapi
_CORE.build_manifest_schema = build_manifest_schema
_CORE.fixture_definitions = fixture_definitions
_CORE._base_manifest = _base_manifest
_CORE.generated_client_source = generated_client_source
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
