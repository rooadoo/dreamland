"""Deterministic compilation and immutable artifacts for live World Forge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from dreamland.login_banner import validate_profile_login_banners
from dreamland.product.forge import WorldSeed
from dreamland.state_profiles import (
    StateProfile,
    StateProfileLevel,
    state_profile_to_session,
)

FORGE_COMPILER_REVISION = "1"
FORGE_VALIDATOR_REVISION = "2"
FORGE_REVISION_SCHEMA = "1"
MAX_PROFILE_BYTES = 262_144
MAX_PROFILE_HOSTS = 24
MAX_PROFILE_NODES = 4_096
_BLANK_RESIDUE = (
    "blank-world",
    "operator-defined",
    "temporary detached-profile scaffold",
    "temporary placeholder host metadata",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_ID_PREFIX = "world-revision:sha256:"
_DIGEST_PREFIX = "sha256:"
_FORGE_PROFILE_NAME_PREFIX = "world-forge-"
_FORGE_PROFILE_CREATED_AT = "1970-01-01T00:00:00+00:00"


def live_forge_source(
    compiler_revision: str = FORGE_COMPILER_REVISION,
) -> dict[str, object]:
    return {
        "kind": "single_attempt_live_vertical_slice",
        "label": "Single-attempt live World Forge vertical slice",
        "provider_calls": True,
        "live_forge_evidence": True,
        "compiler_revision": compiler_revision,
    }


def live_forge_receipt(
    *,
    compiler_revision: str,
    validator_revision: str,
    revision_id: str,
    digest: str,
) -> dict[str, object]:
    return {
        "validation": {
            "state": "passed",
            "safe_summary": "The single live candidate passed the configured independent validators and synthetic boot.",
            "validator_revision": validator_revision,
        },
        "fixed_selection_reason": "Attempt A was the only candidate in this live vertical-slice checkpoint.",
        "revision": {
            "revision_id": revision_id,
            "revision_label": "Audience world candidate",
            "immutable_digest": digest,
        },
        "human_selection": "none",
        "provenance": live_forge_source(compiler_revision),
    }


@dataclass(frozen=True, slots=True)
class ForgeInstruction:
    compiler_revision: str
    seed_hash: str
    text: str

    def to_document(self) -> dict[str, str]:
        return {
            "compiler_revision": self.compiler_revision,
            "seed_hash": self.seed_hash,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class ForgeProfileIssue:
    code: str
    safe_message: str

    def to_document(self) -> dict[str, str]:
        return {"code": self.code, "safe_message": self.safe_message}


@dataclass(frozen=True, slots=True)
class ForgeValidationReport:
    validator_revision: str
    valid: bool
    issues: tuple[ForgeProfileIssue, ...]
    canonical_bytes: int
    host_count: int
    node_count: int
    synthetic_boot_passed: bool

    def to_document(self) -> dict[str, object]:
        return {
            "validator_revision": self.validator_revision,
            "valid": self.valid,
            "issues": [issue.to_document() for issue in self.issues],
            "canonical_bytes": self.canonical_bytes,
            "host_count": self.host_count,
            "node_count": self.node_count,
            "synthetic_boot_passed": self.synthetic_boot_passed,
        }


@dataclass(frozen=True, slots=True)
class ImmutableForgeRevision:
    revision_id: str
    digest: str
    path: Path
    document: dict[str, object]
    profile: StateProfile = field(repr=False)


class ForgeProfileGenerator(Protocol):
    def generate(self, instruction: ForgeInstruction) -> StateProfile:
        pass


@dataclass(frozen=True, slots=True)
class ForgeVerticalSliceResult:
    instruction: ForgeInstruction
    validation: ForgeValidationReport
    revision: ImmutableForgeRevision
    receipt: dict[str, object]


class SingleAttemptForge:
    """Run one explicit candidate attempt; redundancy belongs to a later phase."""

    def __init__(
        self, generator: ForgeProfileGenerator, store: ImmutableForgeStore
    ) -> None:
        self.generator = generator
        self.store = store

    def run(self, seed: WorldSeed) -> ForgeVerticalSliceResult:
        instruction = compile_world_seed(seed)
        profile = self.generator.generate(instruction)
        validation = validate_forge_profile(profile)
        if not validation.valid:
            raise ValueError("World Forge candidate validation failed")
        revision = self.store.save(profile)
        receipt = live_forge_receipt(
            compiler_revision=instruction.compiler_revision,
            validator_revision=validation.validator_revision,
            revision_id=revision.revision_id,
            digest=revision.digest,
        )
        return ForgeVerticalSliceResult(instruction, validation, revision, receipt)


def compile_world_seed(seed: WorldSeed) -> ForgeInstruction:
    """Compile one confirmed seed into byte-stable Spiritguide direction."""
    seed_document = seed.to_document()
    seed_json = json.dumps(
        seed_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    text = (
        "DREAMLAND World Forge instruction v1\n"
        "Construct one coherent reusable world-level DREAMLAND state profile.\n"
        "Replace every temporary scaffold value with scenario-specific content.\n"
        "Preserve the confirmed seed exactly as authoritative direction; do not "
        "reinterpret, omit, or rewrite its values. Prefer physical systems when "
        "the seed does not require virtualization. Materialize only a bounded "
        "initial environment and leave further depth for just-in-time generation.\n"
        f"Confirmed seed canonical JSON:\n{seed_json}"
    )
    return ForgeInstruction(FORGE_COMPILER_REVISION, seed.seed_hash, text)


def canonical_revision_document(profile: StateProfile) -> dict[str, object]:
    """Exclude mutable profile/session labels from the selected revision hash."""
    return {
        "kind": "dreamland.world_forge_revision",
        "schema_revision": FORGE_REVISION_SCHEMA,
        "profile": {"level": profile.level, "state": deepcopy(profile.state)},
    }


def validate_forge_profile(profile: StateProfile) -> ForgeValidationReport:
    issues: list[ForgeProfileIssue] = []
    try:
        validate_profile_login_banners(profile.state)
    except ValueError:
        issues.append(
            _issue(
                "login_banner_invalid",
                "The candidate initial host login banner is missing or invalid.",
            )
        )
    document = canonical_revision_document(profile)
    encoded = _canonical_bytes(document)
    world = profile.state.get("world_state")
    hosts: dict[object, object] = {}
    if profile.level != "world":
        issues.append(
            _issue("profile_level_invalid", "The candidate is not a world profile.")
        )
    if not isinstance(world, dict):
        issues.append(
            _issue("world_state_missing", "The candidate has no world state.")
        )
    else:
        value = world.get("hosts_by_name")
        if isinstance(value, dict):
            hosts = value
        else:
            issues.append(
                _issue(
                    "host_registry_invalid", "The candidate host registry is invalid."
                )
            )
        references: dict[str, dict[object, object] | None] = {}
        for field, code in (
            ("entry_host", "entry_host_invalid"),
            ("active_host", "active_host_invalid"),
        ):
            reference = world.get(field)
            host = hosts.get(reference) if isinstance(reference, str) else None
            references[field] = host if isinstance(host, dict) else None
            if not isinstance(reference, str) or not isinstance(host, dict):
                issues.append(
                    _issue(code, f"The candidate {field.replace('_', ' ')} is invalid.")
                )
        entry = references["entry_host"]
        if entry is not None:
            shell = entry.get("shell")
            default = shell.get("default") if isinstance(shell, dict) else None
            name = default.get("name") if isinstance(default, dict) else None
            command = default.get("command") if isinstance(default, dict) else None
            argv = default.get("argv") if isinstance(default, dict) else None
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(command, str)
                or not command.startswith("/")
                or not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) and item for item in argv)
            ):
                issues.append(
                    _issue(
                        "starting_shell_invalid",
                        "The candidate starting shell is invalid.",
                    )
                )
        else:
            issues.append(
                _issue(
                    "starting_shell_invalid",
                    "The candidate starting shell is invalid.",
                )
            )
    node_count = _node_count(document)
    if len(encoded) > MAX_PROFILE_BYTES:
        issues.append(
            _issue(
                "profile_too_large",
                "The candidate exceeds the initial profile size limit.",
            )
        )
    if len(hosts) > MAX_PROFILE_HOSTS:
        issues.append(
            _issue(
                "host_limit_exceeded", "The candidate contains too many initial hosts."
            )
        )
    if node_count > MAX_PROFILE_NODES:
        issues.append(
            _issue(
                "materialization_limit_exceeded",
                "The candidate is too heavily materialized.",
            )
        )
    if _contains_blank_residue(document):
        issues.append(
            _issue(
                "blank_residue_present",
                "The candidate retains temporary scaffold content.",
            )
        )
    synthetic_boot_passed = False
    if not issues:
        try:
            session = state_profile_to_session(
                profile,
                current_session_id="forge-synthetic-boot",
                authenticated_username="forge-validator",
            )
            synthetic_boot_passed = bool(session.shell_stack and session.world_state)
        except (KeyError, TypeError, ValueError):
            synthetic_boot_passed = False
        if not synthetic_boot_passed:
            issues.append(
                _issue(
                    "synthetic_boot_failed",
                    "The candidate failed its synthetic session boot.",
                )
            )
    return ForgeValidationReport(
        FORGE_VALIDATOR_REVISION,
        not issues,
        tuple(issues),
        len(encoded),
        len(hosts),
        node_count,
        synthetic_boot_passed,
    )


class ImmutableForgeStore:
    """Create-only writes and verified reads for selected Forge revisions."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def save(self, profile: StateProfile) -> ImmutableForgeRevision:
        report = validate_forge_profile(profile)
        if not report.valid:
            raise ValueError("only a validated World Forge profile may be stored")
        document = canonical_revision_document(profile)
        data = _canonical_bytes(document)
        digest = hashlib.sha256(data).hexdigest()
        revision_id = f"world-revision:sha256:{digest}"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        path = self.directory / f"sha256-{digest}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            try:
                existing = self.load(revision_id, f"sha256:{digest}")
            except ValueError as error:
                raise ValueError(
                    "immutable World Forge revision digest collision"
                ) from error
            if existing.document != document:
                raise ValueError(
                    "immutable World Forge revision digest collision"
                ) from None
            return existing
        else:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        stored_profile = _profile_from_revision_document(document, digest)
        return ImmutableForgeRevision(
            revision_id,
            f"sha256:{digest}",
            path,
            document,
            stored_profile,
        )

    def load(self, revision_id: str, digest: str) -> ImmutableForgeRevision:
        """Reopen one exact immutable revision without consulting profile stores."""
        digest_hex = _require_matching_revision_identity(revision_id, digest)
        path = self.directory / f"sha256-{digest_hex}.json"
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        try:
            directory_descriptor = os.open(self.directory, directory_flags)
        except OSError as error:
            raise ValueError("immutable World Forge revision is unavailable") from error
        try:
            directory_metadata = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            ):
                raise ValueError("immutable World Forge revision store is unsafe")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            try:
                descriptor = os.open(
                    path.name,
                    flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as error:
                raise ValueError(
                    "immutable World Forge revision is unavailable"
                ) from error
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise ValueError("immutable World Forge revision is unsafe")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    data = stream.read(MAX_PROFILE_BYTES + 1)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_descriptor)
        if len(data) > MAX_PROFILE_BYTES:
            raise ValueError("immutable World Forge revision exceeds the size limit")
        if hashlib.sha256(data).hexdigest() != digest_hex:
            raise ValueError("immutable World Forge revision digest does not match")
        document = _strict_revision_json(data)
        try:
            canonical = _canonical_bytes(document)
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError(
                "immutable World Forge revision is not canonical"
            ) from error
        if canonical != data:
            raise ValueError("immutable World Forge revision is not canonical")
        profile = _profile_from_revision_document(document, digest_hex)
        try:
            validation = validate_forge_profile(profile)
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError(
                "immutable World Forge revision failed validation"
            ) from error
        if not validation.valid:
            raise ValueError("immutable World Forge revision failed validation")
        if canonical_revision_document(profile) != document:
            raise ValueError("immutable World Forge revision schema is invalid")
        return ImmutableForgeRevision(revision_id, digest, path, document, profile)


def _canonical_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _require_matching_revision_identity(revision_id: str, digest: str) -> str:
    if not isinstance(revision_id, str) or not isinstance(digest, str):
        raise ValueError("immutable World Forge revision identity is invalid")
    if not revision_id.startswith(_REVISION_ID_PREFIX) or not digest.startswith(
        _DIGEST_PREFIX
    ):
        raise ValueError("immutable World Forge revision identity is invalid")
    revision_digest = revision_id.removeprefix(_REVISION_ID_PREFIX)
    digest_hex = digest.removeprefix(_DIGEST_PREFIX)
    if (
        _SHA256_PATTERN.fullmatch(revision_digest) is None
        or _SHA256_PATTERN.fullmatch(digest_hex) is None
        or revision_digest != digest_hex
    ):
        raise ValueError("immutable World Forge revision identity is invalid")
    return digest_hex


def _profile_from_revision_document(
    document: Mapping[str, object],
    digest_hex: str,
) -> StateProfile:
    if set(document) != {"kind", "schema_revision", "profile"}:
        raise ValueError("immutable World Forge revision schema is invalid")
    if document.get("kind") != "dreamland.world_forge_revision":
        raise ValueError("immutable World Forge revision kind is invalid")
    if document.get("schema_revision") != FORGE_REVISION_SCHEMA:
        raise ValueError("immutable World Forge revision schema is unsupported")
    profile_document = document.get("profile")
    if not isinstance(profile_document, dict) or set(profile_document) != {
        "level",
        "state",
    }:
        raise ValueError("immutable World Forge profile schema is invalid")
    level = profile_document.get("level")
    state = profile_document.get("state")
    if level != "world" or not isinstance(state, dict):
        raise ValueError("immutable World Forge profile schema is invalid")
    if _SHA256_PATTERN.fullmatch(digest_hex) is None:
        raise ValueError("immutable World Forge revision digest is invalid")
    return StateProfile(
        name=f"{_FORGE_PROFILE_NAME_PREFIX}{digest_hex}",
        level=cast(StateProfileLevel, level),
        state=deepcopy(state),
        created_at=_FORGE_PROFILE_CREATED_AT,
    )


def _strict_revision_json(data: bytes) -> dict[str, object]:
    try:
        text = data.decode("utf-8")

        def reject_constant(value: str) -> object:
            raise ValueError(f"invalid JSON constant: {value}")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise ValueError("immutable World Forge revision is not strict JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("immutable World Forge revision must be an object")
    return parsed


def _issue(code: str, message: str) -> ForgeProfileIssue:
    return ForgeProfileIssue(code, message)


def _node_count(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(
            _node_count(key) + _node_count(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return 1 + sum(_node_count(item) for item in value)
    return 1


def _contains_blank_residue(value: object) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in _BLANK_RESIDUE)
    if isinstance(value, dict):
        return any(
            _contains_blank_residue(key) or _contains_blank_residue(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_blank_residue(item) for item in value)
    return False
