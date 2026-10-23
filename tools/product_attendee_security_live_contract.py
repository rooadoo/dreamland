#!/usr/bin/env python3
"""Build the Phase 9B attendee-dedicated live security contract kit.

This identity-only successor preserves every Product RC1 route, payload,
revocation rule, and restart/recovery boundary.  It describes only the
explicitly started, loopback-only attendee provider pool; all production
capabilities remain disabled.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_security_contract() -> ModuleType:
    path = Path(__file__).with_name("product_attendee_security_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_live_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published attendee security contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_SECURITY: Any = _load_security_contract()
_CORE: Any = _SECURITY._CORE
_MODULES = tuple(dict.fromkeys((_SECURITY, *_SECURITY._MODULES)))

CONTRACT_NAME = _SECURITY.CONTRACT_NAME
CONTRACT_REVISION = "0.8.0-rc.2"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _SECURITY.CONTRACT_COMPATIBILITY
LIVE_SECURITY_EXECUTOR = "private-attendee-live-provider-pool-v1"
PUBLICATION_IMPLEMENTATION_FILES = frozenset(
    {
        "src/dreamland/agent_core.py",
        "src/dreamland/model_providers.py",
        "src/dreamland/product/attendee.py",
        "src/dreamland/product/attendee_live.py",
        "src/dreamland/product/attendee_provider.py",
        "src/dreamland/product/attendee_security.py",
        "src/dreamland/product/authored_artifacts.py",
        "src/dreamland/product/launch_selection_runtime.py",
        "src/dreamland/product/live_agent.py",
        "src/dreamland/product/runtime_executor.py",
        "tools/product_attendee_security_contract.py",
        "tools/product_attendee_security_live.py",
        "tools/product_attendee_security_live_contract.py",
        "tools/product_attendee_security_preview.py",
    }
)

EXPECTED_OPERATIONS = _SECURITY.EXPECTED_OPERATIONS
MUTATION_OPERATIONS = _SECURITY.MUTATION_OPERATIONS
SESSION_OPERATIONS = _SECURITY.SESSION_OPERATIONS
SAFE_ERROR_CASES = _SECURITY.SAFE_ERROR_CASES
ATTENDEE_SAFE_ERROR_CASES = _SECURITY.ATTENDEE_SAFE_ERROR_CASES
CLIENT_SMOKE_VALIDATED_FIXTURES = _SECURITY.CLIENT_SMOKE_VALIDATED_FIXTURES
CLIENT_SMOKE_REJECTED_MUTATIONS = _SECURITY.CLIENT_SMOKE_REJECTED_MUTATIONS

for module in _MODULES:
    module.CONTRACT_REVISION = CONTRACT_REVISION
    module.CONTRACT_DIRECTORY = CONTRACT_DIRECTORY
    module.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    if hasattr(module, "PREVIEW_EXECUTOR"):
        module.PREVIEW_EXECUTOR = LIVE_SECURITY_EXECUTOR
    if hasattr(module, "LIVE_EXECUTOR"):
        module.LIVE_EXECUTOR = LIVE_SECURITY_EXECUTOR
    if hasattr(module, "SECURITY_EXECUTOR"):
        module.SECURITY_EXECUTOR = LIVE_SECURITY_EXECUTOR

_base_build_openapi = _SECURITY.build_openapi
_base_build_manifest_schema = _SECURITY.build_manifest_schema
_base_fixture_definitions = _SECURITY.fixture_definitions
_base_manifest_document = _SECURITY._base_manifest_document
_base_generated_client_source = _SECURITY.generated_client_source
_base_refresh_contract = _SECURITY.refresh_contract
_base_client_smoke = _SECURITY.client_smoke
_base_publish_contract = _SECURITY._base_publish_contract


def build_openapi() -> dict[str, Any]:
    """Return RC1 semantics with only the live attendee identity changed."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Attendee Live Security Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Unfrozen Phase 9B identity for one explicitly started, "
            "loopback-only, attendee-dedicated provider pool behind the "
            "Phase 9A private security boundary. Every RC1 operation, "
            "revocation outcome, and explicit restart boundary is preserved. "
            "All production product capabilities remain disabled."
        ),
    }
    preview = document["components"]["schemas"]["PreviewIdentity"]["properties"]
    preview["executor"]["const"] = LIVE_SECURITY_EXECUTOR
    preview["provider_calls"]["const"] = True

    live = document["x-dreamland-development-live"]
    live.update(
        {
            "explicit_start_only": True,
            "loopback_only": True,
            "provider_calls": True,
            "usage_accounting": "full_reservation_conservative",
            "capability_evidence": False,
            "normal_runtime_composition": False,
        }
    )
    attendee = document["x-dreamland-attendee-integration-preview"]
    attendee["provider_dispatch"] = "accepted_attendee_commands_only"
    attendee["normal_runtime_composition"] = False
    attendee["capability_evidence"] = False
    security = document["x-dreamland-private-attendee-security"]
    security["availability"] = "attendee_dedicated_provider_loopback_development_only"
    document["x-dreamland-attendee-provider-boundary"] = {
        "worker_process_start_method": "spawn",
        "fixed_capacity": True,
        "readiness_before_listen": True,
        "fresh_dispatch_rejected_while_draining": True,
        "deadline_containment": "local_worker_process",
        "ambiguous_usage": "full_reservation",
        "automatic_redispatch": False,
        "sdk_retries": 0,
        "credential_source": "dedicated_owner_only_child_read_file",
        "ordinary_runtime_credentials_reused": False,
        "stage_or_forge_authority_composed": False,
        "local_kill_proves_upstream_termination": False,
        "deployment_supervisor_required": True,
        "restart_recovery": "explicit_client_context_restart_only",
        "production_capability_evidence": False,
    }
    return document


def fixture_definitions() -> tuple[Any, ...]:
    fixtures = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        _replace_live_context(payload)
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


def _replace_live_context(value: Any) -> None:
    if isinstance(value, dict):
        preview = value.get("preview")
        if isinstance(preview, dict) and {
            "test_only",
            "integration_only",
            "executor",
            "provider_calls",
            "live_capability_evidence",
        }.issubset(preview):
            preview["executor"] = LIVE_SECURITY_EXECUTOR
            preview["provider_calls"] = True
        for item in value.values():
            _replace_live_context(item)
    elif isinstance(value, list):
        for item in value:
            _replace_live_context(item)


def _manifest_document() -> dict[str, Any]:
    manifest = _base_manifest_document()
    prefix = (
        ".venv/bin/python tools/product_attendee_security_live_contract.py"
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
                    "tools/product_attendee_security_contract.py",
                    "tools/product_attendee_security_live_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_attendee_security_contract.py",
                    "tools/product_attendee_security_live_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return (
        _base_generated_client_source(openapi)
        .replace(
            "Generated by tools/product_attendee_security_contract.py",
            "Generated by tools/product_attendee_security_live_contract.py",
            1,
        )
        .replace('"provider_calls": False', '"provider_calls": True')
    )


def refresh_contract(root: Path | str) -> dict[str, Any]:
    root_path = Path(root).resolve()
    manifest_path = root_path / "manifest.json"
    if manifest_path.is_file():
        manifest = _CORE.load_json(manifest_path)
        if isinstance(manifest, dict) and manifest.get("publication_status") == (
            "published"
        ):
            raise _CORE.ContractError("published live security artifacts are immutable")
    return _base_refresh_contract(root_path)


def client_smoke(root: Path | str, *, output: Path | None = None) -> dict[str, Any]:
    result = _base_client_smoke(root, output=output)
    result["validated_client_fixtures"] = CLIENT_SMOKE_VALIDATED_FIXTURES
    result["rejected_client_mutations"] = CLIENT_SMOKE_REJECTED_MUTATIONS
    return result


def _verify_publication_implementation(root: Path, backend_commit: str) -> None:
    repository_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository_result.returncode != 0:
        raise _CORE.ContractError(
            "live security contract publication requires a Git repository"
        )
    repository = Path(repository_result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise _CORE.ContractError(
            "live security contract root is outside its Git repository"
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
            ["git", "-C", str(repository), "show", f"{backend_commit}:{relative}"],
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
                "backend-commit does not contain the exact Phase 9B "
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
for module in _SECURITY._LIVE._MODULES:
    module._base_manifest = _manifest_document
_CORE.refresh_contract = refresh_contract
_CORE.client_smoke = client_smoke
_CORE.publish_contract = publish_contract

build_events_schema = _CORE.build_events_schema
validate_contract = _CORE.validate_contract


def main(argv: list[str] | None = None) -> int:
    return _CORE.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
