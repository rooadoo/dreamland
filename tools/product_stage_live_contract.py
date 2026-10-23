#!/usr/bin/env python3
"""Build the capability-disabled Phase 3D live-development contract kit."""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_stage_contract() -> ModuleType:
    path = Path(__file__).with_name("product_stage_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_stage_live_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Presenter Stage contract generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_STAGE: Any = _load_stage_contract()
_CORE: Any = _STAGE._CORE

CONTRACT_NAME = _STAGE.CONTRACT_NAME
CONTRACT_REVISION = "0.4.0-rc.2"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _STAGE.CONTRACT_COMPATIBILITY
LIVE_EXECUTOR = "development-live-langgraph-v1"

for module in (_STAGE, _CORE):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY

_stage_build_openapi = _STAGE.build_openapi
_stage_build_manifest_schema = _STAGE.build_manifest_schema
_stage_fixture_definitions = _STAGE.fixture_definitions
_stage_base_manifest = _STAGE._base_manifest
_stage_generated_client_source = _STAGE.generated_client_source


def build_openapi() -> dict[str, Any]:
    document = _stage_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Presenter Stage Live Development Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen, private, manually invoked Phase 3D development target. "
            "It may call the configured model provider but remains loopback-only, "
            "capability-disabled, and unsuitable as production evidence."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]["properties"]
    preview["executor"]["const"] = LIVE_EXECUTOR
    preview["provider_calls"]["const"] = True
    document["x-dreamland-development-live"] = {
        "explicit_start_only": True,
        "loopback_only": True,
        "provider_calls": True,
        "usage_accounting": "full_reservation_conservative",
        "capability_evidence": False,
        "normal_runtime_composition": False,
    }
    return document


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = []
    for fixture in _stage_fixture_definitions():
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
    return tuple(fixtures)


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
            preview["executor"] = LIVE_EXECUTOR
            preview["provider_calls"] = True
        for item in value.values():
            _replace_preview_context(item)
    elif isinstance(value, list):
        for item in value:
            _replace_preview_context(item)


def _base_manifest() -> dict[str, Any]:
    manifest = _stage_base_manifest()
    prefix = (
        ".venv/bin/python tools/product_stage_live_contract.py"
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
    schema = _stage_build_manifest_schema()
    _replace_tool_path(schema)
    return schema


def _replace_tool_path(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_stage_contract.py",
                    "tools/product_stage_live_contract.py",
                )
            else:
                _replace_tool_path(item)
    elif isinstance(value, list):
        for item in value:
            _replace_tool_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return (
        _stage_generated_client_source(openapi)
        .replace(
            "Generated by tools/product_stage_contract.py",
            "Generated by tools/product_stage_live_contract.py",
            1,
        )
        .replace("deterministic-no-provider-v1", LIVE_EXECUTOR)
        .replace('"provider_calls": False', '"provider_calls": True')
    )


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

build_events_schema = _STAGE.build_events_schema
refresh_contract = _STAGE.refresh_contract
validate_contract = _STAGE.validate_contract
client_smoke = _STAGE.client_smoke
publish_contract = _STAGE.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _STAGE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
