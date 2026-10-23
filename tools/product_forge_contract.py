#!/usr/bin/env python3
"""Build and validate the separate World Forge integration-preview contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CONTRACT_NAME = "dreamland-private-world-forge-api"
CONTRACT_REVISION = "0.1.0-rc.4"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
EXPECTED_FILES = {
    "openapi.json",
    "events.schema.json",
    "manifest.json",
    "manifest.schema.json",
    "fixtures/index.json",
    "generated/world_forge_client.ts",
}
ERROR_CODES = (
    "invalid_request",
    "recipe_unknown",
    "recipe_revision_mismatch",
    "validation_failed",
    "preparation_expired",
    "preparation_consumed",
    "preparation_not_found",
    "preparation_capacity_exhausted",
    "authority_denied",
    "service_epoch_changed",
    "capacity_exhausted",
    "idempotency_capacity_exhausted",
    "idempotency_conflict",
    "idempotency_key_invalid",
    "job_not_found",
    "job_terminal",
    "resync_required",
    "request_too_large",
    "response_too_large",
    "unsupported_media_type",
    "temporarily_unavailable",
)
ERROR_REACHABILITY = {
    code: (
        "schema-only"
        if code in {"response_too_large", "temporarily_unavailable"}
        else "http"
    )
    for code in ERROR_CODES
}
ERROR_REACHABILITY_DETAILS = {
    **{
        code: "Served by a current provider-free preview HTTP condition."
        for code, reachability in ERROR_REACHABILITY.items()
        if reachability == "http"
    },
    "response_too_large": (
        "Schema-only defensive serializer guard; current bounded responses do not reach it."
    ),
    "temporarily_unavailable": (
        "Schema-only future availability case; the current preview has no emitter."
    ),
}
LIFECYCLE_STATES = (
    "accepted",
    "queued",
    "building",
    "generated",
    "validating",
    "selected",
    "failed",
    "cancelled",
    "deadline_exceeded",
)


class ForgeContractError(ValueError):
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
    reachability_detail: str | None = None


def _string(
    *, minimum: int = 1, maximum: int = 512, pattern: str | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "string",
        "minLength": minimum,
        "maxLength": maximum,
    }
    if pattern is not None:
        value["pattern"] = pattern
    return value


def _object(
    properties: dict[str, Any], required: tuple[str, ...], *, additive: bool = False
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additive,
    }


def _safe_text() -> dict[str, Any]:
    return _object(
        {
            "text": _string(minimum=0, maximum=1024),
            "clipped": {"type": "boolean"},
            "controls_marked": {"type": "boolean"},
        },
        ("text", "clipped", "controls_marked"),
    )


def build_openapi() -> dict[str, Any]:
    schemas = _schemas()
    security = [{"privateBearer": []}]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DREAMLAND Private World Forge Integration Preview",
            "version": CONTRACT_REVISION,
            "description": (
                "Provider-free, loopback-only World Forge browser-integration preview. "
                "It provides no Stage, publication, catalog, attendee, or live-Forge authority."
            ),
        },
        "servers": [
            {"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "0"}}}
        ],
        "security": security,
        "paths": {
            "/forge/v1/identity": {
                "get": _operation("getForgeIdentity", "ForgeIdentity", actor=False)
            },
            "/forge/v1/recipes": {
                "get": _operation("listForgeRecipes", "RecipeListResponse")
            },
            "/forge/v1/preparations": {
                "post": _operation(
                    "prepareForgeSeed",
                    "PreparationResponse",
                    request="PrepareSeedRequest",
                    created=True,
                )
            },
            "/forge/v1/builds": {
                "post": _operation(
                    "confirmAndSubmitForgeBuild",
                    "SubmitBuildResponse",
                    request="SubmitBuildRequest",
                    idempotent=True,
                    created=True,
                )
            },
            "/forge/v1/builds/{reference}": {
                "get": _operation("getForgeBuild", "BuildProjection", reference=True)
            },
            "/forge/v1/builds/{reference}/events": {
                "get": _operation(
                    "getForgeBuildEvents", "EventPage", reference=True, cursor=True
                )
            },
            "/forge/v1/builds/{reference}/cancel": {
                "post": _operation(
                    "cancelForgeBuild",
                    "CancelBuildResponse",
                    request="CancelBuildRequest",
                    reference=True,
                    idempotent=True,
                )
            },
        },
        "components": {
            "securitySchemes": {
                "privateBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque",
                }
            },
            "parameters": _parameters(),
            "schemas": schemas,
        },
        "x-dreamland-world-forge": {
            "contract_name": CONTRACT_NAME,
            "integration_preview": True,
            "provider_calls": False,
            "live_forge_evidence": False,
            "stage_authority": False,
            "publication_authority": False,
            "normal_runtime_composition": False,
            "events_require_snapshot_reconciliation": True,
        },
    }


def _parameters() -> dict[str, Any]:
    def header(name: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "in": "header",
            "required": True,
            "schema": schema,
        }

    return {
        "ExpectedEpoch": header("X-Dreamland-Expected-Epoch", _string(maximum=160)),
        "ActorContext": header("X-Dreamland-Actor-Context", _string(maximum=256)),
        "IdempotencyKey": header("Idempotency-Key", _string(maximum=128)),
        "Reference": {
            "name": "reference",
            "in": "path",
            "required": True,
            "schema": _string(maximum=256),
        },
        "AfterCursor": {
            "name": "after",
            "in": "query",
            "required": True,
            "schema": _string(maximum=256),
        },
    }


def _operation(
    operation_id: str,
    response: str,
    *,
    request: str | None = None,
    reference: bool = False,
    cursor: bool = False,
    idempotent: bool = False,
    created: bool = False,
    actor: bool = True,
) -> dict[str, Any]:
    parameters = []
    if actor:
        parameters.extend(
            [
                {"$ref": "#/components/parameters/ExpectedEpoch"},
                {"$ref": "#/components/parameters/ActorContext"},
            ]
        )
    if idempotent:
        parameters.append({"$ref": "#/components/parameters/IdempotencyKey"})
    if reference:
        parameters.append({"$ref": "#/components/parameters/Reference"})
    if cursor:
        parameters.append({"$ref": "#/components/parameters/AfterCursor"})
    value: dict[str, Any] = {
        "operationId": operation_id,
        "parameters": parameters,
        "responses": {
            str(201 if created else 200): {
                "description": "Success",
                "headers": {
                    "Cache-Control": {
                        "schema": {"type": "string", "const": "no-store"}
                    },
                    "X-Content-Type-Options": {
                        "schema": {"type": "string", "const": "nosniff"}
                    },
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{response}"}
                    }
                },
            },
            "default": {
                "description": "Safe failure",
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/SafeErrorEnvelope"}
                    }
                },
            },
        },
        "x-dreamland-private-server-only": True,
    }
    if request is not None:
        value["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{request}"}
                }
            },
        }
    return value


def _schemas() -> dict[str, Any]:
    token = _string(maximum=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    timestamp = _string(maximum=40, pattern=r"^\d{4}-\d{2}-\d{2}T")
    safe_text = _safe_text()
    answer = _object(
        {
            "field_id": _string(maximum=64, pattern=r"^[a-z][a-z0-9-]*$"),
            "kind": {"type": "string", "enum": ["short-text", "integer"]},
            "value": _string(minimum=1, maximum=160),
        },
        ("field_id", "kind", "value"),
    )
    preview_source = _object(
        {
            "kind": {"type": "string", "const": "provider_free_integration_preview"},
            "label": {
                "type": "string",
                "const": "Deterministic no-provider World Forge backend integration preview",
            },
            "provider_calls": {"type": "boolean", "const": False},
            "live_forge_evidence": {"type": "boolean", "const": False},
        },
        ("kind", "label", "provider_calls", "live_forge_evidence"),
    )
    live_source = _object(
        {
            "kind": {"type": "string", "const": "single_attempt_live_vertical_slice"},
            "label": {
                "type": "string",
                "const": "Single-attempt live World Forge vertical slice",
            },
            "provider_calls": {"type": "boolean", "const": True},
            "live_forge_evidence": {"type": "boolean", "const": True},
            "compiler_revision": {"type": "string", "const": "1"},
        },
        (
            "kind",
            "label",
            "provider_calls",
            "live_forge_evidence",
            "compiler_revision",
        ),
    )
    source = {"oneOf": [preview_source, live_source]}
    position = _object(
        {
            "service_epoch": token,
            "stream_id": token,
            "last_event_id": {"oneOf": [token, {"type": "null"}]},
            "sequence": {"type": "integer", "minimum": 0},
            "cursor": token,
        },
        ("service_epoch", "stream_id", "last_event_id", "sequence", "cursor"),
    )
    seed_properties = {
        "kind": {"type": "string", "const": "dreamland.world_seed"},
        "schema_revision": {"type": "string", "const": "1"},
        "recipe_id": {"type": "string", "const": "world-recipe:bsides-room:v1"},
        "recipe_revision": {"type": "string", "const": "1"},
        "answers": {"type": "array", "items": answer, "minItems": 8, "maxItems": 8},
        "rendered_sentence": _string(maximum=768),
        "confirmation_state": {"type": "string", "const": "confirmed"},
        "confirmed_at": timestamp,
        "seed_hash": _string(maximum=71, pattern=r"^sha256:[a-f0-9]{64}$"),
    }
    preparation_properties = {
        key: value
        for key, value in seed_properties.items()
        if key not in {"confirmed_at", "seed_hash"}
    }
    preparation_properties.update(
        {
            "kind": {"type": "string", "const": "dreamland.world_seed_preparation"},
            "confirmation_state": {"type": "string", "const": "unconfirmed"},
            "preparation_digest": _string(maximum=71, pattern=r"^sha256:[a-f0-9]{64}$"),
        }
    )
    display_answer = _object(
        {
            "field_id": answer["properties"]["field_id"],
            "kind": answer["properties"]["kind"],
            "value": safe_text,
        },
        ("field_id", "kind", "value"),
    )
    receipt = _object(
        {
            "validation": _object(
                {
                    "state": {"type": "string", "const": "passed"},
                    "safe_summary": _string(maximum=512),
                    "validator_revision": _string(maximum=64),
                },
                ("state", "safe_summary"),
            ),
            "fixed_selection_reason": _string(maximum=512),
            "revision": _object(
                {
                    "revision_id": token,
                    "revision_label": _string(maximum=128),
                    "immutable_digest": _string(
                        maximum=71, pattern=r"^sha256:[a-f0-9]{64}$"
                    ),
                },
                ("revision_id", "revision_label", "immutable_digest"),
            ),
            "human_selection": {"type": "string", "const": "none"},
            "provenance": source,
        },
        (
            "validation",
            "fixed_selection_reason",
            "revision",
            "human_selection",
            "provenance",
        ),
    )
    lifecycle = _object(
        {
            "state": {"type": "string", "enum": list(LIFECYCLE_STATES)},
            "changed_at": timestamp,
            "safe_message": _string(maximum=512),
        },
        ("state", "changed_at"),
    )
    lifecycle["allOf"] = [
        {
            "if": {
                "properties": {
                    "state": {"enum": ["failed", "cancelled", "deadline_exceeded"]}
                },
                "required": ["state"],
            },
            "then": {"required": ["safe_message"]},
        }
    ]
    cancellation = {
        "oneOf": [
            _object({"state": {"type": "string", "const": "available"}}, ("state",)),
            _object(
                {
                    "state": {"type": "string", "const": "unavailable"},
                    "safe_reason": _string(maximum=512),
                },
                ("state", "safe_reason"),
            ),
        ]
    }
    recipe_field = _object(
        {
            "field_id": answer["properties"]["field_id"],
            "kind": answer["properties"]["kind"],
            "label": safe_text,
            "prompt": safe_text,
            "minimum": {"type": "integer"},
            "maximum": {"type": "integer"},
            "minimum_characters": {"type": "integer", "minimum": 0},
            "maximum_characters": {"type": "integer", "minimum": 1},
            "maximum_input_characters": {"type": "integer", "minimum": 1},
            "accepted_pattern": _string(maximum=128),
        },
        ("field_id", "kind", "label", "prompt"),
    )
    recipe_field["oneOf"] = [
        {
            "properties": {"kind": {"const": "short-text"}},
            "required": ["minimum_characters", "maximum_characters"],
            "not": {
                "anyOf": [
                    {"required": [name]}
                    for name in (
                        "minimum",
                        "maximum",
                        "maximum_input_characters",
                        "accepted_pattern",
                    )
                ]
            },
        },
        {
            "properties": {"kind": {"const": "integer"}},
            "required": [
                "minimum",
                "maximum",
                "maximum_input_characters",
                "accepted_pattern",
            ],
            "not": {
                "anyOf": [
                    {"required": ["minimum_characters"]},
                    {"required": ["maximum_characters"]},
                ]
            },
        },
    ]
    build_projection = _object(
        {
            "kind": {"type": "string", "const": "forge_job_projection"},
            "service_epoch": token,
            "job_id": token,
            "reference": token,
            "seed": {"$ref": "#/components/schemas/WorldSeed"},
            "lifecycle": lifecycle,
            "deadline_at": timestamp,
            "position": position,
            "terminal": {"type": "boolean"},
            "receipt": {"oneOf": [receipt, {"type": "null"}]},
            "cancellation": cancellation,
            "source": source,
        },
        (
            "kind",
            "service_epoch",
            "job_id",
            "reference",
            "seed",
            "lifecycle",
            "deadline_at",
            "position",
            "terminal",
            "receipt",
            "cancellation",
            "source",
        ),
        additive=True,
    )
    build_projection["allOf"] = [
        {
            "if": {
                "properties": {
                    "lifecycle": {
                        "properties": {"state": {"const": "selected"}},
                        "required": ["state"],
                    }
                },
                "required": ["lifecycle"],
            },
            "then": {"properties": {"receipt": receipt}},
            "else": {"properties": {"receipt": {"type": "null"}}},
        },
        {
            "if": {
                "properties": {
                    "lifecycle": {
                        "properties": {
                            "state": {
                                "enum": [
                                    "selected",
                                    "failed",
                                    "cancelled",
                                    "deadline_exceeded",
                                ]
                            }
                        },
                        "required": ["state"],
                    }
                },
                "required": ["lifecycle"],
            },
            "then": {
                "properties": {
                    "terminal": {"const": True},
                    "cancellation": {
                        "properties": {"state": {"const": "unavailable"}},
                        "required": ["state"],
                    },
                }
            },
            "else": {
                "properties": {
                    "terminal": {"const": False},
                    "cancellation": {
                        "properties": {"state": {"const": "available"}},
                        "required": ["state"],
                    },
                }
            },
        },
        {
            "if": {
                "properties": {
                    "source": {
                        "properties": {
                            "kind": {"const": "provider_free_integration_preview"}
                        },
                        "required": ["kind"],
                    }
                },
                "required": ["source"],
            },
            "then": {
                "properties": {
                    "receipt": {
                        "properties": {
                            "provenance": {
                                "properties": {
                                    "kind": {
                                        "const": "provider_free_integration_preview"
                                    }
                                },
                                "required": ["kind"],
                            }
                        }
                    }
                }
            },
            "else": {
                "properties": {
                    "receipt": {
                        "properties": {
                            "provenance": {
                                "properties": {
                                    "kind": {
                                        "const": "single_attempt_live_vertical_slice"
                                    }
                                },
                                "required": ["kind"],
                            }
                        }
                    }
                }
            },
        },
    ]
    schemas: dict[str, Any] = {
        "SafeDisplayText": safe_text,
        "ForgeSource": source,
        "EventPosition": position,
        "WorldSeedAnswer": answer,
        "WorldSeed": _object(seed_properties, tuple(seed_properties)),
        "WorldSeedPreparation": _object(
            preparation_properties, tuple(preparation_properties)
        ),
        "PreparationDisplay": _object(
            {
                "answers": {
                    "type": "array",
                    "items": display_answer,
                    "minItems": 8,
                    "maxItems": 8,
                },
                "rendered_sentence": safe_text,
            },
            ("answers", "rendered_sentence"),
        ),
        "ForgeIdentity": _object(
            {
                "kind": {"type": "string", "const": "forge_identity"},
                "contract_name": {"type": "string", "const": CONTRACT_NAME},
                "contract_revision": {"type": "string", "const": CONTRACT_REVISION},
                "service_epoch": token,
                "source": source,
            },
            ("kind", "contract_name", "contract_revision", "service_epoch", "source"),
        ),
        "RecipeField": recipe_field,
        "Recipe": _object(
            {
                "kind": {"type": "string", "const": "forge_recipe"},
                "recipe_id": token,
                "recipe_revision": _string(maximum=64),
                "title": safe_text,
                "description": safe_text,
                "fields": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/RecipeField"},
                    "minItems": 1,
                    "maxItems": 16,
                },
                "preparation": _object(
                    {
                        "state": {
                            "type": "string",
                            "enum": ["available", "unavailable"],
                        },
                        "safe_reason": _string(maximum=512),
                    },
                    ("state",),
                ),
            },
            (
                "kind",
                "recipe_id",
                "recipe_revision",
                "title",
                "description",
                "fields",
                "preparation",
            ),
        ),
        "RecipeListResponse": _object(
            {
                "kind": {"type": "string", "const": "forge_recipes"},
                "service_epoch": token,
                "recipes": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/Recipe"},
                    "maxItems": 16,
                },
                "source": source,
            },
            ("kind", "service_epoch", "recipes", "source"),
        ),
        "PrepareSeedRequest": _object(
            {
                "recipe_id": token,
                "recipe_revision": _string(maximum=64),
                "answers": {
                    "type": "array",
                    "items": answer,
                    "minItems": 1,
                    "maxItems": 16,
                },
            },
            ("recipe_id", "recipe_revision", "answers"),
        ),
        "PreparationResponse": _object(
            {
                "kind": {"type": "string", "const": "forge_seed_prepared"},
                "service_epoch": token,
                "preparation_reference": token,
                "prepared_at": timestamp,
                "expires_at": timestamp,
                "preparation": {"$ref": "#/components/schemas/WorldSeedPreparation"},
                "display": {"$ref": "#/components/schemas/PreparationDisplay"},
                "source": source,
            },
            (
                "kind",
                "service_epoch",
                "preparation_reference",
                "prepared_at",
                "expires_at",
                "preparation",
                "display",
                "source",
            ),
        ),
        "SubmitBuildRequest": _object(
            {"preparation_reference": token}, ("preparation_reference",)
        ),
        "SubmitBuildResponse": _object(
            {
                "kind": {"type": "string", "const": "forge_job_accepted"},
                "service_epoch": token,
                "job_id": token,
                "reference": token,
                "position": position,
                "deadline_at": timestamp,
                "replayed": {"type": "boolean"},
                "source": source,
            },
            (
                "kind",
                "service_epoch",
                "job_id",
                "reference",
                "position",
                "deadline_at",
                "replayed",
                "source",
            ),
        ),
        "BuildProjection": build_projection,
        "ForgeEvent": _object(
            {
                "stream_id": token,
                "event_id": token,
                "sequence": {"type": "integer", "minimum": 1},
                "cursor": token,
                "job_id": token,
                "state": {"type": "string", "enum": list(LIFECYCLE_STATES)},
                "changed_at": timestamp,
                "terminal": {"type": "boolean"},
                "service_epoch": token,
                "requires_snapshot_reconciliation": {"type": "boolean", "const": True},
            },
            (
                "stream_id",
                "event_id",
                "sequence",
                "cursor",
                "job_id",
                "state",
                "changed_at",
                "terminal",
                "service_epoch",
                "requires_snapshot_reconciliation",
            ),
        ),
        "EventPage": _object(
            {
                "kind": {"type": "string", "const": "forge_events"},
                "service_epoch": token,
                "stream_id": token,
                "events": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ForgeEvent"},
                    "maxItems": 32,
                },
                "position": position,
                "terminal": {"type": "boolean"},
                "page_complete": {"type": "boolean"},
                "requires_snapshot_reconciliation": {"type": "boolean", "const": True},
                "source": source,
            },
            (
                "kind",
                "service_epoch",
                "stream_id",
                "events",
                "position",
                "terminal",
                "page_complete",
                "requires_snapshot_reconciliation",
                "source",
            ),
        ),
        "CancelBuildRequest": _object({}, ()),
        "CancelBuildResponse": _object(
            {
                "kind": {"type": "string", "const": "forge_job_cancelled"},
                "service_epoch": token,
                "job_id": token,
                "position": position,
                "replayed": {"type": "boolean"},
                "source": source,
            },
            ("kind", "service_epoch", "job_id", "position", "replayed", "source"),
        ),
        "ValidationIssue": _object(
            {
                "code": _string(maximum=64),
                "message": _string(maximum=512),
                "field_id": _string(maximum=64),
            },
            ("code", "message"),
        ),
        "SafeErrorEnvelope": _object(
            {
                "kind": {"type": "string", "const": "forge_error"},
                "error": _object(
                    {
                        "code": {"type": "string", "enum": list(ERROR_CODES)},
                        "message": _string(maximum=512),
                        "retry": {
                            "type": "string",
                            "enum": ["none", "resume", "same_request"],
                        },
                        "retry_after_ms": {
                            "oneOf": [
                                {"type": "integer", "minimum": 0, "maximum": 60000},
                                {"type": "null"},
                            ]
                        },
                        "issues": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ValidationIssue"},
                            "maxItems": 16,
                        },
                    },
                    ("code", "message", "retry", "retry_after_ms"),
                ),
                "request_id": token,
            },
            ("kind", "error", "request_id"),
        ),
    }
    return schemas


def build_events_schema() -> dict[str, Any]:
    openapi = build_openapi()
    page = json.loads(json.dumps(openapi["components"]["schemas"]["EventPage"]))
    page["properties"]["events"]["items"]["$ref"] = "#/$defs/ForgeEvent"
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://dreamland.invalid/contracts/world-forge/{CONTRACT_REVISION}/events.schema.json",
        "title": "DREAMLAND World Forge integration-preview event page",
        "$defs": {
            "ForgeEvent": openapi["components"]["schemas"]["ForgeEvent"],
            "EventPosition": openapi["components"]["schemas"]["EventPosition"],
        },
        **page,
    }


def _preview_source_fixture() -> dict[str, Any]:
    return {
        "kind": "provider_free_integration_preview",
        "label": "Deterministic no-provider World Forge backend integration preview",
        "provider_calls": False,
        "live_forge_evidence": False,
    }


def _answers_fixture() -> list[dict[str, str]]:
    values = (
        ("day-or-date", "short-text", "Saturday"),
        ("year", "integer", "+02026"),
        ("adjective", "short-text", "weathered"),
        ("industry-or-setting", "short-text", "observatory"),
        ("technology-one", "short-text", "VAX terminals"),
        ("technology-two", "short-text", "radio clocks"),
        ("important-job", "short-text", "track a comet"),
        ("wildcard-event", "short-text", "the clocks disagree"),
    )
    return [
        {"field_id": field, "kind": kind, "value": value}
        for field, kind, value in values
    ]


def fixture_definitions() -> tuple[Fixture, ...]:
    from datetime import UTC, datetime, timedelta

    from dreamland.product.forge import (
        ForgeActorSeed,
        ForgeJobState,
        WorldForgeService,
    )
    from dreamland.product.forge_artifacts import (
        live_forge_receipt,
        live_forge_source,
    )

    class Tokens:
        def __init__(self) -> None:
            self.value = 0

        def __call__(self, purpose: str) -> str:
            self.value += 1
            return f"{purpose}:fixture-{self.value:08d}"

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    authority = "actor:forge-fixture-presenter"
    service = WorldForgeService(
        actors=(
            ForgeActorSeed(
                authority, "presenter-fixture", (now + timedelta(hours=1)).isoformat()
            ),
        ),
        clock=lambda: now,
        token_factory=Tokens(),
        source=_preview_source_fixture(),
    )
    recipe = service.recipe()
    recipes = {
        "kind": "forge_recipes",
        "service_epoch": service.service_epoch,
        "recipes": [recipe],
        "source": _preview_source_fixture(),
    }
    prepared = service.prepare(
        actor_authority=authority,
        expected_epoch=service.service_epoch,
        recipe_id="world-recipe:bsides-room:v1",
        recipe_revision="1",
        answers=_answers_fixture(),
    )
    accepted = service.confirm_and_submit(
        actor_authority=authority,
        expected_epoch=service.service_epoch,
        idempotency_key="fixture-submit",
        preparation_reference=str(prepared["preparation_reference"]),
    )
    reference = str(accepted["reference"])
    projections: dict[str, dict[str, Any]] = {
        "accepted": service.status(
            actor_authority=authority,
            expected_epoch=service.service_epoch,
            reference=reference,
        )
    }
    for state in (
        ForgeJobState.QUEUED,
        ForgeJobState.BUILDING,
        ForgeJobState.GENERATED,
        ForgeJobState.VALIDATING,
        ForgeJobState.SELECTED,
    ):
        projections[state.value] = service.preview_transition(
            reference=reference, state=state
        )
    cancelled_result: dict[str, Any] | None = None
    for terminal in (
        ForgeJobState.FAILED,
        ForgeJobState.CANCELLED,
        ForgeJobState.DEADLINE_EXCEEDED,
    ):
        terminal_prepared = service.prepare(
            actor_authority=authority,
            expected_epoch=service.service_epoch,
            recipe_id="world-recipe:bsides-room:v1",
            recipe_revision="1",
            answers=_answers_fixture(),
        )
        terminal_accepted = service.confirm_and_submit(
            actor_authority=authority,
            expected_epoch=service.service_epoch,
            idempotency_key=f"fixture-{terminal.value}",
            preparation_reference=str(terminal_prepared["preparation_reference"]),
        )
        terminal_reference = str(terminal_accepted["reference"])
        if terminal is ForgeJobState.CANCELLED:
            cancelled_result = service.cancel(
                actor_authority=authority,
                expected_epoch=service.service_epoch,
                reference=terminal_reference,
                idempotency_key="fixture-cancel",
            )
            projections[terminal.value] = service.status(
                actor_authority=authority,
                expected_epoch=service.service_epoch,
                reference=terminal_reference,
            )
        else:
            service.preview_transition(
                reference=terminal_reference, state=ForgeJobState.QUEUED
            )
            service.preview_transition(
                reference=terminal_reference, state=ForgeJobState.BUILDING
            )
            if terminal is ForgeJobState.DEADLINE_EXCEEDED:
                now += timedelta(seconds=121)
            projections[terminal.value] = service.preview_transition(
                reference=terminal_reference, state=terminal
            )
    events = service.events(
        actor_authority=authority,
        expected_epoch=service.service_epoch,
        reference=reference,
        after_cursor=str(accepted["position"]["cursor"]),
    )
    fixtures = [
        Fixture(
            "recipes",
            "canonical-valid",
            "fixtures/canonical-valid/recipes.json",
            "openapi.json#/components/schemas/RecipeListResponse",
            recipes,
            "render_recipe",
        ),
        Fixture(
            "prepare_request",
            "canonical-valid",
            "fixtures/canonical-valid/prepare-request.json",
            "openapi.json#/components/schemas/PrepareSeedRequest",
            {
                "recipe_id": "world-recipe:bsides-room:v1",
                "recipe_revision": "1",
                "answers": _answers_fixture(),
            },
            "prepare_review",
        ),
        Fixture(
            "prepared",
            "canonical-valid",
            "fixtures/canonical-valid/prepared.json",
            "openapi.json#/components/schemas/PreparationResponse",
            prepared,
            "show_unconfirmed_review",
        ),
        Fixture(
            "submit_request",
            "canonical-valid",
            "fixtures/canonical-valid/submit-request.json",
            "openapi.json#/components/schemas/SubmitBuildRequest",
            {"preparation_reference": prepared["preparation_reference"]},
            "confirm_and_submit",
        ),
        Fixture(
            "accepted_result",
            "canonical-valid",
            "fixtures/canonical-valid/accepted-result.json",
            "openapi.json#/components/schemas/SubmitBuildResponse",
            accepted,
            "store_reference_and_position",
        ),
        Fixture(
            "events",
            "canonical-valid",
            "fixtures/canonical-valid/events.json",
            "openapi.json#/components/schemas/EventPage",
            events,
            "reconcile_snapshot",
        ),
        Fixture(
            "cancel_request",
            "canonical-valid",
            "fixtures/canonical-valid/cancel-request.json",
            "openapi.json#/components/schemas/CancelBuildRequest",
            {},
            "cancel_active_build",
        ),
        Fixture(
            "cancel_result",
            "canonical-valid",
            "fixtures/canonical-valid/cancel-result.json",
            "openapi.json#/components/schemas/CancelBuildResponse",
            cancelled_result or {},
            "reconcile_cancelled_build",
        ),
    ]
    for state, projection in projections.items():
        fixtures.append(
            Fixture(
                f"projection_{state}",
                "canonical-valid",
                f"fixtures/canonical-valid/lifecycle/{state}.json",
                "openapi.json#/components/schemas/BuildProjection",
                projection,
                "render_projection",
            )
        )
    live_selected = json.loads(json.dumps(projections["selected"]))
    live_source = live_forge_source()
    digest = "sha256:" + "a" * 64
    live_selected["source"] = live_source
    live_selected["receipt"] = live_forge_receipt(
        compiler_revision="1",
        validator_revision="1",
        revision_id="world-revision:" + digest,
        digest=digest,
    )
    fixtures.append(
        Fixture(
            "projection_selected_live_vertical_slice",
            "canonical-valid",
            "fixtures/canonical-valid/lifecycle/selected-live-vertical-slice.json",
            "openapi.json#/components/schemas/BuildProjection",
            live_selected,
            "render_live_vertical_slice_receipt",
        )
    )
    for code in ERROR_CODES:
        retry = (
            "same_request"
            if code == "capacity_exhausted"
            else "resume"
            if code == "resync_required"
            else "none"
        )
        payload = {
            "kind": "forge_error",
            "error": {
                "code": code,
                "message": f"Safe World Forge fixture for {code.replace('_', ' ')}.",
                "retry": retry,
                "retry_after_ms": 250 if code == "capacity_exhausted" else None,
            },
            "request_id": f"request:fixture-{code}",
        }
        if code == "validation_failed":
            payload["error"]["issues"] = [
                {
                    "code": "text_length_invalid",
                    "message": "The answer is outside the configured character limits.",
                    "field_id": "adjective",
                }
            ]
        fixtures.append(
            Fixture(
                f"error_{code}",
                "safe-error",
                f"fixtures/safe-errors/{code}.json",
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                payload,
                "handle_safe_error",
                ERROR_REACHABILITY[code],
                ERROR_REACHABILITY_DETAILS[code],
            )
        )
    additive = dict(projections["accepted"])
    additive["future_optional_projection"] = {"value": "ignore"}
    fixtures.append(
        Fixture(
            "projection_unknown_additive",
            "forward-compatibility",
            "fixtures/forward-compatibility/projection-unknown-additive.json",
            "",
            additive,
            "ignore_unknown_response_field",
        )
    )
    unknown_enum = json.loads(json.dumps(projections["accepted"]))
    unknown_enum["lifecycle"]["state"] = "future_state"
    fixtures.append(
        Fixture(
            "projection_unknown_enum",
            "invalid",
            "fixtures/invalid/projection-unknown-enum.json",
            "",
            unknown_enum,
            "fail_closed_and_resync",
        )
    )
    return tuple(fixtures)


def generated_client_source() -> str:
    return f"""// Generated by tools/product_forge_contract.py. Server-only private client.
export const WORLD_FORGE_CONTRACT_REVISION = {json.dumps(CONTRACT_REVISION)} as const;

export interface ForgePrivateConnection {{
  readonly bearer: string;
  readonly actorContext: string;
}}

export class WorldForgePrivateClient {{
  private expectedEpoch: string | null = null;
  constructor(private readonly baseUrl: string, private readonly auth: ForgePrivateConnection) {{}}

  identity(signal?: AbortSignal) {{ return this.request("GET", "/forge/v1/identity", undefined, undefined, signal, false); }}
  setExpectedEpoch(epoch: string) {{ this.expectedEpoch = epoch; }}
  recipes(signal?: AbortSignal) {{ return this.request("GET", "/forge/v1/recipes", undefined, undefined, signal); }}
  prepare(body: unknown, signal?: AbortSignal) {{ return this.request("POST", "/forge/v1/preparations", body, undefined, signal); }}
  submit(body: unknown, idempotencyKey: string, signal?: AbortSignal) {{ return this.request("POST", "/forge/v1/builds", body, idempotencyKey, signal); }}
  snapshot(reference: string, signal?: AbortSignal) {{ return this.request("GET", `/forge/v1/builds/${{encodeURIComponent(reference)}}`, undefined, undefined, signal); }}
  events(reference: string, cursor: string, signal?: AbortSignal) {{ return this.request("GET", `/forge/v1/builds/${{encodeURIComponent(reference)}}/events?after=${{encodeURIComponent(cursor)}}`, undefined, undefined, signal); }}
  cancel(reference: string, idempotencyKey: string, signal?: AbortSignal) {{ return this.request("POST", `/forge/v1/builds/${{encodeURIComponent(reference)}}/cancel`, {{}}, idempotencyKey, signal); }}

  private async request(method: string, path: string, body?: unknown, idempotencyKey?: string, signal?: AbortSignal, requiresActor = true): Promise<unknown> {{
    const headers: Record<string, string> = {{
      Authorization: `Bearer ${{this.auth.bearer}}`,
      Accept: "application/json",
    }};
    if (requiresActor) {{
      if (this.expectedEpoch === null) throw new Error("World Forge identity must be resolved first");
      headers["X-Dreamland-Actor-Context"] = this.auth.actorContext;
      headers["X-Dreamland-Expected-Epoch"] = this.expectedEpoch;
    }}
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey !== undefined) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(this.baseUrl + path, {{ method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal, redirect: "error", cache: "no-store" }});
    const value: unknown = await response.json();
    if (!response.ok) throw Object.assign(new Error("World Forge private request failed"), {{ status: response.status, value }});
    return value;
  }}
}}
"""


def build_manifest_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version",
            "contract_name",
            "contract_revision",
            "compatibility",
            "publication_status",
            "implementation_status",
            "transport_status",
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
                "const": "integration_preview_only",
            },
            "transport_status": {
                "type": "string",
                "const": "manually_invoked_loopback_preview_only",
            },
            "backend_commit": {"type": ["string", "null"]},
            "published_at": {"type": ["string", "null"]},
            "artifacts": {
                "type": "object",
                "additionalProperties": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "fixture_set": {"type": "object"},
            "generation": {"type": "object"},
            "source": {"type": "object"},
        },
        "additionalProperties": False,
    }


def _base_manifest() -> dict[str, Any]:
    prefix = f".venv/bin/python tools/product_forge_contract.py {{verb}} contracts/world-forge-api/v{CONTRACT_REVISION}"
    return {
        "schema_version": 1,
        "contract_name": CONTRACT_NAME,
        "contract_revision": CONTRACT_REVISION,
        "compatibility": CONTRACT_COMPATIBILITY,
        "publication_status": "draft",
        "implementation_status": "integration_preview_only",
        "transport_status": "manually_invoked_loopback_preview_only",
        "backend_commit": None,
        "published_at": None,
        "artifacts": {},
        "fixture_set": {"path": "fixtures/index.json", "index_sha256": ""},
        "generation": {
            "validate_command": prefix.format(verb="validate"),
            "refresh_command": prefix.format(verb="refresh"),
            "client_smoke_command": prefix.format(verb="client-smoke"),
            "publish_command": prefix.format(verb="publish")
            + " --backend-commit COMMIT --published-at TIMESTAMP",
        },
        "source": {
            "kind": "integration_contract",
            "supported_runtime_sources": [
                "provider_free_integration_preview",
                "single_attempt_live_vertical_slice",
            ],
        },
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def refresh_contract(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    fixtures = fixture_definitions()
    fixture_entries = []
    expected = set(EXPECTED_FILES)
    for fixture in fixtures:
        path = root / fixture.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_json_bytes(fixture.payload))
        expected.add(fixture.path)
        fixture_entries.append(
            {
                "case": fixture.case,
                "classification": fixture.classification,
                "path": fixture.path,
                "target": fixture.target,
                "client_action": fixture.client_action,
                "sha256": _sha256(_json_bytes(fixture.payload)),
                **(
                    {"reachability": fixture.reachability}
                    if fixture.reachability is not None
                    else {}
                ),
                **(
                    {"reachability_detail": fixture.reachability_detail}
                    if fixture.reachability_detail is not None
                    else {}
                ),
            }
        )
    index = {"contract_revision": CONTRACT_REVISION, "fixtures": fixture_entries}
    values: dict[str, bytes] = {
        "openapi.json": _json_bytes(build_openapi()),
        "events.schema.json": _json_bytes(build_events_schema()),
        "manifest.schema.json": _json_bytes(build_manifest_schema()),
        "fixtures/index.json": _json_bytes(index),
        "generated/world_forge_client.ts": generated_client_source().encode("utf-8"),
    }
    for relative, data in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = _base_manifest()
    manifest["fixture_set"]["index_sha256"] = _sha256(values["fixtures/index.json"])
    manifest["artifacts"] = {
        "openapi_sha256": _sha256(values["openapi.json"]),
        "events_schema_sha256": _sha256(values["events.schema.json"]),
        "manifest_schema_sha256": _sha256(values["manifest.schema.json"]),
        "generated_client_sha256": _sha256(values["generated/world_forge_client.ts"]),
    }
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(root).as_posix() not in expected:
            path.unlink()
    return {
        "contract_revision": CONTRACT_REVISION,
        "fixture_count": len(fixtures),
        "refreshed": True,
    }


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeContractError(f"invalid JSON artifact: {path}") from error


def validate_contract(root: Path) -> dict[str, Any]:
    try:
        import jsonschema
    except ImportError as error:
        raise ForgeContractError(
            "jsonschema is required to validate the Forge contract"
        ) from error
    if not root.is_dir():
        raise ForgeContractError("Forge contract root is unavailable")
    manifest = _load(root / "manifest.json")
    manifest_schema = _load(root / "manifest.schema.json")
    jsonschema.Draft202012Validator.check_schema(manifest_schema)
    jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
    openapi = _load(root / "openapi.json")
    events_schema = _load(root / "events.schema.json")
    index = _load(root / "fixtures/index.json")
    revision = manifest.get("contract_revision")
    if (
        not isinstance(revision, str)
        or openapi.get("info", {}).get("version") != revision
        or index.get("contract_revision") != revision
    ):
        raise ForgeContractError("Forge contract revision evidence disagrees")
    artifact_paths = {
        "openapi_sha256": "openapi.json",
        "events_schema_sha256": "events.schema.json",
        "manifest_schema_sha256": "manifest.schema.json",
        "generated_client_sha256": "generated/world_forge_client.ts",
    }
    for key, relative in artifact_paths.items():
        if manifest["artifacts"].get(key) != _sha256((root / relative).read_bytes()):
            raise ForgeContractError(f"artifact digest mismatch: {relative}")
    if manifest["fixture_set"].get("index_sha256") != _sha256(
        (root / "fixtures/index.json").read_bytes()
    ):
        raise ForgeContractError("fixture index digest mismatch")
    for entry in index.get("fixtures", []):
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise ForgeContractError("fixture path is invalid")
        data = (root / relative).read_bytes()
        if entry.get("sha256") != _sha256(data):
            raise ForgeContractError(f"fixture digest mismatch: {relative}")
        target = entry.get("target")
        if target:
            pointer = str(target).removeprefix("openapi.json")
            schema = {**openapi, "$ref": pointer}
            try:
                jsonschema.Draft202012Validator(schema).validate(json.loads(data))
            except jsonschema.ValidationError as error:
                raise ForgeContractError(
                    f"fixture schema failure: {relative}: {error.message}"
                ) from error
    jsonschema.Draft202012Validator.check_schema(events_schema)
    if set(openapi["paths"]) != {
        "/forge/v1/identity",
        "/forge/v1/recipes",
        "/forge/v1/preparations",
        "/forge/v1/builds",
        "/forge/v1/builds/{reference}",
        "/forge/v1/builds/{reference}/events",
        "/forge/v1/builds/{reference}/cancel",
    }:
        raise ForgeContractError("Forge operation set is invalid")
    return {
        "contract_revision": revision,
        "fixture_count": len(index["fixtures"]),
        "valid": True,
    }


def client_smoke(root: Path, *, output: Path | None = None) -> dict[str, Any]:
    validated = validate_contract(root)
    source = (root / "generated/world_forge_client.ts").read_text(encoding="utf-8")
    required = (
        "identity(",
        "recipes(",
        "prepare(",
        "submit(",
        "snapshot(",
        "events(",
        "cancel(",
    )
    if any(marker not in source for marker in required):
        raise ForgeContractError("generated Forge client is missing an operation")
    if "server-only" not in source.lower() or "Authorization" not in source:
        raise ForgeContractError("generated Forge client boundary is missing")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "generated/world_forge_client.ts", output)
    return {
        "contract_revision": validated["contract_revision"],
        "operations": len(required),
        "smoke": True,
    }


def publish_contract(
    root: Path, *, backend_commit: str, published_at: str
) -> dict[str, Any]:
    validated = validate_contract(root)
    if validated["contract_revision"] != CONTRACT_REVISION:
        raise ForgeContractError("only the current World Forge RC may be published")
    if re.fullmatch(r"[a-f0-9]{40}", backend_commit) is None:
        raise ForgeContractError("backend commit must be a full lowercase SHA-1")
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ForgeContractError(
            "published-at must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ForgeContractError("published-at must include a timezone")
    manifest = _load(root / "manifest.json")
    if manifest["publication_status"] != "draft":
        raise ForgeContractError("only a draft World Forge RC may be published")
    manifest["publication_status"] = "published"
    manifest["backend_commit"] = backend_commit
    manifest["published_at"] = published_at
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    validate_contract(root)
    return {"contract_revision": CONTRACT_REVISION, "published": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the private World Forge contract kit."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "validate", "client-smoke"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "client-smoke":
            command.add_argument("--output", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("root", type=Path)
    publish.add_argument("--backend-commit", required=True)
    publish.add_argument("--published-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "refresh":
            result = refresh_contract(arguments.root)
        elif arguments.command == "validate":
            result = validate_contract(arguments.root)
        elif arguments.command == "client-smoke":
            result = client_smoke(arguments.root, output=arguments.output)
        elif arguments.command == "publish":
            result = publish_contract(
                arguments.root,
                backend_commit=arguments.backend_commit,
                published_at=arguments.published_at,
            )
        else:
            raise ForgeContractError("unsupported command")
    except (ForgeContractError, OSError, ValueError) as error:
        print(f"World Forge contract error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
