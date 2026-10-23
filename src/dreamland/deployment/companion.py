"""Private role storage and the metadata-only companion deployment contract.

The descriptor grants no Product capability. Its transport credential belongs
to exactly one metadata service and never enters a descriptor or report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any

from dreamland.deployment import storage

VERSION = "0.1.0-rc.1"
CONTRACT_NAME = "dreamland.deployment-control"
ROLES = frozenset({"attendee", "operator"})
PATHS = {
    "identity": "/deployment/v1/identity",
    "health": "/deployment/v1/health",
    "readiness": "/deployment/v1/readiness",
}
CAPABILITIES = {
    "attendee_sessions": False,
    "presenter": False,
    "world_forge": False,
    "world_publication": False,
}
_DESCRIPTOR = "descriptor.json"
_TOKEN = "transport.token"
_LOCK = ".initialize.lock"
_FILES = {_DESCRIPTOR, _TOKEN, _LOCK}
_MAX_DESCRIPTOR_BYTES = 16 * 1024
_MAX_TOKEN_BYTES = 256
_PROJECT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
_NETWORK = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_CREDENTIAL = re.compile(
    r"credential:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_EPOCH = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SAFE_ERROR = "companion deployment storage or configuration is invalid"


class CompanionError(ValueError):
    """The role deployment cannot be initialized or used safely."""


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _json_object(pairs: list[tuple[str, Any]]) -> dict:
    document: dict = {}
    for key, value in pairs:
        if key in document:
            raise CompanionError(_SAFE_ERROR)
        document[key] = value
    return document


def _decode(content: bytes) -> object:
    try:
        return json.loads(content, object_pairs_hook=_json_object)
    except (ValueError, UnicodeError, RecursionError):
        raise CompanionError(_SAFE_ERROR) from None


def _encoded(document: dict) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def contract_artifacts() -> dict[str, bytes]:
    """Return installed, non-secret artifacts for independent companion tooling."""
    try:
        assets = files("dreamland.deployment").joinpath("assets")
        return {
            name: assets.joinpath("companion-" + name).read_bytes()
            for name in ("descriptor.schema.json", "openapi.json", "manifest.json")
        }
    except (OSError, ValueError):
        raise CompanionError("companion contract artifacts are unavailable") from None


def _contract() -> dict:
    artifacts = contract_artifacts()
    return {
        "name": CONTRACT_NAME,
        "version": VERSION,
        "openapi_sha256": hashlib.sha256(artifacts["openapi.json"]).hexdigest(),
    }


def _intent(
    *, role: str, project: str, network: str, image: str, revision: str
) -> dict:
    if (
        not isinstance(role, str)
        or role not in ROLES
        or not _matches(_PROJECT, project)
        or not _matches(_NETWORK, network)
        or not _matches(_IMAGE, image)
        or not _matches(_REVISION, revision)
    ):
        raise CompanionError(_SAFE_ERROR)
    alias = f"{project}-{role}-api"
    return {
        "kind": "dreamland.backend-handoff",
        "schema_version": VERSION,
        "project": project,
        "role": role,
        "image": image,
        "revision": revision,
        "transport": {
            "kind": "docker-network",
            "network": network,
            "alias": alias,
            "port": 8080,
            "base_url": f"http://{alias}:8080",
        },
        "contract": _contract(),
        "paths": dict(PATHS),
        "capabilities": dict(CAPABILITIES),
        "product_contracts": [],
    }


def validate_descriptor(document: object) -> dict:
    """Validate exact contract, disabled authority and correlated connection data."""
    try:
        if not isinstance(document, dict):
            raise CompanionError(_SAFE_ERROR)
        transport = document["transport"]
        if not isinstance(transport, dict):
            raise CompanionError(_SAFE_ERROR)
        expected = _intent(
            role=document["role"],
            project=document["project"],
            network=transport["network"],
            image=document["image"],
            revision=document["revision"],
        )
        credential_id = document["credential_id"]
        if not _matches(_CREDENTIAL, credential_id):
            raise CompanionError(_SAFE_ERROR)
        expected["credential_id"] = credential_id
        # Strict boolean and integer types reject JSON's false == 0 and 8080.0.
        if (
            document != expected
            or type(transport.get("port")) is not int
            or not isinstance(document["capabilities"], dict)
            or any(value is not False for value in document["capabilities"].values())
            or type(document["product_contracts"]) is not list
        ):
            raise CompanionError(_SAFE_ERROR)
        return expected
    except (KeyError, TypeError, ValueError, RecursionError):
        raise CompanionError(_SAFE_ERROR) from None


def validate_response(operation: str, document: object) -> dict:
    """Validate one metadata response without implying Product readiness."""
    try:
        if operation == "error":
            if not isinstance(document, dict) or set(document) != {"error"}:
                raise CompanionError(_SAFE_ERROR)
            error = document["error"]
            if (
                not isinstance(error, dict)
                or set(error) != {"code", "message"}
                or not _matches(re.compile(r"[a-z][a-z0-9_]{0,63}"), error["code"])
                or not isinstance(error["message"], str)
                or not 1 <= len(error["message"]) <= 160
            ):
                raise CompanionError(_SAFE_ERROR)
            return {"error": {"code": error["code"], "message": error["message"]}}
        if not isinstance(document, dict) or not _matches(
            _EPOCH, document["service_epoch"]
        ):
            raise CompanionError(_SAFE_ERROR)
        epoch = document["service_epoch"]
        if operation == "identity":
            expected = {
                "handoff": validate_descriptor(document["handoff"]),
                "service_epoch": epoch,
            }
        else:
            role = document["role"]
            if not isinstance(role, str) or role not in ROLES:
                raise CompanionError(_SAFE_ERROR)
            if operation == "health":
                expected = {"status": "healthy", "role": role, "service_epoch": epoch}
            elif operation == "readiness":
                expected = {
                    "transport_ready": True,
                    "admission_ready": False,
                    "reason": "domain_services_unavailable",
                    "role": role,
                    "service_epoch": epoch,
                    "capabilities": dict(CAPABILITIES),
                }
                if (
                    document["transport_ready"] is not True
                    or document["admission_ready"] is not False
                    or not isinstance(document["capabilities"], dict)
                    or any(
                        value is not False
                        for value in document["capabilities"].values()
                    )
                ):
                    raise CompanionError(_SAFE_ERROR)
            else:
                raise CompanionError(_SAFE_ERROR)
        if document != expected:
            raise CompanionError(_SAFE_ERROR)
        return expected
    except (KeyError, TypeError, ValueError, RecursionError):
        raise CompanionError(_SAFE_ERROR) from None


def validate_contract(root: Path) -> dict:
    """Validate the exact installed metadata kit and its pinned fixture cases."""
    try:
        artifacts = contract_artifacts()
        for name, expected in artifacts.items():
            if (root / name).read_bytes() != expected:
                raise CompanionError(_SAFE_ERROR)
        manifest = _decode(artifacts["manifest.json"])
        if not isinstance(manifest, dict):
            raise CompanionError(_SAFE_ERROR)
        index_bytes = (root / "fixtures" / "index.json").read_bytes()
        if hashlib.sha256(index_bytes).hexdigest() != manifest["fixture_index_sha256"]:
            raise CompanionError(_SAFE_ERROR)
        index = _decode(index_bytes)
        if not isinstance(index, dict) or set(index) != {"fixtures"}:
            raise CompanionError(_SAFE_ERROR)
        for fixture in index["fixtures"]:
            name = fixture["path"]
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[a-z-]+\.json", name) is None
            ):
                raise CompanionError(_SAFE_ERROR)
            content = (root / "fixtures" / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != fixture["sha256"]:
                raise CompanionError(_SAFE_ERROR)
            document = _decode(content)
            valid = True
            try:
                target = fixture["schema"]
                if target == "Handoff":
                    validate_descriptor(document)
                elif target in {"Identity", "Health", "Readiness"}:
                    validate_response(target.lower(), document)
                elif target == "Error":
                    validate_response("error", document)
                else:
                    raise CompanionError(_SAFE_ERROR)
            except CompanionError:
                valid = False
            if valid is not fixture["valid"]:
                raise CompanionError(_SAFE_ERROR)
        return {"validation": "passed", "fixtures": len(index["fixtures"])}
    except (OSError, KeyError, ValueError, TypeError, RecursionError):
        raise CompanionError("companion contract validation failed") from None


def _check_directory(root_fd: int) -> set[str]:
    if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
        raise CompanionError(_SAFE_ERROR)
    names = set(os.listdir(root_fd))
    if names - _FILES:
        raise CompanionError(_SAFE_ERROR)
    return names


def _check_file(root_fd: int, name: str) -> None:
    # Open without reading any bytes; describe() may inspect credential metadata.
    file_fd = os.open(name, os.O_RDONLY | storage._FILE_FLAGS, dir_fd=root_fd)
    try:
        storage._validate_file(file_fd)
        if stat.S_IMODE(os.fstat(file_fd).st_mode) != 0o600:
            raise CompanionError(_SAFE_ERROR)
    finally:
        os.close(file_fd)


def _read_descriptor(root_fd: int) -> dict:
    if _check_directory(root_fd) != _FILES:
        raise CompanionError(_SAFE_ERROR)
    for name in _FILES:
        _check_file(root_fd, name)
    content = storage._read_file(root_fd, _DESCRIPTOR, maximum=_MAX_DESCRIPTOR_BYTES)
    if content is None:
        raise CompanionError(_SAFE_ERROR)
    return validate_descriptor(_decode(content))


def _read_token(root_fd: int, role: str) -> str:
    content = storage._read_file(root_fd, _TOKEN, maximum=_MAX_TOKEN_BYTES)
    if content is None:
        raise CompanionError(_SAFE_ERROR)
    pattern = rb"dreamland-" + role.encode("ascii") + rb"-transport:[A-Za-z0-9_-]{64}\n"
    if re.fullmatch(pattern, content) is None:
        raise CompanionError(_SAFE_ERROR)
    return content.decode("ascii").removesuffix("\n")


def initialize(
    root: Path, *, role: str, project: str, network: str, image: str, revision: str
) -> dict:
    """Initialize one isolated role once; changed or partial intent is rejected."""
    intended = _intent(
        role=role, project=project, network=network, image=image, revision=revision
    )
    try:
        with storage._root_directory(root, create=True) as root_fd:
            _check_directory(root_fd)
            with storage._lock(root_fd):
                names = _check_directory(root_fd)
                if names == _FILES:
                    existing = _read_descriptor(root_fd)
                    _read_token(root_fd, role)
                    intended["credential_id"] = existing["credential_id"]
                    if existing != intended:
                        raise CompanionError(_SAFE_ERROR)
                    return {"initialized": False, "handoff": existing}
                if names != {_LOCK}:
                    raise CompanionError(_SAFE_ERROR)
                _check_file(root_fd, _LOCK)
                intended["credential_id"] = "credential:" + str(uuid.uuid4())
                token = f"dreamland-{role}-transport:{secrets.token_urlsafe(48)}\n"
                # Descriptor is the final publication. Interrupted initialization
                # remains a visible partial state; no retry silently rotates it.
                if not storage._publish(root_fd, _TOKEN, token.encode("ascii")):
                    raise CompanionError(_SAFE_ERROR)
                if not storage._publish(root_fd, _DESCRIPTOR, _encoded(intended)):
                    raise CompanionError(_SAFE_ERROR)
                return {"initialized": True, "handoff": intended}
    except (OSError, ValueError, TypeError):
        raise CompanionError(_SAFE_ERROR) from None


def describe(root: Path) -> dict:
    """Read validated non-secret handoff data; never read transport-token bytes."""
    try:
        with storage._root_directory(root, create=False) as root_fd:
            return _read_descriptor(root_fd)
    except (OSError, ValueError, TypeError):
        raise CompanionError(_SAFE_ERROR) from None


def load_service(root: Path, *, role: str) -> tuple[dict, str]:
    """Load exactly the selected metadata role's descriptor and private bearer."""
    try:
        with storage._root_directory(root, create=False) as root_fd:
            descriptor = _read_descriptor(root_fd)
            if not isinstance(role, str) or descriptor["role"] != role:
                raise CompanionError(_SAFE_ERROR)
            return descriptor, _read_token(root_fd, role)
    except (OSError, ValueError, TypeError):
        raise CompanionError(_SAFE_ERROR) from None


def export_token(root: Path, destination: Path) -> None:
    """Deliver one credential to a new owner-only file in an existing private dir."""
    try:
        destination = Path(destination)
        if ".." in destination.parts or destination.name in {"", ".", ".."}:
            raise CompanionError(_SAFE_ERROR)
        with storage._root_directory(root, create=False) as root_fd:
            descriptor = _read_descriptor(root_fd)
            token = _read_token(root_fd, descriptor["role"])
            with storage._root_directory(destination.parent, create=False) as target_fd:
                if stat.S_IMODE(os.fstat(target_fd).st_mode) != 0o700:
                    raise CompanionError(_SAFE_ERROR)
                source = os.fstat(root_fd)
                target = os.fstat(target_fd)
                if (source.st_dev, source.st_ino) == (target.st_dev, target.st_ino):
                    raise CompanionError(_SAFE_ERROR)
                if not storage._publish(
                    target_fd, destination.name, (token + "\n").encode("ascii")
                ):
                    raise CompanionError(_SAFE_ERROR)
    except (OSError, ValueError, TypeError):
        raise CompanionError(_SAFE_ERROR) from None
