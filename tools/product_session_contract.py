#!/usr/bin/env python3
"""Build and validate the unfrozen Phase 2 product-session contract kit.

The tool is development-only.  It imports no DREAMLAND runtime module and
starts no listener, worker, journal, provider client, or background task.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import py_compile
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.3.0-rc.1"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
AVAILABILITY = "integration-preview-only"
CAPABILITY_NAMES = (
    "product_session_core",
    "presenter_stage_terminal",
    "public_attendee_sessions",
    "forge_to_stage_handoff",
    "stage_xray",
)
CONTRACT_FILES = {
    "openapi_sha256": "openapi.json",
    "events_schema_sha256": "events.schema.json",
    "manifest_schema_sha256": "manifest.schema.json",
    "generated_client_sha256": "generated/product_session_client.py",
}
EXPECTED_OPERATIONS = {
    "/product/v1/capabilities": ("get", "getProductCapabilities"),
    "/product/v1/sessions": ("post", "createProductSession"),
    "/product/v1/sessions/current": ("get", "getCurrentProductSession"),
    "/product/v1/sessions/{reference}": ("get", "getProductSessionSnapshot"),
    "/product/v1/sessions/{reference}/commands": (
        "post",
        "submitProductCommand",
    ),
    "/product/v1/sessions/{reference}/commands/{command_id}": (
        "get",
        "getProductCommandResult",
    ),
    "/product/v1/sessions/{reference}/commands/{command_id}/cancel": (
        "post",
        "cancelProductCommand",
    ),
    "/product/v1/sessions/{reference}/events": (
        "get",
        "getProductSessionEvents",
    ),
    "/product/v1/sessions/{reference}/transcript": (
        "get",
        "getProductSessionTranscript",
    ),
    "/product/v1/sessions/{reference}/reset": ("post", "resetProductSession"),
    "/product/v1/sessions/{reference}/switch": (
        "post",
        "switchProductSession",
    ),
    "/product/v1/sessions/{reference}/close": ("post", "closeProductSession"),
}
MUTATION_OPERATIONS = {
    "createProductSession",
    "submitProductCommand",
    "cancelProductCommand",
    "resetProductSession",
    "switchProductSession",
    "closeProductSession",
}
SESSION_OPERATIONS = set(EXPECTED_OPERATIONS) - {
    "/product/v1/capabilities",
    "/product/v1/sessions",
    "/product/v1/sessions/current",
}
MAX_SAFE_INTEGER = 9_007_199_254_740_991
FORBIDDEN_PROJECTION_KEYS = {
    "admin",
    "admin_payload",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "environment",
    "internal_path",
    "launch_grant",
    "model_prompt",
    "password",
    "private_key",
    "prompt",
    "provider_envelope",
    "provider_payload",
    "provider_request",
    "provider_response",
    "raw_state",
    "reasoning",
    "runtime_document",
    "runtime_state",
    "secret",
    "session_authority",
    "stack_trace",
    "state_profile",
    "system_prompt",
    "token",
    "traceback",
    "world_state",
}
KNOWN_EVENT_TYPES = (
    "session.created",
    "command.accepted",
    "command.running",
    "command.output",
    "command.cancel_requested",
    "state.accepted",
    "command.completed",
    "command.interrupted",
    "command.failed",
    "session.terminal",
)
SAFE_ERROR_CASES = {
    "unauthorized": (
        401,
        "Missing or invalid private product authority.",
        "none",
        None,
    ),
    "not_found": (
        404,
        "The requested private product resource is unavailable.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        400,
        "Expected product service epoch evidence is required.",
        "none",
        None,
    ),
    "session_lost": (
        410,
        "The product session was lost after service restart.",
        "none",
        None,
    ),
    "outcome_unknown": (
        410,
        "The prior product mutation outcome is unknown after service restart.",
        "none",
        None,
    ),
    "authority_denied": (
        403,
        "The requested product resource is unavailable.",
        "none",
        None,
    ),
    "invalid_request": (400, "The private product request is invalid.", "none", None),
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
        "The request body exceeds the product preview limit.",
        "none",
        None,
    ),
    "idempotency_conflict": (
        409,
        "The idempotency key was already used for another request.",
        "none",
        None,
    ),
    "current_session_exists": (
        409,
        "A current product session already exists.",
        "resume_snapshot",
        None,
    ),
    "control_revision_conflict": (
        409,
        "The product session control revision changed.",
        "resume_snapshot",
        None,
    ),
    "command_in_progress": (
        409,
        "A product command is already in progress.",
        "resume_snapshot",
        None,
    ),
    "command_slot_conflict": (
        409,
        "The product command slot generation changed.",
        "resume_snapshot",
        None,
    ),
    "command_not_found": (
        404,
        "The product command is unavailable.",
        "resume_snapshot",
        None,
    ),
    "command_limit_reached": (
        409,
        "The product session command limit was reached.",
        "none",
        None,
    ),
    "command_too_large": (
        413,
        "The product command exceeds this session's limit.",
        "none",
        None,
    ),
    "invalid_command_encoding": (
        422,
        "Command input must be well-formed UTF-8.",
        "none",
        None,
    ),
    "invalid_command_text": (
        422,
        "Command text contains unsupported characters.",
        "none",
        None,
    ),
    "invalid_command_syntax": (
        422,
        "Command syntax is incomplete or unsupported.",
        "none",
        None,
    ),
    "compound_syntax_unsupported": (
        422,
        "Shell-control syntax is unsupported in the browser terminal; use SSH for compound shell workflows.",
        "none",
        None,
    ),
    "snapshot_required": (
        409,
        "The product cursor requires an authoritative snapshot.",
        "resume_snapshot",
        None,
    ),
    "session_closed": (
        409,
        "The product session is no longer active.",
        "resume_snapshot",
        None,
    ),
    "replacement_unavailable": (
        503,
        "The replacement product session could not be provisioned.",
        "same_request",
        None,
    ),
    "capacity_exhausted": (
        503,
        "Product command capacity is temporarily exhausted.",
        "after_delay",
        1000,
    ),
    "hosted_budget_exhausted": (
        503,
        "Product hosted-work budget is temporarily unavailable.",
        "after_delay",
        1000,
    ),
    "resource_unavailable": (
        409,
        "Product command resources are unavailable.",
        "resume_snapshot",
        None,
    ),
    "internal_error": (
        500,
        "The product preview request could not be completed.",
        "none",
        None,
    ),
}


class ContractError(RuntimeError):
    """A deterministic contract validation failure."""


@dataclass(frozen=True, slots=True)
class Fixture:
    case: str
    classification: str
    path: str
    target: str
    payload: Any
    client_action: str

    @property
    def expected(self) -> str:
        return "invalid" if self.classification == "invalid" else "valid"


def canonical_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_text(value), encoding="utf-8")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot load canonical JSON: {path}") from error


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _string(pattern: str, *, minimum: int = 1, maximum: int = 512) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": minimum,
        "maxLength": maximum,
        "pattern": pattern,
    }


def _integer(*, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _object(
    properties: dict[str, Any],
    required: tuple[str, ...] | list[str],
    *,
    additive: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additive,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": [schema, {"type": "null"}]}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _array(items: dict[str, Any], *, maximum: int) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": maximum}


def _success_content(schema: str, description: str) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {
            "Cache-Control": {"$ref": "#/components/headers/CacheControl"},
            "X-Content-Type-Options": {"$ref": "#/components/headers/NoSniff"},
        },
        "content": {"application/json": {"schema": _ref(schema)}},
    }


def _responses(
    success_schema: str, description: str, *, created: bool = False
) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "201" if created else "200": _success_content(success_schema, description)
    }
    for status in (400, 401, 403, 404, 409, 410, 411, 413, 415, 422, 429, 500, 503):
        responses[str(status)] = {"$ref": "#/components/responses/SafeError"}
    return responses


def _operation(
    operation_id: str,
    response_schema: str,
    description: str,
    *,
    parameters: list[str],
    request_schema: str | None = None,
    created: bool = False,
    accepted: bool = False,
    idempotency_family: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operationId": operation_id,
        "description": description,
        "security": [{"ProductServiceBearer": []}],
        "x-dreamland-availability": AVAILABILITY,
        "parameters": [
            {"$ref": f"#/components/parameters/{parameter}"} for parameter in parameters
        ],
        "responses": _responses(response_schema, description, created=created),
    }
    if request_schema is not None:
        value["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _ref(request_schema)}},
        }
    if idempotency_family is not None:
        value["x-dreamland-idempotency"] = {
            "operation_family": idempotency_family,
            "policy": "#/x-dreamland-idempotency-policy",
        }
    if accepted:
        value["responses"]["202"] = value["responses"].pop("200")
    return value


def build_openapi() -> dict[str, Any]:
    """Return the reviewed OpenAPI trust anchor for the Phase 2 preview."""
    schemas = _schemas()
    common_read = [
        "SessionReference",
        "ExpectedEpoch",
        "ActorContext",
        "SessionCapability",
    ]
    common_mutation = [*common_read, "IdempotencyKey"]
    paths: dict[str, Any] = {
        "/product/v1/capabilities": {
            "get": _operation(
                "getProductCapabilities",
                "ProductCapabilitiesResponse",
                "Return the fail-closed Phase 2 integration-preview capability projection.",
                parameters=[],
            )
        },
        "/product/v1/sessions": {
            "post": _operation(
                "createProductSession",
                "CreateSessionResponse",
                "Create one isolated session from an authorized immutable launch grant.",
                parameters=["ExpectedEpoch", "ActorContext", "IdempotencyKey"],
                request_schema="CreateSessionRequest",
                created=True,
                idempotency_family="session.create",
            )
        },
        "/product/v1/sessions/current": {
            "get": _operation(
                "getCurrentProductSession",
                "CurrentSessionResponse",
                "Return the actor's current binding without creating a session.",
                parameters=["ExpectedEpoch", "ActorContext"],
            )
        },
        "/product/v1/sessions/{reference}": {
            "get": _operation(
                "getProductSessionSnapshot",
                "SnapshotResponse",
                "Return one internally consistent authoritative product snapshot.",
                parameters=common_read,
            )
        },
        "/product/v1/sessions/{reference}/commands": {
            "post": _operation(
                "submitProductCommand",
                "SubmitCommandResponse",
                "Classify and accept one product-single-v1 command into the sole slot.",
                parameters=common_mutation,
                request_schema="SubmitCommandRequest",
                accepted=True,
                idempotency_family="command.submit",
            )
        },
        "/product/v1/sessions/{reference}/commands/{command_id}": {
            "get": _operation(
                "getProductCommandResult",
                "CommandResultResponse",
                "Return authoritative current or terminal truth for one accepted command.",
                parameters=[*common_read, "CommandId"],
            )
        },
        "/product/v1/sessions/{reference}/commands/{command_id}/cancel": {
            "post": _operation(
                "cancelProductCommand",
                "CancelCommandResponse",
                "Race exact cancellation against terminal publication for one slot generation.",
                parameters=[*common_mutation, "CommandId"],
                request_schema="CancelCommandRequest",
                idempotency_family="command.cancel",
            )
        },
        "/product/v1/sessions/{reference}/events": {
            "get": _events_operation(common_read)
        },
        "/product/v1/sessions/{reference}/transcript": {
            "get": _transcript_operation(common_read)
        },
        "/product/v1/sessions/{reference}/reset": {
            "post": _operation(
                "resetProductSession",
                "ReplacementResponse",
                "Provision a clean copy of the same immutable revision before atomic cutover.",
                parameters=common_mutation,
                request_schema="ResetSessionRequest",
                idempotency_family="session.reset",
            )
        },
        "/product/v1/sessions/{reference}/switch": {
            "post": _operation(
                "switchProductSession",
                "ReplacementResponse",
                "Provision the granted immutable revision before atomic cutover.",
                parameters=common_mutation,
                request_schema="SwitchSessionRequest",
                idempotency_family="session.switch",
            )
        },
        "/product/v1/sessions/{reference}/close": {
            "post": _operation(
                "closeProductSession",
                "CloseSessionResponse",
                "Fence active work, revoke mutation authority, and retain bounded recovery truth.",
                parameters=common_mutation,
                request_schema="CloseSessionRequest",
                idempotency_family="session.close",
            )
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DREAMLAND Private Product Session Core Preview",
            "version": CONTRACT_REVISION,
            "description": (
                "Unfrozen, private, manually invoked Phase 2 integration preview. "
                "All product capabilities remain disabled and this document is not "
                "live-capability evidence."
            ),
        },
        "servers": [],
        "security": [{"ProductServiceBearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ProductServiceBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque-private-product-service-capability",
                    "description": "Server-side BFF authority; never browser-visible.",
                }
            },
            "parameters": _parameters(),
            "headers": {
                "CacheControl": {
                    "description": "Private responses are never cacheable.",
                    "schema": {"type": "string", "const": "no-store"},
                },
                "NoSniff": {
                    "description": "Disable response MIME sniffing.",
                    "schema": {"type": "string", "const": "nosniff"},
                },
            },
            "responses": {
                "SafeError": {
                    "description": "Bounded private safe error.",
                    "headers": {
                        "Cache-Control": {"$ref": "#/components/headers/CacheControl"},
                        "X-Content-Type-Options": {
                            "$ref": "#/components/headers/NoSniff"
                        },
                    },
                    "content": {
                        "application/problem+json": {
                            "schema": _ref("SafeErrorEnvelope")
                        }
                    },
                }
            },
            "schemas": schemas,
        },
        "x-dreamland-private-boundary": {
            "browser_direct_access": False,
            "admin_authority_accepted": False,
            "session_locators_grant_authority": False,
            "master_activation_default": "disabled",
            "idle_runtime_when_disabled": "no_product_objects_or_tasks",
        },
        "x-dreamland-idempotency-policy": _idempotency_policy(),
        "x-dreamland-event-policy": {
            "delivery": "at-least-once",
            "json_replay": "exact-cursor-after-exclusive",
            "sse_replay": "Last-Event-ID-after-exclusive",
            "unknown_informational": "ignore_payload_and_advance_cursor",
            "unknown_authoritative": "stop_incremental_apply_and_resume_snapshot",
            "old_epoch": "session_lost_not_snapshot_required",
            "within_epoch_cursor_loss": "snapshot_required",
        },
    }


def _parameters() -> dict[str, Any]:
    def header(
        name: str, schema: dict[str, Any], required: bool = True
    ) -> dict[str, Any]:
        return {
            "name": name,
            "in": "header",
            "required": required,
            "schema": schema,
        }

    def query(name: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "in": "query",
            "required": False,
            "schema": schema,
        }

    return {
        "ExpectedEpoch": header("X-Dreamland-Expected-Epoch", _ref("ServiceEpoch")),
        "ActorContext": header("X-Dreamland-Actor-Context", _ref("ActorContext")),
        "SessionCapability": header(
            "X-Dreamland-Session-Capability", _ref("SessionAuthority")
        ),
        "IdempotencyKey": header("Idempotency-Key", _ref("IdempotencyKey")),
        "SessionReference": {
            "name": "reference",
            "in": "path",
            "required": True,
            "schema": _ref("SessionReference"),
        },
        "CommandId": {
            "name": "command_id",
            "in": "path",
            "required": True,
            "schema": _ref("CommandId"),
        },
        "AfterStreamId": query("after_stream_id", _ref("EventStreamId")),
        "AfterSequence": query("after_sequence", _integer(minimum=1)),
        "AfterEventId": query("after_event_id", _ref("EventId")),
        "LastEventId": header("Last-Event-ID", _ref("EventId"), required=False),
        "AfterTranscriptId": query("after_transcript_id", _ref("TranscriptId")),
        "AfterTranscriptPosition": query("after_position", _integer(minimum=1)),
        "AfterEntryId": query("after_entry_id", _ref("TranscriptEntryId")),
    }


def _events_operation(common_read: list[str]) -> dict[str, Any]:
    operation = _operation(
        "getProductSessionEvents",
        "EventPageResponse",
        "Replay retained typed events as JSON or stream them as SSE.",
        parameters=[
            *common_read,
            "AfterStreamId",
            "AfterSequence",
            "AfterEventId",
            "LastEventId",
        ],
    )
    success = operation["responses"]["200"]
    success["content"]["text/event-stream"] = {
        "schema": {"type": "string"},
        "x-dreamland-frame-data-schema": "events.schema.json#/$defs/ProductEvent",
        "x-dreamland-sse-id": "event_id",
        "x-dreamland-heartbeat": "comment-only-no-product-state",
    }
    operation["x-dreamland-cursor-query"] = {
        "parameters": ["after_stream_id", "after_sequence", "after_event_id"],
        "presence": "all-or-none",
        "initial_snapshot": "omit-all",
        "last_event_id": "sse-only-and-mutually-exclusive-with-query-cursor",
    }
    return operation


def _transcript_operation(common_read: list[str]) -> dict[str, Any]:
    operation = _operation(
        "getProductSessionTranscript",
        "TranscriptPageResponse",
        "Return retained inert-text transcript entries after an exact cursor.",
        parameters=[
            *common_read,
            "AfterTranscriptId",
            "AfterTranscriptPosition",
            "AfterEntryId",
        ],
    )
    operation["x-dreamland-cursor-query"] = {
        "parameters": ["after_transcript_id", "after_position", "after_entry_id"],
        "presence": "all-or-none",
        "initial_snapshot": "omit-all",
    }
    return operation


def _idempotency_policy() -> dict[str, Any]:
    return {
        "scope": [
            "service_epoch",
            "actor_identity",
            "authority_class",
            "operation_family",
            "idempotency_key",
        ],
        "canonical_body_binding": True,
        "exact_retry": "replay_recorded_logical_outcome_before_current_state_evaluation",
        "changed_body": "idempotency_conflict",
        "retention": "related_session_or_tombstone_plus_bounded_cleanup_grace",
        "epoch_rotation": "old_mutation_outcome_unknown_and_never_redispatched",
        "unclaimed_rejections": [
            "private_authentication_or_route_rejection",
            "missing_malformed_or_stale_expected_epoch",
            "actor_context_rejection",
            "malformed_headers_media_json_or_canonical_body",
            "product_single_v1_classifier_rejection",
            "retryable_capacity_or_budget_rejection_before_dispatch",
        ],
        "claimed_deterministic_rejections": [
            "launch_or_session_authority_denied",
            "current_session_exists",
            "control_revision_conflict",
            "command_in_progress",
            "command_slot_conflict",
            "command_not_found",
            "command_or_session_limit_reached",
            "nonretryable_resource_unavailable",
        ],
        "postclaim_success": "record_before_response",
    }


def _response_schema(
    properties: dict[str, Any], required: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    return _object(
        {
            **properties,
            "contract": _ref("ContractIdentity"),
            "preview": _ref("PreviewIdentity"),
        },
        [*required, "contract", "preview"],
        additive=True,
    )


def _schemas() -> dict[str, Any]:
    identity = {
        "ServiceEpoch": _string(r"^epoch:[A-Za-z0-9_-]{16,128}$", maximum=134),
        "ActorContext": _string(r"^actor:[A-Za-z0-9_-]{16,128}$", maximum=134),
        "LaunchGrant": _string(r"^launch-grant:[A-Za-z0-9_-]{24,480}$", maximum=493),
        "SessionAuthority": _string(
            r"^session-cap:[A-Za-z0-9_-]{24,256}$", maximum=268
        ),
        "SessionReference": _string(
            r"^session-ref:[A-Za-z0-9_-]{16,128}$", maximum=140
        ),
        "SessionId": _string(r"^session:[A-Za-z0-9_-]{16,128}$", maximum=136),
        "CommandId": _string(r"^command:[A-Za-z0-9_-]{16,128}$", maximum=136),
        "RequestId": _string(r"^request:[A-Za-z0-9_-]{8,128}$", maximum=136),
        "IdempotencyKey": _string(
            r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$", minimum=16, maximum=128
        ),
        "SelectedRecordId": _string(
            r"^selected-record:[A-Za-z0-9_-]{16,128}$", maximum=144
        ),
        "ImmutableRevisionId": _string(
            r"^world-revision:[A-Za-z0-9_-]{16,128}$", maximum=143
        ),
        "Sha256": _string(r"^[0-9a-f]{64}$", minimum=64, maximum=64),
        "EventStreamId": _string(r"^stream:[A-Za-z0-9_-]{8,96}$", maximum=103),
        "EventId": _string(
            r"^event:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$", maximum=119
        ),
        "TranscriptId": _string(r"^transcript:[A-Za-z0-9_-]{8,96}$", maximum=107),
        "TranscriptEntryId": _string(
            r"^entry:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$", maximum=119
        ),
    }
    contract_identity = _object(
        {
            "name": {"type": "string", "const": CONTRACT_NAME},
            "revision": {"type": "string", "const": CONTRACT_REVISION},
            "compatibility": {"type": "string", "const": CONTRACT_COMPATIBILITY},
        },
        ("name", "revision", "compatibility"),
    )
    preview_identity = _object(
        {
            "test_only": {"type": "boolean", "const": True},
            "integration_only": {"type": "boolean", "const": True},
            "executor": {"type": "string", "const": "deterministic-no-provider-v1"},
            "provider_calls": {"type": "boolean", "const": False},
            "live_capability_evidence": {"type": "boolean", "const": False},
        },
        (
            "test_only",
            "integration_only",
            "executor",
            "provider_calls",
            "live_capability_evidence",
        ),
    )
    event_cursor = {
        "oneOf": [
            _object(
                {
                    "stream_id": _ref("EventStreamId"),
                    "sequence": {"type": "integer", "const": 0},
                    "event_id": {"type": "null"},
                },
                ("stream_id", "sequence", "event_id"),
            ),
            _object(
                {
                    "stream_id": _ref("EventStreamId"),
                    "sequence": _integer(minimum=1),
                    "event_id": _ref("EventId"),
                },
                ("stream_id", "sequence", "event_id"),
            ),
        ]
    }
    transcript_cursor = {
        "oneOf": [
            _object(
                {
                    "transcript_id": _ref("TranscriptId"),
                    "position": {"type": "integer", "const": 0},
                    "entry_id": {"type": "null"},
                },
                ("transcript_id", "position", "entry_id"),
            ),
            _object(
                {
                    "transcript_id": _ref("TranscriptId"),
                    "position": _integer(minimum=1),
                    "entry_id": _ref("TranscriptEntryId"),
                },
                ("transcript_id", "position", "entry_id"),
            ),
        ]
    }
    usage = {
        "oneOf": [
            _object(
                {
                    "state": {"type": "string", "const": "pending"},
                    "tokens": {"type": "null"},
                    "spend_units": {"type": "null"},
                },
                ("state", "tokens", "spend_units"),
            ),
            _object(
                {
                    "state": {"type": "string", "const": "settled"},
                    "tokens": _integer(),
                    "spend_units": _integer(),
                },
                ("state", "tokens", "spend_units"),
            ),
        ]
    }
    guest_termination = {
        "oneOf": [
            _object(
                {
                    "kind": {"type": "string", "const": "exited"},
                    "exit_status": {
                        "type": "integer",
                        "minimum": -(2**31),
                        "maximum": 2**31 - 1,
                    },
                },
                ("kind", "exit_status"),
            ),
            _object(
                {
                    "kind": {"type": "string", "const": "signaled"},
                    "signal": _string(r"^SIG[A-Z0-9]{1,16}$", maximum=19),
                },
                ("kind", "signal"),
            ),
        ]
    }
    state_effect = {
        "oneOf": [
            _object({"kind": {"type": "string", "const": "none"}}, ("kind",)),
            _object(
                {
                    "kind": {"type": "string", "const": "committed"},
                    "accepted_state_revision": _integer(minimum=1),
                },
                ("kind", "accepted_state_revision"),
            ),
            _object(
                {
                    "kind": {"type": "string", "const": "rejected"},
                    "reason": _nullable(_string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64)),
                },
                ("kind",),
            ),
        ]
    }
    completed_result = _object(
        {
            "outcome": {"type": "string", "const": "completed"},
            "output_clipped": {"type": "boolean"},
            "usage": {"$ref": "#/components/schemas/ResourceUsage"},
            "guest_termination": {"$ref": "#/components/schemas/GuestTermination"},
            "state_effect": {"$ref": "#/components/schemas/StateEffect"},
        },
        ("outcome", "output_clipped", "usage", "guest_termination", "state_effect"),
        additive=True,
    )
    completed_result["allOf"] = [{"not": {"required": ["reason"]}}]
    noncompleted_variants: list[dict[str, Any]] = []
    for outcome in ("interrupted", "failed"):
        schema = _object(
            {
                "outcome": {"type": "string", "const": outcome},
                "output_clipped": {"type": "boolean"},
                "usage": {"$ref": "#/components/schemas/ResourceUsage"},
                "reason": _string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64),
            },
            ("outcome", "output_clipped", "usage", "reason"),
            additive=True,
        )
        schema["allOf"] = [
            {
                "not": {
                    "anyOf": [
                        {"required": ["guest_termination"]},
                        {"required": ["state_effect"]},
                    ]
                }
            }
        ]
        noncompleted_variants.append(schema)
    noncompleted_result = {"oneOf": noncompleted_variants}
    command_projection = _object(
        {
            "command_id": _ref("CommandId"),
            "slot_generation": _integer(minimum=1),
            "status": {
                "type": "string",
                "enum": [
                    "accepted",
                    "running",
                    "cancel_requested",
                    "completed",
                    "interrupted",
                    "failed",
                ],
            },
            "accepted_at_ms": _integer(),
            "deadline_at_ms": _integer(),
            "terminal_at_ms": _nullable(_integer()),
            "output": _object(
                {
                    "bytes": _integer(),
                    "chunks": _integer(),
                    "clipped": {"type": "boolean"},
                },
                ("bytes", "chunks", "clipped"),
            ),
            "result": _nullable({"$ref": "#/components/schemas/TerminalCommandResult"}),
        },
        (
            "command_id",
            "slot_generation",
            "status",
            "accepted_at_ms",
            "deadline_at_ms",
            "terminal_at_ms",
            "output",
            "result",
        ),
        additive=True,
    )
    transcript_entry = _object(
        {
            "transcript_id": _ref("TranscriptId"),
            "position": _integer(minimum=1),
            "entry_id": _ref("TranscriptEntryId"),
            "kind": {
                "type": "string",
                "enum": ["command", "stdout", "stderr", "system", "safe_error"],
            },
            "recorded_at_ms": _integer(),
            "command_id": _nullable(_ref("CommandId")),
            "text": {"type": "string", "maxLength": 65536},
            "encoding": {"type": "string", "const": "utf-8"},
            "projection": {"type": "string", "const": "inert-text-v1"},
            "transformed": {"type": "boolean"},
            "clipped": {"type": "boolean"},
            "omitted_before_bytes": _integer(),
            "omitted_after_bytes": _integer(),
            "chunk_index": _nullable(_integer()),
            "final": _nullable({"type": "boolean"}),
        },
        (
            "transcript_id",
            "position",
            "entry_id",
            "kind",
            "recorded_at_ms",
            "command_id",
            "text",
            "encoding",
            "projection",
            "transformed",
            "clipped",
            "omitted_before_bytes",
            "omitted_after_bytes",
            "chunk_index",
            "final",
        ),
        additive=True,
    )
    transcript_page = _object(
        {
            "kind": {"type": "string", "const": "transcript_page"},
            "entries": _array(
                {"$ref": "#/components/schemas/TranscriptEntry"}, maximum=512
            ),
            "retention_floor": {"$ref": "#/components/schemas/TranscriptCursor"},
            "position": {"$ref": "#/components/schemas/TranscriptCursor"},
            "omitted_before_entries": _integer(),
            "omitted_after_entries": _integer(),
            "retained_text_bytes": _integer(maximum=1_048_576),
        },
        (
            "kind",
            "entries",
            "retention_floor",
            "position",
            "omitted_before_entries",
            "omitted_after_entries",
            "retained_text_bytes",
        ),
        additive=True,
    )
    coordinates = _object(
        {
            "control_revision": _integer(minimum=1),
            "command_slot_generation": _integer(),
            "accepted_state_revision": _integer(),
            "event": {"$ref": "#/components/schemas/EventCursor"},
            "transcript": {"$ref": "#/components/schemas/TranscriptCursor"},
        },
        (
            "control_revision",
            "command_slot_generation",
            "accepted_state_revision",
            "event",
            "transcript",
        ),
        additive=True,
    )
    session_snapshot = _session_snapshot_schema()
    schemas: dict[str, Any] = {
        **identity,
        "ContractIdentity": contract_identity,
        "PreviewIdentity": preview_identity,
        "EventCursor": event_cursor,
        "TranscriptCursor": transcript_cursor,
        "ResourceUsage": usage,
        "GuestTermination": guest_termination,
        "StateEffect": state_effect,
        "CompletedCommandResult": completed_result,
        "NoncompletedCommandResult": noncompleted_result,
        "TerminalCommandResult": {
            "oneOf": [_ref("CompletedCommandResult"), _ref("NoncompletedCommandResult")]
        },
        "CommandProjection": command_projection,
        "TranscriptEntry": transcript_entry,
        "TranscriptPage": transcript_page,
        "SessionCoordinates": coordinates,
        "SessionSnapshot": session_snapshot,
    }
    schemas.update(_request_response_schemas())
    schemas.update(_fixture_only_schemas())
    return schemas


def _session_snapshot_schema() -> dict[str, Any]:
    lifecycle_active = _object(
        {
            "state": {"type": "string", "const": "active"},
            "created_at_ms": _integer(),
            "last_activity_at_ms": _integer(),
            "absolute_expires_at_ms": _integer(),
            "idle_expires_at_ms": _integer(),
        },
        (
            "state",
            "created_at_ms",
            "last_activity_at_ms",
            "absolute_expires_at_ms",
            "idle_expires_at_ms",
        ),
        additive=True,
    )
    lifecycle_terminal = _object(
        {
            "state": {"type": "string", "enum": ["closed", "expired", "replaced"]},
            "created_at_ms": _integer(),
            "last_activity_at_ms": _integer(),
            "absolute_expires_at_ms": _integer(),
            "idle_expires_at_ms": _integer(),
            "terminal_at_ms": _integer(),
            "terminal_reason": _string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64),
        },
        (
            "state",
            "created_at_ms",
            "last_activity_at_ms",
            "absolute_expires_at_ms",
            "idle_expires_at_ms",
            "terminal_at_ms",
            "terminal_reason",
        ),
        additive=True,
    )
    snapshot = _object(
        {
            "session_id": _ref("SessionId"),
            "reference": _ref("SessionReference"),
            "authority_class": {"type": "string", "enum": ["attendee", "presenter"]},
            "starting_revision": _object(
                {
                    "selected_record_id": _ref("SelectedRecordId"),
                    "revision_id": _ref("ImmutableRevisionId"),
                    "sha256": _ref("Sha256"),
                },
                ("selected_record_id", "revision_id", "sha256"),
                additive=True,
            ),
            "launch_binding": _object(
                {
                    "sha256": _ref("Sha256"),
                    "limits": {
                        "type": "object",
                        "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,63}$"},
                        "additionalProperties": _integer(minimum=1),
                        "maxProperties": 32,
                    },
                    "policy_id": _string(
                        r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$", maximum=128
                    ),
                    "notice_version": _string(
                        r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$", maximum=128
                    ),
                },
                ("sha256", "limits", "policy_id", "notice_version"),
                additive=True,
            ),
            "coordinates": {"$ref": "#/components/schemas/SessionCoordinates"},
            "available_controls": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "snapshot",
                        "events",
                        "transcript",
                        "submit",
                        "cancel",
                        "reset",
                        "switch",
                        "close",
                    ],
                },
                "uniqueItems": True,
                "maxItems": 8,
            },
            "lifecycle": {"oneOf": [lifecycle_active, lifecycle_terminal]},
            "recovery": _object(
                {
                    "tombstone": {"type": "boolean"},
                    "retain_until_ms": _nullable(_integer()),
                },
                ("tombstone", "retain_until_ms"),
                additive=True,
            ),
            "command_slot": {
                "oneOf": [
                    _object(
                        {
                            "state": {"type": "string", "const": "available"},
                            "generation": _integer(),
                            "active_command_id": {"type": "null"},
                        },
                        ("state", "generation", "active_command_id"),
                    ),
                    _object(
                        {
                            "state": {"type": "string", "const": "occupied"},
                            "generation": _integer(minimum=1),
                            "active_command_id": _ref("CommandId"),
                        },
                        ("state", "generation", "active_command_id"),
                    ),
                    _object(
                        {
                            "state": {"type": "string", "const": "terminal"},
                            "generation": _integer(),
                            "active_command_id": {"type": "null"},
                        },
                        ("state", "generation", "active_command_id"),
                    ),
                ]
            },
            "latest_command": _nullable(_ref("CommandProjection")),
            "transcript": {"$ref": "#/components/schemas/TranscriptPage"},
        },
        (
            "session_id",
            "reference",
            "authority_class",
            "starting_revision",
            "launch_binding",
            "coordinates",
            "available_controls",
            "lifecycle",
            "recovery",
            "command_slot",
            "latest_command",
            "transcript",
        ),
        additive=True,
    )
    snapshot["allOf"] = [
        {
            "not": {
                "anyOf": [
                    {"required": [name]} for name in sorted(FORBIDDEN_PROJECTION_KEYS)
                ]
            }
        }
    ]
    return snapshot


def _request_response_schemas() -> dict[str, Any]:
    capability_state = _object(
        {
            "declared_revision": {"type": "string", "const": CONTRACT_REVISION},
            "enabled": {"type": "boolean", "const": False},
            "enabled_revision": {"type": "null"},
            "reason": {"type": "string", "const": "integration_preview_only"},
        },
        ("declared_revision", "enabled", "enabled_revision", "reason"),
    )
    capability_map = _object(
        {name: capability_state for name in CAPABILITY_NAMES},
        CAPABILITY_NAMES,
    )
    capabilities = _response_schema(
        {
            "service_epoch": _ref("ServiceEpoch"),
            "request_id": _ref("RequestId"),
            "admission": _object(
                {
                    "state": {"type": "string", "const": "closed"},
                    "reason": {"type": "string", "const": "integration_preview_only"},
                },
                ("state", "reason"),
            ),
            "capabilities": capability_map,
        },
        ("service_epoch", "request_id", "admission", "capabilities"),
    )
    safe_error = _response_schema(
        {
            "error": _object(
                {
                    "code": _string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64),
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                        "pattern": r"^[^\u0000-\u001f\u007f-\u009f]+$",
                    },
                    "retry": {
                        "type": "string",
                        "enum": [
                            "none",
                            "same_request",
                            "resume_snapshot",
                            "after_delay",
                        ],
                    },
                    "retry_after_ms": _nullable(_integer(minimum=1, maximum=60_000)),
                    "request_id": _ref("RequestId"),
                },
                ("code", "message", "retry", "retry_after_ms", "request_id"),
            )
        },
        ("error",),
    )
    create_request = _object({"launch_grant": _ref("LaunchGrant")}, ("launch_grant",))
    create_response = _response_schema(
        {
            "kind": {"type": "string", "const": "session_created"},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "session_authority": _ref("SessionAuthority"),
            "snapshot": _ref("SessionSnapshot"),
        },
        ("kind", "service_epoch", "replayed", "session_authority", "snapshot"),
    )
    no_current = _response_schema(
        {
            "kind": {"type": "string", "const": "no_current_session"},
            "service_epoch": _ref("ServiceEpoch"),
        },
        ("kind", "service_epoch"),
    )
    current_active = _response_schema(
        {
            "kind": {"type": "string", "const": "current_session"},
            "service_epoch": _ref("ServiceEpoch"),
            "session_authority": _ref("SessionAuthority"),
            "snapshot": _ref("SessionSnapshot"),
        },
        ("kind", "service_epoch", "session_authority", "snapshot"),
    )
    current_terminal = _response_schema(
        {
            "kind": {"type": "string", "const": "current_session_terminal"},
            "service_epoch": _ref("ServiceEpoch"),
            "snapshot": _ref("SessionSnapshot"),
        },
        ("kind", "service_epoch", "snapshot"),
    )
    snapshot_response = _response_schema(
        {
            "kind": {"type": "string", "const": "session_snapshot"},
            "service_epoch": _ref("ServiceEpoch"),
            "snapshot": _ref("SessionSnapshot"),
        },
        ("kind", "service_epoch", "snapshot"),
    )
    submit_request = _object(
        {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "pattern": r"^[^\u0000\r\n]*$",
                "x-dreamland-classifier": "product-single-v1",
                "x-dreamland-canonicalization": "trim-leading-trailing-ascii-sp-ht-only",
            },
            "expected_slot_generation": _integer(),
        },
        ("command", "expected_slot_generation"),
    )
    submit_response = _response_schema(
        {
            "kind": {"type": "string", "const": "command_accepted"},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "command_id": _ref("CommandId"),
            "canonical_command": {"type": "string", "minLength": 1, "maxLength": 4096},
            "slot_generation": _integer(minimum=1),
            "event_cursor": _ref("EventCursor"),
            "request_id": _ref("RequestId"),
        },
        (
            "kind",
            "service_epoch",
            "replayed",
            "command_id",
            "canonical_command",
            "slot_generation",
            "event_cursor",
            "request_id",
        ),
    )
    command_result = _response_schema(
        {
            "kind": {"type": "string", "const": "command_result"},
            "service_epoch": _ref("ServiceEpoch"),
            "session_id": _ref("SessionId"),
            "terminal": {"type": "boolean"},
            "command": _ref("CommandProjection"),
            "event_cursor": _ref("EventCursor"),
        },
        ("kind", "service_epoch", "session_id", "terminal", "command", "event_cursor"),
    )
    cancel_request = _object(
        {"slot_generation": _integer(minimum=1)}, ("slot_generation",)
    )
    cancel_requested = _response_schema(
        {
            "kind": {"type": "string", "const": "command_cancel"},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "outcome": {"type": "string", "const": "cancel_requested"},
            "command_id": _ref("CommandId"),
            "slot_generation": _integer(minimum=1),
            "event_cursor": _ref("EventCursor"),
        },
        (
            "kind",
            "service_epoch",
            "replayed",
            "outcome",
            "command_id",
            "slot_generation",
            "event_cursor",
        ),
    )
    cancel_terminal = _response_schema(
        {
            "kind": {"type": "string", "const": "command_cancel"},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "outcome": {"type": "string", "const": "already_terminal"},
            "command_id": _ref("CommandId"),
            "slot_generation": _integer(minimum=1),
            "command_result": _ref("TerminalCommandResult"),
            "event_cursor": _ref("EventCursor"),
        },
        (
            "kind",
            "service_epoch",
            "replayed",
            "outcome",
            "command_id",
            "slot_generation",
            "command_result",
            "event_cursor",
        ),
    )
    event_page = _response_schema(
        {
            "kind": {"type": "string", "const": "event_page"},
            "service_epoch": _ref("ServiceEpoch"),
            "events": _array(
                {"$ref": "events.schema.json#/$defs/ProductEvent"}, maximum=256
            ),
            "retention_floor": _ref("EventCursor"),
            "position": _ref("EventCursor"),
            "retained_payload_bytes": _integer(maximum=262_144),
        },
        (
            "kind",
            "service_epoch",
            "events",
            "retention_floor",
            "position",
            "retained_payload_bytes",
        ),
    )
    transcript_response = _response_schema(
        {
            "kind": {"type": "string", "const": "transcript_page"},
            "service_epoch": _ref("ServiceEpoch"),
            "entries": _array(_ref("TranscriptEntry"), maximum=512),
            "retention_floor": _ref("TranscriptCursor"),
            "position": _ref("TranscriptCursor"),
            "omitted_before_entries": _integer(),
            "omitted_after_entries": _integer(),
            "retained_text_bytes": _integer(maximum=1_048_576),
        },
        (
            "kind",
            "service_epoch",
            "entries",
            "retention_floor",
            "position",
            "omitted_before_entries",
            "omitted_after_entries",
            "retained_text_bytes",
        ),
    )
    control_request = _object(
        {"expected_control_revision": _integer(minimum=1)},
        ("expected_control_revision",),
    )
    switch_request = _object(
        {
            "expected_control_revision": _integer(minimum=1),
            "launch_grant": _ref("LaunchGrant"),
        },
        ("expected_control_revision", "launch_grant"),
    )
    replacement_response = _response_schema(
        {
            "kind": {"type": "string", "enum": ["session_reset", "session_switched"]},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "session_authority": _ref("SessionAuthority"),
            "previous_snapshot": _ref("SessionSnapshot"),
            "snapshot": _ref("SessionSnapshot"),
        },
        (
            "kind",
            "service_epoch",
            "replayed",
            "session_authority",
            "previous_snapshot",
            "snapshot",
        ),
    )
    close_response = _response_schema(
        {
            "kind": {"type": "string", "const": "session_closed"},
            "service_epoch": _ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "snapshot": _ref("SessionSnapshot"),
        },
        ("kind", "service_epoch", "replayed", "snapshot"),
    )
    return {
        "CapabilityState": capability_state,
        "ProductCapabilitiesResponse": capabilities,
        "SafeErrorEnvelope": safe_error,
        "CreateSessionRequest": create_request,
        "CreateSessionResponse": create_response,
        "NoCurrentSessionResponse": no_current,
        "CurrentActiveSessionResponse": current_active,
        "CurrentTerminalSessionResponse": current_terminal,
        "CurrentSessionResponse": {
            "oneOf": [
                _ref("NoCurrentSessionResponse"),
                _ref("CurrentActiveSessionResponse"),
                _ref("CurrentTerminalSessionResponse"),
            ]
        },
        "SnapshotResponse": snapshot_response,
        "SubmitCommandRequest": submit_request,
        "SubmitCommandResponse": submit_response,
        "CommandResultResponse": command_result,
        "CancelCommandRequest": cancel_request,
        "CancelRequestedResponse": cancel_requested,
        "CancelAlreadyTerminalResponse": cancel_terminal,
        "CancelCommandResponse": {
            "oneOf": [
                _ref("CancelRequestedResponse"),
                _ref("CancelAlreadyTerminalResponse"),
            ]
        },
        "EventPageResponse": event_page,
        "TranscriptPageResponse": transcript_response,
        "ResetSessionRequest": control_request,
        "SwitchSessionRequest": switch_request,
        "CloseSessionRequest": control_request,
        "ReplacementResponse": replacement_response,
        "CloseSessionResponse": close_response,
    }


def _fixture_only_schemas() -> dict[str, Any]:
    authority = _object(
        {
            "actor_context": _ref("ActorContext"),
            "role": {"type": "string", "enum": ["attendee", "presenter"]},
            "actor_id": _string(r"^fixture-actor-[a-z0-9-]{1,48}$", maximum=64),
            "expires_at_ms": _integer(),
        },
        ("actor_context", "role", "actor_id", "expires_at_ms"),
    )
    grant = _object(
        {
            "launch_grant": _ref("LaunchGrant"),
            "actor_id": _string(r"^fixture-actor-[a-z0-9-]{1,48}$", maximum=64),
            "role": {"type": "string", "enum": ["attendee", "presenter"]},
            "selected_record_id": _ref("SelectedRecordId"),
            "revision_id": _ref("ImmutableRevisionId"),
            "expires_at_ms": _integer(),
            "allowed_operations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["session.create", "session.switch"],
                },
                "uniqueItems": True,
                "minItems": 1,
                "maxItems": 2,
            },
            "limits": {
                "type": "object",
                "additionalProperties": _integer(minimum=1),
                "maxProperties": 32,
            },
            "policy_id": {"type": "string"},
            "notice_version": {"type": "string"},
        },
        (
            "launch_grant",
            "actor_id",
            "role",
            "selected_record_id",
            "revision_id",
            "expires_at_ms",
            "allowed_operations",
            "limits",
            "policy_id",
            "notice_version",
        ),
    )
    recovery = _object(
        {
            "scenario": {
                "type": "string",
                "enum": [
                    "create_response_lost_after_commit",
                    "command_accept_response_lost",
                    "promised_terminal_event_withheld",
                    "cancel_ack_response_lost",
                    "event_cursor_evicted",
                    "epoch_rotated_after_uncertain_mutation",
                    "forced_terminal_usage_settles_late",
                ],
            },
            "same_key_replay": {"type": "boolean"},
            "automatic_new_key_redispatch": {"type": "boolean", "const": False},
            "reconcile_with": {
                "type": "string",
                "enum": [
                    "same_request",
                    "command_result",
                    "snapshot",
                    "fresh_epoch_binding",
                ],
            },
            "expected_code": _nullable(_string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64)),
            "expected_outcome": _nullable(
                _string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64)
            ),
        },
        (
            "scenario",
            "same_key_replay",
            "automatic_new_key_redispatch",
            "reconcile_with",
            "expected_code",
            "expected_outcome",
        ),
    )
    return {
        "IntegrationAuthorityFixture": authority,
        "IntegrationLaunchGrantFixture": grant,
        "IntegrationRecoveryFixture": recovery,
    }


def build_events_schema() -> dict[str, Any]:
    """Return the bounded typed-event schema used by JSON replay and SSE."""
    safe_data = {
        "type": "object",
        "propertyNames": {"not": {"enum": sorted(FORBIDDEN_PROJECTION_KEYS)}},
        "maxProperties": 256,
        "additionalProperties": True,
    }
    envelope = _object(
        {
            "schema_revision": {"type": "string", "const": "1"},
            "stream_id": _string(r"^stream:[A-Za-z0-9_-]{8,96}$", maximum=103),
            "sequence": _integer(minimum=1),
            "event_id": _string(
                r"^event:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$", maximum=119
            ),
            "type": _string(r"^[a-z][a-z0-9_.-]{0,127}$", maximum=128),
            "criticality": {
                "type": "string",
                "enum": ["authoritative", "informational"],
            },
            "emitted_at_ms": _integer(),
            "command_id": _nullable(
                _string(r"^command:[A-Za-z0-9_-]{16,128}$", maximum=136)
            ),
            "data": safe_data,
        },
        (
            "schema_revision",
            "stream_id",
            "sequence",
            "event_id",
            "type",
            "criticality",
            "emitted_at_ms",
            "command_id",
            "data",
        ),
        additive=True,
    )
    defs: dict[str, Any] = {
        "EventEnvelope": envelope,
        "SessionCreatedData": _object(
            {
                "control_revision": _integer(minimum=1),
                "slot_generation": {"type": "integer", "const": 0},
                "accepted_state_revision": {"type": "integer", "const": 0},
            },
            ("control_revision", "slot_generation", "accepted_state_revision"),
            additive=True,
        ),
        "CommandAcceptedData": _object(
            {
                "slot_generation": _integer(minimum=1),
                "accepted_state_revision": _integer(),
                "transcript_position": _integer(minimum=1),
            },
            ("slot_generation", "accepted_state_revision", "transcript_position"),
            additive=True,
        ),
        "CommandRunningData": _object(
            {"slot_generation": _integer(minimum=1)},
            ("slot_generation",),
            additive=True,
        ),
        "CommandOutputData": _object(
            {
                "stream": {"type": "string", "enum": ["stdout", "stderr"]},
                "chunk_index": _integer(),
                "text": {"type": "string", "maxLength": 16384},
                "encoding": {"type": "string", "const": "utf-8"},
                "final": {"type": "boolean"},
                "clipped": {"type": "boolean"},
                "omitted_before_bytes": _integer(),
                "omitted_after_bytes": _integer(),
                "transformed": {"type": "boolean"},
                "projection": {"type": "string", "const": "inert-text-v1"},
                "transcript_position": _integer(minimum=1),
            },
            (
                "stream",
                "chunk_index",
                "text",
                "encoding",
                "final",
                "clipped",
                "omitted_before_bytes",
                "omitted_after_bytes",
                "transformed",
                "projection",
                "transcript_position",
            ),
            additive=True,
        ),
        "CommandCancelRequestedData": _object(
            {"slot_generation": _integer(minimum=1)},
            ("slot_generation",),
            additive=True,
        ),
        "ForegroundStateAcceptedData": _object(
            {
                "source": {"type": "string", "const": "foreground_command"},
                "slot_generation": _integer(minimum=1),
                "previous_accepted_state_revision": _integer(),
                "accepted_state_revision": _integer(minimum=1),
                "replacement_sha256": _string(
                    r"^[0-9a-f]{64}$", minimum=64, maximum=64
                ),
            },
            (
                "source",
                "slot_generation",
                "previous_accepted_state_revision",
                "accepted_state_revision",
                "replacement_sha256",
            ),
            additive=True,
        ),
        "EnrichmentStateAcceptedData": _object(
            {
                "source": {"type": "string", "const": "enrichment"},
                "proposal_id": _string(
                    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", maximum=128
                ),
                "previous_accepted_state_revision": _integer(),
                "accepted_state_revision": _integer(minimum=1),
                "replacement_sha256": _string(
                    r"^[0-9a-f]{64}$", minimum=64, maximum=64
                ),
            },
            (
                "source",
                "proposal_id",
                "previous_accepted_state_revision",
                "accepted_state_revision",
                "replacement_sha256",
            ),
            additive=True,
        ),
        "CommandTerminalData": _object(
            {
                "slot_generation": _integer(minimum=1),
                "accepted_state_revision": _integer(),
                "outcome": {
                    "type": "string",
                    "enum": ["completed", "interrupted", "failed"],
                },
                "output_clipped": {"type": "boolean"},
            },
            ("slot_generation", "accepted_state_revision", "outcome", "output_clipped"),
            additive=True,
        ),
        "SessionTerminalData": _object(
            {
                "control_revision": _integer(minimum=2),
                "terminal_reason": _string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64),
                "slot_generation": _integer(),
                "accepted_state_revision": _integer(),
            },
            (
                "control_revision",
                "terminal_reason",
                "slot_generation",
                "accepted_state_revision",
            ),
            additive=True,
        ),
    }
    conditions: list[dict[str, Any]] = []
    known = {
        "session.created": ("authoritative", "SessionCreatedData", False),
        "command.accepted": ("authoritative", "CommandAcceptedData", True),
        "command.running": ("informational", "CommandRunningData", True),
        "command.output": ("informational", "CommandOutputData", True),
        "command.cancel_requested": (
            "authoritative",
            "CommandCancelRequestedData",
            True,
        ),
        "session.terminal": ("authoritative", "SessionTerminalData", False),
    }
    for event_type, (criticality, data_name, command_required) in known.items():
        then_properties: dict[str, Any] = {
            "criticality": {"const": criticality},
            "data": {"$ref": f"#/$defs/{data_name}"},
        }
        then_properties["command_id"] = (
            _string(r"^command:[A-Za-z0-9_-]{16,128}$", maximum=136)
            if command_required
            else {"type": "null"}
        )
        conditions.append(
            {
                "if": {
                    "properties": {"type": {"const": event_type}},
                    "required": ["type"],
                },
                "then": {"properties": then_properties},
            }
        )
    conditions.append(
        {
            "if": {
                "properties": {"type": {"const": "state.accepted"}},
                "required": ["type"],
            },
            "then": {
                "properties": {
                    "criticality": {"const": "authoritative"},
                    "data": {
                        "oneOf": [
                            {"$ref": "#/$defs/ForegroundStateAcceptedData"},
                            {"$ref": "#/$defs/EnrichmentStateAcceptedData"},
                        ]
                    },
                }
            },
        }
    )
    for outcome in ("completed", "interrupted", "failed"):
        conditions.append(
            {
                "if": {
                    "properties": {"type": {"const": f"command.{outcome}"}},
                    "required": ["type"],
                },
                "then": {
                    "properties": {
                        "criticality": {"const": "authoritative"},
                        "command_id": _string(
                            r"^command:[A-Za-z0-9_-]{16,128}$", maximum=136
                        ),
                        "data": {
                            "allOf": [
                                {"$ref": "#/$defs/CommandTerminalData"},
                                {"properties": {"outcome": {"const": outcome}}},
                            ]
                        },
                    }
                },
            }
        )
    defs["ProductEvent"] = {"allOf": [{"$ref": "#/$defs/EventEnvelope"}, *conditions]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://dreamland.invalid/contracts/product-api/{CONTRACT_DIRECTORY}/events.schema.json",
        "title": "DREAMLAND Product Session Core Preview Event",
        "description": "Unfrozen bounded event envelope for JSON replay and SSE data frames.",
        "$ref": "#/$defs/ProductEvent",
        "$defs": defs,
        "x-dreamland-unknown-event-policy": {
            "informational": "ignore_payload_and_advance_cursor",
            "authoritative": "stop_incremental_apply_and_resume_snapshot",
        },
        "x-dreamland-retention": {
            "maximum_events": 256,
            "maximum_retained_payload_bytes": 262144,
            "maximum_event_payload_bytes": 16384,
        },
    }


def build_manifest_schema() -> dict[str, Any]:
    sha = _string(r"^[0-9a-f]{64}$", minimum=64, maximum=64)
    capability = _object(
        {
            "declared_revision": {"type": "string", "const": CONTRACT_REVISION},
            "enabled": {"type": "boolean", "const": False},
            "enabled_revision": {"type": "null"},
            "reason": {"type": "string", "const": "integration_preview_only"},
        },
        ("declared_revision", "enabled", "enabled_revision", "reason"),
    )
    command_prefix = (
        ".venv/bin/python tools/product_session_contract.py"
        f" {{verb}} contracts/product-api/{CONTRACT_DIRECTORY}"
    )
    schema = _object(
        {
            "schema_version": {"type": "integer", "const": 1},
            "contract_name": {"type": "string", "const": CONTRACT_NAME},
            "contract_revision": {"type": "string", "const": CONTRACT_REVISION},
            "compatibility": {"type": "string", "const": CONTRACT_COMPATIBILITY},
            "freeze_status": {
                "type": "string",
                "const": "pending_ui_tracked_doc_acknowledgement",
            },
            "implementation_status": {
                "type": "string",
                "const": "integration_preview_only",
            },
            "transport_status": {
                "type": "string",
                "const": "manually_invoked_loopback_preview_only",
            },
            "publication_status": {
                "type": "string",
                "enum": ["release_candidate", "published"],
            },
            "backend_commit": {
                "oneOf": [
                    {"type": "null"},
                    _string(r"^[0-9a-f]{40}$", minimum=40, maximum=40),
                ]
            },
            "published_at": {
                "oneOf": [
                    {"type": "null"},
                    {"type": "string", "format": "date-time", "maxLength": 64},
                ]
            },
            "capabilities": _object(
                {name: capability for name in CAPABILITY_NAMES},
                CAPABILITY_NAMES,
            ),
            "artifacts": _object(
                {key: sha for key in CONTRACT_FILES},
                tuple(CONTRACT_FILES),
            ),
            "fixture_set": _object(
                {
                    "path": {"type": "string", "const": "fixtures/index.json"},
                    "index_sha256": sha,
                },
                ("path", "index_sha256"),
            ),
            "generation": _object(
                {
                    "validate_command": {
                        "type": "string",
                        "const": command_prefix.format(verb="validate"),
                    },
                    "refresh_command": {
                        "type": "string",
                        "const": command_prefix.format(verb="refresh"),
                    },
                    "client_smoke_command": {
                        "type": "string",
                        "const": command_prefix.format(verb="client-smoke"),
                    },
                    "publish_command": {
                        "type": "string",
                        "const": (
                            command_prefix.format(verb="publish")
                            + " --backend-commit COMMIT --published-at TIMESTAMP"
                        ),
                    },
                },
                (
                    "validate_command",
                    "refresh_command",
                    "client_smoke_command",
                    "publish_command",
                ),
            ),
            "event_transport": _object(
                {
                    "json_replay": {"type": "boolean", "const": True},
                    "sse": {"type": "boolean", "const": True},
                    "delivery": {"type": "string", "const": "at-least-once"},
                },
                ("json_replay", "sse", "delivery"),
            ),
        },
        (
            "schema_version",
            "contract_name",
            "contract_revision",
            "compatibility",
            "freeze_status",
            "implementation_status",
            "transport_status",
            "publication_status",
            "backend_commit",
            "published_at",
            "capabilities",
            "artifacts",
            "fixture_set",
            "generation",
            "event_transport",
        ),
    )
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://dreamland.invalid/contracts/product-api/{CONTRACT_DIRECTORY}/manifest.schema.json",
            "title": "DREAMLAND Product Session Core Preview Manifest",
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "publication_status": {"const": "release_candidate"}
                        }
                    },
                    "then": {
                        "properties": {
                            "backend_commit": {"type": "null"},
                            "published_at": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"publication_status": {"const": "published"}}
                    },
                    "then": {
                        "properties": {
                            "backend_commit": _string(
                                r"^[0-9a-f]{40}$", minimum=40, maximum=40
                            ),
                            "published_at": {
                                "type": "string",
                                "format": "date-time",
                                "maxLength": 64,
                            },
                        }
                    },
                },
            ],
        }
    )
    return schema


def _base_manifest() -> dict[str, Any]:
    command_prefix = (
        ".venv/bin/python tools/product_session_contract.py"
        f" {{verb}} contracts/product-api/{CONTRACT_DIRECTORY}"
    )
    return {
        "schema_version": 1,
        "contract_name": CONTRACT_NAME,
        "contract_revision": CONTRACT_REVISION,
        "compatibility": CONTRACT_COMPATIBILITY,
        "freeze_status": "pending_ui_tracked_doc_acknowledgement",
        "implementation_status": "integration_preview_only",
        "transport_status": "manually_invoked_loopback_preview_only",
        "publication_status": "release_candidate",
        "backend_commit": None,
        "published_at": None,
        "capabilities": {
            name: {
                "declared_revision": CONTRACT_REVISION,
                "enabled": False,
                "enabled_revision": None,
                "reason": "integration_preview_only",
            }
            for name in CAPABILITY_NAMES
        },
        "artifacts": {key: "0" * 64 for key in CONTRACT_FILES},
        "fixture_set": {"path": "fixtures/index.json", "index_sha256": "0" * 64},
        "generation": {
            "validate_command": command_prefix.format(verb="validate"),
            "refresh_command": command_prefix.format(verb="refresh"),
            "client_smoke_command": command_prefix.format(verb="client-smoke"),
            "publish_command": (
                command_prefix.format(verb="publish")
                + " --backend-commit COMMIT --published-at TIMESTAMP"
            ),
        },
        "event_transport": {
            "json_replay": True,
            "sse": True,
            "delivery": "at-least-once",
        },
    }


_EPOCH = "epoch:phase2preview0001"
_SESSION_ID = "session:phase2_alpha_0001"
_REFERENCE = "session-ref:phase2_alpha_0001"
_SESSION_AUTHORITY = "session-cap:phase2_alpha_session_cap_000001"
_COMMAND_ID = "command:11111111111111111111111111111111"
_STREAM_ID = "stream:phase2alphaevents"
_TRANSCRIPT_ID = "transcript:phase2alphatranscript"
_REVISION_ID = "world-revision:phase2_alpha_revision_0001"
_SELECTED_ID = "selected-record:phase2_alpha_0001"
_SHA = "a" * 64
_BINDING_SHA = "b" * 64


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
            "executor": "deterministic-no-provider-v1",
            "provider_calls": False,
            "live_capability_evidence": False,
        },
    }


def _event_cursor(sequence: int) -> dict[str, Any]:
    return {
        "stream_id": _STREAM_ID,
        "sequence": sequence,
        "event_id": None if sequence == 0 else f"event:phase2alphaevent:{sequence}",
    }


def _transcript_cursor(position: int) -> dict[str, Any]:
    return {
        "transcript_id": _TRANSCRIPT_ID,
        "position": position,
        "entry_id": None if position == 0 else f"entry:phase2alphaentry:{position}",
    }


def _transcript_entry(
    position: int,
    kind: str,
    text: str,
    *,
    chunk_index: int | None,
    final: bool | None,
) -> dict[str, Any]:
    return {
        "transcript_id": _TRANSCRIPT_ID,
        "position": position,
        "entry_id": f"entry:phase2alphaentry:{position}",
        "kind": kind,
        "recorded_at_ms": 1_777_000_000_000 + position,
        "command_id": _COMMAND_ID,
        "text": text,
        "encoding": "utf-8",
        "projection": "inert-text-v1",
        "transformed": False,
        "clipped": False,
        "omitted_before_bytes": 0,
        "omitted_after_bytes": 0,
        "chunk_index": chunk_index,
        "final": final,
    }


def _transcript_page(position: int = 0) -> dict[str, Any]:
    entries = []
    if position >= 1:
        entries.append(
            _transcript_entry(1, "command", "uname -a", chunk_index=None, final=None)
        )
    if position >= 2:
        entries.append(
            _transcript_entry(
                2, "stdout", "Linux dreamland\n", chunk_index=0, final=False
            )
        )
    if position >= 3:
        entries.append(_transcript_entry(3, "stdout", "", chunk_index=1, final=True))
    return {
        "kind": "transcript_page",
        "entries": entries,
        "retention_floor": _transcript_cursor(0),
        "position": _transcript_cursor(position),
        "omitted_before_entries": 0,
        "omitted_after_entries": 0,
        "retained_text_bytes": sum(len(item["text"].encode()) for item in entries),
    }


def _terminal_result(
    outcome: str,
    *,
    state_effect: str = "none",
    pending_usage: bool = False,
) -> dict[str, Any]:
    usage: dict[str, Any] = (
        {"state": "pending", "tokens": None, "spend_units": None}
        if pending_usage
        else {"state": "settled", "tokens": 37, "spend_units": 120}
    )
    result: dict[str, Any] = {
        "outcome": outcome,
        "output_clipped": False,
        "usage": usage,
    }
    if outcome == "completed":
        result["guest_termination"] = {"kind": "exited", "exit_status": 0}
        effect: dict[str, Any] = {"kind": state_effect}
        if state_effect == "committed":
            effect["accepted_state_revision"] = 1
        elif state_effect == "rejected":
            effect["reason"] = "state_validation_rejected"
        result["state_effect"] = effect
    else:
        result["reason"] = (
            "cancelled" if outcome == "interrupted" else "command_deadline_exceeded"
        )
    return result


def _command_projection(
    status: str,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    terminal = status in {"completed", "interrupted", "failed"}
    return {
        "command_id": _COMMAND_ID,
        "slot_generation": 1,
        "status": status,
        "accepted_at_ms": 1_777_000_000_010,
        "deadline_at_ms": 1_777_000_030_010,
        "terminal_at_ms": 1_777_000_000_300 if terminal else None,
        "output": {
            "bytes": 16 if terminal else 0,
            "chunks": 1 if terminal else 0,
            "clipped": False,
        },
        "result": result,
    }


def _snapshot(
    *,
    state: str = "active",
    command: dict[str, Any] | None = None,
    event_sequence: int = 1,
    transcript_position: int = 0,
    reference: str = _REFERENCE,
    session_id: str = _SESSION_ID,
    control_revision: int = 1,
) -> dict[str, Any]:
    terminal = state != "active"
    generation = 0 if command is None else int(command["slot_generation"])
    occupied = command is not None and command["status"] not in {
        "completed",
        "interrupted",
        "failed",
    }
    lifecycle: dict[str, Any] = {
        "state": state,
        "created_at_ms": 1_777_000_000_000,
        "last_activity_at_ms": 1_777_000_000_010,
        "absolute_expires_at_ms": 1_777_003_600_000,
        "idle_expires_at_ms": 1_777_001_200_000,
    }
    if terminal:
        lifecycle.update(
            {
                "terminal_at_ms": 1_777_000_001_000,
                "terminal_reason": "replaced"
                if state == "replaced"
                else ("closed" if state == "closed" else "idle_expired"),
            }
        )
    controls = (
        []
        if terminal
        else ["snapshot", "events", "transcript", "submit", "reset", "switch", "close"]
    )
    if occupied:
        controls.append("cancel")
    return {
        "session_id": session_id,
        "reference": reference,
        "authority_class": "attendee",
        "starting_revision": {
            "selected_record_id": _SELECTED_ID,
            "revision_id": _REVISION_ID,
            "sha256": _SHA,
        },
        "launch_binding": {
            "sha256": _BINDING_SHA,
            "limits": {
                "absolute_ttl_ms": 3_600_000,
                "idle_ttl_ms": 1_200_000,
                "max_commands": 16,
                "max_command_bytes": 4096,
                "command_reserved_tokens": 2048,
                "command_reserved_spend_units": 1000,
            },
            "policy_id": "preview-policy-v1",
            "notice_version": "preview-notice-v1",
        },
        "coordinates": {
            "control_revision": control_revision,
            "command_slot_generation": generation,
            "accepted_state_revision": 1
            if command and result_committed(command)
            else 0,
            "event": _event_cursor(event_sequence),
            "transcript": _transcript_cursor(transcript_position),
        },
        "available_controls": controls,
        "lifecycle": lifecycle,
        "recovery": {
            "tombstone": terminal,
            "retain_until_ms": 1_777_000_601_000 if terminal else None,
        },
        "command_slot": {
            "state": "terminal"
            if terminal
            else ("occupied" if occupied else "available"),
            "generation": generation,
            "active_command_id": _COMMAND_ID if occupied else None,
        },
        "latest_command": command,
        "transcript": _transcript_page(transcript_position),
    }


def result_committed(command: dict[str, Any]) -> bool:
    result = command.get("result")
    return bool(
        isinstance(result, dict)
        and isinstance(result.get("state_effect"), dict)
        and result["state_effect"].get("kind") == "committed"
    )


def _event(
    sequence: int,
    event_type: str,
    criticality: str,
    data: dict[str, Any],
    *,
    command_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_revision": "1",
        "stream_id": _STREAM_ID,
        "sequence": sequence,
        "event_id": f"event:phase2alphaevent:{sequence}",
        "type": event_type,
        "criticality": criticality,
        "emitted_at_ms": 1_777_000_000_000 + sequence,
        "command_id": command_id,
        "data": data,
    }


def _known_events() -> list[dict[str, Any]]:
    return [
        _event(
            1,
            "session.created",
            "authoritative",
            {"control_revision": 1, "slot_generation": 0, "accepted_state_revision": 0},
            command_id=None,
        ),
        _event(
            2,
            "command.accepted",
            "authoritative",
            {
                "slot_generation": 1,
                "accepted_state_revision": 0,
                "transcript_position": 1,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            3,
            "command.running",
            "informational",
            {"slot_generation": 1},
            command_id=_COMMAND_ID,
        ),
        _event(
            4,
            "command.output",
            "informational",
            {
                "stream": "stdout",
                "chunk_index": 0,
                "text": "Linux dreamland\n",
                "encoding": "utf-8",
                "final": False,
                "clipped": False,
                "omitted_before_bytes": 0,
                "omitted_after_bytes": 0,
                "transformed": False,
                "projection": "inert-text-v1",
                "transcript_position": 2,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            5,
            "command.cancel_requested",
            "authoritative",
            {"slot_generation": 1},
            command_id=_COMMAND_ID,
        ),
        _event(
            6,
            "state.accepted",
            "authoritative",
            {
                "source": "foreground_command",
                "slot_generation": 1,
                "previous_accepted_state_revision": 0,
                "accepted_state_revision": 1,
                "replacement_sha256": "c" * 64,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            7,
            "command.completed",
            "authoritative",
            {
                "slot_generation": 1,
                "accepted_state_revision": 1,
                "outcome": "completed",
                "output_clipped": False,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            8,
            "command.interrupted",
            "authoritative",
            {
                "slot_generation": 1,
                "accepted_state_revision": 0,
                "outcome": "interrupted",
                "output_clipped": False,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            9,
            "command.failed",
            "authoritative",
            {
                "slot_generation": 1,
                "accepted_state_revision": 0,
                "outcome": "failed",
                "output_clipped": False,
            },
            command_id=_COMMAND_ID,
        ),
        _event(
            10,
            "session.terminal",
            "authoritative",
            {
                "control_revision": 2,
                "terminal_reason": "closed",
                "slot_generation": 1,
                "accepted_state_revision": 1,
            },
            command_id=None,
        ),
    ]


def _success(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, **_context()}


def fixture_definitions() -> tuple[Fixture, ...]:
    fixtures: list[Fixture] = []

    def add(
        case: str,
        classification: str,
        path: str,
        target: str,
        payload: Any,
        action: str,
    ) -> None:
        fixtures.append(Fixture(case, classification, path, target, payload, action))

    add(
        "capabilities_all_disabled",
        "canonical-valid",
        "fixtures/canonical-valid/capabilities/all-disabled.json",
        "openapi.json#/components/schemas/ProductCapabilitiesResponse",
        _success(
            {
                "service_epoch": _EPOCH,
                "request_id": "request:phase2_capabilities_0001",
                "admission": {"state": "closed", "reason": "integration_preview_only"},
                "capabilities": {
                    name: {
                        "declared_revision": CONTRACT_REVISION,
                        "enabled": False,
                        "enabled_revision": None,
                        "reason": "integration_preview_only",
                    }
                    for name in CAPABILITY_NAMES
                },
            }
        ),
        "fail_closed",
    )
    add(
        "authority_alpha",
        "canonical-valid",
        "fixtures/canonical-valid/authority/actor-alpha.json",
        "openapi.json#/components/schemas/IntegrationAuthorityFixture",
        {
            "actor_context": "actor:phase2_alpha_context_0001",
            "role": "attendee",
            "actor_id": "fixture-actor-alpha",
            "expires_at_ms": 1_777_003_600_000,
        },
        "server_side_fixture_only",
    )
    add(
        "launch_grant_alpha",
        "canonical-valid",
        "fixtures/canonical-valid/authority/launch-grant-alpha.json",
        "openapi.json#/components/schemas/IntegrationLaunchGrantFixture",
        {
            "launch_grant": "launch-grant:phase2_alpha_grant_00000001",
            "actor_id": "fixture-actor-alpha",
            "role": "attendee",
            "selected_record_id": _SELECTED_ID,
            "revision_id": _REVISION_ID,
            "expires_at_ms": 1_777_003_600_000,
            "allowed_operations": ["session.create", "session.switch"],
            "limits": {
                "absolute_ttl_ms": 3_600_000,
                "idle_ttl_ms": 1_200_000,
                "max_commands": 16,
                "max_command_bytes": 4096,
            },
            "policy_id": "preview-policy-v1",
            "notice_version": "preview-notice-v1",
        },
        "server_side_fixture_only",
    )
    active_snapshot = _snapshot()
    create_request = {"launch_grant": "launch-grant:phase2_alpha_grant_00000001"}
    create_success = _success(
        {
            "kind": "session_created",
            "service_epoch": _EPOCH,
            "replayed": False,
            "session_authority": _SESSION_AUTHORITY,
            "snapshot": active_snapshot,
        }
    )
    add(
        "create_request",
        "canonical-valid",
        "fixtures/canonical-valid/sessions/create-request.json",
        "openapi.json#/components/schemas/CreateSessionRequest",
        create_request,
        "send",
    )
    add(
        "create_success",
        "canonical-valid",
        "fixtures/canonical-valid/sessions/create-success.json",
        "openapi.json#/components/schemas/CreateSessionResponse",
        create_success,
        "accept",
    )
    add(
        "create_replay",
        "canonical-valid",
        "fixtures/canonical-valid/sessions/create-replay.json",
        "openapi.json#/components/schemas/CreateSessionResponse",
        {**create_success, "replayed": True},
        "accept_recorded_result",
    )
    add(
        "current_none",
        "canonical-valid",
        "fixtures/canonical-valid/current/none.json",
        "openapi.json#/components/schemas/CurrentSessionResponse",
        _success({"kind": "no_current_session", "service_epoch": _EPOCH}),
        "accept",
    )
    add(
        "current_active",
        "canonical-valid",
        "fixtures/canonical-valid/current/active.json",
        "openapi.json#/components/schemas/CurrentSessionResponse",
        _success(
            {
                "kind": "current_session",
                "service_epoch": _EPOCH,
                "session_authority": _SESSION_AUTHORITY,
                "snapshot": active_snapshot,
            }
        ),
        "accept",
    )
    add(
        "snapshot_active",
        "canonical-valid",
        "fixtures/canonical-valid/snapshots/active.json",
        "openapi.json#/components/schemas/SnapshotResponse",
        _success(
            {
                "kind": "session_snapshot",
                "service_epoch": _EPOCH,
                "snapshot": active_snapshot,
            }
        ),
        "replace_local_projection",
    )

    submit_request = {"command": "uname -a", "expected_slot_generation": 0}
    accepted = _success(
        {
            "kind": "command_accepted",
            "service_epoch": _EPOCH,
            "replayed": False,
            "command_id": _COMMAND_ID,
            "canonical_command": "uname -a",
            "slot_generation": 1,
            "event_cursor": _event_cursor(2),
            "request_id": "request:phase2_command_0001",
        }
    )
    add(
        "submit_request",
        "canonical-valid",
        "fixtures/canonical-valid/commands/submit-request.json",
        "openapi.json#/components/schemas/SubmitCommandRequest",
        submit_request,
        "send_after_server_classifier",
    )
    add(
        "command_accepted",
        "canonical-valid",
        "fixtures/canonical-valid/commands/accepted.json",
        "openapi.json#/components/schemas/SubmitCommandResponse",
        accepted,
        "track_exact_command",
    )
    add(
        "command_accepted_replay",
        "canonical-valid",
        "fixtures/canonical-valid/commands/accepted-replay.json",
        "openapi.json#/components/schemas/SubmitCommandResponse",
        {**accepted, "replayed": True},
        "accept_recorded_result",
    )
    running_command = _command_projection("running")
    add(
        "command_running",
        "canonical-valid",
        "fixtures/canonical-valid/commands/result-running.json",
        "openapi.json#/components/schemas/CommandResultResponse",
        _success(
            {
                "kind": "command_result",
                "service_epoch": _EPOCH,
                "session_id": _SESSION_ID,
                "terminal": False,
                "command": running_command,
                "event_cursor": _event_cursor(3),
            }
        ),
        "wait_or_reconcile",
    )

    completed_none = _terminal_result("completed", state_effect="none")
    completed_committed = _terminal_result("completed", state_effect="committed")
    completed_signaled = _terminal_result("completed", state_effect="rejected")
    completed_signaled["guest_termination"] = {"kind": "signaled", "signal": "SIGTERM"}
    result_cases = [
        (
            "completed_exited_none",
            "completed-exited-none.json",
            _command_projection("completed", result=completed_none),
        ),
        (
            "completed_exited_committed",
            "completed-exited-committed.json",
            _command_projection("completed", result=completed_committed),
        ),
        (
            "completed_signaled_rejected",
            "completed-signaled-rejected.json",
            _command_projection("completed", result=completed_signaled),
        ),
        (
            "interrupted",
            "interrupted.json",
            _command_projection("interrupted", result=_terminal_result("interrupted")),
        ),
        (
            "failed_pending_usage",
            "failed-pending-usage.json",
            _command_projection(
                "failed", result=_terminal_result("failed", pending_usage=True)
            ),
        ),
        (
            "failed_settled_usage",
            "failed-settled-usage.json",
            _command_projection("failed", result=_terminal_result("failed")),
        ),
    ]
    for case, filename, command in result_cases:
        add(
            f"command_{case}",
            "canonical-valid",
            f"fixtures/canonical-valid/commands/{filename}",
            "openapi.json#/components/schemas/CommandResultResponse",
            _success(
                {
                    "kind": "command_result",
                    "service_epoch": _EPOCH,
                    "session_id": _SESSION_ID,
                    "terminal": True,
                    "command": command,
                    "event_cursor": _event_cursor(7),
                }
            ),
            "install_authoritative_truth",
        )

    cancel_request = {"slot_generation": 1}
    cancel_requested = _success(
        {
            "kind": "command_cancel",
            "service_epoch": _EPOCH,
            "replayed": False,
            "outcome": "cancel_requested",
            "command_id": _COMMAND_ID,
            "slot_generation": 1,
            "event_cursor": _event_cursor(5),
        }
    )
    add(
        "cancel_request",
        "canonical-valid",
        "fixtures/canonical-valid/cancel/request.json",
        "openapi.json#/components/schemas/CancelCommandRequest",
        cancel_request,
        "send",
    )
    add(
        "cancel_requested",
        "canonical-valid",
        "fixtures/canonical-valid/cancel/requested.json",
        "openapi.json#/components/schemas/CancelCommandResponse",
        cancel_requested,
        "keep_slot_busy_until_terminal",
    )
    add(
        "cancel_requested_replay",
        "canonical-valid",
        "fixtures/canonical-valid/cancel/requested-replay.json",
        "openapi.json#/components/schemas/CancelCommandResponse",
        {**cancel_requested, "replayed": True},
        "accept_original_ack_before_new_truth",
    )
    add(
        "cancel_already_terminal",
        "canonical-valid",
        "fixtures/canonical-valid/cancel/already-terminal.json",
        "openapi.json#/components/schemas/CancelCommandResponse",
        _success(
            {
                "kind": "command_cancel",
                "service_epoch": _EPOCH,
                "replayed": False,
                "outcome": "already_terminal",
                "command_id": _COMMAND_ID,
                "slot_generation": 1,
                "command_result": completed_none,
                "event_cursor": _event_cursor(7),
            }
        ),
        "install_terminal_truth",
    )

    known_events = _known_events()
    for event in known_events:
        add(
            "event_" + event["type"].replace(".", "_"),
            "canonical-valid",
            "fixtures/canonical-valid/events/"
            + event["type"].replace(".", "-")
            + ".json",
            "events.schema.json#/$defs/ProductEvent",
            event,
            "apply_or_correlate",
        )
    add(
        "event_page",
        "canonical-valid",
        "fixtures/canonical-valid/events/page.json",
        "openapi.json#/components/schemas/EventPageResponse",
        _success(
            {
                "kind": "event_page",
                "service_epoch": _EPOCH,
                "events": known_events[:4],
                "retention_floor": _event_cursor(0),
                "position": _event_cursor(4),
                "retained_payload_bytes": 596,
            }
        ),
        "deduplicate_and_apply_in_order",
    )
    transcript_response = _success({"service_epoch": _EPOCH, **_transcript_page(3)})
    add(
        "transcript_page",
        "canonical-valid",
        "fixtures/canonical-valid/transcript/page.json",
        "openapi.json#/components/schemas/TranscriptPageResponse",
        transcript_response,
        "render_inert_text",
    )

    reset_request = {"expected_control_revision": 1}
    old_replaced = _snapshot(state="replaced", control_revision=2)
    new_snapshot = _snapshot(
        reference="session-ref:phase2_alpha_reset_0002",
        session_id="session:phase2_alpha_reset_0002",
    )
    reset_success = _success(
        {
            "kind": "session_reset",
            "service_epoch": _EPOCH,
            "replayed": False,
            "session_authority": "session-cap:phase2_alpha_reset_cap_000002",
            "previous_snapshot": old_replaced,
            "snapshot": new_snapshot,
        }
    )
    add(
        "reset_request",
        "canonical-valid",
        "fixtures/canonical-valid/replacement/reset-request.json",
        "openapi.json#/components/schemas/ResetSessionRequest",
        reset_request,
        "send",
    )
    add(
        "reset_success",
        "canonical-valid",
        "fixtures/canonical-valid/replacement/reset-success.json",
        "openapi.json#/components/schemas/ReplacementResponse",
        reset_success,
        "atomically_replace_authority",
    )
    switch_request = {
        "expected_control_revision": 1,
        "launch_grant": "launch-grant:phase2_switch_grant_00000002",
    }
    switch_success = {
        **reset_success,
        "kind": "session_switched",
        "session_authority": "session-cap:phase2_alpha_switch_cap_000002",
    }
    add(
        "switch_request",
        "canonical-valid",
        "fixtures/canonical-valid/replacement/switch-request.json",
        "openapi.json#/components/schemas/SwitchSessionRequest",
        switch_request,
        "send",
    )
    add(
        "switch_success",
        "canonical-valid",
        "fixtures/canonical-valid/replacement/switch-success.json",
        "openapi.json#/components/schemas/ReplacementResponse",
        switch_success,
        "atomically_replace_authority",
    )
    close_request = {"expected_control_revision": 1}
    closed_snapshot = _snapshot(state="closed", control_revision=2)
    close_success = _success(
        {
            "kind": "session_closed",
            "service_epoch": _EPOCH,
            "replayed": False,
            "snapshot": closed_snapshot,
        }
    )
    add(
        "close_request",
        "canonical-valid",
        "fixtures/canonical-valid/close/request.json",
        "openapi.json#/components/schemas/CloseSessionRequest",
        close_request,
        "send",
    )
    add(
        "close_success",
        "canonical-valid",
        "fixtures/canonical-valid/close/success.json",
        "openapi.json#/components/schemas/CloseSessionResponse",
        close_success,
        "install_terminal_truth",
    )
    add(
        "current_terminal_closed",
        "canonical-valid",
        "fixtures/canonical-valid/current/terminal-closed.json",
        "openapi.json#/components/schemas/CurrentSessionResponse",
        _success(
            {
                "kind": "current_session_terminal",
                "service_epoch": _EPOCH,
                "snapshot": closed_snapshot,
            }
        ),
        "display_tombstone_without_mutation_authority",
    )

    for code, (_status, message, retry, retry_after) in SAFE_ERROR_CASES.items():
        add(
            f"safe_error_{code}",
            "canonical-valid",
            f"fixtures/canonical-valid/errors/{code.replace('_', '-')}.json",
            "openapi.json#/components/schemas/SafeErrorEnvelope",
            _success(
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "retry": retry,
                        "retry_after_ms": retry_after,
                        "request_id": f"request:error_{code}_0001",
                    }
                }
            ),
            "follow_retry_category_conservatively",
        )

    recovery_values: list[tuple[str, bool, str, str | None, str | None]] = [
        (
            "create_response_lost_after_commit",
            True,
            "same_request",
            None,
            "session_created",
        ),
        (
            "command_accept_response_lost",
            True,
            "same_request",
            None,
            "command_accepted",
        ),
        (
            "promised_terminal_event_withheld",
            False,
            "command_result",
            None,
            "completed",
        ),
        ("cancel_ack_response_lost", True, "same_request", None, "cancel_requested"),
        ("event_cursor_evicted", False, "snapshot", "snapshot_required", None),
        (
            "epoch_rotated_after_uncertain_mutation",
            False,
            "fresh_epoch_binding",
            "outcome_unknown",
            None,
        ),
        ("forced_terminal_usage_settles_late", False, "command_result", None, "failed"),
    ]
    for scenario, replay, reconcile, recovery_code, recovery_outcome in recovery_values:
        add(
            f"recovery_{scenario}",
            "canonical-valid",
            f"fixtures/canonical-valid/recovery/{scenario.replace('_', '-')}.json",
            "openapi.json#/components/schemas/IntegrationRecoveryFixture",
            {
                "scenario": scenario,
                "same_key_replay": replay,
                "automatic_new_key_redispatch": False,
                "reconcile_with": reconcile,
                "expected_code": recovery_code,
                "expected_outcome": recovery_outcome,
            },
            "exercise_deterministic_preview_scenario",
        )

    additive_snapshot = _success(
        {
            "kind": "session_snapshot",
            "service_epoch": _EPOCH,
            "snapshot": {**active_snapshot, "future_safe_projection": {"version": 2}},
        }
    )
    add(
        "forward_snapshot_additive",
        "forward-compat",
        "fixtures/forward-compat/snapshots/additive-fields.json",
        "openapi.json#/components/schemas/SnapshotResponse",
        additive_snapshot,
        "preserve_safe_additive_fields",
    )
    unknown_info = _event(
        11,
        "future.progress",
        "informational",
        {"safe_label": "working"},
        command_id=_COMMAND_ID,
    )
    unknown_auth = _event(
        12,
        "future.authoritative_change",
        "authoritative",
        {"safe_label": "changed"},
        command_id=None,
    )
    add(
        "forward_unknown_informational_event",
        "forward-compat",
        "fixtures/forward-compat/events/unknown-informational.json",
        "events.schema.json#/$defs/ProductEvent",
        unknown_info,
        "ignore_payload_and_advance_cursor",
    )
    add(
        "forward_unknown_authoritative_event",
        "forward-compat",
        "fixtures/forward-compat/events/unknown-authoritative.json",
        "events.schema.json#/$defs/ProductEvent",
        unknown_auth,
        "stop_incremental_apply_and_resume_snapshot",
    )
    unknown_error = _success(
        {
            "error": {
                "code": "future_safe_failure",
                "message": "A future safe failure occurred.",
                "retry": "none",
                "retry_after_ms": None,
                "request_id": "request:future_error_0001",
            }
        }
    )
    add(
        "forward_unknown_error",
        "forward-compat",
        "fixtures/forward-compat/errors/unknown-code.json",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        unknown_error,
        "surface_safe_error_without_retry_inference",
    )
    additive_entry = _success({"service_epoch": _EPOCH, **_transcript_page(3)})
    additive_entry["entries"][0]["future_safe_style"] = "dim"
    add(
        "forward_transcript_additive",
        "forward-compat",
        "fixtures/forward-compat/transcript/additive-entry-field.json",
        "openapi.json#/components/schemas/TranscriptPageResponse",
        additive_entry,
        "ignore_safe_additive_rendering_metadata",
    )

    invalid_completed_missing = _terminal_result("completed")
    invalid_completed_missing.pop("guest_termination")
    invalid_completed_ambiguous = _terminal_result("completed")
    invalid_completed_ambiguous["guest_termination"]["signal"] = "SIGTERM"
    invalid_failed_effect = _terminal_result("failed")
    invalid_failed_effect["state_effect"] = {"kind": "none"}
    invalid_committed = _terminal_result("completed", state_effect="committed")
    invalid_committed["state_effect"].pop("accepted_state_revision")
    invalid_pending = _terminal_result("failed", pending_usage=True)
    invalid_pending["usage"]["tokens"] = 1
    invalid_settled = _terminal_result("failed")
    invalid_settled["usage"]["tokens"] = None
    terminal_invalids = [
        (
            "completed_missing_termination",
            "completed-missing-termination.json",
            invalid_completed_missing,
        ),
        (
            "completed_ambiguous_termination",
            "completed-ambiguous-termination.json",
            invalid_completed_ambiguous,
        ),
        (
            "failed_with_state_effect",
            "failed-with-state-effect.json",
            invalid_failed_effect,
        ),
        (
            "committed_missing_revision",
            "committed-missing-revision.json",
            invalid_committed,
        ),
        (
            "pending_usage_with_number",
            "pending-usage-with-number.json",
            invalid_pending,
        ),
        ("settled_usage_with_null", "settled-usage-with-null.json", invalid_settled),
    ]
    for case, filename, payload in terminal_invalids:
        add(
            case,
            "invalid",
            f"fixtures/invalid/commands/{filename}",
            "openapi.json#/components/schemas/TerminalCommandResult",
            payload,
            "reject",
        )
    add(
        "submit_compound",
        "invalid",
        "fixtures/invalid/commands/compound-syntax.json",
        "classifier:product-single-v1",
        {"command": "uname | head", "expected_slot_generation": 0},
        "reject_before_identity_slot_journal_or_budget",
    )
    add(
        "submit_line_break",
        "invalid",
        "fixtures/invalid/commands/line-break.json",
        "openapi.json#/components/schemas/SubmitCommandRequest",
        {"command": "uname\nid", "expected_slot_generation": 0},
        "reject",
    )
    bad_snapshot = _success(
        {
            "kind": "session_snapshot",
            "service_epoch": _EPOCH,
            "snapshot": {**active_snapshot, "session_authority": _SESSION_AUTHORITY},
        }
    )
    add(
        "snapshot_authority_leak",
        "invalid",
        "fixtures/invalid/snapshots/session-authority.json",
        "openapi.json#/components/schemas/SnapshotResponse",
        bad_snapshot,
        "reject_fail_closed",
    )
    bad_slot = json.loads(json.dumps(active_snapshot))
    bad_slot["command_slot"] = {
        "state": "available",
        "generation": 1,
        "active_command_id": _COMMAND_ID,
    }
    add(
        "snapshot_inconsistent_slot",
        "invalid",
        "fixtures/invalid/snapshots/available-with-active-command.json",
        "openapi.json#/components/schemas/SessionSnapshot",
        bad_slot,
        "reject_fail_closed",
    )
    bad_event_cursor = _event_cursor(0)
    bad_event_cursor["event_id"] = "event:phase2alphaevent:1"
    add(
        "event_cursor_zero_with_id",
        "invalid",
        "fixtures/invalid/events/zero-sequence-with-id.json",
        "openapi.json#/components/schemas/EventCursor",
        bad_event_cursor,
        "reject_and_snapshot",
    )
    bad_event_criticality = dict(known_events[0])
    bad_event_criticality["criticality"] = "informational"
    add(
        "known_event_wrong_criticality",
        "invalid",
        "fixtures/invalid/events/wrong-criticality.json",
        "events.schema.json#/$defs/ProductEvent",
        bad_event_criticality,
        "reject_and_snapshot",
    )
    bad_event_secret = _event(
        13,
        "future.progress",
        "informational",
        {"session_authority": _SESSION_AUTHORITY},
        command_id=None,
    )
    add(
        "event_authority_leak",
        "invalid",
        "fixtures/invalid/events/session-authority.json",
        "events.schema.json#/$defs/ProductEvent",
        bad_event_secret,
        "reject_fail_closed",
    )
    bad_error = _success(
        {
            "error": {
                "code": "internal_error",
                "message": "Traceback at /srv/dreamland",
                "retry": "none",
                "retry_after_ms": None,
                "request_id": "request:unsafe_error_0001",
                "stack_trace": "secret",
            }
        }
    )
    add(
        "safe_error_unsafe_field",
        "invalid",
        "fixtures/invalid/errors/unsafe-field.json",
        "openapi.json#/components/schemas/SafeErrorEnvelope",
        bad_error,
        "reject_fail_closed",
    )
    add(
        "create_offering_not_grant",
        "invalid",
        "fixtures/invalid/sessions/offering-id-instead-of-grant.json",
        "openapi.json#/components/schemas/CreateSessionRequest",
        {"launch_grant": "offering:alpha"},
        "reject",
    )

    cases = [fixture.case for fixture in fixtures]
    paths = [fixture.path for fixture in fixtures]
    if len(cases) != len(set(cases)) or len(paths) != len(set(paths)):
        raise RuntimeError("fixture definitions contain duplicate case or path")
    return tuple(sorted(fixtures, key=lambda item: item.path))


def generated_client_source(openapi: dict[str, Any]) -> str:
    operations = {
        operation["operationId"]
        for path_item in openapi["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    expected = {operation_id for _method, operation_id in EXPECTED_OPERATIONS.values()}
    if operations != expected:
        raise ContractError("cannot generate client from an unexpected operation set")
    prefix = f"""# Generated by tools/product_session_contract.py; do not edit.
from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CONTRACT_NAME = {CONTRACT_NAME!r}
CONTRACT_REVISION = {CONTRACT_REVISION!r}
CONTRACT_COMPATIBILITY = {CONTRACT_COMPATIBILITY!r}
EXPECTED_OPERATIONS = {dict(sorted(EXPECTED_OPERATIONS.items()))!r}
CAPABILITY_NAMES = {CAPABILITY_NAMES!r}
KNOWN_EVENT_TYPES = {KNOWN_EVENT_TYPES!r}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 5
MAX_SAFE_INTEGER = 9_007_199_254_740_991
"""
    body = r"""
EPOCH_RE = re.compile(r"^epoch:[A-Za-z0-9_-]{16,128}$")
ACTOR_RE = re.compile(r"^actor:[A-Za-z0-9_-]{16,128}$")
SESSION_CAP_RE = re.compile(r"^session-cap:[A-Za-z0-9_-]{24,256}$")
REFERENCE_RE = re.compile(r"^session-ref:[A-Za-z0-9_-]{16,128}$")
COMMAND_RE = re.compile(r"^command:[A-Za-z0-9_-]{16,128}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
EVENT_ID_RE = re.compile(r"^event:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$")
STREAM_ID_RE = re.compile(r"^stream:[A-Za-z0-9_-]{8,96}$")
TRANSCRIPT_ID_RE = re.compile(r"^transcript:[A-Za-z0-9_-]{8,96}$")
ENTRY_ID_RE = re.compile(r"^entry:[A-Za-z0-9_-]{8,96}:[1-9][0-9]{0,15}$")
FORBIDDEN_PROJECTION_KEYS = {
    "admin", "api_key", "authorization", "credential", "credentials",
    "environment", "internal_path", "launch_grant", "model_prompt",
    "password", "private_key", "prompt", "provider_envelope",
    "provider_payload", "raw_state", "reasoning", "runtime_document",
    "runtime_state", "secret", "session_authority", "stack_trace",
    "state_profile", "system_prompt", "token", "traceback", "world_state",
}


class ProductSessionPreviewError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.payload = payload
        code = payload.get("error", {}).get("code", "invalid_safe_error")
        super().__init__(f"{code} ({status})")


class _NoRedirect(HTTPRedirectHandler):
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


def _bounded_header(name: str, value: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\r" in value or "\n" in value:
        raise ValueError(f"{name} must be bounded and single-line")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{name} is malformed")
    return value


def _base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("product base URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("product base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("product base URL must not contain a path")
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("plain HTTP product base URL must be loopback")
    return value


def _read_json(response: Any) -> dict[str, Any]:
    content_type = response.headers.get_content_type()
    if content_type not in {"application/json", "application/problem+json"}:
        raise ValueError("private product response must use a bounded JSON media type")
    if "no-store" not in response.headers.get("Cache-Control", "").lower():
        raise ValueError("private product response must be non-cacheable")
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("private product response is too large")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("private product response is not canonical JSON") from error
    if not isinstance(value, dict):
        raise ValueError("private product response must be an object")
    return value


def _context(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("private product response must be an object")
    identity = payload.get("contract")
    if not isinstance(identity, dict) or (
        identity.get("name"), identity.get("revision"), identity.get("compatibility")
    ) != (CONTRACT_NAME, CONTRACT_REVISION, CONTRACT_COMPATIBILITY):
        raise ValueError("incompatible private product contract")
    preview = payload.get("preview")
    if preview != {
        "test_only": True,
        "integration_only": True,
        "executor": "deterministic-no-provider-v1",
        "provider_calls": False,
        "live_capability_evidence": False,
    }:
        raise ValueError("response is not the pinned integration-only preview")
    return payload


def _no_forbidden_projection_keys(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN_PROJECTION_KEYS.intersection(value)
        if forbidden:
            raise ValueError("private or internal field crossed the safe projection")
        for item in value.values():
            _no_forbidden_projection_keys(item)
    elif isinstance(value, list):
        for item in value:
            _no_forbidden_projection_keys(item)


def validate_terminal_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("terminal command result must be an object")
    outcome = value.get("outcome")
    usage = value.get("usage")
    if not isinstance(usage, dict) or usage.get("state") not in {"pending", "settled"}:
        raise ValueError("terminal command result has invalid usage truth")
    if usage["state"] == "pending":
        if usage.get("tokens") is not None or usage.get("spend_units") is not None:
            raise ValueError("pending usage must not invent resource totals")
    elif not all(isinstance(usage.get(name), int) and usage[name] >= 0 for name in ("tokens", "spend_units")):
        raise ValueError("settled usage requires non-negative totals")
    if outcome == "completed":
        if "reason" in value:
            raise ValueError("completed command cannot carry a failure reason")
        termination = value.get("guest_termination")
        effect = value.get("state_effect")
        if not isinstance(termination, dict) or not isinstance(effect, dict):
            raise ValueError("completed command requires termination and state effect")
        if termination.get("kind") == "exited":
            if not isinstance(termination.get("exit_status"), int) or "signal" in termination:
                raise ValueError("exited termination must carry only exit_status")
        elif termination.get("kind") == "signaled":
            if not isinstance(termination.get("signal"), str) or "exit_status" in termination:
                raise ValueError("signaled termination must carry only signal")
        else:
            raise ValueError("unknown guest termination")
        kind = effect.get("kind")
        if kind == "committed":
            if not isinstance(effect.get("accepted_state_revision"), int) or effect["accepted_state_revision"] < 1:
                raise ValueError("committed state effect requires resulting revision")
        elif kind in {"none", "rejected"}:
            if "accepted_state_revision" in effect:
                raise ValueError("noncommitted state effect cannot carry a resulting revision")
        else:
            raise ValueError("unknown state effect")
    elif outcome in {"interrupted", "failed"}:
        if not isinstance(value.get("reason"), str) or "guest_termination" in value or "state_effect" in value:
            raise ValueError("interrupted/failed truth must carry only a typed reason")
    else:
        raise ValueError("unknown terminal command outcome")
    return value


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("product snapshot must be an object")
    _no_forbidden_projection_keys(value)
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, dict) or set(("control_revision", "command_slot_generation", "accepted_state_revision", "event", "transcript")) - set(coordinates):
        raise ValueError("product snapshot has conflated or missing coordinates")
    slot = value.get("command_slot")
    if not isinstance(slot, dict):
        raise ValueError("product snapshot has no command slot")
    active = slot.get("active_command_id")
    if (slot.get("state") == "occupied") != isinstance(active, str):
        raise ValueError("product snapshot command slot is inconsistent")
    latest = value.get("latest_command")
    if isinstance(latest, dict) and latest.get("result") is not None:
        validate_terminal_result(latest["result"])
    return value


def validate_event(value: Any) -> str:
    if not isinstance(value, dict) or not EVENT_ID_RE.fullmatch(str(value.get("event_id", ""))):
        raise ValueError("product event identity is invalid")
    _no_forbidden_projection_keys(value.get("data"))
    event_type = value.get("type")
    criticality = value.get("criticality")
    if event_type in KNOWN_EVENT_TYPES:
        return "apply_or_correlate"
    if criticality == "informational":
        return "ignore_payload_and_advance_cursor"
    if criticality == "authoritative":
        return "stop_incremental_apply_and_resume_snapshot"
    raise ValueError("product event criticality is invalid")


def _cursor_query(
    after: dict[str, Any] | None,
    *,
    namespace_key: str,
    namespace_parameter: str,
    namespace_pattern: re.Pattern[str],
    position_key: str,
    position_parameter: str,
    item_key: str,
    item_parameter: str,
    item_pattern: re.Pattern[str],
) -> dict[str, Any] | None:
    if after is None:
        return None
    if not isinstance(after, dict):
        raise ValueError("product cursor must be an object")
    try:
        namespace = after[namespace_key]
        position = after[position_key]
        item_id = after[item_key]
    except KeyError as error:
        raise ValueError("product cursor is incomplete") from error
    namespace = _bounded_header("cursor namespace", namespace, namespace_pattern)
    if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= MAX_SAFE_INTEGER:
        raise ValueError("product cursor position is invalid")
    if position == 0:
        if item_id is not None:
            raise ValueError("initial product cursor must have a null item ID")
        return None
    item_id = _bounded_header("cursor item ID", item_id, item_pattern)
    return {
        namespace_parameter: namespace,
        position_parameter: position,
        item_parameter: item_id,
    }


def _event_cursor_query(after: dict[str, Any] | None) -> dict[str, Any] | None:
    return _cursor_query(
        after,
        namespace_key="stream_id",
        namespace_parameter="after_stream_id",
        namespace_pattern=STREAM_ID_RE,
        position_key="sequence",
        position_parameter="after_sequence",
        item_key="event_id",
        item_parameter="after_event_id",
        item_pattern=EVENT_ID_RE,
    )


def _transcript_cursor_query(after: dict[str, Any] | None) -> dict[str, Any] | None:
    return _cursor_query(
        after,
        namespace_key="transcript_id",
        namespace_parameter="after_transcript_id",
        namespace_pattern=TRANSCRIPT_ID_RE,
        position_key="position",
        position_parameter="after_position",
        item_key="entry_id",
        item_parameter="after_entry_id",
        item_pattern=ENTRY_ID_RE,
    )


class ProductSessionPreviewClient:
    def __init__(self, base_url: str, bearer: str) -> None:
        self.base_url = _base_url(base_url)
        self.bearer = _bounded_header("product service bearer", bearer)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _headers(
        self,
        *,
        epoch: str | None = None,
        actor: str | None = None,
        session_capability: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self.bearer}
        if epoch is not None:
            headers["X-Dreamland-Expected-Epoch"] = _bounded_header("expected epoch", epoch, EPOCH_RE)
        if actor is not None:
            headers["X-Dreamland-Actor-Context"] = _bounded_header("actor context", actor, ACTOR_RE)
        if session_capability is not None:
            headers["X-Dreamland-Session-Capability"] = _bounded_header("session capability", session_capability, SESSION_CAP_RE)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _bounded_header("idempotency key", idempotency_key, IDEMPOTENCY_RE)
        return headers

    def _request(self, method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if query:
            path += "?" + urlencode(query)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > 8192:
                raise ValueError("private product request is too large")
            headers = {**headers, "Content-Type": "application/json", "Content-Length": str(len(data))}
        request = Request(self.base_url + path, headers=headers, data=data, method=method)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return _context(_read_json(response))
        except HTTPError as error:
            with error:
                payload = _context(_read_json(error))
            raise ProductSessionPreviewError(error.code, payload) from None

    def capabilities(self) -> dict[str, Any]:
        payload = self._request("GET", "/product/v1/capabilities", self._headers())
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(CAPABILITY_NAMES):
            raise ValueError("capability response is incomplete")
        if any(state.get("enabled") is not False for state in capabilities.values() if isinstance(state, dict)):
            raise ValueError("integration preview cannot advertise a live capability")
        return payload

    def create(self, *, epoch: str, actor: str, key: str, launch_grant: str) -> dict[str, Any]:
        return self._request("POST", "/product/v1/sessions", self._headers(epoch=epoch, actor=actor, idempotency_key=key), {"launch_grant": launch_grant})

    def current(self, *, epoch: str, actor: str) -> dict[str, Any]:
        return self._request("GET", "/product/v1/sessions/current", self._headers(epoch=epoch, actor=actor))

    def snapshot(self, *, epoch: str, actor: str, capability: str, reference: str) -> dict[str, Any]:
        payload = self._request("GET", self._session_path(reference), self._headers(epoch=epoch, actor=actor, session_capability=capability))
        validate_snapshot(payload.get("snapshot"))
        return payload

    def submit(self, *, epoch: str, actor: str, capability: str, reference: str, key: str, command: str, expected_slot_generation: int) -> dict[str, Any]:
        return self._request("POST", self._session_path(reference) + "/commands", self._headers(epoch=epoch, actor=actor, session_capability=capability, idempotency_key=key), {"command": command, "expected_slot_generation": expected_slot_generation})

    def command_result(self, *, epoch: str, actor: str, capability: str, reference: str, command_id: str) -> dict[str, Any]:
        command_id = _bounded_header("command id", command_id, COMMAND_RE)
        payload = self._request("GET", self._session_path(reference) + "/commands/" + quote(command_id, safe=""), self._headers(epoch=epoch, actor=actor, session_capability=capability))
        result = payload.get("command", {}).get("result")
        if result is not None:
            validate_terminal_result(result)
        return payload

    def cancel(self, *, epoch: str, actor: str, capability: str, reference: str, command_id: str, key: str, slot_generation: int) -> dict[str, Any]:
        command_id = _bounded_header("command id", command_id, COMMAND_RE)
        return self._request("POST", self._session_path(reference) + "/commands/" + quote(command_id, safe="") + "/cancel", self._headers(epoch=epoch, actor=actor, session_capability=capability, idempotency_key=key), {"slot_generation": slot_generation})

    def events(self, *, epoch: str, actor: str, capability: str, reference: str, after: dict[str, Any] | None = None) -> dict[str, Any]:
        query = _event_cursor_query(after)
        payload = self._request("GET", self._session_path(reference) + "/events", self._headers(epoch=epoch, actor=actor, session_capability=capability), query=query)
        for event in payload.get("events", ()):
            validate_event(event)
        return payload

    def transcript(self, *, epoch: str, actor: str, capability: str, reference: str, after: dict[str, Any] | None = None) -> dict[str, Any]:
        query = _transcript_cursor_query(after)
        return self._request("GET", self._session_path(reference) + "/transcript", self._headers(epoch=epoch, actor=actor, session_capability=capability), query=query)

    def reset(self, *, epoch: str, actor: str, capability: str, reference: str, key: str, expected_control_revision: int) -> dict[str, Any]:
        return self._control_mutation("reset", epoch, actor, capability, reference, key, {"expected_control_revision": expected_control_revision})

    def switch(self, *, epoch: str, actor: str, capability: str, reference: str, key: str, expected_control_revision: int, launch_grant: str) -> dict[str, Any]:
        return self._control_mutation("switch", epoch, actor, capability, reference, key, {"expected_control_revision": expected_control_revision, "launch_grant": launch_grant})

    def close(self, *, epoch: str, actor: str, capability: str, reference: str, key: str, expected_control_revision: int) -> dict[str, Any]:
        return self._control_mutation("close", epoch, actor, capability, reference, key, {"expected_control_revision": expected_control_revision})

    def _control_mutation(self, operation: str, epoch: str, actor: str, capability: str, reference: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._session_path(reference) + "/" + operation, self._headers(epoch=epoch, actor=actor, session_capability=capability, idempotency_key=key), body)

    @staticmethod
    def _session_path(reference: str) -> str:
        reference = _bounded_header("session reference", reference, REFERENCE_RE)
        return "/product/v1/sessions/" + quote(reference, safe="")
"""
    return prefix + body.lstrip()


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer in {"", "#"}:
        return document
    value = document
    text = pointer[1:] if pointer.startswith("#") else pointer
    if not text.startswith("/"):
        raise ContractError(f"invalid JSON pointer: {pointer}")
    for encoded in text[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ContractError(f"unresolved JSON pointer: {pointer}")
    return value


def _dereference(
    value: Any,
    *,
    document_name: str,
    documents: dict[str, Any],
    trail: tuple[tuple[str, str], ...] = (),
) -> Any:
    if isinstance(value, list):
        return [
            _dereference(
                item, document_name=document_name, documents=documents, trail=trail
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if not isinstance(reference, str):
        return {
            key: _dereference(
                item, document_name=document_name, documents=documents, trail=trail
            )
            for key, item in value.items()
        }
    if reference.startswith("#"):
        target_name, pointer = document_name, reference
    else:
        target_name, separator, fragment = reference.partition("#")
        if target_name not in documents:
            raise ContractError(
                f"external schema reference is unavailable: {reference}"
            )
        pointer = "#" + fragment if separator else "#"
    marker = (target_name, pointer)
    if marker in trail:
        raise ContractError(f"recursive schema reference is unsupported: {reference}")
    target = _json_pointer(documents[target_name], pointer)
    resolved = _dereference(
        target,
        document_name=target_name,
        documents=documents,
        trail=(*trail, marker),
    )
    siblings = {key: item for key, item in value.items() if key != "$ref"}
    if not siblings:
        return resolved
    return {
        "allOf": [
            resolved,
            _dereference(
                siblings,
                document_name=document_name,
                documents=documents,
                trail=trail,
            ),
        ]
    }


def _schema_target(
    target: str,
    documents: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    if target == "classifier:product-single-v1":
        return None, target
    document_name, separator, fragment = target.partition("#")
    if not separator or document_name not in documents:
        raise ContractError(f"fixture schema target is invalid: {target}")
    value = _json_pointer(documents[document_name], "#" + fragment)
    resolved = _dereference(
        value,
        document_name=document_name,
        documents=documents,
    )
    if not isinstance(resolved, dict):
        raise ContractError(f"fixture schema target is not a schema: {target}")
    return resolved, target


def _validation_errors(schema: dict[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(value)]


def _classifier_accepts(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "command",
        "expected_slot_generation",
    }:
        return False
    command = value.get("command")
    if not isinstance(command, str) or not command.strip(" \t"):
        return False
    if any(character in command for character in ("\x00", "\r", "\n")):
        return False
    # The canonical backend classifier owns the full grammar.  This fixture
    # target pins the reviewed compound-control examples without copying that
    # parser into the contract tool.
    return not any(
        operator in command
        for operator in ("|", ">", "<", "&&", "||", "&", ";", "`", "$(")
    )


def _assert_canonical_json(path: Path, value: Any) -> None:
    if path.read_text(encoding="utf-8") != canonical_text(value):
        raise ContractError(f"JSON artifact is not canonical: {path}")


def _walk(value: Any) -> Any:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _assert_safe_projection(value: Any, *, label: str) -> None:
    for item in _walk(value):
        if isinstance(item, dict):
            forbidden = FORBIDDEN_PROJECTION_KEYS.intersection(item)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ContractError(
                    f"{label} contains forbidden projection fields: {names}"
                )


def _validate_openapi(openapi: dict[str, Any]) -> None:
    if openapi != build_openapi():
        raise ContractError(
            "openapi.json differs from the reviewed Phase 2 trust anchor"
        )
    if openapi.get("openapi") != "3.1.0" or openapi.get("servers") != []:
        raise ContractError("OpenAPI must be 3.1.0 with no advertised server")
    if openapi.get("security") != [{"ProductServiceBearer": []}]:
        raise ContractError(
            "OpenAPI must require only private product bearer authority"
        )
    if set(openapi.get("paths", {})) != set(EXPECTED_OPERATIONS):
        raise ContractError("OpenAPI route set differs from the Phase 2 preview")
    for path, (method, operation_id) in EXPECTED_OPERATIONS.items():
        path_item = openapi["paths"][path]
        if (
            set(path_item) != {method}
            or path_item[method].get("operationId") != operation_id
        ):
            raise ContractError(f"OpenAPI operation mismatch: {path}")
        operation = path_item[method]
        if operation.get("security") != [{"ProductServiceBearer": []}]:
            raise ContractError(
                f"operation weakens private bearer security: {operation_id}"
            )
        if operation.get("x-dreamland-availability") != AVAILABILITY:
            raise ContractError(f"operation overstates availability: {operation_id}")
        if (
            operation_id in MUTATION_OPERATIONS
            and "x-dreamland-idempotency" not in operation
        ):
            raise ContractError(
                f"mutation has no idempotency declaration: {operation_id}"
            )
    submit = openapi["paths"]["/product/v1/sessions/{reference}/commands"]["post"]
    if "202" not in submit["responses"] or "200" in submit["responses"]:
        raise ContractError("asynchronous command acceptance must use HTTP 202")
    event_operation = openapi["paths"]["/product/v1/sessions/{reference}/events"]["get"]
    content = event_operation["responses"]["200"]["content"]
    if set(content) != {"application/json", "text/event-stream"}:
        raise ContractError("event operation must expose JSON replay and SSE")
    if (
        event_operation.get("x-dreamland-cursor-query", {}).get("presence")
        != "all-or-none"
    ):
        raise ContractError("event cursor tuple must be all-or-none")
    transcript_operation = openapi["paths"][
        "/product/v1/sessions/{reference}/transcript"
    ]["get"]
    if (
        transcript_operation.get("x-dreamland-cursor-query", {}).get("presence")
        != "all-or-none"
    ):
        raise ContractError("transcript cursor tuple must be all-or-none")
    if openapi["x-dreamland-private-boundary"] != {
        "browser_direct_access": False,
        "admin_authority_accepted": False,
        "session_locators_grant_authority": False,
        "master_activation_default": "disabled",
        "idle_runtime_when_disabled": "no_product_objects_or_tasks",
    }:
        raise ContractError("OpenAPI private/no-idle-cost boundary changed")


def _validate_event_semantics(value: Any, *, case: str) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_event_semantics(item, case=case)
        return
    if not isinstance(value, dict):
        return
    if {"event_id", "sequence"}.issubset(value):
        event_id = value.get("event_id")
        sequence = value.get("sequence")
        if isinstance(event_id, str) and isinstance(sequence, int):
            if not event_id.endswith(f":{sequence}"):
                raise ContractError(f"event fixture position mismatch: {case}")
    if {"entry_id", "position"}.issubset(value):
        entry_id = value.get("entry_id")
        position = value.get("position")
        if isinstance(entry_id, str) and isinstance(position, int):
            if not entry_id.endswith(f":{position}"):
                raise ContractError(f"transcript fixture position mismatch: {case}")
    for item in value.values():
        if isinstance(item, (dict, list)):
            _validate_event_semantics(item, case=case)


def _validate_fixture_semantics(fixture: Fixture, payload: Any) -> None:
    if fixture.classification != "invalid":
        _validate_event_semantics(payload, case=fixture.case)
    target = fixture.target
    safe_target_markers = (
        "Snapshot",
        "CommandResult",
        "Transcript",
        "EventPage",
        "events.schema",
        "SafeError",
    )
    if fixture.classification != "invalid" and any(
        marker in target for marker in safe_target_markers
    ):
        _assert_safe_projection(payload, label=fixture.case)
    if target.endswith("SafeErrorEnvelope") and fixture.classification != "invalid":
        error = payload["error"]
        retry_after = error["retry_after_ms"]
        if (error["retry"] == "after_delay") != isinstance(retry_after, int):
            raise ContractError(
                f"safe error retry metadata is inconsistent: {fixture.case}"
            )
        canonical = SAFE_ERROR_CASES.get(error["code"])
        if canonical is not None:
            _status, message, retry, delay = canonical
            if (error["message"], error["retry"], retry_after) != (
                message,
                retry,
                delay,
            ):
                raise ContractError(
                    f"safe error differs from canonical projection: {fixture.case}"
                )


def _expected_files() -> set[str]:
    fixture_paths = {fixture.path for fixture in fixture_definitions()}
    return {
        "openapi.json",
        "events.schema.json",
        "manifest.schema.json",
        "manifest.json",
        "generated/product_session_client.py",
        "fixtures/index.json",
        *fixture_paths,
    }


def _fixture_index(fixtures: tuple[Fixture, ...], root: Path) -> dict[str, Any]:
    recovery = sorted(
        fixture.payload["scenario"]
        for fixture in fixtures
        if fixture.target.endswith("IntegrationRecoveryFixture")
    )
    return {
        "contract_revision": CONTRACT_REVISION,
        "fixtures": [
            {
                "case": fixture.case,
                "class": fixture.classification,
                "expected": fixture.expected,
                "path": fixture.path,
                "schema_target": fixture.target,
                "client_action": fixture.client_action,
                "sha256": sha256_file(root / fixture.path),
            }
            for fixture in fixtures
        ],
        "coverage": {
            "operations": sorted(
                operation_id for _method, operation_id in EXPECTED_OPERATIONS.values()
            ),
            "event_types": list(KNOWN_EVENT_TYPES),
            "safe_error_codes": sorted(SAFE_ERROR_CASES),
            "recovery_scenarios": recovery,
        },
    }


def refresh_contract(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    if root.name != CONTRACT_DIRECTORY:
        raise ContractError(f"contract root must be named {CONTRACT_DIRECTORY}")
    root.mkdir(parents=True, exist_ok=True)
    fixtures = fixture_definitions()
    write_json(root / "openapi.json", build_openapi())
    write_json(root / "events.schema.json", build_events_schema())
    write_json(root / "manifest.schema.json", build_manifest_schema())
    client_path = root / "generated" / "product_session_client.py"
    client_path.parent.mkdir(parents=True, exist_ok=True)
    client_path.write_text(generated_client_source(build_openapi()), encoding="utf-8")
    expected_fixture_paths = {fixture.path for fixture in fixtures}
    fixture_root = root / "fixtures"
    if fixture_root.exists():
        for path in fixture_root.rglob("*.json"):
            relative = path.relative_to(root).as_posix()
            if (
                relative != "fixtures/index.json"
                and relative not in expected_fixture_paths
            ):
                path.unlink()
    for fixture in fixtures:
        write_json(root / fixture.path, fixture.payload)
    index = _fixture_index(fixtures, root)
    write_json(root / "fixtures" / "index.json", index)
    manifest_path = root / "manifest.json"
    manifest = _base_manifest()
    if manifest_path.exists():
        current = load_json(manifest_path)
        if (
            isinstance(current, dict)
            and current.get("contract_revision") == CONTRACT_REVISION
        ):
            for name in ("publication_status", "backend_commit", "published_at"):
                manifest[name] = current.get(name, manifest[name])
    manifest["artifacts"] = {
        key: sha256_file(root / relative) for key, relative in CONTRACT_FILES.items()
    }
    manifest["fixture_set"]["index_sha256"] = sha256_file(
        root / "fixtures" / "index.json"
    )
    write_json(manifest_path, manifest)
    return validate_contract(root)


def validate_contract(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    if root.name != CONTRACT_DIRECTORY or not root.is_dir():
        raise ContractError(
            f"contract root must be an existing {CONTRACT_DIRECTORY} directory"
        )
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    expected_files = _expected_files()
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ContractError(
            f"contract file inventory mismatch; missing={missing}, extra={extra}"
        )
    openapi = load_json(root / "openapi.json")
    events = load_json(root / "events.schema.json")
    manifest_schema = load_json(root / "manifest.schema.json")
    manifest = load_json(root / "manifest.json")
    fixture_index = load_json(root / "fixtures" / "index.json")
    for relative, value in (
        ("openapi.json", openapi),
        ("events.schema.json", events),
        ("manifest.schema.json", manifest_schema),
        ("manifest.json", manifest),
        ("fixtures/index.json", fixture_index),
    ):
        _assert_canonical_json(root / relative, value)
    if not all(
        isinstance(value, dict)
        for value in (openapi, events, manifest_schema, manifest, fixture_index)
    ):
        raise ContractError("top-level contract artifacts must be JSON objects")
    _validate_openapi(openapi)
    if events != build_events_schema():
        raise ContractError(
            "events.schema.json differs from the reviewed event trust anchor"
        )
    if manifest_schema != build_manifest_schema():
        raise ContractError(
            "manifest.schema.json differs from the reviewed manifest trust anchor"
        )
    Draft202012Validator.check_schema(events)
    Draft202012Validator.check_schema(manifest_schema)
    documents = {"openapi.json": openapi, "events.schema.json": events}
    for name, schema in openapi["components"]["schemas"].items():
        resolved = _dereference(
            schema, document_name="openapi.json", documents=documents
        )
        try:
            Draft202012Validator.check_schema(resolved)
        except Exception as error:
            raise ContractError(f"invalid OpenAPI component schema: {name}") from error
    errors = _validation_errors(manifest_schema, manifest)
    if errors:
        raise ContractError(
            "manifest does not satisfy its schema: " + "; ".join(errors[:3])
        )
    for key, relative in CONTRACT_FILES.items():
        if manifest["artifacts"].get(key) != sha256_file(root / relative):
            raise ContractError(f"manifest artifact digest mismatch: {relative}")
    if manifest["fixture_set"].get("index_sha256") != sha256_file(
        root / "fixtures" / "index.json"
    ):
        raise ContractError("manifest fixture-index digest mismatch")
    if any(
        state.get("enabled") is not False for state in manifest["capabilities"].values()
    ):
        raise ContractError(
            "the unfrozen integration preview cannot enable a capability"
        )
    client_source = (root / "generated" / "product_session_client.py").read_text(
        encoding="utf-8"
    )
    if client_source != generated_client_source(openapi):
        raise ContractError(
            "generated server-side client differs from the OpenAPI trust anchor"
        )
    fixtures = fixture_definitions()
    expected_index = _fixture_index(fixtures, root)
    if fixture_index != expected_index:
        raise ContractError(
            "fixtures/index.json differs from deterministic fixture inventory"
        )
    counts = {"canonical-valid": 0, "forward-compat": 0, "invalid": 0}
    for fixture in fixtures:
        payload = load_json(root / fixture.path)
        _assert_canonical_json(root / fixture.path, payload)
        if sha256_file(root / fixture.path) != next(
            item["sha256"]
            for item in fixture_index["fixtures"]
            if item["case"] == fixture.case
        ):
            raise ContractError(f"fixture digest mismatch: {fixture.case}")
        schema, _target = _schema_target(fixture.target, documents)
        if schema is None:
            valid = _classifier_accepts(payload)
            errors = [] if valid else ["rejected by product-single-v1"]
        else:
            errors = _validation_errors(schema, payload)
        if fixture.expected == "valid" and errors:
            raise ContractError(f"valid fixture rejected ({fixture.case}): {errors[0]}")
        if fixture.expected == "invalid" and not errors:
            raise ContractError(
                f"invalid fixture unexpectedly accepted: {fixture.case}"
            )
        _validate_fixture_semantics(fixture, payload)
        counts[fixture.classification] += 1
    known_fixture_types = {
        fixture.payload["type"]
        for fixture in fixtures
        if fixture.classification == "canonical-valid"
        and fixture.target == "events.schema.json#/$defs/ProductEvent"
    }
    if known_fixture_types != set(KNOWN_EVENT_TYPES):
        raise ContractError("canonical fixtures do not cover every known event type")
    recovery_expected = {
        "create_response_lost_after_commit",
        "command_accept_response_lost",
        "promised_terminal_event_withheld",
        "cancel_ack_response_lost",
        "event_cursor_evicted",
        "epoch_rotated_after_uncertain_mutation",
        "forced_terminal_usage_settles_late",
    }
    if set(fixture_index["coverage"]["recovery_scenarios"]) != recovery_expected:
        raise ContractError("recovery fixture coverage is incomplete")
    return {
        "contract_revision": CONTRACT_REVISION,
        "compatibility": CONTRACT_COMPATIBILITY,
        "freeze_status": manifest["freeze_status"],
        "implementation_status": manifest["implementation_status"],
        "transport_status": manifest["transport_status"],
        "routes": len(EXPECTED_OPERATIONS),
        "operations": len(EXPECTED_OPERATIONS),
        "fixtures": len(fixtures),
        "canonical_valid": counts["canonical-valid"],
        "forward_compat": counts["forward-compat"],
        "invalid": counts["invalid"],
        "known_event_types": len(KNOWN_EVENT_TYPES),
        "safe_error_codes": len(SAFE_ERROR_CASES),
        "recovery_scenarios": len(recovery_expected),
        "capabilities_enabled": 0,
        "generated_client_sha256": manifest["artifacts"]["generated_client_sha256"],
    }


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = validate_contract(root)
    root_path = Path(root).resolve()
    source = root_path / "generated" / "product_session_client.py"
    if output is None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "product_session_client.py"
            shutil.copyfile(source, destination)
            py_compile.compile(str(destination), doraise=True)
            _smoke_import(destination, root_path)
        generated_path = "temporary"
    else:
        destination = output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        py_compile.compile(str(destination), doraise=True)
        _smoke_import(destination, root_path)
        generated_path = str(destination)
    return {
        **result,
        "generated_path": generated_path,
        "validated_client_fixtures": 6,
    }


def _smoke_import(path: Path, root: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "dreamland_product_session_generated", path
    )
    if spec is None or spec.loader is None:
        raise ContractError("cannot import generated product-session client")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    completed = load_json(
        root / "fixtures/canonical-valid/commands/completed-exited-committed.json"
    )
    module.validate_terminal_result(completed["command"]["result"])
    pending = load_json(
        root / "fixtures/canonical-valid/commands/failed-pending-usage.json"
    )
    module.validate_terminal_result(pending["command"]["result"])
    snapshot = load_json(root / "fixtures/canonical-valid/snapshots/active.json")
    module.validate_snapshot(snapshot["snapshot"])
    unknown_info = load_json(
        root / "fixtures/forward-compat/events/unknown-informational.json"
    )
    if module.validate_event(unknown_info) != "ignore_payload_and_advance_cursor":
        raise ContractError("generated client mishandles unknown informational events")
    unknown_auth = load_json(
        root / "fixtures/forward-compat/events/unknown-authoritative.json"
    )
    if (
        module.validate_event(unknown_auth)
        != "stop_incremental_apply_and_resume_snapshot"
    ):
        raise ContractError("generated client mishandles unknown authoritative events")
    error_payload = load_json(
        root / "fixtures/canonical-valid/errors/unauthorized.json"
    )
    body = io.BytesIO(json.dumps(error_payload).encode("utf-8"))
    headers = Message()
    headers["Content-Type"] = "application/problem+json"
    headers["Cache-Control"] = "no-store"
    http_error = HTTPError(
        "http://127.0.0.1/product/v1/capabilities",
        401,
        "Unauthorized",
        headers,
        body,
    )

    class ErrorOpener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            raise http_error

    client = module.ProductSessionPreviewClient(
        "http://127.0.0.1", "private-preview-smoke-bearer"
    )
    client._opener = ErrorOpener()
    try:
        client.capabilities()
    except module.ProductSessionPreviewError as error:
        if error.status != 401:
            raise ContractError(
                "generated client lost the safe-error status"
            ) from error
    else:
        raise ContractError("generated client did not project the safe error")
    if not body.closed:
        raise ContractError("generated client did not close the HTTP error response")


def _published_at(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ContractError("published-at must be a bounded RFC 3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ContractError(
            "published-at must be a bounded RFC 3339 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ContractError("published-at must include a UTC offset")
    return value


def _repository_root(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ContractError("contract root is not inside a Git repository")
    repository = Path(result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise ContractError("contract root is outside its Git repository") from error
    return repository


def _verify_publication_commit(root: Path, backend_commit: str) -> None:
    repository = _repository_root(root)
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
    for relative in sorted(_expected_files()):
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
            raise ContractError(
                "backend-commit does not contain the exact product-session artifact: "
                + relative
            )


def publish_contract(
    root: Path | str,
    *,
    backend_commit: str,
    published_at: str,
) -> dict[str, Any]:
    if (
        not isinstance(backend_commit, str)
        or len(backend_commit) != 40
        or any(character not in "0123456789abcdef" for character in backend_commit)
    ):
        raise ContractError(
            "backend-commit must be exactly 40 lowercase hex characters"
        )
    published_at = _published_at(published_at)
    root_path = Path(root).resolve()
    validate_contract(root_path)
    manifest_path = root_path / "manifest.json"
    manifest = load_json(manifest_path)
    if (
        manifest.get("publication_status") != "release_candidate"
        or manifest.get("backend_commit") is not None
        or manifest.get("published_at") is not None
    ):
        raise ContractError("only an unpublished release candidate may be published")
    _verify_publication_commit(root_path, backend_commit)
    manifest["publication_status"] = "published"
    manifest["backend_commit"] = backend_commit
    manifest["published_at"] = published_at
    write_json(manifest_path, manifest)
    return validate_contract(root_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "refresh"):
        child = subparsers.add_parser(command)
        child.add_argument("root", type=Path)
    smoke = subparsers.add_parser("client-smoke")
    smoke.add_argument("root", type=Path)
    smoke.add_argument("--output", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("root", type=Path)
    publish.add_argument("--backend-commit", required=True)
    publish.add_argument("--published-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            result = validate_contract(arguments.root)
        elif arguments.command == "refresh":
            result = refresh_contract(arguments.root)
        elif arguments.command == "client-smoke":
            result = client_smoke(arguments.root, output=arguments.output)
        else:
            result = publish_contract(
                arguments.root,
                backend_commit=arguments.backend_commit,
                published_at=arguments.published_at,
            )
    except ContractError as error:
        print(f"contract error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
