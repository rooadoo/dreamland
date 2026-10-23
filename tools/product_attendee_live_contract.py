#!/usr/bin/env python3
"""Build the capability-disabled Phase 7D live attendee contract kit.

The live release candidate preserves the published Phase 7C wire contract and
pins a truthful provider-backed development identity. Importing this generator
creates no provider, listener, worker, runtime composition, or capability.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_attendee_contract() -> ModuleType:
    path = Path(__file__).with_name("product_attendee_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_live_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published attendee contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_ATTENDEE: Any = _load_attendee_contract()
_CORE: Any = _ATTENDEE._CORE
_MODULES = tuple(_ATTENDEE._MODULES)

CONTRACT_NAME = _ATTENDEE.CONTRACT_NAME
CONTRACT_REVISION = "0.7.0-rc.4"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _ATTENDEE.CONTRACT_COMPATIBILITY
LIVE_EXECUTOR = "development-live-attendee-langgraph-v1"

EXPECTED_OPERATIONS = _ATTENDEE.EXPECTED_OPERATIONS
MUTATION_OPERATIONS = _ATTENDEE.MUTATION_OPERATIONS
SESSION_OPERATIONS = _ATTENDEE.SESSION_OPERATIONS
SAFE_ERROR_CASES = _ATTENDEE.SAFE_ERROR_CASES
ATTENDEE_SAFE_ERROR_CASES = _ATTENDEE.ATTENDEE_SAFE_ERROR_CASES
CLIENT_SMOKE_VALIDATED_FIXTURES = _ATTENDEE.CLIENT_SMOKE_VALIDATED_FIXTURES
CLIENT_SMOKE_REJECTED_MUTATIONS = _ATTENDEE.CLIENT_SMOKE_REJECTED_MUTATIONS

for module in (_ATTENDEE, *_MODULES):
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    if hasattr(module, "PREVIEW_EXECUTOR"):
        module.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    if hasattr(module, "LIVE_EXECUTOR"):
        module.LIVE_EXECUTOR = LIVE_EXECUTOR

_attendee_build_openapi = _ATTENDEE.build_openapi
_attendee_build_manifest_schema = _ATTENDEE.build_manifest_schema
_attendee_fixture_definitions = _ATTENDEE.fixture_definitions
_attendee_base_manifest = _ATTENDEE._base_manifest_document
_attendee_generated_client_source = _ATTENDEE.generated_client_source


def build_openapi() -> dict[str, Any]:
    """Return RC3 with only revision and live-development identity changed."""
    document = _attendee_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Authored Attendee Live Development",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen Phase 7D live-development identity for one explicitly "
            "started, loopback-only authored attendee target. Accepted "
            "attendee session commands may call the configured provider; "
            "bootstrap, listing, exchange, and lifecycle reads do not. All "
            "production product capabilities remain disabled."
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
    # The three attendee-facade operations remain provider-free. Provider work
    # belongs only to accepted commands on the existing session routes.
    document["x-dreamland-attendee-integration-preview"]["provider_dispatch"] = False
    return document


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = []
    for fixture in _attendee_fixture_definitions():
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
    manifest = _attendee_base_manifest()
    prefix = (
        ".venv/bin/python tools/product_attendee_live_contract.py"
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
    schema = _attendee_build_manifest_schema()
    _replace_generator_path(schema)
    return schema


def _replace_generator_path(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_attendee_contract.py",
                    "tools/product_attendee_live_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_attendee_contract.py",
                    "tools/product_attendee_live_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return (
        _attendee_generated_client_source(openapi)
        .replace(
            "Generated by tools/product_attendee_contract.py",
            "Generated by tools/product_attendee_live_contract.py",
            1,
        )
        .replace('"provider_calls": False', '"provider_calls": True')
    )


for module in _MODULES:
    module.build_openapi = build_openapi
    module.build_manifest_schema = build_manifest_schema
    module.fixture_definitions = fixture_definitions
    module._base_manifest = _base_manifest
    module.generated_client_source = generated_client_source

_ATTENDEE.build_openapi = build_openapi
_ATTENDEE.build_manifest_schema = build_manifest_schema
_ATTENDEE.fixture_definitions = fixture_definitions
_ATTENDEE.generated_client_source = generated_client_source

build_events_schema = _CORE.build_events_schema
refresh_contract = _ATTENDEE.refresh_contract
validate_contract = _ATTENDEE.validate_contract
client_smoke = _ATTENDEE.client_smoke
publish_contract = _ATTENDEE.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
