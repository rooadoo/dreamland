#!/usr/bin/env python3
"""Build the additive display-safe Presenter Stage X-ray contract kit.

The kit extends the corrected Forge-to-Stage RC2 baseline with private,
presenter-only snapshot and detail reads.  It remains an explicitly started
development target and does not advertise production capability evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_forge_stage_contract() -> ModuleType:
    path = Path(__file__).with_name("product_forge_stage_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_xray_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Forge-to-Stage contract generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_BASE: Any = _load_forge_stage_contract()
_LIVE: Any = _BASE._LIVE
_STAGE: Any = _BASE._STAGE
_CORE: Any = _BASE._CORE

CONTRACT_NAME = _BASE.CONTRACT_NAME
CONTRACT_REVISION = "0.6.0-rc.3"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _BASE.CONTRACT_COMPATIBILITY
LIVE_EXECUTOR = "development-live-forge-stage-xray-langgraph-v1"
XRAY_SCHEMA_REVISION = "stage-xray-v1"
XRAY_CLIENT_SMOKE_REJECTED_MUTATIONS = 82
CLIENT_SMOKE_VALIDATED_FIXTURES = 11
CLIENT_SMOKE_REJECTED_MUTATIONS = (
    _BASE.CLIENT_SMOKE_REJECTED_MUTATIONS + XRAY_CLIENT_SMOKE_REJECTED_MUTATIONS
)

XRAY_SNAPSHOT_PATH = "/product/v1/stage/sessions/{reference}/xray"
XRAY_DETAIL_PATH = (
    "/product/v1/stage/sessions/{reference}/xray/details/{xray_entity_id}"
)
XRAY_OPERATIONS = {
    XRAY_SNAPSHOT_PATH: ("get", "getStageXraySnapshot"),
    XRAY_DETAIL_PATH: ("get", "getStageXrayDetail"),
}
EXPECTED_OPERATIONS = {**_BASE.EXPECTED_OPERATIONS, **XRAY_OPERATIONS}
SESSION_OPERATIONS = {*_BASE.SESSION_OPERATIONS, *XRAY_OPERATIONS}

XRAY_SAFE_ERROR_CASES = {
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
SAFE_ERROR_CASES = {**_BASE.SAFE_ERROR_CASES, **XRAY_SAFE_ERROR_CASES}

for module in (_BASE, _LIVE, _STAGE, _CORE):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    module.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
    module.SESSION_OPERATIONS = SESSION_OPERATIONS
    module.SAFE_ERROR_CASES = SAFE_ERROR_CASES
_BASE.LIVE_EXECUTOR = LIVE_EXECUTOR
_LIVE.LIVE_EXECUTOR = LIVE_EXECUTOR

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
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Presenter Stage X-ray Development Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen private Phase 6 development contract for a bounded, "
            "display-safe projection of an authoritative Forge-backed Presenter "
            "Stage. All production product capabilities remain disabled."
        ),
    }
    schemas = document["components"]["schemas"]
    schemas.update(_xray_schemas())

    stage_projection = schemas["StageProjection"]
    stage_projection["properties"]["xray_available"] = {"type": "boolean"}
    controls = schemas["SessionSnapshot"]["properties"]["available_controls"]
    controls["items"]["enum"].append("xray")
    controls["maxItems"] = 9
    private_retry = schemas["SafeErrorEnvelope"]["properties"]["error"]["properties"][
        "retry"
    ]["enum"]
    private_retry.append("resume_xray_snapshot")

    forge_snapshot = schemas["ForgePresenterStageSnapshot"]
    forge_snapshot.setdefault("allOf", []).append(
        {
            "if": {
                "properties": {
                    "lifecycle": {
                        "properties": {"state": {"const": "active"}},
                        "required": ["state"],
                    }
                },
                "required": ["lifecycle"],
            },
            "then": {
                "properties": {
                    "stage": {
                        "properties": {"xray_available": {"const": True}},
                        "required": ["xray_available"],
                    }
                }
            },
            "else": {
                "properties": {
                    "stage": {
                        "properties": {"xray_available": {"const": False}},
                        "required": ["xray_available"],
                    }
                }
            },
        }
    )

    parameters = document["components"]["parameters"]
    parameters.update(
        {
            "XrayEntityId": {
                "name": "xray_entity_id",
                "in": "path",
                "required": True,
                "schema": _CORE._ref("XrayEntityId"),
            },
            "XrayExpectedAcceptedStateRevision": {
                "name": "accepted_state_revision",
                "in": "query",
                "required": True,
                "schema": _CORE._integer(),
            },
            "XrayExpectedStreamId": {
                "name": "xray_stream_id",
                "in": "query",
                "required": True,
                "schema": _CORE._ref("XrayStreamId"),
            },
            "XrayExpectedSequence": {
                "name": "xray_sequence",
                "in": "query",
                "required": True,
                "schema": _CORE._integer(minimum=1),
            },
        }
    )
    document["paths"].update(
        {
            XRAY_SNAPSHOT_PATH: {
                "get": _CORE._operation(
                    "getStageXraySnapshot",
                    "StageXraySnapshotResponse",
                    (
                        "Read one bounded display-safe projection and its two "
                        "authoritative detail-read coordinates."
                    ),
                    parameters=[
                        "SessionReference",
                        "ExpectedEpoch",
                        "ActorContext",
                        "SessionCapability",
                    ],
                )
            },
            XRAY_DETAIL_PATH: {
                "get": _CORE._operation(
                    "getStageXrayDetail",
                    "StageXrayDetailResponse",
                    (
                        "Read one allowlisted entity detail only when accepted "
                        "state and X-ray projection position still match."
                    ),
                    parameters=[
                        "SessionReference",
                        "XrayEntityId",
                        "ExpectedEpoch",
                        "ActorContext",
                        "SessionCapability",
                        "XrayExpectedAcceptedStateRevision",
                        "XrayExpectedStreamId",
                        "XrayExpectedSequence",
                    ],
                )
            },
        }
    )
    document["x-dreamland-forge-stage-handoff"]["xray_included"] = True
    document["x-dreamland-stage-xray"] = {
        "schema_revision": XRAY_SCHEMA_REVISION,
        "authority": "presenter_session_only",
        "accepted_state_coordinate": "accepted_state_revision",
        "projection_coordinate": "projection_position",
        "stage_event_position": "correlation_watermark_only",
        "detail_preconditions": [
            "accepted_state_revision",
            "xray_stream_id",
            "xray_sequence",
        ],
        "raw_runtime_state": False,
        "pending_is_authoritative": False,
        "normal_runtime_composition": False,
        "capability_evidence": False,
    }
    return document


def _xray_schemas() -> dict[str, Any]:
    safe_text = _CORE._object(
        {
            "text": _CORE._string(r"^[^\u0000-\u001f\u007f-\u009f]*$", maximum=512),
            "clipped": {"type": "boolean"},
            "control_marked": {"type": "boolean"},
        },
        ("text", "clipped", "control_marked"),
    )
    entity_id = _CORE._string(r"^(?:host|fact):[0-9a-f]{32}$", minimum=37, maximum=37)
    position = _CORE._object(
        {
            "stream_id": _CORE._ref("XrayStreamId"),
            "sequence": _CORE._integer(minimum=1),
            "cursor": _CORE._string(
                r"^xray-cursor:[0-9a-f]{32}:[1-9][0-9]*$", maximum=96
            ),
        },
        ("stream_id", "sequence", "cursor"),
    )
    coordinates = {
        "schema_revision": {
            "type": "string",
            "const": XRAY_SCHEMA_REVISION,
        },
        "accepted_state_revision": _CORE._integer(),
        "projection_position": _CORE._ref("XrayProjectionPosition"),
        "stage_event_watermark": _CORE._ref("EventCursor"),
    }
    host = _CORE._object(
        {
            "entity_id": _CORE._ref("XrayEntityId"),
            "entity_kind": {"type": "string", "const": "host"},
            "hostname": _CORE._ref("XraySafeText"),
            "active": {"type": "boolean"},
            "entry": {"type": "boolean"},
            "accepted_fact_count": _CORE._integer(),
            "service_count": _CORE._integer(),
            "theme": _CORE._ref("XraySafeText"),
        },
        (
            "entity_id",
            "entity_kind",
            "hostname",
            "active",
            "entry",
            "accepted_fact_count",
            "service_count",
        ),
    )
    fact = _CORE._object(
        {
            "entity_id": _CORE._ref("XrayEntityId"),
            "entity_kind": {"type": "string", "const": "fact"},
            "label": _CORE._ref("XraySafeText"),
            "statement": _CORE._ref("XraySafeText"),
            "scope": {"type": "string", "enum": ["world", "host"]},
            "related_host_id": {
                "oneOf": [_CORE._ref("XrayEntityId"), {"type": "null"}]
            },
        },
        (
            "entity_id",
            "entity_kind",
            "label",
            "statement",
            "scope",
            "related_host_id",
        ),
    )
    field = _CORE._object(
        {
            "name": _CORE._string(r"^[a-z][a-z0-9_]{0,63}$", maximum=64),
            "label": _CORE._string(r"^[^\u0000-\u001f\u007f-\u009f]+$", maximum=96),
            "value": _CORE._ref("XraySafeText"),
        },
        ("name", "label", "value"),
    )
    detail = _CORE._object(
        {
            "entity_id": _CORE._ref("XrayEntityId"),
            "entity_kind": {"type": "string", "enum": ["host", "fact"]},
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _CORE._ref("XrayDetailField"),
            },
            "accepted": {"type": "boolean", "const": True},
            "related_host_id": {
                "oneOf": [_CORE._ref("XrayEntityId"), {"type": "null"}]
            },
            "evidence": _CORE._object(
                {"state": {"type": "string", "enum": ["present", "unavailable"]}},
                ("state",),
            ),
        },
        ("entity_id", "entity_kind", "fields", "accepted", "evidence"),
    )
    count = _CORE._object(
        {
            "included_count": _CORE._integer(),
            "omitted_count": _CORE._integer(),
        },
        ("included_count", "omitted_count"),
    )
    snapshot_xray = _CORE._object(
        {
            **coordinates,
            "overview": _CORE._object(
                {
                    "world_name": _CORE._ref("XraySafeText"),
                    "theme": _CORE._ref("XraySafeText"),
                    "description": _CORE._ref("XraySafeText"),
                    "active_host_id": _CORE._ref("XrayEntityId"),
                },
                (),
            ),
            "hosts": {
                "type": "array",
                "maxItems": 64,
                "items": _CORE._ref("XrayHostSummary"),
            },
            "facts": {
                "type": "array",
                "maxItems": 128,
                "items": _CORE._ref("XrayFactSummary"),
            },
            "activity": _CORE._object(
                {
                    "foreground": _CORE._ref("XrayActivityState"),
                    "enrichment": _CORE._ref("XrayActivityState"),
                },
                ("foreground", "enrichment"),
            ),
            "completeness": _CORE._object(
                {
                    "complete": {"type": "boolean"},
                    "hosts": _CORE._ref("XraySectionCount"),
                    "facts": _CORE._ref("XraySectionCount"),
                    "clipped_field_count": _CORE._integer(),
                    "control_marked_field_count": _CORE._integer(),
                },
                (
                    "complete",
                    "hosts",
                    "facts",
                    "clipped_field_count",
                    "control_marked_field_count",
                ),
            ),
        },
        (
            *coordinates,
            "overview",
            "hosts",
            "facts",
            "activity",
            "completeness",
        ),
    )
    response_common = {
        "service_epoch": _CORE._ref("ServiceEpoch"),
    }
    return {
        "XraySafeText": safe_text,
        "XrayEntityId": entity_id,
        "XrayStreamId": _CORE._string(
            r"^xray-stream:[0-9a-f]{32}$", minimum=44, maximum=44
        ),
        "XrayProjectionPosition": position,
        "XrayHostSummary": host,
        "XrayFactSummary": fact,
        "XrayDetailField": field,
        "XrayEntityDetail": detail,
        "XraySectionCount": count,
        "XrayActivityState": _CORE._object(
            {
                "state": {
                    "type": "string",
                    "enum": ["idle", "active", "unavailable"],
                },
                "command_id": _CORE._ref("CommandId"),
                "status": {
                    "type": "string",
                    "enum": ["accepted", "running", "cancel_requested", "active"],
                },
            },
            ("state",),
        ),
        "StageXraySnapshot": snapshot_xray,
        "StageXrayDetail": _CORE._object(
            {**coordinates, "detail": _CORE._ref("XrayEntityDetail")},
            (*coordinates, "detail"),
        ),
        "StageXraySnapshotResponse": _CORE._response_schema(
            {
                "kind": {"type": "string", "const": "stage_xray_snapshot"},
                **response_common,
                "xray": _CORE._ref("StageXraySnapshot"),
            },
            ("kind", "service_epoch", "xray"),
        ),
        "StageXrayDetailResponse": _CORE._response_schema(
            {
                "kind": {"type": "string", "const": "stage_xray_detail"},
                **response_common,
                "xray": _CORE._ref("StageXrayDetail"),
            },
            ("kind", "service_epoch", "xray"),
        ),
    }


def fixture_definitions() -> tuple[Any, ...]:
    fixtures: list[Any] = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        if fixture.case in {
            "forge_stage_handoff_created",
            "forge_stage_handoff_resumed",
            "forge_stage_handoff_exact_retry",
            "forge_stage_replacement_success",
        }:
            snapshot = payload["snapshot"]
            snapshot["stage"]["xray_available"] = True
            if "xray" not in snapshot["available_controls"]:
                snapshot["available_controls"].append("xray")
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

    snapshot = _xray_snapshot_fixture()
    host_id = snapshot["xray"]["hosts"][0]["entity_id"]
    fact_id = snapshot["xray"]["facts"][0]["entity_id"]
    host_detail = _xray_detail_fixture(host_id, "host", "Hostname", "console")
    fact_detail = _xray_detail_fixture(
        fact_id,
        "fact",
        "Statement",
        "The harbor radio link is intermittent.",
    )
    stale = _problem_fixture("xray_resync_required")
    fixtures.extend(
        (
            _CORE.Fixture(
                "stage_xray_snapshot",
                "canonical-valid",
                "fixtures/canonical-valid/xray/snapshot.json",
                "openapi.json#/components/schemas/StageXraySnapshotResponse",
                snapshot,
                "replace_xray_master_projection_atomically",
            ),
            _CORE.Fixture(
                "stage_xray_host_detail",
                "canonical-valid",
                "fixtures/canonical-valid/xray/host-detail.json",
                "openapi.json#/components/schemas/StageXrayDetailResponse",
                host_detail,
                "render_revision_bound_host_detail",
            ),
            _CORE.Fixture(
                "stage_xray_fact_detail",
                "canonical-valid",
                "fixtures/canonical-valid/xray/fact-detail.json",
                "openapi.json#/components/schemas/StageXrayDetailResponse",
                fact_detail,
                "render_revision_bound_fact_detail",
            ),
            _CORE.Fixture(
                "stage_xray_stale_detail",
                "canonical-valid",
                "fixtures/canonical-valid/errors/xray-resync-required.json",
                "openapi.json#/components/schemas/SafeErrorEnvelope",
                stale,
                "discard_stale_detail_and_refresh_xray_snapshot",
            ),
        )
    )
    return tuple(fixtures)


def _xray_coordinates() -> dict[str, object]:
    return {
        "schema_revision": XRAY_SCHEMA_REVISION,
        "accepted_state_revision": 3,
        "projection_position": {
            "stream_id": "xray-stream:" + "a" * 32,
            "sequence": 7,
            "cursor": "xray-cursor:" + "a" * 32 + ":7",
        },
        "stage_event_watermark": {
            "stream_id": "stream:phase2alphaevent",
            "sequence": 5,
            "event_id": "event:phase2alphaevent:5",
        },
    }


def _safe_text(value: str) -> dict[str, object]:
    return {"text": value, "clipped": False, "control_marked": False}


def _xray_snapshot_fixture() -> dict[str, object]:
    host_id = "host:" + "1" * 32
    fact_id = "fact:" + "2" * 32
    return _success(
        {
            "kind": "stage_xray_snapshot",
            "service_epoch": _CORE._EPOCH,
            "xray": {
                **_xray_coordinates(),
                "overview": {
                    "world_name": _safe_text("harbor-control"),
                    "theme": _safe_text("Harbor control during a radio outage"),
                    "active_host_id": host_id,
                },
                "hosts": [
                    {
                        "entity_id": host_id,
                        "entity_kind": "host",
                        "hostname": _safe_text("console"),
                        "active": True,
                        "entry": True,
                        "accepted_fact_count": 1,
                        "service_count": 2,
                    }
                ],
                "facts": [
                    {
                        "entity_id": fact_id,
                        "entity_kind": "fact",
                        "label": _safe_text("radio-link"),
                        "statement": _safe_text(
                            "The harbor radio link is intermittent."
                        ),
                        "scope": "world",
                        "related_host_id": None,
                    }
                ],
                "activity": {
                    "foreground": {"state": "idle"},
                    "enrichment": {"state": "idle"},
                },
                "completeness": {
                    "complete": True,
                    "hosts": {"included_count": 1, "omitted_count": 0},
                    "facts": {"included_count": 1, "omitted_count": 0},
                    "clipped_field_count": 0,
                    "control_marked_field_count": 0,
                },
            },
        }
    )


def _xray_detail_fixture(
    entity_id: str,
    kind: str,
    label: str,
    value: str,
) -> dict[str, object]:
    return _success(
        {
            "kind": "stage_xray_detail",
            "service_epoch": _CORE._EPOCH,
            "xray": {
                **_xray_coordinates(),
                "detail": {
                    "entity_id": entity_id,
                    "entity_kind": kind,
                    "fields": [
                        {
                            "name": "hostname" if kind == "host" else "statement",
                            "label": label,
                            "value": _safe_text(value),
                        }
                    ],
                    "accepted": True,
                    "evidence": {"state": "unavailable"},
                },
            },
        }
    )


def _problem_fixture(code: str) -> dict[str, object]:
    status, message, retry, retry_after_ms = SAFE_ERROR_CASES[code]
    error = {
        "code": code,
        "message": message,
        "retry": retry,
        "retry_after_ms": retry_after_ms,
        "request_id": "request:stage-xray-00000001",
        "context": {},
    }
    return {
        "type": f"urn:dreamland:product:error:{code}",
        "title": "DREAMLAND product request failed",
        "status": status,
        "error": error,
        **_context(),
    }


def _success(value: dict[str, object]) -> dict[str, object]:
    value.update(_context())
    return value


def _context() -> dict[str, object]:
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


def _validate_fixture_semantics(fixture: Any, payload: Any) -> None:
    _base_validate_fixture_semantics(fixture, payload)
    if fixture.case not in {
        "stage_xray_snapshot",
        "stage_xray_host_detail",
        "stage_xray_fact_detail",
    }:
        return
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "world_state",
        "SessionState",
        "prompt",
        "reasoning",
        "credential",
        "provider_envelope",
        "environment",
        "backend_path",
        "failure_capsule",
    ):
        if forbidden in serialized:
            raise _CORE.ContractError(
                f"Stage X-ray fixture exposes forbidden content: {fixture.case}"
            )
    xray = payload.get("xray")
    if not isinstance(xray, dict):
        raise _CORE.ContractError(f"Stage X-ray fixture is incomplete: {fixture.case}")
    position = xray.get("projection_position")
    if (
        xray.get("schema_revision") != XRAY_SCHEMA_REVISION
        or isinstance(xray.get("accepted_state_revision"), bool)
        or not isinstance(xray.get("accepted_state_revision"), int)
        or not isinstance(position, dict)
        or not str(position.get("cursor", "")).endswith(f":{position.get('sequence')}")
    ):
        raise _CORE.ContractError(
            f"Stage X-ray fixture coordinates diverge: {fixture.case}"
        )


def _base_manifest_document() -> dict[str, Any]:
    manifest = _base_manifest()
    prefix = (
        ".venv/bin/python tools/product_xray_contract.py"
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


def _replace_generator_path(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_forge_stage_contract.py",
                    "tools/product_xray_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_forge_stage_contract.py",
                    "tools/product_xray_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _base_generated_client_source(openapi)
    source = source.replace(
        "Generated by tools/product_forge_stage_contract.py",
        "Generated by tools/product_xray_contract.py",
        1,
    )
    regex_marker = 'SHA256_RE = re.compile(r"^[0-9a-f]{64}$")\n'
    regexes = regex_marker + (
        'XRAY_ENTITY_RE = re.compile(r"^(?:host|fact):[0-9a-f]{32}$")\n'
        'XRAY_STREAM_RE = re.compile(r"^xray-stream:[0-9a-f]{32}$")\n'
    )
    if regex_marker not in source:
        raise _CORE.ContractError("generated X-ray regex insertion point is missing")
    source = source.replace(regex_marker, regexes, 1)
    context_start = "def _context(payload: Any) -> dict[str, Any]:\n"
    context_end = "\n\ndef _no_forbidden_projection_keys(value: Any) -> None:\n"
    if source.count(context_start) != 1 or source.count(context_end) != 1:
        raise _CORE.ContractError(
            "generated X-ray response-context replacement point is missing"
        )
    prefix, remainder = source.split(context_start, 1)
    _old_context, suffix = remainder.split(context_end, 1)
    strict_context = r"""def _context(
    payload: Any,
    *,
    require_service_epoch: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("private product response must be an object")
    identity = payload.get("contract")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"name", "revision", "compatibility"}
        or (
            identity["name"],
            identity["revision"],
            identity["compatibility"],
        )
        != (CONTRACT_NAME, CONTRACT_REVISION, CONTRACT_COMPATIBILITY)
    ):
        raise ValueError("incompatible private product contract")
    preview = payload.get("preview")
    if preview != {
        "test_only": True,
        "integration_only": True,
        "executor": "development-live-forge-stage-xray-langgraph-v1",
        "provider_calls": True,
        "live_capability_evidence": False,
    }:
        raise ValueError("response is not the pinned integration-only preview")
    if require_service_epoch:
        _bounded_header("service epoch", payload.get("service_epoch"), EPOCH_RE)
    return payload
"""
    source = prefix + strict_context + context_end + suffix
    error_context = "payload = _context(_read_json(error))"
    if source.count(error_context) != 1:
        raise _CORE.ContractError(
            "generated X-ray safe-error context replacement point is missing"
        )
    source = source.replace(
        error_context,
        "payload = _context(_read_json(error), require_service_epoch=False)",
        1,
    )
    validation_marker = "class ProductSessionPreviewClient:\n"
    validation = r"""
def _validate_xray_coordinates(xray: Any) -> tuple[int, str, int]:
    if not isinstance(xray, dict) or xray.get("schema_revision") != "stage-xray-v1":
        raise ValueError("Stage X-ray coordinates are incomplete")
    accepted = _validate_xray_integer(
        "Stage X-ray accepted-state revision",
        xray.get("accepted_state_revision"),
    )
    position = xray.get("projection_position")
    watermark = xray.get("stage_event_watermark")
    if not isinstance(position, dict) or set(position) != {"stream_id", "sequence", "cursor"}:
        raise ValueError("Stage X-ray positions are incomplete")
    stream_id = _forge_identifier("X-ray stream ID", position.get("stream_id"), XRAY_STREAM_RE)
    sequence = _validate_xray_integer(
        "Stage X-ray sequence",
        position.get("sequence"),
        minimum=1,
    )
    if position.get("cursor") != "xray-cursor:" + stream_id.removeprefix("xray-stream:") + f":{sequence}":
        raise ValueError("Stage X-ray cursor diverged")
    _validate_xray_event_cursor(watermark)
    return accepted, stream_id, sequence


def _xray_exact_object(value: Any, required: set[str], optional: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        raise ValueError(f"{name} violates its strict object schema")
    return value


def _validate_xray_integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_SAFE_INTEGER:
        raise ValueError(f"{name} is invalid")
    return value


def _validate_xray_safe_text(value: Any) -> dict[str, Any]:
    value = _xray_exact_object(value, {"text", "clipped", "control_marked"}, set(), "Stage X-ray safe text")
    text = value["text"]
    if not isinstance(text, str) or not 1 <= len(text) <= 512 or any(ord(character) <= 31 or 127 <= ord(character) <= 159 for character in text):
        raise ValueError("Stage X-ray safe text is invalid")
    if not isinstance(value["clipped"], bool) or not isinstance(value["control_marked"], bool):
        raise ValueError("Stage X-ray safe-text flags are invalid")
    return value


def _validate_xray_entity_id(value: Any, expected_kind: str | None = None) -> str:
    entity_id = _forge_identifier("X-ray entity ID", value, XRAY_ENTITY_RE)
    if expected_kind is not None and not entity_id.startswith(expected_kind + ":"):
        raise ValueError("Stage X-ray entity kind and ID disagree")
    return entity_id


def _validate_xray_event_cursor(value: Any) -> None:
    value = _xray_exact_object(value, {"stream_id", "sequence", "event_id"}, set(), "Stage X-ray event watermark")
    _forge_identifier("event stream ID", value["stream_id"], STREAM_ID_RE)
    sequence = _validate_xray_integer("event sequence", value["sequence"])
    event_id = value["event_id"]
    if sequence == 0:
        if event_id is not None:
            raise ValueError("zero Stage event watermark must not name an event")
    elif not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("Stage X-ray event watermark ID is invalid")
    _forge_snapshot_event_sequence({"coordinates": {"event": value}})


def _validate_xray_activity(value: Any) -> None:
    value = _xray_exact_object(value, {"state"}, {"command_id", "status"}, "Stage X-ray activity")
    if value["state"] not in {"idle", "active", "unavailable"}:
        raise ValueError("Stage X-ray activity state is invalid")
    if "command_id" in value:
        _forge_identifier("command ID", value["command_id"], COMMAND_RE)
    if "status" in value and value["status"] not in {"accepted", "running", "cancel_requested", "active"}:
        raise ValueError("Stage X-ray activity status is invalid")


def _validate_xray_section_count(value: Any) -> None:
    value = _xray_exact_object(value, {"included_count", "omitted_count"}, set(), "Stage X-ray section count")
    _validate_xray_integer("included count", value["included_count"])
    _validate_xray_integer("omitted count", value["omitted_count"])


def _validate_xray_host(value: Any) -> None:
    required = {"entity_id", "entity_kind", "hostname", "active", "entry", "accepted_fact_count", "service_count"}
    value = _xray_exact_object(value, required, {"theme"}, "Stage X-ray host summary")
    if value["entity_kind"] != "host":
        raise ValueError("Stage X-ray host kind is invalid")
    _validate_xray_entity_id(value["entity_id"], "host")
    _validate_xray_safe_text(value["hostname"])
    if not isinstance(value["active"], bool) or not isinstance(value["entry"], bool):
        raise ValueError("Stage X-ray host flags are invalid")
    _validate_xray_integer("accepted fact count", value["accepted_fact_count"])
    _validate_xray_integer("service count", value["service_count"])
    if "theme" in value:
        _validate_xray_safe_text(value["theme"])


def _validate_xray_fact(value: Any) -> None:
    required = {"entity_id", "entity_kind", "label", "statement", "scope", "related_host_id"}
    value = _xray_exact_object(value, required, set(), "Stage X-ray fact summary")
    if value["entity_kind"] != "fact" or value["scope"] not in {"world", "host"}:
        raise ValueError("Stage X-ray fact kind or scope is invalid")
    _validate_xray_entity_id(value["entity_id"], "fact")
    _validate_xray_safe_text(value["label"])
    _validate_xray_safe_text(value["statement"])
    if value["related_host_id"] is not None:
        _validate_xray_entity_id(value["related_host_id"], "host")


def _validate_xray_detail_record(value: Any, expected_entity_id: str) -> None:
    required = {"entity_id", "entity_kind", "fields", "accepted", "evidence"}
    value = _xray_exact_object(value, required, {"related_host_id"}, "Stage X-ray entity detail")
    kind = value["entity_kind"]
    if kind not in {"host", "fact"} or value["accepted"] is not True:
        raise ValueError("Stage X-ray detail authority is invalid")
    entity_id = _validate_xray_entity_id(value["entity_id"], kind)
    if entity_id != expected_entity_id:
        raise ValueError("Stage X-ray detail entity diverged")
    fields = value["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
        raise ValueError("Stage X-ray detail fields are invalid")
    for field in fields:
        field = _xray_exact_object(field, {"name", "label", "value"}, set(), "Stage X-ray detail field")
        if not isinstance(field["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field["name"]):
            raise ValueError("Stage X-ray detail field name is invalid")
        if not isinstance(field["label"], str) or not 1 <= len(field["label"]) <= 96 or any(ord(character) <= 31 or 127 <= ord(character) <= 159 for character in field["label"]):
            raise ValueError("Stage X-ray detail field label is invalid")
        _validate_xray_safe_text(field["value"])
    if "related_host_id" in value and value["related_host_id"] is not None:
        _validate_xray_entity_id(value["related_host_id"], "host")
    evidence = _xray_exact_object(value["evidence"], {"state"}, set(), "Stage X-ray evidence")
    if evidence["state"] not in {"present", "unavailable"}:
        raise ValueError("Stage X-ray evidence state is invalid")


def validate_stage_xray_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "stage_xray_snapshot":
        raise ValueError("Stage X-ray snapshot response is invalid")
    _context(value)
    xray = _xray_exact_object(value.get("xray"), {"schema_revision", "accepted_state_revision", "projection_position", "stage_event_watermark", "overview", "hosts", "facts", "activity", "completeness"}, set(), "Stage X-ray snapshot")
    _validate_xray_coordinates(xray)
    overview = _xray_exact_object(xray["overview"], set(), {"world_name", "theme", "description", "active_host_id"}, "Stage X-ray overview")
    for name in ("world_name", "theme", "description"):
        if name in overview:
            _validate_xray_safe_text(overview[name])
    if "active_host_id" in overview:
        _validate_xray_entity_id(overview["active_host_id"], "host")
    if not isinstance(xray["hosts"], list) or len(xray["hosts"]) > 64 or not isinstance(xray["facts"], list) or len(xray["facts"]) > 128:
        raise ValueError("Stage X-ray master collections are invalid")
    for item in xray["hosts"]:
        _validate_xray_host(item)
    for item in xray["facts"]:
        _validate_xray_fact(item)
    activity = _xray_exact_object(xray["activity"], {"foreground", "enrichment"}, set(), "Stage X-ray activity projection")
    _validate_xray_activity(activity["foreground"])
    _validate_xray_activity(activity["enrichment"])
    completeness = _xray_exact_object(xray["completeness"], {"complete", "hosts", "facts", "clipped_field_count", "control_marked_field_count"}, set(), "Stage X-ray completeness")
    if not isinstance(completeness["complete"], bool):
        raise ValueError("Stage X-ray completeness flag is invalid")
    _validate_xray_section_count(completeness["hosts"])
    _validate_xray_section_count(completeness["facts"])
    _validate_xray_integer("clipped field count", completeness["clipped_field_count"])
    _validate_xray_integer("control-marked field count", completeness["control_marked_field_count"])
    return value


def validate_stage_xray_detail(value: Any, *, expected_entity_id: str, expected_accepted_state_revision: int, expected_xray_stream_id: str, expected_xray_sequence: int) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != "stage_xray_detail":
        raise ValueError("Stage X-ray detail response is invalid")
    _context(value)
    expected_entity_id = _validate_xray_entity_id(expected_entity_id)
    expected_accepted_state_revision = _validate_xray_integer(
        "expected Stage X-ray accepted-state revision",
        expected_accepted_state_revision,
    )
    expected_xray_stream_id = _forge_identifier(
        "expected X-ray stream ID",
        expected_xray_stream_id,
        XRAY_STREAM_RE,
    )
    expected_xray_sequence = _validate_xray_integer(
        "expected Stage X-ray sequence",
        expected_xray_sequence,
        minimum=1,
    )
    xray = _xray_exact_object(value.get("xray"), {"schema_revision", "accepted_state_revision", "projection_position", "stage_event_watermark", "detail"}, set(), "Stage X-ray detail")
    observed = _validate_xray_coordinates(xray)
    expected = (expected_accepted_state_revision, expected_xray_stream_id, expected_xray_sequence)
    if observed != expected:
        raise ValueError("Stage X-ray detail coordinates diverged")
    _validate_xray_detail_record(xray["detail"], expected_entity_id)
    return value


"""
    if validation_marker not in source:
        raise _CORE.ContractError(
            "generated X-ray validation insertion point is missing"
        )
    source = source.replace(validation_marker, validation + validation_marker, 1)
    method_marker = "    def _control_mutation("
    methods = r"""
    def stage_xray_snapshot(self, *, epoch: str, actor: str, capability: str, reference: str) -> dict[str, Any]:
        reference = _bounded_header("session reference", reference, REFERENCE_RE)
        path = "/product/v1/stage/sessions/" + quote(reference, safe="") + "/xray"
        payload = self._request("GET", path, self._headers(epoch=epoch, actor=actor, session_capability=capability))
        return validate_stage_xray_snapshot(payload)

    def stage_xray_detail(self, *, epoch: str, actor: str, capability: str, reference: str, entity_id: str, accepted_state_revision: int, xray_stream_id: str, xray_sequence: int) -> dict[str, Any]:
        entity_id = _validate_xray_entity_id(entity_id)
        accepted_state_revision = _validate_xray_integer(
            "expected Stage X-ray accepted-state revision",
            accepted_state_revision,
        )
        xray_stream_id = _forge_identifier(
            "expected X-ray stream ID",
            xray_stream_id,
            XRAY_STREAM_RE,
        )
        xray_sequence = _validate_xray_integer(
            "expected Stage X-ray sequence",
            xray_sequence,
            minimum=1,
        )
        reference = _bounded_header("session reference", reference, REFERENCE_RE)
        path = "/product/v1/stage/sessions/" + quote(reference, safe="") + "/xray/details/" + quote(entity_id, safe="")
        payload = self._request("GET", path, self._headers(epoch=epoch, actor=actor, session_capability=capability), query={"accepted_state_revision": accepted_state_revision, "xray_stream_id": xray_stream_id, "xray_sequence": xray_sequence})
        return validate_stage_xray_detail(payload, expected_entity_id=entity_id, expected_accepted_state_revision=accepted_state_revision, expected_xray_stream_id=xray_stream_id, expected_xray_sequence=xray_sequence)

"""
    if method_marker not in source:
        raise _CORE.ContractError("generated X-ray client insertion point is missing")
    return source.replace(method_marker, methods + method_marker, 1)


def _smoke_import(path: Path, root: Path) -> None:
    _base_smoke_import(path, root)
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_xray_generated_smoke",
        path,
    )
    if specification is None or specification.loader is None:
        raise _CORE.ContractError("cannot import generated Stage X-ray client")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    snapshot = _CORE.load_json(root / "fixtures/canonical-valid/xray/snapshot.json")
    host = _CORE.load_json(root / "fixtures/canonical-valid/xray/host-detail.json")
    fact = _CORE.load_json(root / "fixtures/canonical-valid/xray/fact-detail.json")
    module.validate_stage_xray_snapshot(snapshot)
    position = snapshot["xray"]["projection_position"]
    for detail in (host, fact):
        module.validate_stage_xray_detail(
            detail,
            expected_entity_id=detail["xray"]["detail"]["entity_id"],
            expected_accepted_state_revision=snapshot["xray"][
                "accepted_state_revision"
            ],
            expected_xray_stream_id=position["stream_id"],
            expected_xray_sequence=position["sequence"],
        )

    additive_snapshot = deepcopy(snapshot)
    additive_snapshot["future_safe_top_level"] = {"revision": 1}
    module.validate_stage_xray_snapshot(additive_snapshot)

    for accepted_boundary in (0, module.MAX_SAFE_INTEGER):
        changed = deepcopy(snapshot)
        changed["xray"]["accepted_state_revision"] = accepted_boundary
        module.validate_stage_xray_snapshot(changed)

        changed_detail = deepcopy(host)
        changed_detail["xray"]["accepted_state_revision"] = accepted_boundary
        module.validate_stage_xray_detail(
            changed_detail,
            expected_entity_id=changed_detail["xray"]["detail"]["entity_id"],
            expected_accepted_state_revision=accepted_boundary,
            expected_xray_stream_id=position["stream_id"],
            expected_xray_sequence=position["sequence"],
        )

    def set_xray_sequence(payload: dict[str, Any], sequence: int) -> None:
        projection_position = payload["xray"]["projection_position"]
        projection_position["sequence"] = sequence
        stream_suffix = projection_position["stream_id"].removeprefix("xray-stream:")
        projection_position["cursor"] = f"xray-cursor:{stream_suffix}:{sequence}"

    for sequence_boundary in (1, module.MAX_SAFE_INTEGER):
        changed = deepcopy(snapshot)
        set_xray_sequence(changed, sequence_boundary)
        module.validate_stage_xray_snapshot(changed)

        changed_detail = deepcopy(host)
        set_xray_sequence(changed_detail, sequence_boundary)
        module.validate_stage_xray_detail(
            changed_detail,
            expected_entity_id=changed_detail["xray"]["detail"]["entity_id"],
            expected_accepted_state_revision=changed_detail["xray"][
                "accepted_state_revision"
            ],
            expected_xray_stream_id=position["stream_id"],
            expected_xray_sequence=sequence_boundary,
        )

    rejected_mutations = 0

    def reject(label: str, operation: Any) -> None:
        nonlocal rejected_mutations
        try:
            operation()
        except ValueError:
            rejected_mutations += 1
            return
        raise _CORE.ContractError(f"generated X-ray client accepted {label} mutation")

    snapshot_mutations = []
    changed = deepcopy(snapshot)
    changed["xray"]["projection_position"]["sequence"] += 1
    snapshot_mutations.append(("coordinate drift", changed))
    changed = deepcopy(snapshot)
    changed["xray"]["stage_event_watermark"]["event_id"] = "event:phase2alphaevent:4"
    snapshot_mutations.append(("event watermark drift", changed))
    changed = deepcopy(snapshot)
    changed["xray"]["hosts"][0]["raw_state"] = {"secret": True}
    snapshot_mutations.append(("nested raw state", changed))
    changed = deepcopy(snapshot)
    changed["xray"]["hosts"][0]["hostname"]["text"] = ""
    snapshot_mutations.append(("empty required safe text", changed))
    changed = deepcopy(snapshot)
    changed["xray"]["hosts"][0]["entity_kind"] = "fact"
    snapshot_mutations.append(("summary kind and ID disagreement", changed))
    changed = deepcopy(snapshot)
    del changed["xray"]["completeness"]["hosts"]
    snapshot_mutations.append(("missing nested field", changed))
    for label, changed in snapshot_mutations:
        reject(
            label,
            lambda changed=changed: module.validate_stage_xray_snapshot(changed),
        )

    detail_mutations = []
    changed = deepcopy(host)
    changed["xray"]["detail"]["raw_state"] = {"secret": True}
    detail_mutations.append(
        ("extra detail field", changed, changed["xray"]["detail"]["entity_id"])
    )
    changed = deepcopy(host)
    requested_entity_id = "host:" + "f" * 32
    detail_mutations.append(
        ("different returned detail entity", changed, requested_entity_id)
    )
    for label, changed, expected_entity_id in detail_mutations:
        reject(
            label,
            lambda changed=changed, expected_entity_id=expected_entity_id: (
                module.validate_stage_xray_detail(
                    changed,
                    expected_entity_id=expected_entity_id,
                    expected_accepted_state_revision=snapshot["xray"][
                        "accepted_state_revision"
                    ],
                    expected_xray_stream_id=position["stream_id"],
                    expected_xray_sequence=position["sequence"],
                )
            ),
        )

    invalid_accepted_values = (
        ("above maximum", module.MAX_SAFE_INTEGER + 1),
        ("negative", -1),
        ("boolean", True),
        ("string", str(snapshot["xray"]["accepted_state_revision"])),
        ("integral float", float(snapshot["xray"]["accepted_state_revision"])),
        ("nonintegral float", 3.5),
    )
    invalid_sequence_values = (
        ("above maximum", module.MAX_SAFE_INTEGER + 1),
        ("negative", -1),
        ("zero", 0),
        ("boolean", True),
        ("string", str(position["sequence"])),
        ("integral float", float(position["sequence"])),
        ("nonintegral float", 7.5),
    )
    invalid_stream_values = (
        ("null", None),
        ("empty", ""),
        ("malformed", "xray-stream:short"),
        ("overlong", "xray-stream:" + "a" * 33),
        ("non-string", 7),
    )

    for label, invalid in invalid_accepted_values:
        changed = deepcopy(snapshot)
        changed["xray"]["accepted_state_revision"] = invalid
        reject(
            f"snapshot accepted-state {label}",
            lambda changed=changed: module.validate_stage_xray_snapshot(changed),
        )
        changed_detail = deepcopy(host)
        changed_detail["xray"]["accepted_state_revision"] = invalid
        reject(
            f"detail accepted-state {label}",
            lambda changed_detail=changed_detail: module.validate_stage_xray_detail(
                changed_detail,
                expected_entity_id=host["xray"]["detail"]["entity_id"],
                expected_accepted_state_revision=snapshot["xray"][
                    "accepted_state_revision"
                ],
                expected_xray_stream_id=position["stream_id"],
                expected_xray_sequence=position["sequence"],
            ),
        )

    for label, invalid in invalid_sequence_values:
        changed = deepcopy(snapshot)
        changed["xray"]["projection_position"]["sequence"] = invalid
        reject(
            f"snapshot sequence {label}",
            lambda changed=changed: module.validate_stage_xray_snapshot(changed),
        )
        changed_detail = deepcopy(host)
        changed_detail["xray"]["projection_position"]["sequence"] = invalid
        reject(
            f"detail sequence {label}",
            lambda changed_detail=changed_detail: module.validate_stage_xray_detail(
                changed_detail,
                expected_entity_id=host["xray"]["detail"]["entity_id"],
                expected_accepted_state_revision=snapshot["xray"][
                    "accepted_state_revision"
                ],
                expected_xray_stream_id=position["stream_id"],
                expected_xray_sequence=position["sequence"],
            ),
        )

    context_mutations: list[tuple[str, dict[str, Any]]] = []
    changed = deepcopy(snapshot)
    del changed["service_epoch"]
    context_mutations.append(("missing service epoch", changed))
    for label, invalid in (
        ("empty service epoch", ""),
        ("malformed service epoch", "not-an-epoch"),
        ("non-string service epoch", 7),
        ("overlong service epoch", "epoch:" + "a" * 129),
    ):
        changed = deepcopy(snapshot)
        changed["service_epoch"] = invalid
        context_mutations.append((label, changed))
    for field in ("name", "revision", "compatibility"):
        changed = deepcopy(snapshot)
        del changed["contract"][field]
        context_mutations.append((f"missing contract {field}", changed))
    changed = deepcopy(snapshot)
    changed["contract"]["future"] = "not-allowed"
    context_mutations.append(("extra contract field", changed))
    for field in ("name", "revision", "compatibility"):
        changed = deepcopy(snapshot)
        changed["contract"][field] = f"wrong-{field}"
        context_mutations.append((f"incorrect contract {field}", changed))
    for label, changed in context_mutations:
        reject(
            label,
            lambda changed=changed: module.validate_stage_xray_snapshot(changed),
        )

    expected_entity_id = host["xray"]["detail"]["entity_id"]
    accepted_state_revision = snapshot["xray"]["accepted_state_revision"]
    xray_stream_id = position["stream_id"]
    xray_sequence = position["sequence"]
    for label, invalid in invalid_accepted_values:
        reject(
            f"expected accepted-state {label}",
            lambda invalid=invalid: module.validate_stage_xray_detail(
                host,
                expected_entity_id=expected_entity_id,
                expected_accepted_state_revision=invalid,
                expected_xray_stream_id=xray_stream_id,
                expected_xray_sequence=xray_sequence,
            ),
        )
    for label, invalid in invalid_sequence_values:
        reject(
            f"expected sequence {label}",
            lambda invalid=invalid: module.validate_stage_xray_detail(
                host,
                expected_entity_id=expected_entity_id,
                expected_accepted_state_revision=accepted_state_revision,
                expected_xray_stream_id=xray_stream_id,
                expected_xray_sequence=invalid,
            ),
        )
    for label, invalid in invalid_stream_values:
        reject(
            f"expected stream {label}",
            lambda invalid=invalid: module.validate_stage_xray_detail(
                host,
                expected_entity_id=expected_entity_id,
                expected_accepted_state_revision=accepted_state_revision,
                expected_xray_stream_id=invalid,
                expected_xray_sequence=xray_sequence,
            ),
        )

    class NoTransport:
        calls = 0

        def open(self, *_args: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("invalid X-ray precondition reached transport")

    client = module.ProductSessionPreviewClient(
        "http://127.0.0.1",
        "private-preview-smoke-bearer",
    )
    no_transport = NoTransport()
    client._opener = no_transport
    request_arguments = {
        "epoch": snapshot["service_epoch"],
        "actor": "actor:stage_xray_smoke_0001",
        "capability": "session-cap:stage_xray_smoke_capability_0001",
        "reference": "session-ref:stage_xray_smoke_0001",
        "entity_id": expected_entity_id,
        "accepted_state_revision": accepted_state_revision,
        "xray_stream_id": xray_stream_id,
        "xray_sequence": xray_sequence,
    }
    for label, invalid in invalid_accepted_values:
        arguments = {**request_arguments, "accepted_state_revision": invalid}
        reject(
            f"pre-transport accepted-state {label}",
            lambda arguments=arguments: client.stage_xray_detail(**arguments),
        )
    for label, invalid in invalid_sequence_values:
        arguments = {**request_arguments, "xray_sequence": invalid}
        reject(
            f"pre-transport sequence {label}",
            lambda arguments=arguments: client.stage_xray_detail(**arguments),
        )
    for label, invalid in invalid_stream_values:
        arguments = {**request_arguments, "xray_stream_id": invalid}
        reject(
            f"pre-transport stream {label}",
            lambda arguments=arguments: client.stage_xray_detail(**arguments),
        )
    if no_transport.calls != 0:
        raise _CORE.ContractError(
            "generated X-ray client dispatched an invalid precondition"
        )

    class TransportCaptured(RuntimeError):
        pass

    class CaptureTransport:
        url: str | None = None

        def open(self, request: Any, **_kwargs: Any) -> None:
            self.url = request.full_url
            raise TransportCaptured

    capture = CaptureTransport()
    client._opener = capture
    canonical_arguments = {
        **request_arguments,
        "accepted_state_revision": module.MAX_SAFE_INTEGER,
        "xray_sequence": module.MAX_SAFE_INTEGER,
    }
    try:
        client.stage_xray_detail(**canonical_arguments)
    except TransportCaptured:
        pass
    else:
        raise _CORE.ContractError(
            "generated X-ray client did not reach canonical query transport"
        )
    expected_query = (
        f"accepted_state_revision={module.MAX_SAFE_INTEGER}"
        "&xray_stream_id=xray-stream%3Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        f"&xray_sequence={module.MAX_SAFE_INTEGER}"
    )
    if capture.url is None or capture.url.partition("?")[2] != expected_query:
        raise _CORE.ContractError(
            "generated X-ray client did not emit canonical decimal coordinates"
        )

    if rejected_mutations != XRAY_CLIENT_SMOKE_REJECTED_MUTATIONS:
        raise _CORE.ContractError(
            f"generated X-ray client-smoke mutation count drifted: {rejected_mutations}"
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
            raise _CORE.ContractError("published Stage X-ray artifacts are immutable")
    return _base_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _base_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


for module in (_BASE, _LIVE, _STAGE, _CORE):
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
