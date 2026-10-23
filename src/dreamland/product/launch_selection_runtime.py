"""Store-free runtime dispatch for source-neutral Presenter launch selections.

This module is inert until an explicit product composition constructs the
codec.  It accepts only the two immutable revision kinds understood by the
launch-selection registry and keeps their runtime serialization paths
separate.  Authored runtime state retains its verified immutable origin;
Forge runtime state delegates to the existing Forge codec and its origin
checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from dreamland.product.authored_artifacts import (
    AUTHORED_REVISION_KIND,
    AUTHORED_REVISION_SCHEMA,
    ImmutableAuthoredRevision,
)
from dreamland.product.forge_artifacts import validate_forge_profile
from dreamland.product.forge_runtime import (
    FORGE_RUNTIME_STATE_KIND,
    ForgeStateProfileCodec,
)
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeState,
)
from dreamland.state_profiles import (
    STATE_PROFILE_KIND,
    STATE_PROFILE_SCHEMA_VERSION,
)

AUTHORED_RUNTIME_STATE_KIND = "dreamland.authored_world_runtime_state"
AUTHORED_RUNTIME_STATE_SCHEMA = "1"
FORGE_REVISION_KIND = "dreamland.world_forge_revision"

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^world-revision:sha256:[0-9a-f]{64}$")
_AUTHORED_PROFILE_NAME_PREFIX = "authored-world-"
_AUTHORED_PROFILE_CREATED_AT = "1970-01-01T00:00:00+00:00"
_MAX_TRACKED_AUTHORED_ORIGINS = 64


@dataclass(frozen=True, slots=True)
class AuthoredRuntimeOrigin:
    """Exact immutable identity retained outside emulated authored state."""

    revision_id: str
    digest: str

    def __post_init__(self) -> None:
        _require_origin_identity(self.revision_id, self.digest)

    def to_document(self) -> dict[str, str]:
        return {"revision_id": self.revision_id, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class LaunchSelectionRuntimeCodecContext:
    """Private delegate state identifying one closed runtime source kind."""

    source_kind: str
    delegate_context: object
    authored_origin: AuthoredRuntimeOrigin | None = None


class LaunchSelectionStateProfileCodec:
    """Dispatch authored and Forge revisions without weakening either codec."""

    def __init__(self) -> None:
        self._authored = DreamlandStateProfileCodec()
        self._forge = ForgeStateProfileCodec()
        self._authored_origins: dict[str, str] = {}
        self._authored_origin_lock = threading.Lock()

    def decode(
        self,
        document: Mapping[str, object],
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState:
        copied = deepcopy(dict(document))
        kind = copied.get("kind")
        if kind == AUTHORED_REVISION_KIND:
            origin, profile_document = _initial_authored_revision(copied)
            self._remember_authored_origin(origin)
            nested = self._authored.decode(
                profile_document,
                checkpoint_namespace=checkpoint_namespace,
            )
            context = LaunchSelectionRuntimeCodecContext(
                AUTHORED_REVISION_KIND,
                nested.codec_context,
                origin,
            )
        elif kind == AUTHORED_RUNTIME_STATE_KIND:
            origin, profile_document = _authored_runtime_document(copied)
            self._require_known_authored_origin(origin)
            nested = self._authored.decode(
                profile_document,
                checkpoint_namespace=checkpoint_namespace,
            )
            context = LaunchSelectionRuntimeCodecContext(
                AUTHORED_REVISION_KIND,
                nested.codec_context,
                origin,
            )
        elif kind in {FORGE_REVISION_KIND, FORGE_RUNTIME_STATE_KIND}:
            nested = self._forge.decode(
                copied,
                checkpoint_namespace=checkpoint_namespace,
            )
            context = LaunchSelectionRuntimeCodecContext(
                FORGE_REVISION_KIND,
                nested.codec_context,
            )
        else:
            raise ValueError("launch-selection runtime document kind is invalid")
        return ProductRuntimeState(
            profile_name=nested.profile_name,
            session=nested.session,
            codec_context=context,
        )

    def encode(self, state: ProductRuntimeState) -> Mapping[str, object]:
        context = state.codec_context
        if not isinstance(context, LaunchSelectionRuntimeCodecContext):
            raise ValueError("launch-selection runtime codec context is invalid")
        nested = ProductRuntimeState(
            profile_name=state.profile_name,
            session=state.session,
            codec_context=context.delegate_context,
        )
        if context.source_kind == FORGE_REVISION_KIND:
            if context.authored_origin is not None:
                raise ValueError("Forge runtime codec context is invalid")
            return self._forge.encode(nested)
        if context.source_kind != AUTHORED_REVISION_KIND:
            raise ValueError("launch-selection runtime source kind is invalid")
        origin = context.authored_origin
        if origin is None:
            raise ValueError("authored runtime codec context is invalid")
        self._require_known_authored_origin(origin)
        profile_document = dict(self._authored.encode(nested))
        _require_authored_runtime_profile(profile_document, origin)
        return {
            "kind": AUTHORED_RUNTIME_STATE_KIND,
            "schema_revision": AUTHORED_RUNTIME_STATE_SCHEMA,
            "origin": origin.to_document(),
            "profile": profile_document,
        }

    def _remember_authored_origin(self, origin: AuthoredRuntimeOrigin) -> None:
        with self._authored_origin_lock:
            known = self._authored_origins.get(origin.revision_id)
            if known is not None and known != origin.digest:
                raise ValueError("authored runtime origin conflicts with identity")
            if known is None:
                if len(self._authored_origins) >= _MAX_TRACKED_AUTHORED_ORIGINS:
                    raise ValueError("authored runtime origin capacity is exhausted")
                self._authored_origins[origin.revision_id] = origin.digest

    def _require_known_authored_origin(self, origin: AuthoredRuntimeOrigin) -> None:
        with self._authored_origin_lock:
            if self._authored_origins.get(origin.revision_id) != origin.digest:
                raise ValueError("authored runtime origin is not verified")


def _initial_authored_revision(
    document: dict[str, object],
) -> tuple[AuthoredRuntimeOrigin, dict[str, object]]:
    if set(document) != {"kind", "schema_revision", "profile"}:
        raise ValueError("authored launch revision schema is invalid")
    if document.get("schema_revision") != AUTHORED_REVISION_SCHEMA:
        raise ValueError("authored launch revision schema is unsupported")
    canonical = _canonical_bytes(document)
    digest_hex = hashlib.sha256(canonical).hexdigest()
    origin = AuthoredRuntimeOrigin(
        revision_id=f"world-revision:sha256:{digest_hex}",
        digest=f"sha256:{digest_hex}",
    )
    revision = ImmutableAuthoredRevision(
        origin.revision_id,
        origin.digest,
        canonical,
    )
    profile = revision.allocate_profile()
    try:
        report = validate_forge_profile(profile)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("authored launch revision failed validation") from error
    if not report.valid:
        raise ValueError("authored launch revision failed validation")
    return origin, profile.to_document()


def _authored_runtime_document(
    document: dict[str, object],
) -> tuple[AuthoredRuntimeOrigin, dict[str, object]]:
    if set(document) != {"kind", "schema_revision", "origin", "profile"}:
        raise ValueError("authored runtime document schema is invalid")
    if document.get("schema_revision") != AUTHORED_RUNTIME_STATE_SCHEMA:
        raise ValueError("authored runtime document schema is unsupported")
    origin_document = document.get("origin")
    if not isinstance(origin_document, dict) or set(origin_document) != {
        "revision_id",
        "digest",
    }:
        raise ValueError("authored runtime origin is invalid")
    revision_id = origin_document.get("revision_id")
    digest = origin_document.get("digest")
    if not isinstance(revision_id, str) or not isinstance(digest, str):
        raise ValueError("authored runtime origin is invalid")
    origin = AuthoredRuntimeOrigin(revision_id, digest)
    profile = document.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("authored runtime profile is invalid")
    _require_authored_runtime_profile(profile, origin)
    return origin, profile


def _require_authored_runtime_profile(
    document: dict[str, object],
    origin: AuthoredRuntimeOrigin,
) -> None:
    if set(document) != {
        "kind",
        "schema_version",
        "name",
        "level",
        "created_at",
        "state",
    }:
        raise ValueError("authored runtime profile schema is invalid")
    digest_hex = _require_origin_identity(origin.revision_id, origin.digest)
    if (
        document.get("kind") != STATE_PROFILE_KIND
        or document.get("schema_version") != STATE_PROFILE_SCHEMA_VERSION
        or document.get("name") != f"{_AUTHORED_PROFILE_NAME_PREFIX}{digest_hex}"
        or document.get("level") != "exact"
        or document.get("created_at") != _AUTHORED_PROFILE_CREATED_AT
        or not isinstance(document.get("state"), dict)
    ):
        raise ValueError("authored runtime profile metadata is invalid")
    _canonical_bytes(document)


def _require_origin_identity(revision_id: str, digest: str) -> str:
    if (
        not isinstance(revision_id, str)
        or _REVISION_PATTERN.fullmatch(revision_id) is None
        or not isinstance(digest, str)
        or _DIGEST_PATTERN.fullmatch(digest) is None
    ):
        raise ValueError("authored runtime origin identity is invalid")
    revision_digest = revision_id.removeprefix("world-revision:sha256:")
    digest_hex = digest.removeprefix("sha256:")
    if revision_digest != digest_hex:
        raise ValueError("authored runtime origin identity is invalid")
    return digest_hex


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    try:
        encoded = (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        parsed = json.loads(encoded)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("launch-selection runtime document is not JSON") from error
    if not isinstance(parsed, dict) or not _same_json_types(parsed, document):
        raise ValueError("launch-selection runtime document is not canonical JSON")
    return encoded


def _same_json_types(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _same_json_types(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json_types(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


__all__ = [
    "AUTHORED_RUNTIME_STATE_KIND",
    "AUTHORED_RUNTIME_STATE_SCHEMA",
    "AuthoredRuntimeOrigin",
    "LaunchSelectionRuntimeCodecContext",
    "LaunchSelectionStateProfileCodec",
]
