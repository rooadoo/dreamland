#!/usr/bin/env python3
"""Build and validate the unfrozen Phase 3 Presenter Stage contract kit.

Phase 3 is additive to the Phase 2 shared-core artifact.  This tool reuses the
reviewed core generator and adds only the Presenter Stage facade operations,
schemas, fixtures, and generated-client methods.  It imports no DREAMLAND
runtime module and starts no listener, worker, provider, or background task.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_core_contract() -> ModuleType:
    path = Path(__file__).with_name("product_session_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_core_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("product-session contract generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_CORE: Any = _load_core_contract()

CONTRACT_NAME = _CORE.CONTRACT_NAME
CONTRACT_REVISION = "0.4.0-rc.1"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _CORE.CONTRACT_COMPATIBILITY
AVAILABILITY = _CORE.AVAILABILITY
CAPABILITY_NAMES = _CORE.CAPABILITY_NAMES

STAGE_LAUNCH_PATH = "/product/v1/stage/sessions"
STAGE_CURRENT_PATH = "/product/v1/stage/sessions/current"
STAGE_REPLACE_PATH = "/product/v1/stage/sessions/{reference}/replace"
STAGE_OPERATIONS = {
    STAGE_LAUNCH_PATH: ("post", "launchPresenterStage"),
    STAGE_CURRENT_PATH: ("get", "getCurrentPresenterStage"),
    STAGE_REPLACE_PATH: ("post", "replacePresenterStage"),
}
EXPECTED_OPERATIONS = {**_CORE.EXPECTED_OPERATIONS, **STAGE_OPERATIONS}
MUTATION_OPERATIONS = {
    *_CORE.MUTATION_OPERATIONS,
    "launchPresenterStage",
    "replacePresenterStage",
}
SESSION_OPERATIONS = {
    *_CORE.SESSION_OPERATIONS,
    STAGE_REPLACE_PATH,
}
SAFE_ERROR_CASES = _CORE.SAFE_ERROR_CASES

_CORE.CONTRACT_REVISION = CONTRACT_REVISION
_CORE.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
_CORE.EXPECTED_OPERATIONS = EXPECTED_OPERATIONS
_CORE.MUTATION_OPERATIONS = MUTATION_OPERATIONS
_CORE.SESSION_OPERATIONS = SESSION_OPERATIONS

_core_build_openapi = _CORE.build_openapi
_core_build_manifest_schema = _CORE.build_manifest_schema
_core_fixture_definitions = _CORE.fixture_definitions
_core_base_manifest = _CORE._base_manifest
_core_generated_client_source = _CORE.generated_client_source


def build_openapi() -> dict[str, Any]:
    document = _core_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Presenter Stage Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, private, manually invoked Phase 3 integration preview. "
            "The Presenter Stage facade is additive to the Phase 2 shared core; "
            "all product capabilities remain disabled and this document is not "
            "live-capability evidence."
        ),
    }
    schemas = document["components"]["schemas"]
    schemas.update(_stage_schemas())
    safe_error = schemas["SafeErrorEnvelope"]["properties"]["error"]
    safe_error["properties"]["context"] = _CORE._object(
        {},
        (),
        additive=True,
    )
    safe_error["properties"]["context"].update(
        {
            "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,63}$"},
            "additionalProperties": _CORE._string(
                r"^[^\u0000-\u001f\u007f-\u009f]+$",
                maximum=512,
            ),
            "maxProperties": 8,
        }
    )
    common_read = [
        "SessionReference",
        "ExpectedEpoch",
        "ActorContext",
        "SessionCapability",
    ]
    document["paths"].update(
        {
            STAGE_LAUNCH_PATH: {
                "post": _CORE._operation(
                    "launchPresenterStage",
                    "StageLaunchResponse",
                    (
                        "Create or resume only the exact presenter selection and "
                        "compatible effective launch policy."
                    ),
                    parameters=["ExpectedEpoch", "ActorContext", "IdempotencyKey"],
                    request_schema="CreateSessionRequest",
                    created=True,
                    idempotency_family="session.create",
                )
            },
            STAGE_CURRENT_PATH: {
                "get": _CORE._operation(
                    "getCurrentPresenterStage",
                    "StageCurrentResponse",
                    "Return the presenter's current Stage binding without allocation.",
                    parameters=["ExpectedEpoch", "ActorContext"],
                )
            },
            STAGE_REPLACE_PATH: {
                "post": _CORE._operation(
                    "replacePresenterStage",
                    "StageReplacementResponse",
                    (
                        "Provision the selected Stage revision before an atomic "
                        "expected-control-CAS cutover."
                    ),
                    parameters=[*common_read, "IdempotencyKey"],
                    request_schema="SwitchSessionRequest",
                    idempotency_family="session.switch",
                )
            },
        }
    )
    document["x-dreamland-presenter-stage"] = {
        "ordinary_resume": "identical_launch_grant_and_compatible_effective_policy",
        "same_revision_different_selection": "current_session_exists",
        "replacement": "explicit_expected_control_cas_provision_before_cutover",
        "admission_class": "presenter_stage_reserved",
        "xray_included": False,
        "browser_authentication_owner": "ui_bff",
    }
    return document


def _stage_schemas() -> dict[str, Any]:
    display = _CORE._object(
        {
            "title": _CORE._string(r"^[^\u0000-\u001f\u007f-\u009f]+$", maximum=120),
            "source_label": _CORE._string(
                r"^[^\u0000-\u001f\u007f-\u009f]+$", maximum=64
            ),
            "revision_label": _CORE._string(
                r"^[^\u0000-\u001f\u007f-\u009f]+$", maximum=96
            ),
        },
        ("title", "source_label", "revision_label"),
    )
    stage = _CORE._object(
        {
            "reference": _CORE._ref("SessionReference"),
            "display": _CORE._ref("StageDisplayMetadata"),
            "admission_class": {
                "type": "string",
                "const": "presenter_stage_reserved",
            },
            "xray_available": {"type": "boolean", "const": False},
        },
        ("reference", "display", "admission_class", "xray_available"),
    )
    stage_snapshot = {
        "allOf": [
            _CORE._ref("SessionSnapshot"),
            _CORE._object(
                {"stage": _CORE._ref("StageProjection")},
                ("stage",),
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
                "snapshot": _CORE._ref("PresenterStageSnapshot"),
            },
            ("kind", "service_epoch", "replayed", "session_authority", "snapshot"),
        )

    no_current = _CORE._response_schema(
        {
            "kind": {"type": "string", "const": "no_current_session"},
            "service_epoch": _CORE._ref("ServiceEpoch"),
        },
        ("kind", "service_epoch"),
    )
    current_active = _CORE._response_schema(
        {
            "kind": {"type": "string", "const": "current_session"},
            "service_epoch": _CORE._ref("ServiceEpoch"),
            "session_authority": _CORE._ref("SessionAuthority"),
            "snapshot": _CORE._ref("PresenterStageSnapshot"),
        },
        ("kind", "service_epoch", "session_authority", "snapshot"),
    )
    current_terminal = _CORE._response_schema(
        {
            "kind": {"type": "string", "const": "current_session_terminal"},
            "service_epoch": _CORE._ref("ServiceEpoch"),
            "snapshot": _CORE._ref("PresenterStageSnapshot"),
        },
        ("kind", "service_epoch", "snapshot"),
    )
    replacement = _CORE._response_schema(
        {
            "kind": {"type": "string", "const": "stage_session_replaced"},
            "service_epoch": _CORE._ref("ServiceEpoch"),
            "replayed": {"type": "boolean"},
            "session_authority": _CORE._ref("SessionAuthority"),
            "previous_snapshot": _CORE._ref("PresenterStageSnapshot"),
            "snapshot": _CORE._ref("PresenterStageSnapshot"),
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
    return {
        "StageDisplayMetadata": display,
        "StageProjection": stage,
        "PresenterStageSnapshot": stage_snapshot,
        "StageCreatedResponse": launch_variant("stage_session_created"),
        "StageResumedResponse": launch_variant("stage_session_resumed"),
        "StageLaunchResponse": {
            "oneOf": [
                _CORE._ref("StageCreatedResponse"),
                _CORE._ref("StageResumedResponse"),
            ]
        },
        "StageNoCurrentResponse": no_current,
        "StageCurrentActiveResponse": current_active,
        "StageCurrentTerminalResponse": current_terminal,
        "StageCurrentResponse": {
            "oneOf": [
                _CORE._ref("StageNoCurrentResponse"),
                _CORE._ref("StageCurrentActiveResponse"),
                _CORE._ref("StageCurrentTerminalResponse"),
            ]
        },
        "StageReplacementResponse": replacement,
        "StageRecoveryFixture": _CORE._object(
            {
                "scenario": {
                    "type": "string",
                    "const": "stage_launch_response_lost_after_commit",
                },
                "same_key_replay": {"type": "boolean", "const": True},
                "automatic_new_key_redispatch": {"type": "boolean", "const": False},
                "reconcile_with": {"type": "string", "const": "same_request"},
                "expected_outcome": {
                    "type": "string",
                    "const": "stage_session_created",
                },
            },
            (
                "scenario",
                "same_key_replay",
                "automatic_new_key_redispatch",
                "reconcile_with",
                "expected_outcome",
            ),
        ),
    }


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = list(_core_fixture_definitions())
    snapshot = _stage_snapshot()
    created = _CORE._success(
        {
            "kind": "stage_session_created",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "session_authority": _CORE._SESSION_AUTHORITY,
            "snapshot": snapshot,
        }
    )
    resumed = deepcopy(created)
    resumed["kind"] = "stage_session_resumed"
    replacement = _CORE._success(
        {
            "kind": "stage_session_replaced",
            "service_epoch": _CORE._EPOCH,
            "replayed": False,
            "session_authority": "session-cap:phase3_stage_replacement_cap_0001",
            "previous_snapshot": _stage_snapshot(state="replaced"),
            "snapshot": _stage_snapshot(
                reference="session-ref:phase3_stage_replacement_0001",
                session_id="session:phase3_stage_replacement_0001",
            ),
        }
    )
    additions = (
        _CORE.Fixture(
            "stage_launch_request",
            "canonical-valid",
            "fixtures/canonical-valid/stage/launch-request.json",
            "openapi.json#/components/schemas/CreateSessionRequest",
            {"launch_grant": "launch-grant:phase3_stage_launch_00000001"},
            "send",
        ),
        _CORE.Fixture(
            "stage_launch_created",
            "canonical-valid",
            "fixtures/canonical-valid/stage/launch-created.json",
            "openapi.json#/components/schemas/StageLaunchResponse",
            created,
            "store_private_authority_and_render_stage",
        ),
        _CORE.Fixture(
            "stage_launch_resumed",
            "canonical-valid",
            "fixtures/canonical-valid/stage/launch-resumed.json",
            "openapi.json#/components/schemas/StageLaunchResponse",
            resumed,
            "resume_same_stage_without_redispatch",
        ),
        _CORE.Fixture(
            "stage_replace_request",
            "canonical-valid",
            "fixtures/canonical-valid/stage/replace-request.json",
            "openapi.json#/components/schemas/SwitchSessionRequest",
            {
                "expected_control_revision": 1,
                "launch_grant": "launch-grant:phase3_stage_replace_00000001",
            },
            "send_explicit_replacement",
        ),
        _CORE.Fixture(
            "stage_replace_success",
            "canonical-valid",
            "fixtures/canonical-valid/stage/replace-success.json",
            "openapi.json#/components/schemas/StageReplacementResponse",
            replacement,
            "rotate_private_authority_after_atomic_cutover",
        ),
        _CORE.Fixture(
            "stage_launch_response_lost",
            "canonical-valid",
            "fixtures/canonical-valid/recovery/stage-launch-response-lost.json",
            "openapi.json#/components/schemas/StageRecoveryFixture",
            {
                "scenario": "stage_launch_response_lost_after_commit",
                "same_key_replay": True,
                "automatic_new_key_redispatch": False,
                "reconcile_with": "same_request",
                "expected_outcome": "stage_session_created",
            },
            "retry_identical_stage_launch",
        ),
        _CORE.Fixture(
            "stage_snapshot_forbidden_raw_state",
            "invalid",
            "fixtures/invalid/stage/raw-world-state.json",
            "openapi.json#/components/schemas/PresenterStageSnapshot",
            {**snapshot, "world_state": {}},
            "reject",
        ),
    )
    fixtures.extend(additions)
    return tuple(fixtures)


def _stage_snapshot(**kwargs: Any) -> dict[str, Any]:
    snapshot = _CORE._snapshot(**kwargs)
    snapshot["authority_class"] = "presenter"
    snapshot["stage"] = {
        "reference": snapshot["reference"],
        "display": {
            "title": "Presenter Demonstration World",
            "source_label": "Authored",
            "revision_label": "Demo A",
        },
        "admission_class": "presenter_stage_reserved",
        "xray_available": False,
    }
    return snapshot


def _base_manifest() -> dict[str, Any]:
    manifest = _core_base_manifest()
    prefix = (
        ".venv/bin/python tools/product_stage_contract.py"
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
    schema = _core_build_manifest_schema()

    def replace_tool(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in tuple(value.items()):
                if isinstance(item, str):
                    value[key] = item.replace(
                        "tools/product_session_contract.py",
                        "tools/product_stage_contract.py",
                    )
                else:
                    replace_tool(item)
        elif isinstance(value, list):
            for item in value:
                replace_tool(item)

    replace_tool(schema)
    return schema


def generated_client_source(openapi: dict[str, Any]) -> str:
    source = _core_generated_client_source(openapi)
    insertion = """
    def launch_stage(self, *, epoch: str, actor: str, key: str, launch_grant: str) -> dict[str, Any]:
        return self._request("POST", "/product/v1/stage/sessions", self._headers(epoch=epoch, actor=actor, idempotency_key=key), {"launch_grant": launch_grant})

    def current_stage(self, *, epoch: str, actor: str) -> dict[str, Any]:
        return self._request("GET", "/product/v1/stage/sessions/current", self._headers(epoch=epoch, actor=actor))

    def replace_stage(self, *, epoch: str, actor: str, capability: str, reference: str, key: str, expected_control_revision: int, launch_grant: str) -> dict[str, Any]:
        reference = _bounded_header("session reference", reference, REFERENCE_RE)
        path = "/product/v1/stage/sessions/" + quote(reference, safe="") + "/replace"
        body = {"expected_control_revision": expected_control_revision, "launch_grant": launch_grant}
        return self._request("POST", path, self._headers(epoch=epoch, actor=actor, session_capability=capability, idempotency_key=key), body)

"""
    marker = "    def _control_mutation("
    if marker not in source:
        raise _CORE.ContractError("generated client insertion point is unavailable")
    return source.replace(marker, insertion + marker, 1).replace(
        "Generated by tools/product_session_contract.py",
        "Generated by tools/product_stage_contract.py",
        1,
    )


_CORE.build_openapi = build_openapi
_CORE.build_manifest_schema = build_manifest_schema
_CORE.fixture_definitions = fixture_definitions
_CORE._base_manifest = _base_manifest
_CORE.generated_client_source = generated_client_source

build_events_schema = _CORE.build_events_schema
refresh_contract = _CORE.refresh_contract
validate_contract = _CORE.validate_contract
client_smoke = _CORE.client_smoke
publish_contract = _CORE.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
