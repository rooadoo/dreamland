#!/usr/bin/env python3
"""Build the provider-free Phase 9A private attendee security contract kit.

This additive release candidate preserves Product RC4 and adds a private,
server-to-server attendee-context revocation operation plus explicit
replay-before-logical-admission semantics.  Importing this generator creates no
listener, worker, provider, runtime composition, credential, or capability.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_contract() -> ModuleType:
    path = Path(__file__).with_name("product_attendee_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published live attendee contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_LIVE: Any = _load_live_contract()
_ATTENDEE: Any = _LIVE._ATTENDEE
_CORE: Any = _LIVE._CORE
_MODULES = tuple(dict.fromkeys((_LIVE, _ATTENDEE, *_LIVE._MODULES)))

CONTRACT_NAME = _LIVE.CONTRACT_NAME
CONTRACT_REVISION = "0.8.0-rc.1"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _LIVE.CONTRACT_COMPATIBILITY
SECURITY_EXECUTOR = "private-attendee-security-no-provider-v1"
PUBLICATION_IMPLEMENTATION_FILES = frozenset(
    {
        "src/dreamland/product/attendee.py",
        "src/dreamland/product/attendee_security.py",
        "tools/product_attendee_security_contract.py",
        "tools/product_attendee_security_preview.py",
    }
)

ATTENDEE_CONTEXT_REVOCATION_PATH = "/product/v1/attendee/contexts/{gateway_context_id}"
ATTENDEE_CONTEXT_REVOCATION_OPERATION = (
    "delete",
    "revokeAttendeeContext",
)
EXPECTED_OPERATIONS = {
    **_LIVE.EXPECTED_OPERATIONS,
    ATTENDEE_CONTEXT_REVOCATION_PATH: ATTENDEE_CONTEXT_REVOCATION_OPERATION,
}
MUTATION_OPERATIONS = {
    *_LIVE.MUTATION_OPERATIONS,
    "revokeAttendeeContext",
}
SESSION_OPERATIONS = set(_LIVE.SESSION_OPERATIONS)

SECURITY_SAFE_ERROR_CASES = {
    "logical_rate_limited": (
        429,
        "Attendee request admission is temporarily limited.",
        "after_delay",
        1_000,
    ),
}
SAFE_ERROR_CASES = {
    **_LIVE.SAFE_ERROR_CASES,
    **SECURITY_SAFE_ERROR_CASES,
}
ATTENDEE_SAFE_ERROR_CASES = {
    **_LIVE.ATTENDEE_SAFE_ERROR_CASES,
    **SECURITY_SAFE_ERROR_CASES,
}

OWN_CLIENT_SMOKE_VALIDATED_FIXTURES = 2
OWN_CLIENT_SMOKE_REJECTED_MUTATIONS = 6
CLIENT_SMOKE_VALIDATED_FIXTURES = (
    _LIVE.CLIENT_SMOKE_VALIDATED_FIXTURES + OWN_CLIENT_SMOKE_VALIDATED_FIXTURES
)
CLIENT_SMOKE_REJECTED_MUTATIONS = (
    _LIVE.CLIENT_SMOKE_REJECTED_MUTATIONS + OWN_CLIENT_SMOKE_REJECTED_MUTATIONS
)

for module in _MODULES:
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    module.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
    module.MUTATION_OPERATIONS = MUTATION_OPERATIONS
    module.SESSION_OPERATIONS = SESSION_OPERATIONS
    module.SAFE_ERROR_CASES = SAFE_ERROR_CASES
    if hasattr(module, "ATTENDEE_SAFE_ERROR_CASES"):
        module.ATTENDEE_SAFE_ERROR_CASES = ATTENDEE_SAFE_ERROR_CASES
    if hasattr(module, "PREVIEW_EXECUTOR"):
        module.PREVIEW_EXECUTOR = SECURITY_EXECUTOR
    if hasattr(module, "LIVE_EXECUTOR"):
        module.LIVE_EXECUTOR = SECURITY_EXECUTOR

_base_build_openapi = _LIVE.build_openapi
_base_build_manifest_schema = _LIVE.build_manifest_schema
_base_fixture_definitions = _LIVE.fixture_definitions
_base_manifest = _LIVE._base_manifest
_base_generated_client_source = _LIVE.generated_client_source
_base_validate_fixture_semantics = _CORE._validate_fixture_semantics
_base_smoke_import = _CORE._smoke_import
_base_refresh_contract = _LIVE.refresh_contract
_base_client_smoke = _LIVE.client_smoke
_base_publish_contract = _CORE.publish_contract


def build_openapi() -> dict[str, Any]:
    """Return Product RC4 plus private revocation and logical admission."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Attendee Security Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, provider-free Phase 9A contract for a private, "
            "loopback-only attendee security boundary. It preserves Product "
            "RC4 session and recovery operations while adding explicit "
            "attendee-context revocation and replay-safe logical request "
            "admission. All production product capabilities remain disabled."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]["properties"]
    preview["executor"]["const"] = SECURITY_EXECUTOR
    preview["provider_calls"]["const"] = False

    schemas = document["components"]["schemas"]
    schemas.update(_security_schemas())
    document["components"]["parameters"]["GatewayContextId"] = {
        "name": "gateway_context_id",
        "in": "path",
        "required": True,
        "schema": _CORE._ref("GatewayContextId"),
        "description": (
            "Opaque server-side gateway context identifier; never "
            "browser-visible and never authority by itself."
        ),
    }

    operation = _CORE._operation(
        "revokeAttendeeContext",
        "AttendeeContextRevocationResponse",
        (
            "Revoke the exact server-side gateway-to-attendee context binding "
            "without echoing either private identifier."
        ),
        parameters=[
            "GatewayContextId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ],
        idempotency_family="attendee.context.revoke",
    )
    operation["x-dreamland-idempotency"].update(
        {
            "scope": [
                "service_epoch",
                "private_service_principal",
                "operation_family",
                "idempotency_key",
            ],
            "reservation_point": (
                "after_exact_replay_or_changed_request_resolution_and_"
                "current_context_binding_validation_before_revoke"
            ),
            "validation_order": [
                "current_service_epoch",
                "private_bff_authority",
                "path_actor_and_idempotency_syntax",
                "exact_replay_or_changed_request_conflict",
                "current_gateway_actor_binding",
                "replay_capacity_invariant",
                "claim",
                "revoke_binding",
                "record_success_before_response",
            ],
            "semantic_request_evidence": {
                "canonical_body": False,
                "path_parameters": ["gateway_context_id"],
                "included_headers": ["X-Dreamland-Actor-Context"],
            },
            "claimed_deterministic_rejections": [],
            "unclaimed_prebinding_rejections": ["authority_denied"],
            "same_key_same_request": (
                "replay_original_success_even_after_context_revocation"
            ),
            "same_key_changed_request": ("idempotency_conflict_after_successful_claim"),
            "fresh_key_after_revocation": "authority_denied",
            "old_epoch": "outcome_unknown_without_redispatch",
            "automatic_new_key_redispatch": False,
        }
    )
    document["paths"][ATTENDEE_CONTEXT_REVOCATION_PATH] = {
        "delete": operation,
    }

    idempotency = document["x-dreamland-idempotency-policy"]
    idempotency["exact_retry"] = (
        "replay_recorded_logical_outcome_before_current_state_and_logical_"
        "admission_evaluation_without_new_admission_charge"
    )
    idempotency["changed_body"] = "idempotency_conflict_before_logical_admission"
    idempotency["unclaimed_rejections"] = list(
        dict.fromkeys([*idempotency["unclaimed_rejections"], "logical_rate_limited"])
    )

    for path in (
        "/product/v1/attendee/contexts",
        "/product/v1/attendee/launch-selections",
    ):
        mutation = document["paths"][path]["post"]
        policy = mutation["x-dreamland-idempotency"]
        policy["unclaimed_retryable_rejections"] = list(
            dict.fromkeys(
                [
                    *policy.get("unclaimed_retryable_rejections", []),
                    "logical_rate_limited",
                ]
            )
        )
        policy["replay_precedes_logical_admission"] = True
        policy["exact_replay_admission_charge"] = 0

    bootstrap_policy = document["paths"]["/product/v1/attendee/contexts"]["post"][
        "x-dreamland-idempotency"
    ]
    bootstrap_policy["reservation_point"] = (
        "after_exact_replay_or_changed_request_resolution_and_logical_"
        "admission_before_claim_context_lookup_and_capacity"
    )
    bootstrap_order = bootstrap_policy["validation_order"]
    replay_or_claim = bootstrap_order.index("exact_replay_or_claim")
    bootstrap_order[replay_or_claim : replay_or_claim + 1] = [
        "exact_replay_or_changed_request_conflict",
        "logical_request_admission",
        "claim",
    ]
    exchange_policy = document["paths"]["/product/v1/attendee/launch-selections"][
        "post"
    ]["x-dreamland-idempotency"]
    exchange_policy["validation_order"] = [
        "current_service_epoch",
        "private_bff_authority",
        "attendee_actor_authority",
        "idempotency_key_and_canonical_body",
        "exact_replay_or_changed_request_conflict",
        "logical_request_admission",
        "claim",
        "selection_and_notice_lookup",
        "record_result_before_response",
    ]
    exchange_policy["reservation_point"] = (
        "after_exact_replay_or_changed_request_resolution_and_logical_"
        "admission_before_claim_selection_and_notice_lookup"
    )

    charged_operations = [
        "bootstrapAttendeeContext",
        "exchangeAttendeeLaunchSelection",
        "createProductSession",
        "submitProductCommand",
        "resetProductSession",
        "switchProductSession",
    ]
    for path_item in document["paths"].values():
        for candidate in path_item.values():
            if not isinstance(candidate, dict):
                continue
            if candidate.get("operationId") in charged_operations:
                candidate["x-dreamland-logical-admission"] = {
                    "policy": (
                        "#/x-dreamland-private-attendee-security/logical_admission"
                    ),
                    "when_authority_class": "attendee",
                    "charge": "first_seen_logical_request_only",
                    "exact_replay_charge": 0,
                    "changed_request_conflict_charge": 0,
                    "retryable_denial_claimed": False,
                }

    document["x-dreamland-development-live"] = {
        "explicit_start_only": True,
        "loopback_only": True,
        "provider_calls": False,
        "usage_accounting": "provider_free_deterministic",
        "capability_evidence": False,
        "normal_runtime_composition": False,
    }
    attendee = document["x-dreamland-attendee-integration-preview"]
    attendee.update(
        {
            "context_revocation": True,
            "logical_request_admission": True,
            "provider_dispatch": False,
            "normal_runtime_composition": False,
            "capability_evidence": False,
        }
    )
    document["x-dreamland-private-attendee-security"] = {
        "availability": "provider_free_loopback_development_only",
        "transport": "private_server_to_server_only",
        "browser_direct_access": False,
        "browser_credentials_accepted": False,
        "production_capabilities_enabled": False,
        "revocation": {
            "authority": "private_attendee_bff_bearer_plus_exact_binding",
            "gateway_context_id_authorizes": False,
            "actor_context_in_path": False,
            "success_echoes_private_identifiers": False,
            "exact_replay_after_revocation": "replay_success",
            "fresh_request_after_revocation": "uniform_authority_denied",
            "prebinding_authority_denial_claimed": False,
            "successful_claim_capacity": "one_slot_per_burnable_context",
            "already_revoked_outcome": "reserved_for_safe_convergence",
            "revokes_existing_sessions": True,
            "fences_active_commands_output_and_accepted_state": True,
            "expires_issued_grants_and_session_authority": True,
            "revokes_future_actor_context_use": True,
        },
        "logical_admission": {
            "charged_operations": charged_operations,
            "applies_to_shared_session_operations_only_when": (
                "authority_class_is_attendee"
            ),
            "exempt_operations": [
                "revokeAttendeeContext",
                "getAttendeeLaunchOptions",
                "getProductCapabilities",
                "getCurrentProductSession",
                "getProductSessionSnapshot",
                "getProductSessionEvents",
                "getProductSessionTranscript",
                "getProductCommandResult",
                "cancelProductCommand",
                "closeProductSession",
            ],
            "exemption_reason": (
                "reads_safety_reconciliation_and_recovery_must_remain_available"
            ),
            "bootstrap_subject": "opaque_gateway_context_sha256",
            "authenticated_subject": "bound_gateway_context_sha256",
            "network_or_ip_identity_used": False,
            "unit": "first_seen_logical_request",
            "exact_claimed_replay": "bypass_without_consumption",
            "changed_request_reuse": ("idempotency_conflict_before_admission"),
            "rejection": "logical_rate_limited",
            "rejection_claimed": False,
            "retry_after_ms_required": True,
            "public_source_throttling_owner": "ui_bff",
        },
    }
    return document


def _security_schemas() -> dict[str, Any]:
    recovery = _CORE._object(
        {
            "scenario": {
                "type": "string",
                "const": "attendee_context_revocation_response_lost_after_commit",
            },
            "operation_family": {
                "type": "string",
                "const": "attendee.context.revoke",
            },
            "same_key_replay": {"type": "boolean", "const": True},
            "replay_after_revocation": {
                "type": "string",
                "const": "return_original_success",
            },
            "changed_request_outcome": {
                "type": "string",
                "const": "idempotency_conflict",
            },
            "fresh_key_after_revocation": {
                "type": "string",
                "const": "authority_denied",
            },
            "prebinding_authority_denial_claimed": {
                "type": "boolean",
                "const": False,
            },
            "old_epoch_outcome": {
                "type": "string",
                "const": "outcome_unknown",
            },
            "automatic_new_key_redispatch": {
                "type": "boolean",
                "const": False,
            },
        },
        (
            "scenario",
            "operation_family",
            "same_key_replay",
            "replay_after_revocation",
            "changed_request_outcome",
            "fresh_key_after_revocation",
            "prebinding_authority_denial_claimed",
            "old_epoch_outcome",
            "automatic_new_key_redispatch",
        ),
    )
    return {
        "AttendeeContextRevocationResponse": _CORE._response_schema(
            {
                "kind": {
                    "type": "string",
                    "const": "attendee_context_revoked",
                },
                "service_epoch": _CORE._ref("ServiceEpoch"),
                "replayed": {"type": "boolean"},
                "outcome": {
                    "type": "string",
                    "enum": ["revoked", "already_revoked"],
                },
            },
            ("kind", "service_epoch", "replayed", "outcome"),
        ),
        "AttendeeContextRevocationRecoveryFixture": recovery,
    }


def fixture_definitions() -> tuple[Any, ...]:
    fixtures: list[Any] = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        _replace_security_context(payload)
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

    fresh = _success(
        {
            "kind": "attendee_context_revoked",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "outcome": "revoked",
        }
    )
    replayed = deepcopy(fresh)
    replayed["replayed"] = True
    recovery = {
        "scenario": "attendee_context_revocation_response_lost_after_commit",
        "operation_family": "attendee.context.revoke",
        "same_key_replay": True,
        "replay_after_revocation": "return_original_success",
        "changed_request_outcome": "idempotency_conflict",
        "fresh_key_after_revocation": "authority_denied",
        "prebinding_authority_denial_claimed": False,
        "old_epoch_outcome": "outcome_unknown",
        "automatic_new_key_redispatch": False,
    }
    fixtures.extend(
        (
            _CORE.Fixture(
                "attendee_context_revocation_fresh",
                "canonical-valid",
                "fixtures/canonical-valid/attendee/context-revoked.json",
                "openapi.json#/components/schemas/AttendeeContextRevocationResponse",
                fresh,
                "discard_private_actor_context_and_session_authority",
            ),
            _CORE.Fixture(
                "attendee_context_revocation_replayed",
                "canonical-valid",
                "fixtures/canonical-valid/attendee/context-revoked-replayed.json",
                "openapi.json#/components/schemas/AttendeeContextRevocationResponse",
                replayed,
                "confirm_original_revocation_after_lost_response",
            ),
            _CORE.Fixture(
                "attendee_context_revocation_response_lost",
                "canonical-valid",
                "fixtures/canonical-valid/recovery/attendee-context-revocation-response-lost.json",
                "openapi.json#/components/schemas/AttendeeContextRevocationRecoveryFixture",
                recovery,
                "retry_identical_revocation_without_redispatch",
            ),
            _CORE.Fixture(
                "attendee_context_revocation_missing_outcome",
                "invalid",
                "fixtures/invalid/attendee/context-revocation-missing-outcome.json",
                "openapi.json#/components/schemas/AttendeeContextRevocationResponse",
                _without(fresh, "outcome"),
                "reject_fail_closed",
            ),
            _CORE.Fixture(
                "attendee_context_revocation_invalid_replay_truth",
                "invalid",
                "fixtures/invalid/attendee/context-revocation-replay-truth.json",
                "openapi.json#/components/schemas/AttendeeContextRevocationResponse",
                {**replayed, "replayed": 1},
                "reject_fail_closed",
            ),
            _CORE.Fixture(
                "attendee_context_revocation_invalid_gateway_context",
                "invalid",
                "fixtures/invalid/attendee/context-revocation-gateway-context.json",
                "openapi.json#/components/schemas/GatewayContextId",
                "gateway-context:short",
                "reject_before_private_dispatch",
            ),
        )
    )
    return tuple(fixtures)


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
                "executor": SECURITY_EXECUTOR,
                "provider_calls": False,
                "live_capability_evidence": False,
            },
        }
    )
    return value


def _replace_security_context(value: Any) -> None:
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, dict) and {
            "test_only",
            "integration_only",
            "executor",
            "provider_calls",
            "live_capability_evidence",
        }.issubset(preview):
            preview["executor"] = SECURITY_EXECUTOR
            preview["provider_calls"] = False
        for item in value.values():
            _replace_security_context(item)
    elif isinstance(value, list):
        for item in value:
            _replace_security_context(item)


def _without(value: dict[str, Any], key: str) -> dict[str, Any]:
    copied = deepcopy(value)
    del copied[key]
    return copied


def _validate_fixture_semantics(fixture: Any, payload: Any) -> None:
    _base_validate_fixture_semantics(fixture, payload)
    if fixture.classification == "invalid":
        return
    if fixture.target.endswith("AttendeeContextRevocationResponse"):
        forbidden = {"actor_context", "gateway_context_id"}.intersection(payload)
        if forbidden:
            raise _CORE.ContractError(
                "attendee revocation response exposes private identifiers: "
                + ", ".join(sorted(forbidden))
            )


def _base_manifest_document() -> dict[str, Any]:
    manifest = _base_manifest()
    prefix = (
        ".venv/bin/python tools/product_attendee_security_contract.py"
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
                    "tools/product_attendee_live_contract.py",
                    "tools/product_attendee_security_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_attendee_live_contract.py",
                    "tools/product_attendee_security_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _base_generated_client_source(openapi)
    source = source.replace(
        "Generated by tools/product_attendee_live_contract.py",
        "Generated by tools/product_attendee_security_contract.py",
        1,
    )
    source = source.replace('"provider_calls": True', '"provider_calls": False')

    validation_marker = "class ProductSessionPreviewClient:\n"
    validation = r"""

def validate_attendee_context_revocation(
    value: Any,
    *,
    expected_epoch: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "attendee_context_revoked":
        raise ValueError("attendee context revocation response is invalid")
    _context(value)
    if not isinstance(value.get("replayed"), bool):
        raise ValueError("attendee context revocation replay truth is invalid")
    if value.get("outcome") not in {"revoked", "already_revoked"}:
        raise ValueError("attendee context revocation outcome is invalid")
    if {"actor_context", "gateway_context_id"}.intersection(value):
        raise ValueError("private attendee identifier crossed the revocation response")
    if expected_epoch is not None:
        expected = _forge_identifier("expected service epoch", expected_epoch, EPOCH_RE)
        if value.get("service_epoch") != expected:
            raise ValueError("returned attendee context revocation epoch diverged")
    return value


"""
    if source.count(validation_marker) != 1:
        raise _CORE.ContractError(
            "generated revocation validation insertion point is missing"
        )
    source = source.replace(validation_marker, validation + validation_marker, 1)

    method_marker = "    def _control_mutation("
    method = r"""    def revoke_attendee_context(self, *, epoch: str, actor: str, key: str, gateway_context_id: str) -> dict[str, Any]:
        gateway_context_id = _forge_identifier(
            "gateway context ID",
            gateway_context_id,
            GATEWAY_CONTEXT_RE,
        )
        payload = self._request(
            "DELETE",
            "/product/v1/attendee/contexts/" + gateway_context_id,
            self._headers(epoch=epoch, actor=actor, idempotency_key=key),
        )
        return validate_attendee_context_revocation(payload, expected_epoch=epoch)

"""
    if source.count(method_marker) != 1:
        raise _CORE.ContractError(
            "generated revocation client insertion point is missing"
        )
    return source.replace(method_marker, method + method_marker, 1)


def _smoke_import(path: Path, root: Path) -> None:
    _base_smoke_import(path, root)
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_generated_smoke",
        path,
    )
    if specification is None or specification.loader is None:
        raise _CORE.ContractError("cannot import generated security client")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    fresh = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/context-revoked.json"
    )
    replayed = _CORE.load_json(
        root / "fixtures/canonical-valid/attendee/context-revoked-replayed.json"
    )
    module.validate_attendee_context_revocation(fresh)
    module.validate_attendee_context_revocation(replayed)

    rejected_mutations = 0

    def reject(label: str, operation: Any) -> None:
        nonlocal rejected_mutations
        try:
            operation()
        except ValueError:
            rejected_mutations += 1
            return
        raise _CORE.ContractError(
            f"generated security client accepted {label} mutation"
        )

    changed = deepcopy(fresh)
    changed["kind"] = "future_kind"
    reject(
        "revocation kind", lambda: module.validate_attendee_context_revocation(changed)
    )
    changed = deepcopy(fresh)
    changed["replayed"] = 1
    reject(
        "revocation replay",
        lambda: module.validate_attendee_context_revocation(changed),
    )
    changed = deepcopy(fresh)
    changed["outcome"] = "future_outcome"
    reject(
        "revocation outcome",
        lambda: module.validate_attendee_context_revocation(changed),
    )
    changed = deepcopy(fresh)
    changed["actor_context"] = "actor:private_attendee_context_0001"
    reject(
        "revocation private actor",
        lambda: module.validate_attendee_context_revocation(changed),
    )
    reject(
        "revocation epoch",
        lambda: module.validate_attendee_context_revocation(
            fresh,
            expected_epoch="epoch:wrong_security_epoch_0001",
        ),
    )

    class RecordingClient(module.ProductSessionPreviewClient):
        call: tuple[Any, ...] | None = None

        def _request(
            self,
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.call = (method, path, headers, body, query)
            return deepcopy(fresh)

    epoch = fresh["service_epoch"]
    actor = "actor:attendee_security_context_0001"
    key = "attendee-security-revoke-key-0001"
    gateway_context_id = "gateway-context:attendee_security_context_0001"
    client = RecordingClient(
        "http://127.0.0.1",
        "private-attendee-security-contract-smoke-bearer",
    )
    client.revoke_attendee_context(
        epoch=epoch,
        actor=actor,
        key=key,
        gateway_context_id=gateway_context_id,
    )
    if client.call is None:
        raise _CORE.ContractError("generated revocation client did not dispatch")
    method, request_path, headers, body, query = client.call
    if (
        method != "DELETE"
        or request_path != "/product/v1/attendee/contexts/" + gateway_context_id
        or headers.get("X-Dreamland-Expected-Epoch") != epoch
        or headers.get("X-Dreamland-Actor-Context") != actor
        or headers.get("Idempotency-Key") != key
        or body is not None
        or query is not None
    ):
        raise _CORE.ContractError("generated revocation request diverged from contract")

    class NoTransport:
        calls = 0

        def open(self, *_args: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("invalid revocation input reached transport")

    invalid_client = module.ProductSessionPreviewClient(
        "http://127.0.0.1",
        "private-attendee-security-contract-smoke-bearer",
    )
    no_transport = NoTransport()
    invalid_client._opener = no_transport
    reject(
        "invalid revocation gateway context before transport",
        lambda: invalid_client.revoke_attendee_context(
            epoch=epoch,
            actor=actor,
            key=key,
            gateway_context_id="gateway-context:short",
        ),
    )
    if no_transport.calls != 0:
        raise _CORE.ContractError("generated security client dispatched invalid input")
    if rejected_mutations != OWN_CLIENT_SMOKE_REJECTED_MUTATIONS:
        raise _CORE.ContractError(
            f"generated security mutation count drifted: {rejected_mutations}"
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
            raise _CORE.ContractError("published security artifacts are immutable")
    return _base_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _base_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


def _verify_publication_implementation(
    root: Path,
    backend_commit: str,
) -> None:
    repository_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository_result.returncode != 0:
        raise _CORE.ContractError(
            "security contract publication requires a Git repository"
        )
    repository = Path(repository_result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise _CORE.ContractError(
            "security contract root is outside its Git repository"
        ) from error

    head_result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head_result.returncode != 0 or head_result.stdout.strip() != backend_commit:
        raise _CORE.ContractError("backend-commit must be the current HEAD")

    for relative in sorted(PUBLICATION_IMPLEMENTATION_FILES):
        current_path = repository / relative
        blob_result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show",
                f"{backend_commit}:{relative}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if (
            not current_path.is_file()
            or blob_result.returncode != 0
            or blob_result.stdout != current_path.read_bytes()
        ):
            raise _CORE.ContractError(
                "backend-commit does not contain the exact Phase 9A "
                f"implementation: {relative}"
            )


def publish_contract(
    root: Path | str,
    *,
    backend_commit: str,
    published_at: str,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    _verify_publication_implementation(root_path, backend_commit)
    return _base_publish_contract(
        root_path,
        backend_commit=backend_commit,
        published_at=published_at,
    )


for module in _MODULES:
    module.build_openapi = build_openapi
    module.build_manifest_schema = build_manifest_schema
    module.fixture_definitions = fixture_definitions
    module.generated_client_source = generated_client_source
# Preserve the live and attendee wrappers' captured predecessor dispatch.  Their
# manifest builders call through globals named ``_base_manifest``; replacing
# those two globals would recurse back into this wrapper.
for module in _LIVE._MODULES:
    module._base_manifest = _base_manifest_document
_CORE._validate_fixture_semantics = _validate_fixture_semantics
_CORE._smoke_import = _smoke_import
_CORE.refresh_contract = refresh_contract
_CORE.client_smoke = client_smoke
_CORE.publish_contract = publish_contract

build_events_schema = _CORE.build_events_schema
validate_contract = _CORE.validate_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
