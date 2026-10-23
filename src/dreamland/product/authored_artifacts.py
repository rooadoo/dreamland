"""Pinned import and immutable storage for operator-authored world profiles.

This module is inert until an operator-only startup path explicitly calls it.
Configured profile paths are trusted local configuration and must never be
accepted from a product transport request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from dreamland.product.forge_artifacts import MAX_PROFILE_BYTES, validate_forge_profile
from dreamland.state_profiles import (
    STATE_PROFILE_KIND,
    STATE_PROFILE_SCHEMA_VERSION,
    StateProfile,
    StateProfileError,
    StateProfileLevel,
    state_profile_from_document,
)

AUTHORED_REVISION_KIND = "dreamland.authored_world_revision"
AUTHORED_REVISION_SCHEMA = "1"
MAX_CONFIGURED_PROFILE_BYTES = MAX_PROFILE_BYTES
MAX_AUTHORED_REVISION_BYTES = MAX_PROFILE_BYTES

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_ID_PREFIX = "world-revision:sha256:"
_DIGEST_PREFIX = "sha256:"
_SYNTHETIC_PROFILE_NAME_PREFIX = "authored-world-"
_SYNTHETIC_PROFILE_CREATED_AT = "1970-01-01T00:00:00+00:00"
_SOURCE_REQUIRED_KEYS = {
    "kind",
    "schema_version",
    "name",
    "level",
    "created_at",
    "state",
}
_SOURCE_OPTIONAL_KEYS = {"source_session_id", "comment"}
_MAX_TEMP_FILE_ATTEMPTS = 16


@dataclass(frozen=True, slots=True)
class ImmutableAuthoredRevision:
    """Metadata-free immutable revision detached from its configured source."""

    revision_id: str
    digest: str
    _canonical_json: bytes = field(repr=False)

    @property
    def immutable_digest(self) -> str:
        return self.digest

    def canonical_bytes(self) -> bytes:
        """Return the immutable canonical representation."""
        return self._canonical_json

    def allocate_document(self) -> dict[str, object]:
        """Allocate an independent metadata-free revision document."""
        value = json.loads(self._canonical_json)
        if not isinstance(value, dict):  # Construction enforces this invariant.
            raise RuntimeError("immutable authored revision is not an object")
        return value

    def allocate_profile(self) -> StateProfile:
        """Allocate an independent profile with a deterministic internal identity."""
        digest_hex = _require_matching_revision_identity(self.revision_id, self.digest)
        return _profile_from_revision_document(self.allocate_document(), digest_hex)


class ImmutableAuthoredStore:
    """Create-only owner-private storage for pinned authored revisions."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def save(
        self,
        profile: StateProfile,
        *,
        expected_digest: str,
    ) -> ImmutableAuthoredRevision:
        """Validate and store a profile only when its configured pin is exact."""
        try:
            document = canonical_authored_revision_document(profile)
            data = _canonical_bytes(document)
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError(
                "authored world profile cannot be canonicalized"
            ) from error
        digest_hex = hashlib.sha256(data).hexdigest()
        if _require_digest(expected_digest) != digest_hex:
            raise ValueError("authored world profile digest does not match its pin")
        snapshot = _profile_from_revision_document(document, digest_hex)
        _require_valid_profile(snapshot)
        return self._save_verified(data, digest_hex)

    def load(self, revision_id: str, digest: str) -> ImmutableAuthoredRevision:
        """Reopen one exact revision without consulting its configured source."""
        digest_hex = _require_matching_revision_identity(revision_id, digest)
        filename = f"sha256-{digest_hex}.json"
        directory_descriptor = self._open_directory(create=False)
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            try:
                descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
            except OSError as error:
                raise ValueError(
                    "immutable authored revision is unavailable"
                ) from error
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise ValueError("immutable authored revision is unsafe")
                if metadata.st_size > MAX_AUTHORED_REVISION_BYTES:
                    raise ValueError(
                        "immutable authored revision exceeds the size limit"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    data = stream.read(MAX_AUTHORED_REVISION_BYTES + 1)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_descriptor)
        if len(data) > MAX_AUTHORED_REVISION_BYTES:
            raise ValueError("immutable authored revision exceeds the size limit")
        if hashlib.sha256(data).hexdigest() != digest_hex:
            raise ValueError("immutable authored revision digest does not match")
        document = _strict_json_object(
            data, failure="immutable authored revision is not strict JSON"
        )
        try:
            canonical = _canonical_bytes(document)
        except (RecursionError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError("immutable authored revision is not canonical") from error
        if canonical != data:
            raise ValueError("immutable authored revision is not canonical")
        profile = _profile_from_revision_document(document, digest_hex)
        _require_valid_profile(profile, artifact=True)
        if canonical_authored_revision_document(profile) != document:
            raise ValueError("immutable authored revision schema is invalid")
        return ImmutableAuthoredRevision(revision_id, digest, canonical)

    def _save_verified(self, data: bytes, digest_hex: str) -> ImmutableAuthoredRevision:
        revision_id = f"{_REVISION_ID_PREFIX}{digest_hex}"
        digest = f"{_DIGEST_PREFIX}{digest_hex}"
        filename = f"sha256-{digest_hex}.json"
        directory_descriptor = self._open_directory(create=True)
        published = False
        temporary_name: str | None = None
        try:
            descriptor = -1
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            for _attempt in range(_MAX_TEMP_FILE_ATTEMPTS):
                candidate = f".authored-{secrets.token_hex(16)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        flags,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    continue
                except OSError as error:
                    raise ValueError(
                        "immutable authored revision cannot be created"
                    ) from error
                temporary_name = candidate
                break
            if descriptor < 0 or temporary_name is None:
                raise ValueError("immutable authored revision cannot be created")

            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    os.fchmod(descriptor, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)

            try:
                os.link(
                    temporary_name,
                    filename,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                published = True
                os.fsync(directory_descriptor)
            except FileExistsError:
                published = False
            except OSError as error:
                raise ValueError(
                    "immutable authored revision cannot be published"
                ) from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except OSError:
                    pass
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)
        if not published:
            existing = self.load(revision_id, digest)
            if existing.canonical_bytes() != data:
                raise ValueError("immutable authored revision digest collision")
            return existing
        return ImmutableAuthoredRevision(revision_id, digest, data)

    def _open_directory(self, *, create: bool) -> int:
        if create:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as error:
                raise ValueError(
                    "immutable authored revision store is unavailable"
                ) from error
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.directory, flags)
        except OSError as error:
            raise ValueError(
                "immutable authored revision store is unavailable"
            ) from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise ValueError("immutable authored revision store is unsafe")
        return descriptor


def canonical_authored_revision_document(
    profile: StateProfile,
) -> dict[str, object]:
    """Remove all mutable source metadata from authored revision identity."""
    return {
        "kind": AUTHORED_REVISION_KIND,
        "schema_revision": AUTHORED_REVISION_SCHEMA,
        "profile": {"level": profile.level, "state": deepcopy(profile.state)},
    }


def authored_revision_digest(profile: StateProfile) -> str:
    """Return the configured pin for a profile's metadata-free revision."""
    data = _canonical_bytes(canonical_authored_revision_document(profile))
    return f"{_DIGEST_PREFIX}{hashlib.sha256(data).hexdigest()}"


def load_configured_authored_profile(source_path: Path | str) -> StateProfile:
    """Read one trusted startup-configured profile path exactly once.

    ``source_path`` is an operator-only local configuration value.  It must not
    be populated from an HTTP request, offering identifier, or other untrusted
    product input.
    """
    data = _read_configured_source(Path(source_path))
    document = _strict_json_object(
        data, failure="configured authored profile is not strict JSON"
    )
    keys = set(document)
    if not _SOURCE_REQUIRED_KEYS.issubset(keys) or not keys.issubset(
        _SOURCE_REQUIRED_KEYS | _SOURCE_OPTIONAL_KEYS
    ):
        raise ValueError("configured authored profile schema is invalid")
    if document.get("kind") != STATE_PROFILE_KIND:
        raise ValueError("configured authored profile kind is invalid")
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != STATE_PROFILE_SCHEMA_VERSION
    ):
        raise ValueError("configured authored profile schema is unsupported")
    if document.get("level") != "world":
        raise ValueError("configured authored profile must be a world profile")
    if not isinstance(document.get("name"), str):
        raise ValueError("configured authored profile name is invalid")
    if not isinstance(document.get("created_at"), str) or not document.get(
        "created_at"
    ):
        raise ValueError("configured authored profile timestamp is invalid")
    source_session = document.get("source_session_id")
    if source_session is not None and not isinstance(source_session, str):
        raise ValueError("configured authored profile metadata is invalid")
    comment = document.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise ValueError("configured authored profile metadata is invalid")
    try:
        return state_profile_from_document(document)
    except StateProfileError as error:
        raise ValueError("configured authored profile schema is invalid") from error


def import_configured_authored_profile(
    source_path: Path | str,
    *,
    expected_digest: str,
    store: ImmutableAuthoredStore,
) -> ImmutableAuthoredRevision:
    """Snapshot one exactly pinned trusted profile into the immutable store."""
    profile = load_configured_authored_profile(source_path)
    return store.save(profile, expected_digest=expected_digest)


def _read_configured_source(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("configured authored profile is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("configured authored profile is not a regular file")
        if metadata.st_size > MAX_CONFIGURED_PROFILE_BYTES:
            raise ValueError("configured authored profile exceeds the size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_CONFIGURED_PROFILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_CONFIGURED_PROFILE_BYTES:
        raise ValueError("configured authored profile exceeds the size limit")
    return data


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json_object(data: bytes, *, failure: str) -> dict[str, object]:
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
        raise ValueError(failure) from error
    if not isinstance(parsed, dict):
        raise ValueError(failure)
    return parsed


def _profile_from_revision_document(
    document: Mapping[str, object], digest_hex: str
) -> StateProfile:
    if set(document) != {"kind", "schema_revision", "profile"}:
        raise ValueError("immutable authored revision schema is invalid")
    if document.get("kind") != AUTHORED_REVISION_KIND:
        raise ValueError("immutable authored revision kind is invalid")
    if document.get("schema_revision") != AUTHORED_REVISION_SCHEMA:
        raise ValueError("immutable authored revision schema is unsupported")
    profile_document = document.get("profile")
    if not isinstance(profile_document, dict) or set(profile_document) != {
        "level",
        "state",
    }:
        raise ValueError("immutable authored profile schema is invalid")
    level = profile_document.get("level")
    state = profile_document.get("state")
    if level != "world" or not isinstance(state, dict):
        raise ValueError("immutable authored profile schema is invalid")
    if _SHA256_PATTERN.fullmatch(digest_hex) is None:
        raise ValueError("immutable authored revision digest is invalid")
    return StateProfile(
        name=f"{_SYNTHETIC_PROFILE_NAME_PREFIX}{digest_hex}",
        level=cast(StateProfileLevel, level),
        state=deepcopy(state),
        created_at=_SYNTHETIC_PROFILE_CREATED_AT,
    )


def _require_valid_profile(profile: StateProfile, *, artifact: bool = False) -> None:
    try:
        validation = validate_forge_profile(profile)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        message = (
            "immutable authored revision failed validation"
            if artifact
            else "authored world profile failed validation"
        )
        raise ValueError(message) from error
    if not validation.valid:
        message = (
            "immutable authored revision failed validation"
            if artifact
            else "authored world profile failed validation"
        )
        raise ValueError(message)


def _require_digest(digest: str) -> str:
    if not isinstance(digest, str) or not digest.startswith(_DIGEST_PREFIX):
        raise ValueError("authored world profile digest pin is invalid")
    digest_hex = digest.removeprefix(_DIGEST_PREFIX)
    if _SHA256_PATTERN.fullmatch(digest_hex) is None:
        raise ValueError("authored world profile digest pin is invalid")
    return digest_hex


def _require_matching_revision_identity(revision_id: str, digest: str) -> str:
    if not isinstance(revision_id, str) or not isinstance(digest, str):
        raise ValueError("immutable authored revision identity is invalid")
    if not revision_id.startswith(_REVISION_ID_PREFIX):
        raise ValueError("immutable authored revision identity is invalid")
    revision_digest = revision_id.removeprefix(_REVISION_ID_PREFIX)
    digest_hex = _require_digest(digest)
    if (
        _SHA256_PATTERN.fullmatch(revision_digest) is None
        or revision_digest != digest_hex
    ):
        raise ValueError("immutable authored revision identity is invalid")
    return digest_hex
