#!/usr/bin/env python3
"""Validate, publish, generate, and serve the private product contract kit.

This tool is development infrastructure. It deliberately imports no DREAMLAND
runtime module and is never composed by ``python -m dreamland``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import py_compile
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.1.0-rc.1"
CAPABILITY_NAMES = (
    "product_session_core",
    "presenter_stage_terminal",
    "public_attendee_sessions",
    "forge_to_stage_handoff",
    "stage_xray",
)
CAPABILITY_SET = frozenset(CAPABILITY_NAMES)
CAPABILITIES_PATH = "/product/v1/capabilities"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
SAFE_ERROR_DENYLIST = frozenset(
    {
        "capability",
        "credential",
        "detail",
        "environment",
        "exception",
        "path",
        "prompt",
        "provider_envelope",
        "raw",
        "reasoning",
        "secret",
        "stack",
        "token",
        "trace",
        "traceback",
    }
)
PHASE0_SAFE_ERROR_MESSAGES = {
    "capacity_exhausted": "Product admission is currently unavailable.",
    "not_found": "The requested private product operation is unavailable.",
    "unauthorized": "Missing or invalid private service authority.",
}
ARTIFACT_PATHS = {
    "openapi_sha256": "openapi.json",
    "events_schema_sha256": "events.schema.json",
    "manifest_schema_sha256": "manifest.schema.json",
}
SCENARIO_FIXTURES = {
    "all-disabled": "fixtures/canonical-valid/capabilities/all-disabled.json",
    "additive-fields": "fixtures/forward-compat/capabilities/additive-fields.json",
    "incompatible-revision": (
        "fixtures/forward-compat/capabilities/incompatible-revision.json"
    ),
    "missing-capability": (
        "fixtures/invalid/capabilities/missing-required-capability.json"
    ),
    "delayed": "fixtures/canonical-valid/capabilities/all-disabled.json",
    "dropped-response": "fixtures/canonical-valid/capabilities/all-disabled.json",
}


class ContractError(RuntimeError):
    """A deterministic contract validation or publication failure."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ContractError(f"non-standard JSON constant: {value}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"could not read JSON {path}: {exc}") from exc


def _canonical_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.write_text(_canonical_json_text(value), encoding="utf-8")


def _contract_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError(f"unsafe contract-relative path: {relative_path!r}")
    candidate = root / relative
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ContractError(f"contract path may not use symlinks: {relative_path}")
    resolved = candidate.resolve(strict=False)
    if root not in resolved.parents:
        raise ContractError(f"contract path escapes its root: {relative_path}")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_pointer(document: Any, pointer: str) -> Any:
    if not pointer or pointer == "#":
        return document
    if not pointer.startswith("#/"):
        raise ContractError(f"only local JSON pointers are supported: {pointer!r}")
    value = document
    for encoded_part in pointer[2:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
            continue
        if isinstance(value, list) and part.isdigit():
            try:
                value = value[int(part)]
                continue
            except IndexError as exc:
                raise ContractError(f"invalid JSON pointer: {pointer!r}") from exc
        raise ContractError(f"invalid JSON pointer: {pointer!r}")
    return value


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _dereference_local(value: Any, document: Any, stack: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [_dereference_local(item, document, stack) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ContractError(
                f"nonlocal or malformed schema reference: {reference!r}"
            )
        if reference in stack:
            chain = " -> ".join((*stack, reference))
            raise ContractError(f"cyclic local schema reference: {chain}")
        resolved = _dereference_local(
            _json_pointer(document, reference), document, (*stack, reference)
        )
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        if not siblings:
            return resolved
        if not isinstance(resolved, dict):
            raise ContractError(f"cannot merge schema reference siblings: {reference}")
        return {
            **resolved,
            **{
                key: _dereference_local(item, document, stack)
                for key, item in siblings.items()
            },
        }
    return {
        key: _dereference_local(item, document, stack) for key, item in value.items()
    }


def _schema_for_target(root: Path, target: str) -> dict[str, Any]:
    document_name, separator, pointer = target.partition("#")
    if not separator:
        raise ContractError(f"fixture schema target must include '#': {target!r}")
    document = load_json(_contract_path(root, document_name))
    if not isinstance(document, dict):
        raise ContractError(f"schema document must be an object: {document_name}")
    selected = _json_pointer(document, f"#{pointer}")
    schema = _dereference_local(selected, document)
    if not isinstance(schema, dict):
        raise ContractError(f"fixture schema must resolve to an object: {target}")
    return schema


def _validation_errors(schema: dict[str, Any], instance: Any) -> list[str]:
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        error.message
        for error in sorted(
            validator.iter_errors(instance), key=lambda item: list(item.path)
        )
    ]


def _validate_openapi(openapi: dict[str, Any], manifest: dict[str, Any]) -> None:
    if openapi.get("openapi") != "3.1.0":
        raise ContractError("openapi.json must declare OpenAPI 3.1.0")
    info = openapi.get("info")
    if not isinstance(info, dict) or info.get("version") != CONTRACT_REVISION:
        raise ContractError("OpenAPI info.version does not match the contract revision")
    if manifest.get("contract_revision") != info.get("version"):
        raise ContractError("OpenAPI and manifest contract revisions differ")
    if openapi.get("servers") != []:
        raise ContractError("Phase 0 OpenAPI must not advertise a live server")
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or set(paths) != {CAPABILITIES_PATH}:
        raise ContractError(
            "Phase 0 OpenAPI must expose only private capability discovery"
        )
    operation = paths.get(CAPABILITIES_PATH, {}).get("get")
    if not isinstance(operation, dict):
        raise ContractError("capability discovery GET operation is missing")
    if operation.get("operationId") != "getProductCapabilities":
        raise ContractError("capability discovery operationId is not canonical")
    if operation.get("x-dreamland-availability") != "contract-simulator-only":
        raise ContractError("Phase 0 operation must be labeled simulator-only")
    security = operation.get("security")
    if security != [{"ProductServiceBearer": []}]:
        raise ContractError("capability discovery must require the private bearer")
    components = openapi.get("components")
    if not isinstance(components, dict):
        raise ContractError("OpenAPI components are missing")
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict) or "ProductServiceBearer" not in schemes:
        raise ContractError("private product bearer scheme is missing")
    bearer = schemes["ProductServiceBearer"]
    if (
        not isinstance(bearer, dict)
        or bearer.get("type") != "http"
        or bearer.get("scheme") != "bearer"
    ):
        raise ContractError("private product authority must be an HTTP bearer")
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise ContractError("capability discovery responses are missing")
    success = responses.get("200")
    unauthorized = responses.get("401")
    if not isinstance(success, dict) or not isinstance(unauthorized, dict):
        raise ContractError("capability discovery must define 200 and 401 responses")
    success_headers = success.get("headers")
    if not isinstance(success_headers, dict):
        raise ContractError("capability discovery success headers are missing")
    cache_control = success_headers.get("Cache-Control")
    if not isinstance(cache_control, dict) or cache_control.get("schema") != {
        "const": "no-store",
        "type": "string",
    }:
        raise ContractError("capability discovery success must declare no-store")
    success_content = success.get("content")
    if not isinstance(success_content, dict):
        raise ContractError("capability discovery success content is missing")
    success_media = success_content.get("application/json")
    if not isinstance(success_media, dict):
        raise ContractError("capability discovery success media type is missing")
    success_schema = success_media.get("schema")
    if success_schema != {"$ref": "#/components/schemas/ProductCapabilitiesResponse"}:
        raise ContractError("capability discovery success schema is not canonical")
    unauthorized_content = unauthorized.get("content")
    if not isinstance(unauthorized_content, dict):
        raise ContractError("capability discovery 401 content is missing")
    unauthorized_media = unauthorized_content.get("application/problem+json")
    if not isinstance(unauthorized_media, dict):
        raise ContractError("capability discovery 401 media type is missing")
    unauthorized_schema = unauthorized_media.get("schema")
    if unauthorized_schema != {"$ref": "#/components/schemas/SafeErrorEnvelope"}:
        raise ContractError("capability discovery 401 must use the safe error envelope")
    unauthorized_headers = unauthorized.get("headers")
    if not isinstance(unauthorized_headers, dict):
        raise ContractError("capability discovery 401 security headers are missing")
    unauthorized_cache = unauthorized_headers.get("Cache-Control")
    if not isinstance(unauthorized_cache, dict) or unauthorized_cache.get("schema") != {
        "const": "no-store",
        "type": "string",
    }:
        raise ContractError("capability discovery 401 must declare no-store")
    unauthorized_challenge = unauthorized_headers.get("WWW-Authenticate")
    if not isinstance(unauthorized_challenge, dict) or unauthorized_challenge.get(
        "schema"
    ) != {
        "const": "Bearer",
        "type": "string",
    }:
        raise ContractError("capability discovery 401 must declare bearer challenge")
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        raise ContractError("OpenAPI schemas are missing")
    capability_response = schemas.get("ProductCapabilitiesResponse")
    if not isinstance(capability_response, dict):
        raise ContractError("product capability response schema is missing")
    capability_projection = capability_response.get("properties", {}).get(
        "capabilities"
    )
    if not isinstance(capability_projection, dict):
        raise ContractError("product capability projection schema is missing")
    capability_properties = capability_projection.get("properties")
    capability_required = capability_projection.get("required")
    if (
        not isinstance(capability_properties, dict)
        or not isinstance(capability_required, list)
        or set(capability_properties) != CAPABILITY_SET
        or set(capability_required) != CAPABILITY_SET
        or capability_projection.get("additionalProperties") is not False
    ):
        raise ContractError("OpenAPI must declare exactly the five capabilities")
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            raise ContractError(f"OpenAPI component schema {name!r} is not an object")
        Draft202012Validator.check_schema(_dereference_local(schema, openapi))
    for item in _walk(openapi):
        if not isinstance(item, dict) or "$ref" not in item:
            continue
        reference = item["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ContractError(f"OpenAPI reference must remain local: {reference!r}")
        _json_pointer(openapi, reference)


def _validate_safe_error(instance: Any) -> None:
    if not isinstance(instance, dict):
        return
    error = instance.get("error")
    if not isinstance(error, dict):
        return
    unsafe = SAFE_ERROR_DENYLIST.intersection(error)
    if unsafe:
        values = ", ".join(sorted(unsafe))
        raise ContractError(f"safe error contains denied fields: {values}")
    code = error.get("code")
    message = error.get("message")
    if (
        not isinstance(code, str)
        or code not in PHASE0_SAFE_ERROR_MESSAGES
        or message != PHASE0_SAFE_ERROR_MESSAGES[code]
    ):
        raise ContractError("safe error code/message is not in the Phase 0 allowlist")


def _phase0_capabilities(instance: Any) -> dict[str, Any] | None:
    if not isinstance(instance, dict):
        return None
    capabilities = instance.get("capabilities")
    return capabilities if isinstance(capabilities, dict) else None


def _requires_consumer_resync(instance: Any) -> bool:
    if not isinstance(instance, dict):
        return False
    if instance.get("criticality") == "authoritative":
        return True
    contract = instance.get("contract")
    if isinstance(contract, dict) and contract.get("revision") != CONTRACT_REVISION:
        return True
    capabilities = _phase0_capabilities(instance)
    if capabilities is not None:
        return any(
            not isinstance(value, dict)
            or value.get("declared_revision") != CONTRACT_REVISION
            for value in capabilities.values()
        )
    return False


def _validate_fixture_expectation(
    *,
    fixture_path: Path,
    fixture: Any,
    schema: dict[str, Any],
    expected: str,
) -> None:
    errors = _validation_errors(schema, fixture)
    if expected in {"valid", "consumer_resync", "reject_phase0_policy"} and errors:
        joined = "; ".join(errors)
        raise ContractError(
            f"fixture {fixture_path} should satisfy its schema: {joined}"
        )
    if expected == "valid":
        if _requires_consumer_resync(fixture):
            raise ContractError(
                f"fixture {fixture_path} requires resync but is classified valid"
            )
        return
    if expected == "consumer_resync":
        if not _requires_consumer_resync(fixture):
            raise ContractError(
                f"fixture {fixture_path} does not exercise a resync condition"
            )
        return
    if expected == "reject":
        if not errors:
            raise ContractError(
                f"fixture {fixture_path} should be rejected by its schema"
            )
        return
    if expected == "reject_phase0_policy":
        capabilities = _phase0_capabilities(fixture)
        if not capabilities or not any(
            isinstance(value, dict) and value.get("enabled") is True
            for value in capabilities.values()
        ):
            raise ContractError(
                f"fixture {fixture_path} does not violate Phase 0 enablement policy"
            )
        return
    raise ContractError(f"unknown fixture expectation {expected!r}")


def validate_contract(root: Path) -> dict[str, Any]:
    root = root.resolve()
    required_files = (
        "openapi.json",
        "events.schema.json",
        "manifest.schema.json",
        "manifest.json",
        "fixtures/index.json",
    )
    required_paths = {name: _contract_path(root, name) for name in required_files}
    missing = [name for name, path in required_paths.items() if not path.is_file()]
    if missing:
        raise ContractError(f"contract is missing required files: {', '.join(missing)}")

    openapi = load_json(required_paths["openapi.json"])
    events_schema = load_json(required_paths["events.schema.json"])
    manifest_schema = load_json(required_paths["manifest.schema.json"])
    manifest = load_json(required_paths["manifest.json"])
    fixture_index = load_json(required_paths["fixtures/index.json"])
    if not all(
        isinstance(value, dict)
        for value in (
            openapi,
            events_schema,
            manifest_schema,
            manifest,
            fixture_index,
        )
    ):
        raise ContractError("contract documents must be JSON objects")

    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator.check_schema(events_schema)
    manifest_errors = _validation_errors(manifest_schema, manifest)
    if manifest_errors:
        raise ContractError("manifest is invalid: " + "; ".join(manifest_errors))
    _validate_openapi(openapi, manifest)

    if manifest.get("contract_name") != CONTRACT_NAME:
        raise ContractError("manifest contract name is not canonical")
    if manifest.get("contract_revision") != CONTRACT_REVISION:
        raise ContractError("manifest contract revision is not canonical")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_SET:
        raise ContractError("manifest must contain exactly the five capabilities")
    if any(value.get("enabled") for value in capabilities.values()):
        raise ContractError("Phase 0 cannot enable a product capability")
    if any(
        value.get("enabled_revision") is not None for value in capabilities.values()
    ):
        raise ContractError(
            "disabled Phase 0 capabilities cannot have enabled revisions"
        )
    status = manifest.get("publication_status")
    commit = manifest.get("backend_commit")
    published_at = manifest.get("published_at")
    if status == "release_candidate" and (
        commit is not None or published_at is not None
    ):
        raise ContractError("release-candidate metadata must remain unpublished")
    if status == "published" and (
        not isinstance(commit, str)
        or not COMMIT_PATTERN.fullmatch(commit)
        or not isinstance(published_at, str)
    ):
        raise ContractError("published metadata requires a commit and timestamp")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ContractError("manifest artifact digests are missing")
    for digest_name, relative_path in ARTIFACT_PATHS.items():
        expected = artifacts.get(digest_name)
        actual = sha256_file(_contract_path(root, relative_path))
        if expected != actual:
            raise ContractError(
                f"artifact digest mismatch for {relative_path}: {expected!r} != {actual}"
            )

    fixture_set = manifest.get("fixture_set")
    if not isinstance(fixture_set, dict):
        raise ContractError("manifest fixture-set metadata is missing")
    index_digest = sha256_file(required_paths["fixtures/index.json"])
    if fixture_set.get("index_sha256") != index_digest:
        raise ContractError("fixture index digest does not match the manifest")

    fixtures = fixture_index.get("fixtures")
    if fixture_index.get("schema_version") != 1 or not isinstance(fixtures, list):
        raise ContractError("fixture index has an unsupported shape")
    indexed_paths: set[str] = set()
    for entry in fixtures:
        if not isinstance(entry, dict):
            raise ContractError("fixture index entries must be objects")
        relative_path = entry.get("path")
        fixture_class = entry.get("class")
        expected = entry.get("expected")
        schema_target = entry.get("schema")
        digest = entry.get("sha256")
        if not all(
            isinstance(value, str)
            for value in (relative_path, fixture_class, expected, schema_target, digest)
        ):
            raise ContractError("fixture index entry contains a non-string field")
        assert isinstance(relative_path, str)
        assert isinstance(fixture_class, str)
        assert isinstance(expected, str)
        assert isinstance(schema_target, str)
        assert isinstance(digest, str)
        if relative_path in indexed_paths:
            raise ContractError(f"fixture appears twice in index: {relative_path}")
        indexed_paths.add(relative_path)
        path = _contract_path(root, relative_path)
        expected_prefix = f"fixtures/{fixture_class}/"
        if not relative_path.startswith(expected_prefix):
            raise ContractError(
                f"fixture class/path mismatch: {fixture_class!r}, {relative_path!r}"
            )
        if not path.is_file():
            raise ContractError(f"indexed fixture does not exist: {relative_path}")
        if not SHA256_PATTERN.fullmatch(digest) or sha256_file(path) != digest:
            raise ContractError(f"fixture digest mismatch: {relative_path}")
        fixture = load_json(path)
        schema = _schema_for_target(root, schema_target)
        _validate_fixture_expectation(
            fixture_path=path,
            fixture=fixture,
            schema=schema,
            expected=expected,
        )
        if "/errors/" in relative_path and expected != "reject":
            _validate_safe_error(fixture)

    discovered_paths: set[str] = set()
    for path in (root / "fixtures").rglob("*.json"):
        if path.name == "index.json":
            continue
        relative_path = path.relative_to(root).as_posix()
        _contract_path(root, relative_path)
        discovered_paths.add(relative_path)
    if indexed_paths != discovered_paths:
        missing_from_index = sorted(discovered_paths - indexed_paths)
        missing_from_disk = sorted(indexed_paths - discovered_paths)
        raise ContractError(
            "fixture index mismatch; unindexed="
            f"{missing_from_index}, missing={missing_from_disk}"
        )

    canonical = load_json(
        _contract_path(
            root,
            "fixtures/canonical-valid/capabilities/all-disabled.json",
        )
    )
    canonical_capabilities = _phase0_capabilities(canonical)
    if (
        canonical_capabilities is None
        or set(canonical_capabilities) != CAPABILITY_SET
        or any(value.get("enabled") for value in canonical_capabilities.values())
    ):
        raise ContractError(
            "canonical capability fixture must contain five disabled entries"
        )

    for path in sorted(root.rglob("*.json")):
        _contract_path(root, path.relative_to(root).as_posix())
        value = load_json(path)
        if path.read_text(encoding="utf-8") != _canonical_json_text(value):
            raise ContractError(
                f"JSON is not in deterministic canonical formatting: {path}"
            )

    return {
        "contract_revision": CONTRACT_REVISION,
        "fixtures": len(fixtures),
        "capabilities": len(capabilities),
        "publication_status": status,
    }


def refresh_contract(root: Path) -> dict[str, Any]:
    root = root.resolve()
    for path in sorted(root.rglob("*.json")):
        _contract_path(root, path.relative_to(root).as_posix())
        if path.name in {"index.json", "manifest.json"}:
            continue
        write_json(path, load_json(path))
    index_path = _contract_path(root, "fixtures/index.json")
    fixture_index = load_json(index_path)
    if not isinstance(fixture_index, dict) or not isinstance(
        fixture_index.get("fixtures"), list
    ):
        raise ContractError("fixture index has an unsupported shape")
    for entry in fixture_index["fixtures"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ContractError("fixture index entry has an unsupported shape")
        entry["sha256"] = sha256_file(_contract_path(root, entry["path"]))
    write_json(index_path, fixture_index)

    manifest_path = _contract_path(root, "manifest.json")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ContractError("manifest must be an object")
    artifacts = manifest.get("artifacts")
    fixture_set = manifest.get("fixture_set")
    if not isinstance(artifacts, dict) or not isinstance(fixture_set, dict):
        raise ContractError("manifest digest sections are missing")
    for digest_name, relative_path in ARTIFACT_PATHS.items():
        artifacts[digest_name] = sha256_file(root / relative_path)
    fixture_set["index_sha256"] = sha256_file(index_path)
    manifest["publication_status"] = "release_candidate"
    manifest["backend_commit"] = None
    manifest["published_at"] = None
    write_json(manifest_path, manifest)
    return validate_contract(root)


def _parse_published_at(value: str) -> str:
    if not RFC3339_PATTERN.fullmatch(value):
        raise ContractError("published-at must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError("published-at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("published-at must include an explicit timezone")
    return value


def _verify_publication_commit(root: Path, backend_commit: str) -> None:
    root = root.resolve()
    repository_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if repository_result.returncode != 0:
        raise ContractError("contract publication requires a Git repository")
    repository = Path(repository_result.stdout.decode("utf-8").strip()).resolve()
    if repository != root and repository not in root.parents:
        raise ContractError("contract root is not contained by its Git repository")
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
    for path in sorted(root.rglob("*.json")):
        relative_contract_path = path.relative_to(root).as_posix()
        _contract_path(root, relative_contract_path)
        if relative_contract_path == "manifest.json":
            continue
        repository_path = path.relative_to(repository).as_posix()
        blob_result = subprocess.run(
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
        if blob_result.returncode != 0 or blob_result.stdout != path.read_bytes():
            raise ContractError(
                "backend-commit does not contain the current contract artifacts: "
                f"{relative_contract_path}"
            )


def publish_contract(
    root: Path, *, backend_commit: str, published_at: str
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(backend_commit):
        raise ContractError(
            "backend-commit must be exactly 40 lowercase hex characters"
        )
    published_at = _parse_published_at(published_at)
    validate_contract(root)
    _verify_publication_commit(root, backend_commit)
    manifest_path = _contract_path(root, "manifest.json")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ContractError("manifest must be an object")
    manifest["publication_status"] = "published"
    manifest["backend_commit"] = backend_commit
    manifest["published_at"] = published_at
    write_json(manifest_path, manifest)
    return validate_contract(root)


def _client_source(openapi: dict[str, Any]) -> str:
    paths = openapi["paths"]
    operation = paths[CAPABILITIES_PATH]["get"]
    operation_id = operation["operationId"]
    capabilities_schema = openapi["components"]["schemas"][
        "ProductCapabilitiesResponse"
    ]["properties"]["capabilities"]
    names = tuple(capabilities_schema["required"])
    if (
        set(names) != CAPABILITY_SET
        or set(capabilities_schema["properties"]) != CAPABILITY_SET
        or capabilities_schema.get("additionalProperties") is not False
    ):
        raise ContractError("cannot generate client from unexpected capability names")
    return f"""# Generated by tools/product_contract.py; do not edit.
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OPERATION_ID = {operation_id!r}
CONTRACT_NAME = {CONTRACT_NAME!r}
CONTRACT_REVISION = {CONTRACT_REVISION!r}
CONTRACT_COMPATIBILITY = "exact-release-candidate"
CAPABILITY_NAMES = {names!r}
CAPABILITIES_PATH = {CAPABILITIES_PATH!r}
EPOCH_PATTERN = re.compile(r"^epoch:[A-Za-z0-9_-]{{16,}}$")


class _NoRedirectHandler(HTTPRedirectHandler):
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


def _validated_base_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {{"http", "https"}} or not parsed.netloc:
        raise ValueError("product base URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("product base URL must not contain credentials")
    if "?" in value or "#" in value or parsed.query or parsed.fragment:
        raise ValueError("product base URL must not contain a query or fragment")
    if parsed.path not in {{"", "/"}}:
        raise ValueError("product base URL must not contain a path")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and hostname not in {{"127.0.0.1", "::1", "localhost"}}:
        raise ValueError("plain HTTP product base URL must be loopback")
    return value


def validate_product_capabilities(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("product capability response must be an object")
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("missing product contract identity")
    expected_identity = (CONTRACT_NAME, CONTRACT_REVISION, CONTRACT_COMPATIBILITY)
    actual_identity = (
        contract.get("name"),
        contract.get("revision"),
        contract.get("compatibility"),
    )
    if actual_identity != expected_identity:
        raise ValueError("incompatible product contract identity")
    service_epoch = payload.get("service_epoch")
    if not isinstance(service_epoch, str) or not EPOCH_PATTERN.fullmatch(service_epoch):
        raise ValueError("missing or malformed product service epoch")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("missing product request id")
    admission = payload.get("admission")
    if (
        not isinstance(admission, dict)
        or admission.get("state") != "closed"
        or not isinstance(admission.get("reason"), str)
        or not admission["reason"]
    ):
        raise ValueError("Phase 0 product admission must remain closed")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("missing product capabilities")
    if set(capabilities) != set(CAPABILITY_NAMES):
        raise ValueError("unknown or missing product capability")
    for name in CAPABILITY_NAMES:
        state = capabilities[name]
        if not isinstance(state, dict):
            raise ValueError(f"invalid capability state: {{name}}")
        if state.get("declared_revision") != CONTRACT_REVISION:
            raise ValueError(f"incompatible capability revision: {{name}}")
        if state.get("enabled") is not False:
            raise ValueError(f"Phase 0 capability must remain disabled: {{name}}")
        if state.get("enabled_revision") is not None:
            raise ValueError(f"disabled capability has an enabled revision: {{name}}")
        reason = state.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError(f"disabled capability has no reason: {{name}}")
    return payload


class ProductContractClient:
    def __init__(self, base_url: str, bearer: str) -> None:
        if not bearer or "\\r" in bearer or "\\n" in bearer:
            raise ValueError("product bearer must be nonempty and single-line")
        self.base_url = _validated_base_url(base_url)
        self.bearer = bearer
        self._proxy_handler = ProxyHandler({{}})
        self._opener = build_opener(self._proxy_handler, _NoRedirectHandler())

    def get_product_capabilities(self) -> dict[str, Any]:
        request = Request(
            self.base_url + CAPABILITIES_PATH,
            headers={{
                "Accept": "application/json",
                "Authorization": "Bearer " + self.bearer,
            }},
            method="GET",
        )
        with self._opener.open(request, timeout=5) as response:
            if response.headers.get_content_type() != "application/json":
                raise ValueError("unexpected product capability content type")
            if "no-store" not in response.headers.get("Cache-Control", "").lower():
                raise ValueError("product capability response must be non-cacheable")
            payload = json.load(response)
        return validate_product_capabilities(payload)
"""


def client_smoke(root: Path, *, output: Path | None = None) -> dict[str, Any]:
    validate_contract(root)
    openapi = load_json(root.resolve() / "openapi.json")
    if not isinstance(openapi, dict):
        raise ContractError("OpenAPI document must be an object")
    source = _client_source(openapi)
    if output is not None:
        destination = output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        py_compile.compile(str(destination), doraise=True)
        generated_path = str(destination)
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "product_contract_client.py"
            destination.write_text(source, encoding="utf-8")
            py_compile.compile(str(destination), doraise=True)
        generated_path = "temporary"
    return {
        "operation_id": "getProductCapabilities",
        "capabilities": len(CAPABILITY_NAMES),
        "generated_path": generated_path,
    }


def _safe_error(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retry": "none",
            "retry_after_ms": None,
            "request_id": request_id,
        }
    }


def _loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ip_address(host.strip()).is_loopback
    except ValueError:
        return False


class _SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        scenario: str,
        response: bytes,
        unauthorized: bytes,
        delay_ms: int,
    ) -> None:
        self.contract_token = token
        self.contract_scenario = scenario
        self.contract_response = response
        self.contract_unauthorized = unauthorized
        self.contract_delay_ms = delay_ms
        self._synchronous_request = False
        super().__init__(address, _SimulatorHandler)

    def handle_request_synchronously(self) -> None:
        """Accept and finish one request before returning to the caller."""

        self._synchronous_request = True
        try:
            self.handle_request()
        finally:
            self._synchronous_request = False

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        if self._synchronous_request:
            HTTPServer.process_request(self, request, client_address)
            return
        super().process_request(request, client_address)


class _SimulatorHandler(BaseHTTPRequestHandler):
    server: _SimulatorServer
    server_version = "DREAMLANDContractSimulator/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self) -> None:
        body = json.dumps(
            _safe_error(
                "not_found",
                "The requested private product operation is unavailable.",
                "request:contract-simulator-not-found",
            ),
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_json(
            HTTPStatus.NOT_FOUND,
            body,
            content_type="application/problem+json",
        )

    def do_GET(self) -> None:
        if urlsplit(self.path).path != CAPABILITIES_PATH:
            self._send_not_found()
            return
        authorization = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.contract_token
        if not hmac.compare_digest(authorization, expected):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                self.server.contract_unauthorized,
                content_type="application/problem+json",
            )
            return
        if self.server.contract_scenario == "dropped-response":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if self.server.contract_scenario == "delayed":
            time.sleep(self.server.contract_delay_ms / 1000)
        self._send_json(HTTPStatus.OK, self.server.contract_response)

    def do_POST(self) -> None:
        self._send_not_found()

    def do_PUT(self) -> None:
        self._send_not_found()

    def do_PATCH(self) -> None:
        self._send_not_found()

    def do_DELETE(self) -> None:
        self._send_not_found()


def build_simulator_server(
    root: Path,
    *,
    host: str,
    port: int,
    token: str,
    scenario: str,
    delay_ms: int,
) -> _SimulatorServer:
    validate_contract(root)
    if not _loopback_host(host):
        raise ContractError("the contract simulator may bind only to loopback")
    if not token:
        raise ContractError("the contract simulator requires a nonempty bearer token")
    if scenario not in SCENARIO_FIXTURES:
        choices = ", ".join(sorted(SCENARIO_FIXTURES))
        raise ContractError(f"unknown simulator scenario; choose one of {choices}")
    if port < 0 or port > 65535:
        raise ContractError("simulator port must be between 0 and 65535")
    if delay_ms < 0 or delay_ms > 60000:
        raise ContractError("simulator delay must be between 0 and 60000 ms")
    response_path = root.resolve() / SCENARIO_FIXTURES[scenario]
    unauthorized_path = (
        root.resolve() / "fixtures/canonical-valid/errors/unauthorized.json"
    )
    response = json.dumps(
        load_json(response_path), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    unauthorized = json.dumps(
        load_json(unauthorized_path), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _SimulatorServer(
        (host, port),
        token=token,
        scenario=scenario,
        response=response,
        unauthorized=unauthorized,
        delay_ms=delay_ms,
    )


def serve_contract(
    root: Path,
    *,
    host: str,
    port: int,
    token: str,
    scenario: str,
    delay_ms: int,
    once: bool,
) -> None:
    server = build_simulator_server(
        root,
        host=host,
        port=port,
        token=token,
        scenario=scenario,
        delay_ms=delay_ms,
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "contract_revision": CONTRACT_REVISION,
                "host": address,
                "port": bound_port,
                "scenario": scenario,
                "test_only": True,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        if once:
            server.handle_request_synchronously()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def _json_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage DREAMLAND private product contract artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "refresh", "client-smoke"):
        command = subparsers.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "client-smoke":
            command.add_argument("--output", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("root", type=Path)
    publish.add_argument("--backend-commit", required=True)
    publish.add_argument("--published-at", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("root", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token", required=True)
    serve.add_argument(
        "--scenario", choices=sorted(SCENARIO_FIXTURES), default="all-disabled"
    )
    serve.add_argument("--delay-ms", type=int, default=1500)
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            _json_result(validate_contract(arguments.root))
        elif arguments.command == "refresh":
            _json_result(refresh_contract(arguments.root))
        elif arguments.command == "publish":
            _json_result(
                publish_contract(
                    arguments.root,
                    backend_commit=arguments.backend_commit,
                    published_at=arguments.published_at,
                )
            )
        elif arguments.command == "client-smoke":
            _json_result(client_smoke(arguments.root, output=arguments.output))
        elif arguments.command == "serve":
            serve_contract(
                arguments.root,
                host=arguments.host,
                port=arguments.port,
                token=arguments.token,
                scenario=arguments.scenario,
                delay_ms=arguments.delay_ms,
                once=arguments.once,
            )
        else:
            raise ContractError(f"unsupported command: {arguments.command}")
    except ContractError as exc:
        print(f"product contract error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
