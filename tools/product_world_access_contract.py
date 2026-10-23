#!/usr/bin/env python3
"""Build the additive provider-free Phase 8A World Access contract kit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CONTRACT_NAME = "dreamland-private-world-access-api"
CONTRACT_REVISION = "0.1.0-rc.3"
CONTRACT_COMPATIBILITY = "unfrozen-integration-preview"
PRODUCT_DEPENDENCY_REVISION = "0.7.0-rc.3"
PRODUCT_DEPENDENCY_MANIFEST_SHA256 = (
    "a6952883e528f4a9272988ebdfc54853bbf2e925f3fa1d75d925636b1c7152ec"
)
FORGE_DEPENDENCY_REVISION = "0.1.0-rc.4"
FORGE_DEPENDENCY_MANIFEST_SHA256 = (
    "0824a9d7f8ea1b1ed264a5f6cd76dd6831054b44932973278b60db1c384baa09"
)
EXPECTED_PATHS = {
    "/world-access/v1/identity",
    "/world-access/v1/promotions",
    "/world-access/v1/promotions/{promotion_id}",
    "/world-access/v1/promotions/{promotion_id}/reveal",
    "/world-access/v1/catalog",
    "/world-access/v1/launch-selections",
}
EXPECTED_OPERATIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("get", "/world-access/v1/identity"): {
        "operation_id": "getWorldAccessIdentity",
        "actor_class": "none",
        "operation_parameters": ("RequestId",),
        "path_parameter": False,
        "request_schema": None,
        "success_status": "200",
        "response_schema": "IdentityResponse",
    },
    ("post", "/world-access/v1/promotions"): {
        "operation_id": "promoteSelectedForgeWorld",
        "actor_class": "operator",
        "operation_parameters": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "path_parameter": False,
        "request_schema": "PromotionRequest",
        "success_status": "201",
        "response_schema": "PromotionResponse",
    },
    ("get", "/world-access/v1/promotions/{promotion_id}"): {
        "operation_id": "getWorldAccessPromotion",
        "actor_class": "operator",
        "operation_parameters": ("RequestId", "ExpectedEpoch", "ActorContext"),
        "path_parameter": True,
        "request_schema": None,
        "success_status": "200",
        "response_schema": "PromotionResponse",
    },
    ("post", "/world-access/v1/promotions/{promotion_id}/reveal"): {
        "operation_id": "revealWorldAccessPromotion",
        "actor_class": "operator",
        "operation_parameters": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "path_parameter": True,
        "request_schema": "RevealRequest",
        "success_status": "200",
        "response_schema": "PromotionResponse",
    },
    ("get", "/world-access/v1/catalog"): {
        "operation_id": "getWorldAccessCatalog",
        "actor_class": "attendee",
        "operation_parameters": ("RequestId", "ExpectedEpoch", "ActorContext"),
        "path_parameter": False,
        "request_schema": None,
        "success_status": "200",
        "response_schema": "CatalogResponse",
    },
    ("post", "/world-access/v1/launch-selections"): {
        "operation_id": "exchangeWorldAccessOffering",
        "actor_class": "attendee",
        "operation_parameters": (
            "RequestId",
            "ExpectedEpoch",
            "ActorContext",
            "IdempotencyKey",
        ),
        "path_parameter": False,
        "request_schema": "LaunchSelectionRequest",
        "success_status": "200",
        "response_schema": "LaunchSelectionResponse",
    },
}
EXPECTED_FILES = {
    "openapi.json",
    "manifest.json",
    "manifest.schema.json",
    "fixtures/index.json",
    "generated/world_access_client.ts",
}
PUBLICATION_IMPLEMENTATION_FILES = {
    "src/dreamland/product/attendee.py",
    "src/dreamland/product/attendee_catalog.py",
    "src/dreamland/product/generated_access.py",
    "src/dreamland/product/launch_selection.py",
    "tools/product_attendee_preview.py",
    "tools/product_launch_selection_preview.py",
    "tools/product_stage_preview.py",
    "tools/product_world_access_contract.py",
    "tools/product_world_access_preview.py",
    "tools/world_access_client_smoke/package-lock.json",
    "tools/world_access_client_smoke/package.json",
    "tools/world_access_client_smoke/run-smoke.mjs",
}


class WorldAccessContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Fixture:
    case: str
    classification: str
    path: str
    target: str
    payload: dict[str, Any]
    client_action: str
    reachability: str | None = None
    http_status: int | None = None
    operation_id: str | None = None
    request_actor_class: str | None = None
    required_actor_class: str | None = None


def _schema_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _parameter_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/parameters/{name}"}


def _response(
    schema: str,
    *,
    description: str = "Successful private response.",
) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {
            "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
            "X-Content-Type-Options": {
                "schema": {"type": "string", "const": "nosniff"}
            },
        },
        "content": {
            "application/json": {"schema": _schema_ref(schema)},
        },
    }


def _error_response() -> dict[str, Any]:
    return {
        "description": "Bounded transport-safe failure.",
        "headers": {
            "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
            "X-Content-Type-Options": {
                "schema": {"type": "string", "const": "nosniff"}
            },
        },
        "content": {
            "application/problem+json": {"schema": _schema_ref("SafeErrorEnvelope")}
        },
    }


def _operation(
    operation_id: str,
    response_schema: str,
    description: str,
    *,
    actor_class: str,
    mutation: bool = False,
    request_schema: str | None = None,
    created: bool = False,
) -> dict[str, Any]:
    if actor_class not in {"none", "operator", "attendee"}:
        raise ValueError("invalid World Access actor class")
    parameters = [_parameter_ref("RequestId")]
    if actor_class != "none":
        parameters.extend(
            [_parameter_ref("ExpectedEpoch"), _parameter_ref("ActorContext")]
        )
    if mutation:
        parameters.append(_parameter_ref("IdempotencyKey"))
    result: dict[str, Any] = {
        "operationId": operation_id,
        "description": description,
        "parameters": parameters,
        "responses": {
            "201" if created else "200": _response(response_schema),
            "default": _error_response(),
        },
        "security": [{"PrivateBearer": []}],
        "x-dreamland-actor-class": actor_class,
    }
    if request_schema is not None:
        result["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _schema_ref(request_schema)}},
        }
    if mutation:
        result["x-dreamland-idempotency"] = {
            "scope": [
                "service_epoch",
                "actor",
                "operation_family",
                "idempotency_key",
            ],
            "same_key_same_body": "replay_original_result_or_claimed_rejection",
            "same_key_changed_body": "idempotency_conflict",
            "transient_capacity": "unclaimed_retry_after_delay",
            "old_epoch": "outcome_unknown_without_redispatch",
        }
    return result


def _schemas() -> dict[str, Any]:
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    }
    timestamp = {
        "type": "string",
        "format": "date-time",
        "minLength": 20,
        "maxLength": 64,
    }
    display = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "source_label", "revision_label"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "source_label": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
            },
            "revision_label": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
            },
        },
    }
    origin = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "const": "authored_profile_snapshot",
                    }
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "const": "world_forge_selection",
                    }
                },
            },
        ]
    }
    contract = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "revision", "compatibility"],
        "properties": {
            "name": {"type": "string", "const": CONTRACT_NAME},
            "revision": {"type": "string", "const": CONTRACT_REVISION},
            "compatibility": {
                "type": "string",
                "const": CONTRACT_COMPATIBILITY,
            },
        },
    }
    preview = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "kind",
            "provider_calls",
            "live_forge_evidence",
            "live_capability_evidence",
            "loopback_only",
            "process_local",
            "capabilities_enabled",
        ],
        "properties": {
            "kind": {
                "type": "string",
                "const": "provider_free_generated_access_preview",
            },
            "provider_calls": {"type": "boolean", "const": False},
            "live_forge_evidence": {"type": "boolean", "const": False},
            "live_capability_evidence": {"type": "boolean", "const": False},
            "loopback_only": {"type": "boolean", "const": True},
            "process_local": {"type": "boolean", "const": True},
            "capabilities_enabled": {
                "type": "array",
                "maxItems": 0,
                "items": {"type": "string"},
            },
        },
    }
    response_common = {
        "request_id": _schema_ref("RequestId"),
        "contract": contract,
        "preview": preview,
    }
    access_revision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["revision_id", "sha256"],
        "properties": {
            "revision_id": {
                "type": "string",
                "pattern": r"^world-revision:sha256:[a-f0-9]{64}$",
            },
            "sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
        },
    }
    promotion_properties = {
        **response_common,
        "kind": {"type": "string", "const": "generated_world_access"},
        "service_epoch": _schema_ref("ServiceEpoch"),
        "promotion_id": _schema_ref("PromotionId"),
        "access_record_id": _schema_ref("AccessRecordId"),
        "revision_number": {"type": "integer", "minimum": 1},
        "state": {"type": "string", "enum": ["hidden", "revealed"]},
        "origin": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind"],
            "properties": {
                "kind": {"type": "string", "const": "world_forge_selection"}
            },
        },
        "revision": access_revision,
        "created_at": timestamp,
        "changed_at": timestamp,
        "expires_at": timestamp,
        "catalog_eligible": {"type": "boolean"},
        "replayed": {"type": "boolean"},
    }
    promotion_required = list(promotion_properties)
    catalog_offering = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "offering_id",
            "offering_revision",
            "origin",
            "display",
            "notice_version",
            "expires_at",
        ],
        "properties": {
            "offering_id": _schema_ref("OfferingId"),
            "offering_revision": _schema_ref("OfferingRevision"),
            "origin": origin,
            "display": display,
            "notice_version": identifier,
            "expires_at": timestamp,
        },
    }
    return {
        "RequestId": {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9_.:-]+$",
        },
        "ServiceEpoch": {
            "type": "string",
            "pattern": r"^epoch:[A-Za-z0-9_-]{16,128}$",
        },
        "PromotionId": {
            "type": "string",
            "pattern": r"^generated-access-promotion:[A-Za-z0-9_-]{16,128}$",
        },
        "AccessRecordId": {
            "type": "string",
            "pattern": r"^generated-access-record:[A-Za-z0-9_-]{16,128}$",
        },
        "OfferingId": {
            "type": "string",
            "pattern": r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "OfferingRevision": {
            "type": "string",
            "pattern": r"^offering-revision:[A-Za-z0-9][A-Za-z0-9_.-]{15,127}$",
        },
        "LaunchGrant": {
            "type": "string",
            "pattern": r"^launch-grant:[A-Za-z0-9_-]{24,480}$",
        },
        "ContractIdentity": contract,
        "PreviewIdentity": preview,
        "IdentityResponse": {
            "type": "object",
            "additionalProperties": True,
            "required": [
                "kind",
                "service_epoch",
                "request_id",
                "contract",
                "preview",
                "dependencies",
            ],
            "properties": {
                **response_common,
                "kind": {"type": "string", "const": "world_access_identity"},
                "service_epoch": _schema_ref("ServiceEpoch"),
                "dependencies": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product_api", "world_forge_api"],
                    "properties": {
                        "product_api": {
                            "type": "string",
                            "const": PRODUCT_DEPENDENCY_REVISION,
                        },
                        "world_forge_api": {
                            "type": "string",
                            "const": FORGE_DEPENDENCY_REVISION,
                        },
                    },
                },
            },
        },
        "PromotionRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["forge_job_reference"],
            "properties": {
                "forge_job_reference": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "pattern": r"^[\x21-\x7e]+$",
                }
            },
        },
        "PromotionResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": promotion_required,
            "properties": promotion_properties,
            "oneOf": [
                {
                    "required": ["state", "catalog_eligible"],
                    "properties": {
                        "state": {"const": "hidden"},
                        "catalog_eligible": {"const": False},
                    },
                },
                {
                    "required": ["state", "catalog_eligible"],
                    "properties": {
                        "state": {"const": "revealed"},
                        "catalog_eligible": {"const": True},
                    },
                },
            ],
        },
        "RevealRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["expected_access_record_id"],
            "properties": {"expected_access_record_id": _schema_ref("AccessRecordId")},
        },
        "CatalogOffering": catalog_offering,
        "CatalogResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "kind",
                "service_epoch",
                "catalog_position",
                "offerings",
                "request_id",
                "contract",
                "preview",
            ],
            "properties": {
                **response_common,
                "kind": {"type": "string", "const": "world_access_catalog"},
                "service_epoch": _schema_ref("ServiceEpoch"),
                "catalog_position": {
                    "type": "string",
                    "pattern": r"^catalog-position:[a-f0-9]{40}$",
                },
                "offerings": {
                    "type": "array",
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": catalog_offering,
                },
            },
        },
        "LaunchSelectionRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "offering_id",
                "offering_revision",
                "notice_version_presented",
            ],
            "properties": {
                "offering_id": _schema_ref("OfferingId"),
                "offering_revision": _schema_ref("OfferingRevision"),
                "notice_version_presented": identifier,
            },
        },
        "LaunchSelectionResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "kind",
                "service_epoch",
                "launch_grant",
                "launch_binding_sha256",
                "grant_expires_at",
                "selection",
                "replayed",
                "request_id",
                "contract",
                "preview",
            ],
            "properties": {
                **response_common,
                "kind": {
                    "type": "string",
                    "const": "attendee_launch_selection_granted",
                },
                "service_epoch": _schema_ref("ServiceEpoch"),
                "launch_grant": _schema_ref("LaunchGrant"),
                "launch_binding_sha256": {
                    "type": "string",
                    "pattern": r"^[a-f0-9]{64}$",
                    "readOnly": True,
                    "description": (
                        "Server-only correlation evidence equal to the Product "
                        "session snapshot launch_binding.sha256. This value is "
                        "private BFF input and must not be accepted from requests "
                        "or exposed to browsers."
                    ),
                },
                "grant_expires_at": timestamp,
                "selection": catalog_offering,
                "replayed": {"type": "boolean"},
            },
        },
        "SafeError": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "message", "retry", "retry_after_ms"],
            "properties": {
                "code": identifier,
                "message": {"type": "string", "minLength": 1, "maxLength": 160},
                "retry": {
                    "type": "string",
                    "enum": ["none", "same_request", "after_delay"],
                },
                "retry_after_ms": {
                    "oneOf": [
                        {"type": "integer", "minimum": 1, "maximum": 60000},
                        {"type": "null"},
                    ]
                },
            },
            "oneOf": [
                {
                    "required": ["retry", "retry_after_ms"],
                    "properties": {
                        "retry": {"const": "after_delay"},
                        "retry_after_ms": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 60000,
                        },
                    },
                },
                {
                    "required": ["retry", "retry_after_ms"],
                    "properties": {
                        "retry": {"const": "none"},
                        "retry_after_ms": {"type": "null"},
                    },
                },
                {
                    "required": ["retry", "retry_after_ms"],
                    "properties": {
                        "retry": {"const": "same_request"},
                        "retry_after_ms": {"type": "null"},
                    },
                },
            ],
        },
        "SafeErrorEnvelope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request_id", "error"],
            "properties": {
                "request_id": _schema_ref("RequestId"),
                "error": _schema_ref("SafeError"),
            },
        },
        "IntegrationHttpScenario": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "condition",
                "operation_id",
                "request_actor_class",
                "required_actor_class",
                "expected_http_status",
                "expected_error_code",
                "side_effects_committed",
            ],
            "properties": {
                "condition": {
                    "type": "string",
                    "enum": [
                        "wrong_actor_class",
                        "offering_revision_changed",
                        "offering_expired",
                    ],
                },
                "operation_id": {
                    "type": "string",
                    "enum": [
                        specification["operation_id"]
                        for specification in EXPECTED_OPERATIONS.values()
                    ],
                },
                "request_actor_class": {
                    "type": "string",
                    "enum": ["operator", "attendee"],
                },
                "required_actor_class": {
                    "type": "string",
                    "enum": ["operator", "attendee"],
                },
                "expected_http_status": {
                    "type": "integer",
                    "minimum": 400,
                    "maximum": 599,
                },
                "expected_error_code": identifier,
                "side_effects_committed": {"type": "boolean", "const": False},
            },
        },
    }


def build_openapi() -> dict[str, Any]:
    paths = {
        "/world-access/v1/identity": {
            "get": _operation(
                "getWorldAccessIdentity",
                "IdentityResponse",
                "Resolve the private World Access contract and current epoch.",
                actor_class="none",
            )
        },
        "/world-access/v1/promotions": {
            "post": _operation(
                "promoteSelectedForgeWorld",
                "PromotionResponse",
                "Create hidden access truth from one server-resolved Forge selection.",
                actor_class="operator",
                mutation=True,
                request_schema="PromotionRequest",
                created=True,
            )
        },
        "/world-access/v1/promotions/{promotion_id}": {
            "parameters": [
                {
                    "name": "promotion_id",
                    "in": "path",
                    "required": True,
                    "schema": _schema_ref("PromotionId"),
                }
            ],
            "get": _operation(
                "getWorldAccessPromotion",
                "PromotionResponse",
                "Read the current safe promotion/access projection.",
                actor_class="operator",
            ),
        },
        "/world-access/v1/promotions/{promotion_id}/reveal": {
            "parameters": [
                {
                    "name": "promotion_id",
                    "in": "path",
                    "required": True,
                    "schema": _schema_ref("PromotionId"),
                }
            ],
            "post": _operation(
                "revealWorldAccessPromotion",
                "PromotionResponse",
                "Publish and reveal the expected hidden access-record revision.",
                actor_class="operator",
                mutation=True,
                request_schema="RevealRequest",
            ),
        },
        "/world-access/v1/catalog": {
            "get": _operation(
                "getWorldAccessCatalog",
                "CatalogResponse",
                "List bounded authored and revealed-generated attendee offerings.",
                actor_class="attendee",
            )
        },
        "/world-access/v1/launch-selections": {
            "post": _operation(
                "exchangeWorldAccessOffering",
                "LaunchSelectionResponse",
                "Exchange exact catalog and notice evidence for a Product grant.",
                actor_class="attendee",
                mutation=True,
                request_schema="LaunchSelectionRequest",
            )
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DREAMLAND Private Generated-World Access Preview",
            "version": CONTRACT_REVISION,
            "description": (
                "Additive, provider-free, loopback-only Phase 8A promotion, "
                "catalog, and attendee launch-selection contract. It is private "
                "BFF input and enables no production capability."
            ),
        },
        "servers": [
            {"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "0"}}}
        ],
        "security": [{"PrivateBearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"PrivateBearer": {"type": "http", "scheme": "bearer"}},
            "parameters": {
                "RequestId": {
                    "name": "X-Request-Id",
                    "in": "header",
                    "required": False,
                    "schema": _schema_ref("RequestId"),
                },
                "ExpectedEpoch": {
                    "name": "X-Dreamland-Expected-Epoch",
                    "in": "header",
                    "required": True,
                    "schema": _schema_ref("ServiceEpoch"),
                },
                "ActorContext": {
                    "name": "X-Dreamland-Actor-Context",
                    "in": "header",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 16,
                        "maxLength": 512,
                        "pattern": r"^[\x21-\x7e]+$",
                    },
                },
                "IdempotencyKey": {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": r"^[\x21-\x7e]+$",
                    },
                },
            },
            "schemas": _schemas(),
        },
        "x-dreamland-boundary": {
            "private_bff_only": True,
            "direct_browser_access": False,
            "provider_calls": False,
            "live_capability_evidence": False,
            "production_capabilities_enabled": [],
            "offering_id_is_authority": False,
            "launch_selection_id_exposed": False,
            "launch_binding_sha256_server_only": True,
            "launch_binding_sha256_browser_exposed": False,
        },
    }


def _common() -> dict[str, Any]:
    return {
        "request_id": "request:phase8a-world-access-0001",
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        },
        "preview": {
            "kind": "provider_free_generated_access_preview",
            "provider_calls": False,
            "live_forge_evidence": False,
            "live_capability_evidence": False,
            "loopback_only": True,
            "process_local": True,
            "capabilities_enabled": [],
        },
    }


def _offering(*, generated: bool) -> dict[str, Any]:
    kind = "world_forge_selection" if generated else "authored_profile_snapshot"
    prefix = "generated" if generated else "authored"
    return {
        "offering_id": f"offering:phase8a-{prefix}",
        "offering_revision": f"offering-revision:{prefix}-" + "a" * 40,
        "origin": {"kind": kind},
        "display": {
            "title": "Audience-created DREAMLAND world"
            if generated
            else "Authored baseline",
            "source_label": "World Forge" if generated else "Authored",
            "revision_label": "World Forge 0123456789ab" if generated else "Pinned A1",
        },
        "notice_version": "notice:phase8a-world-access-v1",
        "expires_at": "2026-08-30T13:00:00Z",
    }


def _promotion(*, revealed: bool, replayed: bool = False) -> dict[str, Any]:
    revision_number = 2 if revealed else 1
    return {
        **_common(),
        "kind": "generated_world_access",
        "service_epoch": "epoch:phase8a_world_access_0001",
        "promotion_id": "generated-access-promotion:phase8a_00000001",
        "access_record_id": f"generated-access-record:phase8a_0000000{revision_number}",
        "revision_number": revision_number,
        "state": "revealed" if revealed else "hidden",
        "origin": {"kind": "world_forge_selection"},
        "revision": {
            "revision_id": "world-revision:sha256:" + "0" * 64,
            "sha256": "0" * 64,
        },
        "created_at": "2026-08-30T12:00:00Z",
        "changed_at": "2026-08-30T12:01:00Z" if revealed else "2026-08-30T12:00:00Z",
        "expires_at": "2026-08-30T13:00:00Z",
        "catalog_eligible": revealed,
        "replayed": replayed,
    }


SAFE_ERRORS = {
    "unauthorized": (
        401,
        "Missing or invalid private World Access authority.",
        "none",
        None,
    ),
    "not_found": (
        404,
        "The requested World Access resource is unavailable.",
        "none",
        None,
    ),
    "invalid_request": (400, "The World Access request is invalid.", "none", None),
    "invalid_json": (400, "The request body is not valid JSON.", "none", None),
    "unsupported_media_type": (
        415,
        "The request content type is unsupported.",
        "none",
        None,
    ),
    "content_length_required": (
        411,
        "A Content-Length header is required.",
        "none",
        None,
    ),
    "request_too_large": (
        413,
        "The request body exceeds the World Access preview limit.",
        "none",
        None,
    ),
    "authority_denied": (
        403,
        "The requested World Access resource is unavailable.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        400,
        "Expected World Access epoch evidence is required.",
        "none",
        None,
    ),
    "outcome_unknown": (
        410,
        "The prior mutation outcome cannot be reconciled.",
        "none",
        None,
    ),
    "service_epoch_changed": (
        410,
        "The World Access service epoch has changed.",
        "none",
        None,
    ),
    "selection_unavailable": (
        404,
        "The selected World Forge revision is unavailable for promotion.",
        "none",
        None,
    ),
    "revision_unavailable": (
        409,
        "The selected World Forge artifact is unavailable for promotion.",
        "none",
        None,
    ),
    "capacity_exhausted": (
        503,
        "Generated-world access capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "identity_capacity_exhausted": (
        503,
        "Generated-world identity capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "idempotency_capacity_exhausted": (
        503,
        "World Access replay capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "idempotency_conflict": (
        409,
        "The idempotency key is already bound to another request.",
        "none",
        None,
    ),
    "access_unavailable": (
        404,
        "The generated-world access record is unavailable.",
        "none",
        None,
    ),
    "source_temporarily_unavailable": (
        503,
        "The selected World Forge revision is temporarily unavailable.",
        "after_delay",
        250,
    ),
    "access_record_changed": (
        409,
        "The generated-world access record has changed.",
        "none",
        None,
    ),
    "publication_capacity_exhausted": (
        503,
        "Generated-world publication capacity is temporarily exhausted.",
        "after_delay",
        250,
    ),
    "publication_conflict": (
        409,
        "The generated-world publication conflicts with current state.",
        "none",
        None,
    ),
    "reveal_unavailable": (
        409,
        "The generated-world access record cannot be revealed.",
        "none",
        None,
    ),
    "launch_selection_unavailable": (
        404,
        "The attendee launch selection is unavailable.",
        "none",
        None,
    ),
    "notice_version_mismatch": (
        409,
        "The presented privacy notice version does not match this offering.",
        "none",
        None,
    ),
    "launch_selection_capacity_exhausted": (
        503,
        "Attendee launch-selection capacity is temporarily exhausted.",
        "after_delay",
        1000,
    ),
    "service_not_open": (
        503,
        "Attendee admission is not open yet.",
        "after_delay",
        1000,
    ),
    "service_closed": (410, "Attendee admission is closed.", "none", None),
    "internal_error": (
        500,
        "The World Access preview could not complete the request.",
        "none",
        None,
    ),
}


def fixture_definitions() -> tuple[Fixture, ...]:
    hidden = _promotion(revealed=False)
    revealed = _promotion(revealed=True)
    authored = _offering(generated=False)
    generated = _offering(generated=True)
    authored_grant = {
        **_common(),
        "kind": "attendee_launch_selection_granted",
        "service_epoch": "epoch:phase8a_world_access_0001",
        "launch_grant": "launch-grant:phase8a_authored_00000001",
        "launch_binding_sha256": "1" * 64,
        "grant_expires_at": "2026-08-30T12:20:00Z",
        "selection": authored,
        "replayed": False,
    }
    fixtures: list[Fixture] = [
        Fixture(
            "identity",
            "canonical-valid",
            "fixtures/canonical-valid/identity.json",
            "openapi.json#/components/schemas/IdentityResponse",
            {
                **_common(),
                "kind": "world_access_identity",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "dependencies": {
                    "product_api": PRODUCT_DEPENDENCY_REVISION,
                    "world_forge_api": FORGE_DEPENDENCY_REVISION,
                },
            },
            "accept",
        ),
        Fixture(
            "promotion-request",
            "canonical-valid",
            "fixtures/canonical-valid/promotion-request.json",
            "openapi.json#/components/schemas/PromotionRequest",
            {"forge_job_reference": "forge-reference:phase8a-selected-0001"},
            "accept",
        ),
        Fixture(
            "hidden-promotion",
            "canonical-valid",
            "fixtures/canonical-valid/hidden-promotion.json",
            "openapi.json#/components/schemas/PromotionResponse",
            hidden,
            "accept",
        ),
        Fixture(
            "hidden-promotion-replay",
            "canonical-valid",
            "fixtures/canonical-valid/hidden-promotion-replay.json",
            "openapi.json#/components/schemas/PromotionResponse",
            {**hidden, "replayed": True},
            "accept",
        ),
        Fixture(
            "hidden-promotion-status",
            "canonical-valid",
            "fixtures/canonical-valid/hidden-promotion-status.json",
            "openapi.json#/components/schemas/PromotionResponse",
            hidden,
            "accept",
        ),
        Fixture(
            "reveal-request",
            "canonical-valid",
            "fixtures/canonical-valid/reveal-request.json",
            "openapi.json#/components/schemas/RevealRequest",
            {"expected_access_record_id": hidden["access_record_id"]},
            "accept",
        ),
        Fixture(
            "revealed-promotion",
            "canonical-valid",
            "fixtures/canonical-valid/revealed-promotion.json",
            "openapi.json#/components/schemas/PromotionResponse",
            revealed,
            "accept",
        ),
        Fixture(
            "revealed-promotion-replay",
            "canonical-valid",
            "fixtures/canonical-valid/revealed-promotion-replay.json",
            "openapi.json#/components/schemas/PromotionResponse",
            {**revealed, "replayed": True},
            "accept",
        ),
        Fixture(
            "authored-catalog",
            "canonical-valid",
            "fixtures/canonical-valid/authored-catalog.json",
            "openapi.json#/components/schemas/CatalogResponse",
            {
                **_common(),
                "kind": "world_access_catalog",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "catalog_position": "catalog-position:" + "1" * 40,
                "offerings": [authored],
            },
            "accept",
        ),
        Fixture(
            "empty-catalog",
            "canonical-valid",
            "fixtures/canonical-valid/empty-catalog.json",
            "openapi.json#/components/schemas/CatalogResponse",
            {
                **_common(),
                "kind": "world_access_catalog",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "catalog_position": "catalog-position:" + "0" * 40,
                "offerings": [],
            },
            "accept",
        ),
        Fixture(
            "generated-only-catalog",
            "canonical-valid",
            "fixtures/canonical-valid/generated-only-catalog.json",
            "openapi.json#/components/schemas/CatalogResponse",
            {
                **_common(),
                "kind": "world_access_catalog",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "catalog_position": "catalog-position:" + "3" * 40,
                "offerings": [generated],
            },
            "accept",
        ),
        Fixture(
            "hidden-generated-offering-omitted",
            "canonical-valid",
            "fixtures/canonical-valid/hidden-generated-offering-omitted.json",
            "openapi.json#/components/schemas/CatalogResponse",
            {
                **_common(),
                "kind": "world_access_catalog",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "catalog_position": "catalog-position:" + "1" * 40,
                "offerings": [authored],
            },
            "accept",
        ),
        Fixture(
            "mixed-catalog",
            "canonical-valid",
            "fixtures/canonical-valid/mixed-catalog.json",
            "openapi.json#/components/schemas/CatalogResponse",
            {
                **_common(),
                "kind": "world_access_catalog",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "catalog_position": "catalog-position:" + "2" * 40,
                "offerings": [authored, generated],
            },
            "accept",
        ),
        Fixture(
            "authored-exchange-request",
            "canonical-valid",
            "fixtures/canonical-valid/authored-exchange-request.json",
            "openapi.json#/components/schemas/LaunchSelectionRequest",
            {
                "offering_id": authored["offering_id"],
                "offering_revision": authored["offering_revision"],
                "notice_version_presented": authored["notice_version"],
            },
            "accept",
        ),
        Fixture(
            "generated-exchange-request",
            "canonical-valid",
            "fixtures/canonical-valid/generated-exchange-request.json",
            "openapi.json#/components/schemas/LaunchSelectionRequest",
            {
                "offering_id": generated["offering_id"],
                "offering_revision": generated["offering_revision"],
                "notice_version_presented": generated["notice_version"],
            },
            "accept",
        ),
        Fixture(
            "authored-grant",
            "canonical-valid",
            "fixtures/canonical-valid/authored-grant.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            authored_grant,
            "accept",
        ),
        Fixture(
            "authored-grant-replay",
            "canonical-valid",
            "fixtures/canonical-valid/authored-grant-replay.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {**authored_grant, "replayed": True},
            "accept",
        ),
        Fixture(
            "generated-grant",
            "canonical-valid",
            "fixtures/canonical-valid/generated-grant.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {
                **_common(),
                "kind": "attendee_launch_selection_granted",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "launch_grant": "launch-grant:phase8a_generated_00000001",
                "launch_binding_sha256": "2" * 64,
                "grant_expires_at": "2026-08-30T12:20:00Z",
                "selection": generated,
                "replayed": False,
            },
            "accept",
        ),
        Fixture(
            "generated-grant-replay",
            "canonical-valid",
            "fixtures/canonical-valid/generated-grant-replay.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {
                **_common(),
                "kind": "attendee_launch_selection_granted",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "launch_grant": "launch-grant:phase8a_generated_00000001",
                "launch_binding_sha256": "2" * 64,
                "grant_expires_at": "2026-08-30T12:20:00Z",
                "selection": generated,
                "replayed": True,
            },
            "accept",
        ),
        Fixture(
            "additive-identity",
            "forward-compatibility",
            "fixtures/forward-compatibility/identity-additive.json",
            "openapi.json#/components/schemas/IdentityResponse",
            {
                **_common(),
                "kind": "world_access_identity",
                "service_epoch": "epoch:phase8a_world_access_0001",
                "dependencies": {
                    "product_api": PRODUCT_DEPENDENCY_REVISION,
                    "world_forge_api": FORGE_DEPENDENCY_REVISION,
                },
                "future_safe_field": {"ignored": True},
            },
            "accept-ignore-unknown-top-level",
        ),
        Fixture(
            "hidden-promotion-catalog-eligible",
            "invalid",
            "fixtures/invalid/hidden-promotion-catalog-eligible.json",
            "openapi.json#/components/schemas/PromotionResponse",
            {**hidden, "catalog_eligible": True},
            "reject",
        ),
        Fixture(
            "revealed-promotion-catalog-ineligible",
            "invalid",
            "fixtures/invalid/revealed-promotion-catalog-ineligible.json",
            "openapi.json#/components/schemas/PromotionResponse",
            {**revealed, "catalog_eligible": False},
            "reject",
        ),
        Fixture(
            "safe-error-after-delay-without-delay",
            "invalid",
            "fixtures/invalid/safe-error-after-delay-without-delay.json",
            "openapi.json#/components/schemas/SafeErrorEnvelope",
            {
                "request_id": "request:phase8a-world-access-error",
                "error": {
                    "code": "capacity_exhausted",
                    "message": "Retry later.",
                    "retry": "after_delay",
                    "retry_after_ms": None,
                },
            },
            "reject",
        ),
        Fixture(
            "safe-error-none-with-delay",
            "invalid",
            "fixtures/invalid/safe-error-none-with-delay.json",
            "openapi.json#/components/schemas/SafeErrorEnvelope",
            {
                "request_id": "request:phase8a-world-access-error",
                "error": {
                    "code": "authority_denied",
                    "message": "The request is unavailable.",
                    "retry": "none",
                    "retry_after_ms": 250,
                },
            },
            "reject",
        ),
        Fixture(
            "safe-error-same-request-with-delay",
            "invalid",
            "fixtures/invalid/safe-error-same-request-with-delay.json",
            "openapi.json#/components/schemas/SafeErrorEnvelope",
            {
                "request_id": "request:phase8a-world-access-error",
                "error": {
                    "code": "outcome_unknown",
                    "message": "Reconcile the same request.",
                    "retry": "same_request",
                    "retry_after_ms": 250,
                },
            },
            "reject",
        ),
        Fixture(
            "promotion-authority-injection",
            "invalid",
            "fixtures/invalid/promotion-authority-injection.json",
            "openapi.json#/components/schemas/PromotionRequest",
            {
                "forge_job_reference": "forge-reference:phase8a-selected-0001",
                "artifact_path": "/private/forge.json",
                "revision_digest": "sha256:" + "0" * 64,
                "raw_state": {},
            },
            "reject",
        ),
        Fixture(
            "promotion-profile-receipt-injection",
            "invalid",
            "fixtures/invalid/promotion-profile-receipt-injection.json",
            "openapi.json#/components/schemas/PromotionRequest",
            {
                "forge_job_reference": "forge-reference:phase8a-selected-0001",
                "profile_name": "colonyship_a1",
                "receipt": {"selected": True},
            },
            "reject",
        ),
        Fixture(
            "catalog-selection-leak",
            "invalid",
            "fixtures/invalid/catalog-selection-leak.json",
            "openapi.json#/components/schemas/CatalogOffering",
            {**generated, "launch_selection_id": "launch-selection:forbidden_00000001"},
            "reject",
        ),
        Fixture(
            "catalog-authority-leak",
            "invalid",
            "fixtures/invalid/catalog-authority-leak.json",
            "openapi.json#/components/schemas/CatalogOffering",
            {
                **generated,
                "launch_grant": "launch-grant:forbidden_authority_00000001",
                "revision_digest": "sha256:" + "0" * 64,
                "artifact_path": "/private/forge.json",
            },
            "reject",
        ),
        Fixture(
            "catalog-unknown-origin",
            "invalid",
            "fixtures/invalid/catalog-unknown-origin.json",
            "openapi.json#/components/schemas/CatalogOffering",
            {**generated, "origin": {"kind": "future_generated_origin"}},
            "reject",
        ),
        Fixture(
            "promotion-unknown-state",
            "invalid",
            "fixtures/invalid/promotion-unknown-state.json",
            "openapi.json#/components/schemas/PromotionResponse",
            {**hidden, "state": "future_access_state"},
            "reject",
        ),
        Fixture(
            "exchange-authority-injection",
            "invalid",
            "fixtures/invalid/exchange-authority-injection.json",
            "openapi.json#/components/schemas/LaunchSelectionRequest",
            {
                "offering_id": generated["offering_id"],
                "offering_revision": generated["offering_revision"],
                "notice_version_presented": generated["notice_version"],
                "launch_grant": "launch-grant:forbidden_authority_00000001",
            },
            "reject",
        ),
        Fixture(
            "exchange-launch-binding-injection",
            "invalid",
            "fixtures/invalid/exchange-launch-binding-injection.json",
            "openapi.json#/components/schemas/LaunchSelectionRequest",
            {
                "offering_id": generated["offering_id"],
                "offering_revision": generated["offering_revision"],
                "notice_version_presented": generated["notice_version"],
                "launch_binding_sha256": "2" * 64,
            },
            "reject",
        ),
        Fixture(
            "grant-launch-binding-missing",
            "invalid",
            "fixtures/invalid/grant-launch-binding-missing.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {
                key: value
                for key, value in authored_grant.items()
                if key != "launch_binding_sha256"
            },
            "reject",
        ),
        Fixture(
            "grant-launch-binding-nonhex",
            "invalid",
            "fixtures/invalid/grant-launch-binding-nonhex.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {**authored_grant, "launch_binding_sha256": "g" * 64},
            "reject",
        ),
        Fixture(
            "grant-launch-binding-uppercase",
            "invalid",
            "fixtures/invalid/grant-launch-binding-uppercase.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {**authored_grant, "launch_binding_sha256": "A" * 64},
            "reject",
        ),
        Fixture(
            "grant-launch-binding-wrong-length",
            "invalid",
            "fixtures/invalid/grant-launch-binding-wrong-length.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {**authored_grant, "launch_binding_sha256": "1" * 63},
            "reject",
        ),
        Fixture(
            "grant-launch-binding-object",
            "invalid",
            "fixtures/invalid/grant-launch-binding-object.json",
            "openapi.json#/components/schemas/LaunchSelectionResponse",
            {**authored_grant, "launch_binding_sha256": {"sha256": "1" * 64}},
            "reject",
        ),
        Fixture(
            "wrong-attendee-actor-for-promotion",
            "http-scenario",
            "fixtures/http-scenarios/wrong-attendee-actor-for-promotion.json",
            "openapi.json#/components/schemas/IntegrationHttpScenario",
            {
                "condition": "wrong_actor_class",
                "operation_id": "promoteSelectedForgeWorld",
                "request_actor_class": "attendee",
                "required_actor_class": "operator",
                "expected_http_status": 403,
                "expected_error_code": "authority_denied",
                "side_effects_committed": False,
            },
            "exercise-http",
            reachability="http",
            http_status=403,
            operation_id="promoteSelectedForgeWorld",
            request_actor_class="attendee",
            required_actor_class="operator",
        ),
        Fixture(
            "wrong-operator-actor-for-catalog",
            "http-scenario",
            "fixtures/http-scenarios/wrong-operator-actor-for-catalog.json",
            "openapi.json#/components/schemas/IntegrationHttpScenario",
            {
                "condition": "wrong_actor_class",
                "operation_id": "getWorldAccessCatalog",
                "request_actor_class": "operator",
                "required_actor_class": "attendee",
                "expected_http_status": 403,
                "expected_error_code": "authority_denied",
                "side_effects_committed": False,
            },
            "exercise-http",
            reachability="http",
            http_status=403,
            operation_id="getWorldAccessCatalog",
            request_actor_class="operator",
            required_actor_class="attendee",
        ),
        Fixture(
            "offering-revision-changed",
            "http-scenario",
            "fixtures/http-scenarios/offering-revision-changed.json",
            "openapi.json#/components/schemas/IntegrationHttpScenario",
            {
                "condition": "offering_revision_changed",
                "operation_id": "exchangeWorldAccessOffering",
                "request_actor_class": "attendee",
                "required_actor_class": "attendee",
                "expected_http_status": 404,
                "expected_error_code": "launch_selection_unavailable",
                "side_effects_committed": False,
            },
            "exercise-http",
            reachability="http",
            http_status=404,
            operation_id="exchangeWorldAccessOffering",
            request_actor_class="attendee",
            required_actor_class="attendee",
        ),
        Fixture(
            "offering-expired",
            "http-scenario",
            "fixtures/http-scenarios/offering-expired.json",
            "openapi.json#/components/schemas/IntegrationHttpScenario",
            {
                "condition": "offering_expired",
                "operation_id": "exchangeWorldAccessOffering",
                "request_actor_class": "attendee",
                "required_actor_class": "attendee",
                "expected_http_status": 404,
                "expected_error_code": "launch_selection_unavailable",
                "side_effects_committed": False,
            },
            "exercise-http",
            reachability="http",
            http_status=404,
            operation_id="exchangeWorldAccessOffering",
            request_actor_class="attendee",
            required_actor_class="attendee",
        ),
    ]
    for code, (_status, message, retry, retry_after_ms) in SAFE_ERRORS.items():
        fixtures.append(
            Fixture(
                f"safe-error-{code}",
                "safe-error",
                f"fixtures/safe-errors/{code}.json",
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                {
                    "request_id": "request:phase8a-world-access-error",
                    "error": {
                        "code": code,
                        "message": message,
                        "retry": retry,
                        "retry_after_ms": retry_after_ms,
                    },
                },
                "handle-safe-error",
                "http",
                _status,
            )
        )
    return tuple(fixtures)


def generated_client_source() -> str:
    return f"""// Generated by tools/product_world_access_contract.py. server-only private BFF client.
export const WORLD_ACCESS_CONTRACT_REVISION = {json.dumps(CONTRACT_REVISION)} as const;

export interface WorldAccessPrivateConnection {{ readonly bearer: string; }}
export interface WorldAccessContractIdentity {{
  readonly name: {json.dumps(CONTRACT_NAME)};
  readonly revision: typeof WORLD_ACCESS_CONTRACT_REVISION;
  readonly compatibility: {json.dumps(CONTRACT_COMPATIBILITY)};
}}
export interface WorldAccessPreviewIdentity {{
  readonly kind: "provider_free_generated_access_preview";
  readonly provider_calls: false;
  readonly live_forge_evidence: false;
  readonly live_capability_evidence: false;
  readonly loopback_only: true;
  readonly process_local: true;
  readonly capabilities_enabled: readonly [];
}}
export type WorldAccessOfferingOrigin =
  | {{ readonly kind: "authored_profile_snapshot" }}
  | {{ readonly kind: "world_forge_selection" }};
export interface WorldAccessOfferingDisplay {{
  readonly title: string;
  readonly source_label: string;
  readonly revision_label: string;
}}
export interface WorldAccessCatalogOffering {{
  readonly offering_id: string;
  readonly offering_revision: string;
  readonly origin: WorldAccessOfferingOrigin;
  readonly display: WorldAccessOfferingDisplay;
  readonly notice_version: string;
  readonly expires_at: string;
}}
export interface WorldAccessLaunchSelectionResponse {{
  readonly kind: "attendee_launch_selection_granted";
  readonly service_epoch: string;
  readonly launch_grant: string;
  readonly launch_binding_sha256: string;
  readonly grant_expires_at: string;
  readonly selection: WorldAccessCatalogOffering;
  readonly replayed: boolean;
  readonly request_id: string;
  readonly contract: WorldAccessContractIdentity;
  readonly preview: WorldAccessPreviewIdentity;
}}

function requirePreviewLoopbackBaseUrl(value: string): string {{
  let parsed: URL;
  try {{
    parsed = new URL(value);
  }} catch {{
    throw new Error("World Access preview base URL must be loopback HTTP");
  }}
  const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
  if (value !== value.trim() || parsed.protocol !== "http:" || !loopback || parsed.username !== "" || parsed.password !== "" || parsed.pathname !== "/" || parsed.search !== "" || parsed.hash !== "") {{
    throw new Error("World Access preview base URL must be loopback HTTP");
  }}
  return parsed.origin;
}}

export class WorldAccessPrivateClient {{
  private expectedEpoch: string | null = null;
  private readonly baseUrl: string;
  private readonly auth: WorldAccessPrivateConnection;
  constructor(baseUrl: string, auth: WorldAccessPrivateConnection) {{
    this.baseUrl = requirePreviewLoopbackBaseUrl(baseUrl);
    this.auth = auth;
  }}
  identity(signal?: AbortSignal) {{ return this.request("GET", "/world-access/v1/identity", undefined, undefined, undefined, signal); }}
  setExpectedEpoch(epoch: string) {{ this.expectedEpoch = epoch; }}
  promote(actorContext: string, body: unknown, idempotencyKey: string, signal?: AbortSignal) {{ return this.request("POST", "/world-access/v1/promotions", actorContext, body, idempotencyKey, signal); }}
  promotion(actorContext: string, promotionId: string, signal?: AbortSignal) {{ return this.request("GET", `/world-access/v1/promotions/${{encodeURIComponent(promotionId)}}`, actorContext, undefined, undefined, signal); }}
  reveal(actorContext: string, promotionId: string, body: unknown, idempotencyKey: string, signal?: AbortSignal) {{ return this.request("POST", `/world-access/v1/promotions/${{encodeURIComponent(promotionId)}}/reveal`, actorContext, body, idempotencyKey, signal); }}
  catalog(actorContext: string, signal?: AbortSignal) {{ return this.request("GET", "/world-access/v1/catalog", actorContext, undefined, undefined, signal); }}
  exchange(actorContext: string, body: unknown, idempotencyKey: string, signal?: AbortSignal): Promise<WorldAccessLaunchSelectionResponse> {{ return this.request("POST", "/world-access/v1/launch-selections", actorContext, body, idempotencyKey, signal) as Promise<WorldAccessLaunchSelectionResponse>; }}

  private async request(method: string, path: string, actorContext?: string, body?: unknown, idempotencyKey?: string, signal?: AbortSignal): Promise<unknown> {{
    const headers: Record<string, string> = {{ Authorization: `Bearer ${{this.auth.bearer}}`, Accept: "application/json, application/problem+json" }};
    if (actorContext !== undefined) {{
      if (this.expectedEpoch === null) throw new Error("World Access identity must be resolved first");
      headers["X-Dreamland-Expected-Epoch"] = this.expectedEpoch;
      headers["X-Dreamland-Actor-Context"] = actorContext;
    }}
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey !== undefined) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(this.baseUrl + path, {{ method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal, redirect: "error", cache: "no-store" }});
    const declaredContentType = response.headers.get("Content-Type");
    const mediaType = declaredContentType?.split(";", 1)[0]?.trim().toLowerCase();
    const expectedMediaType = response.ok ? "application/json" : "application/problem+json";
    if (mediaType !== expectedMediaType) {{
      throw Object.assign(new Error(`World Access response Content-Type must be ${{expectedMediaType}}`), {{ status: response.status, contentType: declaredContentType }});
    }}
    const value: unknown = await response.json();
    if (!response.ok) throw Object.assign(new Error("World Access private request failed"), {{ status: response.status, value }});
    return value;
  }}
}}
"""


def build_manifest_schema() -> dict[str, Any]:
    digest = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    dependency = {
        "type": "object",
        "additionalProperties": False,
        "required": ["contract", "revision", "manifest_sha256"],
        "properties": {
            "contract": {"type": "string"},
            "revision": {"type": "string"},
            "manifest_sha256": digest,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "contract_name",
            "contract_revision",
            "compatibility",
            "publication_status",
            "implementation_status",
            "transport_status",
            "backend_commit",
            "published_at",
            "dependencies",
            "artifacts",
            "fixture_set",
            "generation",
            "source",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "contract_name": {"type": "string", "const": CONTRACT_NAME},
            "contract_revision": {"type": "string", "const": CONTRACT_REVISION},
            "compatibility": {"type": "string", "const": CONTRACT_COMPATIBILITY},
            "publication_status": {"type": "string", "enum": ["draft", "published"]},
            "implementation_status": {
                "type": "string",
                "const": "provider_free_integration_preview_only",
            },
            "transport_status": {
                "type": "string",
                "const": "manually_invoked_loopback_private_bff_only",
            },
            "backend_commit": {"type": ["string", "null"]},
            "published_at": {"type": ["string", "null"]},
            "dependencies": {
                "type": "object",
                "additionalProperties": False,
                "required": ["product_api", "world_forge_api"],
                "properties": {
                    "product_api": dependency,
                    "world_forge_api": dependency,
                },
            },
            "artifacts": {"type": "object", "additionalProperties": digest},
            "fixture_set": {"type": "object"},
            "generation": {"type": "object"},
            "source": {"type": "object"},
        },
        "oneOf": [
            {
                "properties": {
                    "publication_status": {"const": "draft"},
                    "backend_commit": {"type": "null"},
                    "published_at": {"type": "null"},
                }
            },
            {
                "properties": {
                    "publication_status": {"const": "published"},
                    "backend_commit": {
                        "type": "string",
                        "pattern": "^[a-f0-9]{40}$",
                    },
                    "published_at": {
                        "type": "string",
                        "format": "date-time",
                        "minLength": 20,
                        "maxLength": 64,
                    },
                }
            },
        ],
    }


def _base_manifest() -> dict[str, Any]:
    root = f"contracts/world-access-api/v{CONTRACT_REVISION}"
    tool = ".venv/bin/python tools/product_world_access_contract.py"
    client_tooling = Path(__file__).resolve().parent / "world_access_client_smoke"
    return {
        "schema_version": 1,
        "contract_name": CONTRACT_NAME,
        "contract_revision": CONTRACT_REVISION,
        "compatibility": CONTRACT_COMPATIBILITY,
        "publication_status": "draft",
        "implementation_status": "provider_free_integration_preview_only",
        "transport_status": "manually_invoked_loopback_private_bff_only",
        "backend_commit": None,
        "published_at": None,
        "dependencies": {
            "product_api": {
                "contract": "dreamland-private-product-api",
                "revision": PRODUCT_DEPENDENCY_REVISION,
                "manifest_sha256": PRODUCT_DEPENDENCY_MANIFEST_SHA256,
            },
            "world_forge_api": {
                "contract": "dreamland-private-world-forge-api",
                "revision": FORGE_DEPENDENCY_REVISION,
                "manifest_sha256": FORGE_DEPENDENCY_MANIFEST_SHA256,
            },
        },
        "artifacts": {},
        "fixture_set": {"path": "fixtures/index.json", "index_sha256": ""},
        "generation": {
            "refresh_command": f"{tool} refresh {root}",
            "validate_command": f"{tool} validate {root}",
            "client_smoke_command": f"{tool} client-smoke {root}",
            "publish_command": f"{tool} publish {root} --backend-commit COMMIT --published-at TIMESTAMP",
            "client_toolchain": {
                "install_command": "npm ci --prefix tools/world_access_client_smoke",
                "package": "tools/world_access_client_smoke/package.json",
                "package_sha256": _sha256(
                    (client_tooling / "package.json").read_bytes()
                ),
                "package_lock": "tools/world_access_client_smoke/package-lock.json",
                "package_lock_sha256": _sha256(
                    (client_tooling / "package-lock.json").read_bytes()
                ),
                "harness": "tools/world_access_client_smoke/run-smoke.mjs",
                "harness_sha256": _sha256(
                    (client_tooling / "run-smoke.mjs").read_bytes()
                ),
                "typescript": "6.0.3",
            },
        },
        "source": {
            "kind": "provider_free_generated_access_preview",
            "provider_calls": False,
            "live_forge_evidence": False,
            "live_capability_evidence": False,
            "normal_runtime_integration": False,
            "production_capabilities_enabled": [],
        },
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture_material() -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    entries: list[dict[str, Any]] = []
    values: dict[str, bytes] = {}
    for fixture in fixture_definitions():
        data = _json_bytes(fixture.payload)
        values[fixture.path] = data
        entries.append(
            {
                "case": fixture.case,
                "classification": fixture.classification,
                "path": fixture.path,
                "target": fixture.target,
                "client_action": fixture.client_action,
                "sha256": _sha256(data),
                **(
                    {"reachability": fixture.reachability}
                    if fixture.reachability
                    else {}
                ),
                **(
                    {"http_status": fixture.http_status}
                    if fixture.http_status is not None
                    else {}
                ),
                **(
                    {"operation_id": fixture.operation_id}
                    if fixture.operation_id is not None
                    else {}
                ),
                **(
                    {"request_actor_class": fixture.request_actor_class}
                    if fixture.request_actor_class is not None
                    else {}
                ),
                **(
                    {"required_actor_class": fixture.required_actor_class}
                    if fixture.required_actor_class is not None
                    else {}
                ),
            }
        )
    return entries, values


def refresh_contract(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    expected = set(EXPECTED_FILES)
    entries, fixture_values = _fixture_material()
    for relative, data in fixture_values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        expected.add(relative)
    index = {"contract_revision": CONTRACT_REVISION, "fixtures": entries}
    values = {
        "openapi.json": _json_bytes(build_openapi()),
        "manifest.schema.json": _json_bytes(build_manifest_schema()),
        "fixtures/index.json": _json_bytes(index),
        "generated/world_access_client.ts": generated_client_source().encode("utf-8"),
    }
    for relative, data in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = _base_manifest()
    manifest["fixture_set"]["index_sha256"] = _sha256(values["fixtures/index.json"])
    manifest["artifacts"] = {
        "openapi_sha256": _sha256(values["openapi.json"]),
        "manifest_schema_sha256": _sha256(values["manifest.schema.json"]),
        "generated_client_sha256": _sha256(values["generated/world_access_client.ts"]),
    }
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(root).as_posix() not in expected:
            path.unlink()
    return {
        "contract_revision": CONTRACT_REVISION,
        "fixture_count": len(entries),
        "refreshed": True,
    }


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorldAccessContractError(f"invalid JSON artifact: {path}") from error


def _publication_timestamp(value: object) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 64:
        raise WorldAccessContractError("published-at must be a bounded ISO-8601 value")
    if (
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
            value,
        )
        is None
    ):
        raise WorldAccessContractError("published-at must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorldAccessContractError("published-at must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise WorldAccessContractError("published-at must include a timezone")
    return value


def _resolve_pointer(document: Any, pointer: str) -> Any:
    current = document
    for part in pointer.removeprefix("#/").split("/"):
        if not isinstance(current, dict) or part not in current:
            raise WorldAccessContractError(f"unresolved local reference: {pointer}")
        current = current[part]
    return current


def _validate_operation_surface(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or set(paths) != EXPECTED_PATHS:
        raise WorldAccessContractError("World Access route set is invalid")
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    actual_operations: set[tuple[str, str]] = set()
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            raise WorldAccessContractError("World Access path item is invalid")
        actual_methods = set(path_item) & methods
        actual_operations.update((method, path) for method in actual_methods)
        allowed_path_keys = actual_methods | (
            {"parameters"} if "parameters" in path_item else set()
        )
        if set(path_item) != allowed_path_keys:
            raise WorldAccessContractError(
                "World Access path item contains undeclared fields"
            )
    if actual_operations != set(EXPECTED_OPERATIONS):
        raise WorldAccessContractError("World Access operation set is invalid")

    promotion_path_parameter = {
        "name": "promotion_id",
        "in": "path",
        "required": True,
        "schema": _schema_ref("PromotionId"),
    }
    for (method, path), expected in EXPECTED_OPERATIONS.items():
        path_item = paths[path]
        expected_path_parameters = (
            [promotion_path_parameter] if expected["path_parameter"] else []
        )
        if path_item.get("parameters", []) != expected_path_parameters:
            raise WorldAccessContractError("World Access path parameters are invalid")
        operation = path_item.get(method)
        if not isinstance(operation, dict):
            raise WorldAccessContractError("World Access operation is invalid")
        if operation.get("operationId") != expected["operation_id"]:
            raise WorldAccessContractError("World Access operation identity is invalid")
        if operation.get("security") != [{"PrivateBearer": []}]:
            raise WorldAccessContractError("World Access bearer security is invalid")
        if operation.get("x-dreamland-actor-class") != expected["actor_class"]:
            raise WorldAccessContractError("World Access actor class is invalid")
        expected_parameters = [
            _parameter_ref(name) for name in expected["operation_parameters"]
        ]
        if operation.get("parameters") != expected_parameters:
            raise WorldAccessContractError(
                "World Access operation parameters are invalid"
            )

        request_schema = expected["request_schema"]
        if request_schema is None:
            if "requestBody" in operation:
                raise WorldAccessContractError("World Access GET body is undeclared")
        elif operation.get("requestBody") != {
            "required": True,
            "content": {"application/json": {"schema": _schema_ref(request_schema)}},
        }:
            raise WorldAccessContractError("World Access request schema is invalid")

        success_status = expected["success_status"]
        responses = operation.get("responses")
        if not isinstance(responses, dict) or set(responses) != {
            success_status,
            "default",
        }:
            raise WorldAccessContractError(
                "World Access response status set is invalid"
            )
        success = responses[success_status]
        problem = responses["default"]
        if not isinstance(success, dict) or success.get("content") != {
            "application/json": {"schema": _schema_ref(expected["response_schema"])}
        }:
            raise WorldAccessContractError("World Access success response is invalid")
        if not isinstance(problem, dict) or problem.get("content") != {
            "application/problem+json": {"schema": _schema_ref("SafeErrorEnvelope")}
        }:
            raise WorldAccessContractError("World Access problem response is invalid")


def validate_contract(root: Path) -> dict[str, Any]:
    try:
        import jsonschema
    except ImportError as error:
        raise WorldAccessContractError("jsonschema is required") from error
    if not root.is_dir():
        raise WorldAccessContractError("World Access contract root is unavailable")
    manifest = _load(root / "manifest.json")
    manifest_schema = _load(root / "manifest.schema.json")
    openapi = _load(root / "openapi.json")
    index = _load(root / "fixtures/index.json")
    expected_entries, expected_fixture_values = _fixture_material()
    canonical_values = {
        "openapi.json": _json_bytes(build_openapi()),
        "manifest.schema.json": _json_bytes(build_manifest_schema()),
        "fixtures/index.json": _json_bytes(
            {"contract_revision": CONTRACT_REVISION, "fixtures": expected_entries}
        ),
        "generated/world_access_client.ts": generated_client_source().encode("utf-8"),
        **expected_fixture_values,
    }
    for relative, expected_data in canonical_values.items():
        if (root / relative).read_bytes() != expected_data:
            raise WorldAccessContractError(
                f"World Access artifact is not canonical: {relative}"
            )
    jsonschema.Draft202012Validator.check_schema(manifest_schema)
    jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
    if manifest["publication_status"] == "published":
        _publication_timestamp(manifest["published_at"])
    _validate_operation_surface(openapi)
    if openapi != build_openapi():
        raise WorldAccessContractError("World Access OpenAPI artifact is not canonical")
    if (
        openapi.get("info", {}).get("version") != CONTRACT_REVISION
        or index.get("contract_revision") != CONTRACT_REVISION
    ):
        raise WorldAccessContractError("World Access revision evidence disagrees")
    artifacts = {
        "openapi_sha256": "openapi.json",
        "manifest_schema_sha256": "manifest.schema.json",
        "generated_client_sha256": "generated/world_access_client.ts",
    }
    for key, relative in artifacts.items():
        if manifest["artifacts"].get(key) != _sha256((root / relative).read_bytes()):
            raise WorldAccessContractError(f"artifact digest mismatch: {relative}")
    if manifest["fixture_set"].get("index_sha256") != _sha256(
        (root / "fixtures/index.json").read_bytes()
    ):
        raise WorldAccessContractError("fixture index digest mismatch")
    base_manifest = _base_manifest()
    if manifest["dependencies"] != base_manifest["dependencies"]:
        raise WorldAccessContractError("dependency pins changed")
    if (
        manifest["generation"] != base_manifest["generation"]
        or manifest["source"] != base_manifest["source"]
    ):
        raise WorldAccessContractError("generation or source evidence changed")
    expected_manifest = base_manifest
    expected_manifest["publication_status"] = manifest["publication_status"]
    expected_manifest["backend_commit"] = manifest["backend_commit"]
    expected_manifest["published_at"] = manifest["published_at"]
    expected_manifest["fixture_set"]["index_sha256"] = _sha256(
        canonical_values["fixtures/index.json"]
    )
    expected_manifest["artifacts"] = {
        "openapi_sha256": _sha256(canonical_values["openapi.json"]),
        "manifest_schema_sha256": _sha256(canonical_values["manifest.schema.json"]),
        "generated_client_sha256": _sha256(
            canonical_values["generated/world_access_client.ts"]
        ),
    }
    if manifest != expected_manifest:
        raise WorldAccessContractError("World Access manifest is not canonical")
    fixture_count = 0
    invalid_count = 0
    fixture_paths: set[str] = set()
    fixture_cases: set[str] = set()
    for entry in index.get("fixtures", []):
        relative = entry.get("path")
        target = entry.get("target")
        if not isinstance(relative, str) or not isinstance(target, str):
            raise WorldAccessContractError("fixture index entry is invalid")
        case = entry.get("case")
        if (
            not isinstance(case, str)
            or case in fixture_cases
            or relative in fixture_paths
        ):
            raise WorldAccessContractError("fixture index identity is invalid")
        fixture_cases.add(case)
        fixture_paths.add(relative)
        data = (root / relative).read_bytes()
        if entry.get("sha256") != _sha256(data):
            raise WorldAccessContractError(f"fixture digest mismatch: {relative}")
        schema = {**openapi, "$ref": target.removeprefix("openapi.json")}
        validator = jsonschema.Draft202012Validator(schema)
        payload = json.loads(data)
        errors = tuple(validator.iter_errors(payload))
        classification = entry.get("classification")
        if classification not in {
            "canonical-valid",
            "forward-compatibility",
            "invalid",
            "safe-error",
            "http-scenario",
        }:
            raise WorldAccessContractError("fixture classification is invalid")
        invalid = classification == "invalid"
        if invalid == (not errors):
            raise WorldAccessContractError(
                f"fixture classification failure: {relative}"
            )
        if classification == "http-scenario":
            if (
                entry.get("reachability") != "http"
                or entry.get("http_status") != payload.get("expected_http_status")
                or entry.get("operation_id") != payload.get("operation_id")
                or entry.get("request_actor_class")
                != payload.get("request_actor_class")
                or entry.get("required_actor_class")
                != payload.get("required_actor_class")
            ):
                raise WorldAccessContractError("HTTP scenario evidence is invalid")
        fixture_count += 1
        invalid_count += int(invalid)
    expected_fixtures = fixture_definitions()
    if fixture_cases != {fixture.case for fixture in expected_fixtures}:
        raise WorldAccessContractError("fixture case coverage is incomplete")
    expected_files = EXPECTED_FILES | {fixture.path for fixture in expected_fixtures}
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual_files != expected_files:
        raise WorldAccessContractError("World Access artifact file set is invalid")
    refs = []
    pending = [openapi]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref" and isinstance(child, str) and child.startswith("#/"):
                    refs.append(child)
                else:
                    pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    for pointer in refs:
        _resolve_pointer(openapi, pointer)
    if invalid_count < 3:
        raise WorldAccessContractError("invalid fixture coverage is incomplete")
    return {
        "contract_revision": CONTRACT_REVISION,
        "fixture_count": fixture_count,
        "valid": True,
    }


def client_smoke(
    root: Path,
    *,
    output: Path | None = None,
    node_executable: Path | str | None = None,
) -> dict[str, Any]:
    validated = validate_contract(root)
    source = (root / "generated/world_access_client.ts").read_text(encoding="utf-8")
    markers = (
        "identity(",
        "promote(",
        "promotion(",
        "reveal(",
        "catalog(",
        "exchange(",
    )
    if any(marker not in source for marker in markers):
        raise WorldAccessContractError("generated World Access client is incomplete")
    for marker in (
        "server-only",
        "Authorization",
        "X-Dreamland-Expected-Epoch",
        "X-Dreamland-Actor-Context",
        "Idempotency-Key",
        'Accept: "application/json, application/problem+json"',
        "response Content-Type must be",
        "requirePreviewLoopbackBaseUrl",
        'parsed.protocol !== "http:"',
        'parsed.hostname === "127.0.0.1"',
        'parsed.hostname === "localhost"',
        'parsed.username !== ""',
        'parsed.pathname !== "/"',
        "return parsed.origin",
    ):
        if marker not in source:
            raise WorldAccessContractError(
                "generated World Access client boundary is incomplete"
            )
    node = str(node_executable) if node_executable is not None else shutil.which("node")
    if node is None:
        raise WorldAccessContractError(
            "Node.js is required for generated World Access client compilation"
        )
    tool_root = Path(__file__).resolve().parent / "world_access_client_smoke"
    harness = tool_root / "run-smoke.mjs"
    typescript = tool_root / "node_modules/typescript/package.json"
    if not harness.is_file() or not typescript.is_file():
        raise WorldAccessContractError(
            "World Access client toolchain is unavailable; run npm ci --prefix "
            "tools/world_access_client_smoke"
        )
    success_fixture = root / "fixtures/canonical-valid/identity.json"
    selection_fixture = root / "fixtures/canonical-valid/generated-grant.json"
    problem_fixture = root / "fixtures/safe-errors/idempotency_conflict.json"
    try:
        completed = subprocess.run(
            [
                node,
                str(harness),
                "--client",
                str(root / "generated/world_access_client.ts"),
                "--success-fixture",
                str(success_fixture),
                "--selection-fixture",
                str(selection_fixture),
                "--problem-fixture",
                str(problem_fixture),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorldAccessContractError(
            "generated World Access client compilation could not complete"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        raise WorldAccessContractError(
            "generated World Access client compilation failed"
            + (f": {detail}" if detail else "")
        )
    try:
        evidence = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorldAccessContractError(
            "generated World Access client evidence is invalid"
        ) from error
    expected_evidence = {
        "compiled": True,
        "content_type_mismatch_rejected": True,
        "launch_binding_sha256_consumed": True,
        "launch_selection_fields_consumed": True,
        "missing_content_type_rejected": True,
        "operations": 6,
        "transport": "bounded_fake_fetch",
        "typescript": "6.0.3",
    }
    if not isinstance(evidence, dict):
        raise WorldAccessContractError(
            "generated World Access client evidence is incomplete"
        )
    node_major = evidence.pop("node_major", None)
    if (
        not isinstance(node_major, int)
        or node_major < 20
        or evidence != expected_evidence
    ):
        raise WorldAccessContractError(
            "generated World Access client evidence is incomplete"
        )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "generated/world_access_client.ts", output)
    return {
        "contract_revision": validated["contract_revision"],
        "operations": len(markers),
        "compiled": True,
        "content_type_mismatch_rejected": True,
        "launch_binding_sha256_consumed": True,
        "launch_selection_fields_consumed": True,
        "missing_content_type_rejected": True,
        "node_major": node_major,
        "transport": "bounded_fake_fetch",
        "typescript": "6.0.3",
        "smoke": True,
    }


def _repository_root(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorldAccessContractError("contract root is not inside a Git repository")
    repository = Path(result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise WorldAccessContractError(
            "contract root is outside its Git repository"
        ) from error
    return repository


def _verify_publication_commit(root: Path, backend_commit: str) -> None:
    repository = _repository_root(root)
    commit = subprocess.run(
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
    if commit.returncode != 0:
        raise WorldAccessContractError("backend-commit does not name a Git commit")
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip() != backend_commit:
        raise WorldAccessContractError("backend-commit must be the current HEAD")
    ancestor = subprocess.run(
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
    if ancestor.returncode != 0:
        raise WorldAccessContractError(
            "backend-commit is not reachable from the current HEAD"
        )
    expected_files = EXPECTED_FILES | {
        fixture.path for fixture in fixture_definitions()
    }
    for relative in sorted(expected_files):
        path = root / relative
        repository_path = path.relative_to(repository).as_posix()
        blob = subprocess.run(
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
        if blob.returncode != 0 or blob.stdout != path.read_bytes():
            raise WorldAccessContractError(
                "backend-commit does not contain the exact World Access artifact: "
                + relative
            )
    for relative in sorted(PUBLICATION_IMPLEMENTATION_FILES):
        path = repository / relative
        blob = subprocess.run(
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
            not path.is_file()
            or blob.returncode != 0
            or blob.stdout != path.read_bytes()
        ):
            raise WorldAccessContractError(
                "backend-commit does not contain the exact World Access implementation: "
                + relative
            )


def publish_contract(
    root: Path,
    *,
    backend_commit: str,
    published_at: str,
    node_executable: Path | str | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{40}", backend_commit) is None:
        raise WorldAccessContractError("backend commit must be a full lowercase SHA-1")
    published_at = _publication_timestamp(published_at)
    root = root.resolve()
    validate_contract(root)
    manifest = _load(root / "manifest.json")
    if (
        manifest["publication_status"] != "draft"
        or manifest["backend_commit"] is not None
        or manifest["published_at"] is not None
    ):
        raise WorldAccessContractError("only a draft World Access RC may be published")
    _verify_publication_commit(root, backend_commit)
    client_smoke(root, node_executable=node_executable)
    manifest["publication_status"] = "published"
    manifest["backend_commit"] = backend_commit
    manifest["published_at"] = published_at
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    validate_contract(root)
    return {"contract_revision": CONTRACT_REVISION, "published": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the private World Access contract kit."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "validate", "client-smoke"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "client-smoke":
            command.add_argument("--output", type=Path)
            command.add_argument("--node", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("root", type=Path)
    publish.add_argument("--backend-commit", required=True)
    publish.add_argument("--published-at", required=True)
    publish.add_argument("--node", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "refresh":
            result = refresh_contract(arguments.root)
        elif arguments.command == "validate":
            result = validate_contract(arguments.root)
        elif arguments.command == "client-smoke":
            result = client_smoke(
                arguments.root,
                output=arguments.output,
                node_executable=arguments.node,
            )
        elif arguments.command == "publish":
            result = publish_contract(
                arguments.root,
                backend_commit=arguments.backend_commit,
                published_at=arguments.published_at,
                node_executable=arguments.node,
            )
        else:
            raise WorldAccessContractError("unsupported command")
    except (OSError, ValueError, WorldAccessContractError) as error:
        print(f"World Access contract error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
