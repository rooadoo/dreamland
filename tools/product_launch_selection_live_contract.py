#!/usr/bin/env python3
"""Build the capability-disabled Phase 7B live launch-selection contract.

The live release candidate preserves the Phase 7A route and data model while
pinning a truthful provider-backed development identity.  Importing this tool
creates no provider, listener, worker, artifact, or runtime composition.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_launch_selection_contract() -> ModuleType:
    path = Path(__file__).with_name("product_launch_selection_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_launch_selection_live_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Presenter launch-selection contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_SELECTION: Any = _load_launch_selection_contract()
_BASE: Any = _SELECTION._BASE
_FORGE: Any = _SELECTION._FORGE
_LIVE: Any = _SELECTION._LIVE
_STAGE: Any = _SELECTION._STAGE
_CORE: Any = _SELECTION._CORE

CONTRACT_NAME = _SELECTION.CONTRACT_NAME
CONTRACT_REVISION = "0.7.0-rc.2"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _SELECTION.CONTRACT_COMPATIBILITY
LIVE_EXECUTOR = "development-live-launch-selection-langgraph-v1"

for module in (_SELECTION, _BASE, _FORGE, _LIVE, _STAGE, _CORE):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
for module in (_SELECTION, _BASE, _FORGE, _LIVE):
    module.LIVE_EXECUTOR = LIVE_EXECUTOR
_SELECTION.PREVIEW_EXECUTOR = LIVE_EXECUTOR

_selection_build_openapi = _SELECTION.build_openapi
_selection_build_manifest_schema = _SELECTION.build_manifest_schema
_selection_fixture_definitions = _SELECTION.fixture_definitions
_selection_base_manifest = _SELECTION._base_manifest
_selection_generated_client_source = _SELECTION.generated_client_source


def build_openapi() -> dict[str, Any]:
    """Return the Phase 7A API with a truthful live-development identity."""
    document = _selection_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Presenter Launch Selection Live Development",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen Phase 7B live-development contract for one explicitly "
            "started, loopback-only authored and World Forge Presenter launch "
            "selection target. Forge builds and accepted Stage commands may "
            "call configured providers; all production capabilities remain "
            "disabled."
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
    # Listing, exchange, and adjacent Stage provisioning do not dispatch a
    # provider even in the live composition. Provider work belongs only to a
    # submitted Forge job or an accepted Stage command.
    document["x-dreamland-stage-launch-selection"]["provider_dispatch"] = False
    return document


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = []
    for fixture in _selection_fixture_definitions():
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
    manifest = _selection_base_manifest()
    prefix = (
        ".venv/bin/python tools/product_launch_selection_live_contract.py"
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
    schema = _selection_build_manifest_schema()
    _replace_generator_path(schema)
    return schema


def _replace_generator_path(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_launch_selection_contract.py",
                    "tools/product_launch_selection_live_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_launch_selection_contract.py",
                    "tools/product_launch_selection_live_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return (
        _selection_generated_client_source(openapi)
        .replace(
            "Generated by tools/product_launch_selection_contract.py",
            "Generated by tools/product_launch_selection_live_contract.py",
            1,
        )
        .replace('"provider_calls": False', '"provider_calls": True')
    )


for module in (_SELECTION, _BASE, _FORGE, _LIVE, _STAGE, _CORE):
    module.build_openapi = build_openapi
    module.build_manifest_schema = build_manifest_schema
    module.fixture_definitions = fixture_definitions
    module._base_manifest = _base_manifest
    module.generated_client_source = generated_client_source

build_events_schema = _CORE.build_events_schema
refresh_contract = _SELECTION.refresh_contract
validate_contract = _SELECTION.validate_contract
client_smoke = _SELECTION.client_smoke
publish_contract = _SELECTION.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
