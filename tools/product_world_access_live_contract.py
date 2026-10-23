#!/usr/bin/env python3
"""Build the capability-disabled Phase 8B live World Access contract kit.

The live release candidate preserves World Access RC3's six-operation wire,
authority, idempotency, and recovery contract.  It changes only the contract
and dependency revisions plus truthful, loopback-only live-development
identity.  Importing this generator creates no provider, listener, worker,
runtime composition, or capability.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_world_access_contract() -> ModuleType:
    path = Path(__file__).with_name("product_world_access_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_world_access_live_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published World Access contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_BASE: Any = _load_world_access_contract()

CONTRACT_NAME = _BASE.CONTRACT_NAME
CONTRACT_REVISION = "0.1.0-rc.4"
CONTRACT_COMPATIBILITY = _BASE.CONTRACT_COMPATIBILITY
PRODUCT_DEPENDENCY_REVISION = "0.7.0-rc.4"
PRODUCT_DEPENDENCY_MANIFEST_SHA256 = (
    "1e90c8cbc2cd0465710bcddb931504de11e75d98c7a10e32ec84bc470de62588"
)
FORGE_DEPENDENCY_REVISION = _BASE.FORGE_DEPENDENCY_REVISION
FORGE_DEPENDENCY_MANIFEST_SHA256 = _BASE.FORGE_DEPENDENCY_MANIFEST_SHA256
LIVE_KIND = "live_generated_world_access_development"
LIVE_IMPLEMENTATION_STATUS = "provider_backed_live_development_only"

EXPECTED_PATHS = _BASE.EXPECTED_PATHS
EXPECTED_OPERATIONS = _BASE.EXPECTED_OPERATIONS
EXPECTED_FILES = _BASE.EXPECTED_FILES
WorldAccessContractError = _BASE.WorldAccessContractError

PUBLICATION_IMPLEMENTATION_FILES = set(_BASE.PUBLICATION_IMPLEMENTATION_FILES) | {
    "tools/product_attendee_live.py",
    "tools/product_world_access_live.py",
    "tools/product_world_access_live_contract.py",
    "tools/world_access_live_client_smoke/run-smoke.mjs",
}

_base_build_openapi = _BASE.build_openapi
_base_build_manifest_schema = _BASE.build_manifest_schema
_base_fixture_definitions = _BASE.fixture_definitions
_base_manifest_document = _BASE._base_manifest
_base_generated_client_source = _BASE.generated_client_source

_BASE.CONTRACT_REVISION = CONTRACT_REVISION
_BASE.PRODUCT_DEPENDENCY_REVISION = PRODUCT_DEPENDENCY_REVISION
_BASE.PRODUCT_DEPENDENCY_MANIFEST_SHA256 = PRODUCT_DEPENDENCY_MANIFEST_SHA256
_BASE.PUBLICATION_IMPLEMENTATION_FILES = PUBLICATION_IMPLEMENTATION_FILES


def _live_preview(preview: dict[str, Any]) -> None:
    expected = {
        "kind",
        "provider_calls",
        "live_forge_evidence",
        "live_capability_evidence",
        "loopback_only",
        "process_local",
        "capabilities_enabled",
    }
    if not expected.issubset(preview):
        raise RuntimeError("World Access preview identity changed unexpectedly")
    preview.update(
        {
            "kind": LIVE_KIND,
            "provider_calls": True,
            "live_forge_evidence": True,
            "live_attendee_execution": True,
            "live_capability_evidence": False,
            "loopback_only": True,
            "process_local": True,
            "capabilities_enabled": [],
        }
    )


def _replace_live_identity(value: Any) -> None:
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, dict):
            _live_preview(preview)
        for item in value.values():
            _replace_live_identity(item)
    elif isinstance(value, list):
        for item in value:
            _replace_live_identity(item)


def build_openapi() -> dict[str, Any]:
    """Return RC3's operations with only live-development identity changed."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Live World Access Development",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen Phase 8B live-development identity for one explicitly "
            "started, loopback-only composition. Authoritative live World "
            "Forge selections may be promoted and revealed for provider-backed "
            "attendee execution. All production capabilities remain disabled."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]
    properties = preview["properties"]
    properties["kind"]["const"] = LIVE_KIND
    properties["provider_calls"]["const"] = True
    properties["live_forge_evidence"]["const"] = True
    properties["live_attendee_execution"] = {"type": "boolean", "const": True}
    preview["required"].insert(
        preview["required"].index("live_capability_evidence"),
        "live_attendee_execution",
    )
    boundary = document["x-dreamland-boundary"]
    boundary["provider_calls"] = True
    boundary["live_forge_evidence"] = True
    boundary["live_attendee_execution"] = True
    document["x-dreamland-development-live"] = {
        "capability_evidence": False,
        "explicit_start_only": True,
        "live_attendee_execution": True,
        "live_forge_evidence": True,
        "loopback_only": True,
        "normal_runtime_composition": False,
        "provider_calls": True,
        "production_capabilities_enabled": [],
    }
    return document


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        _replace_live_identity(payload)
        fixtures.append(replace(fixture, payload=payload))
    return tuple(fixtures)


def build_manifest_schema() -> dict[str, Any]:
    schema = _base_build_manifest_schema()
    schema["properties"]["implementation_status"]["const"] = LIVE_IMPLEMENTATION_STATUS
    return schema


def _base_manifest() -> dict[str, Any]:
    manifest = _base_manifest_document()
    root = f"contracts/world-access-api/v{CONTRACT_REVISION}"
    tool = ".venv/bin/python tools/product_world_access_live_contract.py"
    harness = Path(__file__).resolve().parent / (
        "world_access_live_client_smoke/run-smoke.mjs"
    )
    manifest["implementation_status"] = LIVE_IMPLEMENTATION_STATUS
    manifest["generation"].update(
        {
            "refresh_command": f"{tool} refresh {root}",
            "validate_command": f"{tool} validate {root}",
            "client_smoke_command": f"{tool} client-smoke {root}",
            "publish_command": (
                f"{tool} publish {root} --backend-commit COMMIT "
                "--published-at TIMESTAMP"
            ),
        }
    )
    manifest["generation"]["client_toolchain"].update(
        {
            "harness": "tools/world_access_live_client_smoke/run-smoke.mjs",
            "harness_sha256": _BASE._sha256(harness.read_bytes()),
        }
    )
    manifest["source"] = {
        "kind": LIVE_KIND,
        "provider_calls": True,
        "live_forge_evidence": True,
        "live_attendee_execution": True,
        "live_capability_evidence": False,
        "normal_runtime_integration": False,
        "production_capabilities_enabled": [],
    }
    return manifest


def generated_client_source() -> str:
    source = _base_generated_client_source()
    replacements = {
        "Generated by tools/product_world_access_contract.py": (
            "Generated by tools/product_world_access_live_contract.py"
        ),
        'readonly kind: "provider_free_generated_access_preview";': (
            f'readonly kind: "{LIVE_KIND}";'
        ),
        "readonly provider_calls: false;": "readonly provider_calls: true;",
        "readonly live_forge_evidence: false;": (
            "readonly live_forge_evidence: true;\n"
            "  readonly live_attendee_execution: true;"
        ),
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError(f"generated client marker changed: {old}")
        source = source.replace(old, new, 1)
    return source


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
    if any(marker not in source for marker in markers) or any(
        marker not in source
        for marker in (
            "server-only",
            "Authorization",
            "X-Dreamland-Expected-Epoch",
            "X-Dreamland-Actor-Context",
            "Idempotency-Key",
            'Accept: "application/json, application/problem+json"',
            "response Content-Type must be",
            "requirePreviewLoopbackBaseUrl",
            f'readonly kind: "{LIVE_KIND}"',
            "readonly provider_calls: true",
            "readonly live_forge_evidence: true",
            "readonly live_attendee_execution: true",
        )
    ):
        raise WorldAccessContractError(
            "generated live World Access client boundary is incomplete"
        )
    node = str(node_executable) if node_executable is not None else shutil.which("node")
    if node is None:
        raise WorldAccessContractError(
            "Node.js is required for generated live World Access client compilation"
        )
    tool_root = Path(__file__).resolve().parent
    harness = tool_root / "world_access_live_client_smoke/run-smoke.mjs"
    typescript = (
        tool_root / "world_access_client_smoke/node_modules/typescript/package.json"
    )
    if not harness.is_file() or not typescript.is_file():
        raise WorldAccessContractError(
            "live World Access client toolchain is unavailable; run npm ci "
            "--prefix tools/world_access_client_smoke"
        )
    try:
        completed = subprocess.run(
            [
                node,
                str(harness),
                "--client",
                str(root / "generated/world_access_client.ts"),
                "--success-fixture",
                str(root / "fixtures/canonical-valid/identity.json"),
                "--selection-fixture",
                str(root / "fixtures/canonical-valid/generated-grant.json"),
                "--problem-fixture",
                str(root / "fixtures/safe-errors/idempotency_conflict.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorldAccessContractError(
            "generated live World Access client compilation could not complete"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        raise WorldAccessContractError(
            "generated live World Access client compilation failed"
            + (f": {detail}" if detail else "")
        )
    try:
        evidence = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorldAccessContractError(
            "generated live World Access client evidence is invalid"
        ) from error
    expected = {
        "compiled": True,
        "content_type_mismatch_rejected": True,
        "launch_binding_sha256_consumed": True,
        "launch_selection_fields_consumed": True,
        "live_identity_consumed": True,
        "missing_content_type_rejected": True,
        "operations": 6,
        "transport": "bounded_fake_fetch",
        "typescript": "6.0.3",
    }
    if not isinstance(evidence, dict):
        raise WorldAccessContractError(
            "generated live World Access client evidence is incomplete"
        )
    node_major = evidence.pop("node_major", None)
    if not isinstance(node_major, int) or node_major < 20 or evidence != expected:
        raise WorldAccessContractError(
            "generated live World Access client evidence is incomplete"
        )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "generated/world_access_client.ts", output)
    return {
        "contract_revision": validated["contract_revision"],
        **expected,
        "node_major": node_major,
        "smoke": True,
    }


_BASE.build_openapi = build_openapi
_BASE.fixture_definitions = fixture_definitions
_BASE.build_manifest_schema = build_manifest_schema
_BASE._base_manifest = _base_manifest
_BASE.generated_client_source = generated_client_source
_BASE.client_smoke = client_smoke

refresh_contract = _BASE.refresh_contract
validate_contract = _BASE.validate_contract
publish_contract = _BASE.publish_contract


def main(argv: list[str] | None = None) -> int:
    return _BASE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
