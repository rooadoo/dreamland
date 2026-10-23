#!/usr/bin/env python3
"""Build the Phase 9C supervised attendee deployment contract identity.

This identity-only RC4 wrapper preserves the complete published RC3 Product
wire contract and live-provider privacy identity.  It adds only truthful
metadata for the explicitly invoked, loopback-only operational supervisor and
its finite availability, recovery, signal, and rotation boundaries.  Published
RC1 through RC3 artifacts remain immutable and every production capability
remains disabled.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_identity_contract() -> ModuleType:
    path = Path(__file__).with_name(
        "product_attendee_security_live_identity_contract.py"
    )
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_deployment_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published attendee live identity contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_LIVE_IDENTITY: Any = _load_live_identity_contract()
_CORE: Any = _LIVE_IDENTITY._CORE
_MODULES = tuple(dict.fromkeys((_LIVE_IDENTITY, *_LIVE_IDENTITY._MODULES)))

CONTRACT_NAME = _LIVE_IDENTITY.CONTRACT_NAME
CONTRACT_REVISION = "0.8.0-rc.4"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _LIVE_IDENTITY.CONTRACT_COMPATIBILITY
LIVE_SECURITY_EXECUTOR = _LIVE_IDENTITY.LIVE_SECURITY_EXECUTOR
ATTENDEE_NOTICE_VERSION = _LIVE_IDENTITY.ATTENDEE_NOTICE_VERSION
ATTENDEE_POLICY_ID = _LIVE_IDENTITY.ATTENDEE_POLICY_ID
DEPLOYMENT_TRANSPORT_STATUS = "explicitly_invoked_loopback_deployment_preview_only"
DEPLOYMENT_BOUNDARY = {
    "invocation": "explicit_only",
    "listener_exposure": "loopback_only",
    "availability_window": {
        "configuration": "exact_utc_open_admission_close_and_recovery_close",
        "deadline_projection": "startup_utc_anchor_plus_monotonic_elapsed",
        "admission_cutoff_trigger": "time_driven_without_request",
        "fresh_request_admission_at_or_after_cutoff": False,
    },
    "cutoff_recovery": {
        "interval": "bounded",
        "listener": "loopback_recovery_only",
        "fresh_work_admission": False,
    },
    "supervised_convergence": {
        "signals": ["SIGINT", "SIGTERM"],
        "drain_before_shutdown": True,
        "bounded": True,
        "request_threads_joined": True,
    },
    "rotation": {
        "credentials": "process_restart_only",
        "service_epoch": "process_restart_only",
        "hot_reload": False,
        "automatic_authority_remint": False,
    },
    "automatic_redispatch": False,
    "deliberate_access_end_owner": "ui_bff",
    "dreamland_access_ended_wire_error": False,
    "production_capability_evidence": False,
}
PUBLICATION_IMPLEMENTATION_FILES = frozenset(
    {
        *_LIVE_IDENTITY.PUBLICATION_IMPLEMENTATION_FILES,
        "tools/product_attendee_contract.py",
        "tools/product_attendee_live_contract.py",
        "tools/product_attendee_security_deployment.py",
        "tools/product_attendee_security_deployment_contract.py",
        "tools/product_forge_stage_contract.py",
        "tools/product_launch_selection_contract.py",
        "tools/product_launch_selection_live_contract.py",
        "tools/product_session_contract.py",
        "tools/product_stage_contract.py",
        "tools/product_stage_live_contract.py",
        "tools/product_xray_contract.py",
    }
)

EXPECTED_OPERATIONS = _LIVE_IDENTITY.EXPECTED_OPERATIONS
MUTATION_OPERATIONS = _LIVE_IDENTITY.MUTATION_OPERATIONS
SESSION_OPERATIONS = _LIVE_IDENTITY.SESSION_OPERATIONS
SAFE_ERROR_CASES = _LIVE_IDENTITY.SAFE_ERROR_CASES
ATTENDEE_SAFE_ERROR_CASES = _LIVE_IDENTITY.ATTENDEE_SAFE_ERROR_CASES
CLIENT_SMOKE_VALIDATED_FIXTURES = _LIVE_IDENTITY.CLIENT_SMOKE_VALIDATED_FIXTURES
CLIENT_SMOKE_REJECTED_MUTATIONS = _LIVE_IDENTITY.CLIENT_SMOKE_REJECTED_MUTATIONS

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

_base_build_openapi = _LIVE_IDENTITY.build_openapi
_base_build_manifest_schema = _LIVE_IDENTITY.build_manifest_schema
_base_fixture_definitions = _LIVE_IDENTITY.fixture_definitions
_base_manifest_document = _LIVE_IDENTITY._manifest_document
_base_generated_client_source = _LIVE_IDENTITY.generated_client_source
_base_refresh_contract = _LIVE_IDENTITY.refresh_contract
_base_client_smoke = _LIVE_IDENTITY.client_smoke
_base_publish_contract = _LIVE_IDENTITY._base_publish_contract


def build_openapi() -> dict[str, Any]:
    """Return RC3 wire truth with only the Phase 9C identity metadata added."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Attendee Supervised Deployment Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Identity-only Phase 9C successor for the explicitly invoked, "
            "loopback-only attendee deployment preview. It preserves every "
            "RC3 operation, privacy identity, revocation result, and explicit "
            "restart boundary while declaring finite availability, recovery, "
            "signal-convergence, and restart-only rotation behavior. All "
            "production product capabilities remain disabled."
        ),
    }
    document["x-dreamland-attendee-deployment-boundary"] = deepcopy(DEPLOYMENT_BOUNDARY)
    return document


def fixture_definitions() -> tuple[Any, ...]:
    """Return the exact RC3 fixture cases under the RC4 identity."""
    return _base_fixture_definitions()


def _manifest_document() -> dict[str, Any]:
    manifest = _base_manifest_document()
    manifest["transport_status"] = DEPLOYMENT_TRANSPORT_STATUS
    prefix = (
        ".venv/bin/python "
        "tools/product_attendee_security_deployment_contract.py"
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
    _replace_identity_metadata(schema)
    schema["title"] = "DREAMLAND Attendee Supervised Deployment Manifest"
    return schema


def _replace_identity_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(
                    "tools/product_attendee_security_live_identity_contract.py",
                    "tools/product_attendee_security_deployment_contract.py",
                ).replace(
                    "manually_invoked_loopback_preview_only",
                    DEPLOYMENT_TRANSPORT_STATUS,
                )
            else:
                _replace_identity_metadata(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_attendee_security_live_identity_contract.py",
                    "tools/product_attendee_security_deployment_contract.py",
                ).replace(
                    "manually_invoked_loopback_preview_only",
                    DEPLOYMENT_TRANSPORT_STATUS,
                )
            else:
                _replace_identity_metadata(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return _base_generated_client_source(openapi).replace(
        "Generated by tools/product_attendee_security_live_identity_contract.py",
        "Generated by tools/product_attendee_security_deployment_contract.py",
        1,
    )


def refresh_contract(root: Path | str) -> dict[str, Any]:
    root_path = Path(root).resolve()
    manifest_path = root_path / "manifest.json"
    if manifest_path.is_file():
        manifest = _CORE.load_json(manifest_path)
        if isinstance(manifest, dict) and manifest.get("publication_status") == (
            "published"
        ):
            raise _CORE.ContractError("published deployment artifacts are immutable")
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
            "deployment contract publication requires a Git repository"
        )
    repository = Path(repository_result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise _CORE.ContractError(
            "deployment contract root is outside its Git repository"
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
                "backend-commit does not contain the exact Phase 9C "
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
for module in _LIVE_IDENTITY._LIVE._SECURITY._LIVE._MODULES:
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
