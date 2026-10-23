#!/usr/bin/env python3
"""Build the corrected Phase 9B live-attendee privacy identity contract.

This RC3 wrapper preserves the published RC2 route and recovery contract while
binding the live-provider launch option to the provider-aware notice and
policy already used by the authored attendee live-development experience.
Published RC2 artifacts remain immutable.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_live_security_contract() -> ModuleType:
    path = Path(__file__).with_name("product_attendee_security_live_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_live_identity_base_contract",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("published live attendee security contract is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_LIVE: Any = _load_live_security_contract()
_CORE: Any = _LIVE._CORE
_MODULES = tuple(dict.fromkeys((_LIVE, *_LIVE._MODULES)))

CONTRACT_NAME = _LIVE.CONTRACT_NAME
CONTRACT_REVISION = "0.8.0-rc.3"
CONTRACT_DIRECTORY = f"v{CONTRACT_REVISION}"
CONTRACT_COMPATIBILITY = _LIVE.CONTRACT_COMPATIBILITY
LIVE_SECURITY_EXECUTOR = _LIVE.LIVE_SECURITY_EXECUTOR
ATTENDEE_NOTICE_VERSION = "notice:attendee-live-development-v1"
ATTENDEE_POLICY_ID = "policy:attendee-live-authored-v1"
PUBLICATION_IMPLEMENTATION_FILES = frozenset(
    {
        *_LIVE.PUBLICATION_IMPLEMENTATION_FILES,
        "tools/product_attendee_security_live_identity_contract.py",
    }
)

EXPECTED_OPERATIONS = _LIVE.EXPECTED_OPERATIONS
MUTATION_OPERATIONS = _LIVE.MUTATION_OPERATIONS
SESSION_OPERATIONS = _LIVE.SESSION_OPERATIONS
SAFE_ERROR_CASES = _LIVE.SAFE_ERROR_CASES
ATTENDEE_SAFE_ERROR_CASES = _LIVE.ATTENDEE_SAFE_ERROR_CASES
CLIENT_SMOKE_VALIDATED_FIXTURES = _LIVE.CLIENT_SMOKE_VALIDATED_FIXTURES
CLIENT_SMOKE_REJECTED_MUTATIONS = _LIVE.CLIENT_SMOKE_REJECTED_MUTATIONS

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

_base_build_openapi = _LIVE.build_openapi
_base_build_manifest_schema = _LIVE.build_manifest_schema
_base_fixture_definitions = _LIVE.fixture_definitions
_base_manifest_document = _LIVE._manifest_document
_base_generated_client_source = _LIVE.generated_client_source
_base_refresh_contract = _LIVE.refresh_contract
_base_client_smoke = _LIVE.client_smoke
_base_publish_contract = _LIVE._base_publish_contract


def build_openapi() -> dict[str, Any]:
    """Return RC2 semantics with the corrected live privacy identity."""
    document = _base_build_openapi()
    document["info"] = {
        "title": "DREAMLAND Private Attendee Live Security Preview",
        "version": CONTRACT_REVISION,
        "description": (
            "Corrective Phase 9B identity for the explicitly started, "
            "loopback-only attendee provider boundary. It preserves every "
            "RC2 operation, revocation result, and explicit restart boundary "
            "while binding launch selection to the provider-aware authored "
            "attendee notice and policy. All production product capabilities "
            "remain disabled."
        ),
    }
    boundary = document["x-dreamland-attendee-provider-boundary"]
    boundary["privacy_identity"] = {
        "notice_version": ATTENDEE_NOTICE_VERSION,
        "policy_id": ATTENDEE_POLICY_ID,
    }
    return document


def fixture_definitions() -> tuple[Any, ...]:
    """Replace notice identity only in attendee option/selection fixtures."""
    fixtures = []
    for fixture in _base_fixture_definitions():
        payload = deepcopy(fixture.payload)
        if _is_attendee_fixture(fixture.path):
            _replace_attendee_notice(payload)
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


def _is_attendee_fixture(path: str) -> bool:
    return path.startswith(
        (
            "fixtures/canonical-valid/attendee/",
            "fixtures/forward-compat/attendee/",
            "fixtures/invalid/attendee/",
        )
    )


def _replace_attendee_notice(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if item == "notice:attendee-preview-v1":
                value[key] = ATTENDEE_NOTICE_VERSION
            else:
                _replace_attendee_notice(item)
    elif isinstance(value, list):
        for item in value:
            _replace_attendee_notice(item)


def _manifest_document() -> dict[str, Any]:
    manifest = _base_manifest_document()
    prefix = (
        ".venv/bin/python "
        "tools/product_attendee_security_live_identity_contract.py"
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
                    "tools/product_attendee_security_live_contract.py",
                    "tools/product_attendee_security_live_identity_contract.py",
                )
            else:
                _replace_generator_path(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(
                    "tools/product_attendee_security_live_contract.py",
                    "tools/product_attendee_security_live_identity_contract.py",
                )
            else:
                _replace_generator_path(item)


def generated_client_source(openapi: dict[str, Any]) -> str:
    return _base_generated_client_source(openapi).replace(
        "Generated by tools/product_attendee_security_live_contract.py",
        "Generated by tools/product_attendee_security_live_identity_contract.py",
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
            raise _CORE.ContractError("published live identity artifacts are immutable")
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
            "live identity contract publication requires a Git repository"
        )
    repository = Path(repository_result.stdout.strip()).resolve()
    try:
        root.relative_to(repository)
    except ValueError as error:
        raise _CORE.ContractError(
            "live identity contract root is outside its Git repository"
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
                "backend-commit does not contain the exact corrective Phase 9B "
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
for module in _LIVE._SECURITY._LIVE._MODULES:
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
