"""Store-free runtime hydration for verified World Forge revisions.

This module is inert until a product runtime explicitly constructs its codec.
It never consults or mutates the ordinary state-profile store.  The immutable
Forge artifact identity remains distinct from product lifecycle document
hashes after session-local state starts to change.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from dreamland.product.forge_artifacts import (
    _canonical_bytes,
    _profile_from_revision_document,
    _require_matching_revision_identity,
    validate_forge_profile,
)
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeState,
)
from dreamland.state_profiles import StateProfile, state_profile_from_document

FORGE_RUNTIME_STATE_KIND = "dreamland.world_forge_runtime_state"
FORGE_RUNTIME_STATE_SCHEMA = "1"
_FORGE_REVISION_KIND = "dreamland.world_forge_revision"
_MAX_TRACKED_ORIGINS = 64


@dataclass(frozen=True, slots=True)
class ForgeRuntimeOrigin:
    """Exact stored-byte identity of the immutable Forge starting revision."""

    revision_id: str
    digest: str

    def __post_init__(self) -> None:
        _require_matching_revision_identity(self.revision_id, self.digest)

    def to_document(self) -> dict[str, str]:
        return {"revision_id": self.revision_id, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class ForgeRuntimeCodecContext:
    """Private codec context kept separate from emulated environment state."""

    origin: ForgeRuntimeOrigin
    profile: StateProfile


class ForgeStateProfileCodec:
    """Hydrate canonical Forge envelopes and detached Forge runtime updates.

    A canonical ``dreamland.world_forge_revision`` document is accepted as the
    initial state.  Once a command changes state, ``encode`` emits a private
    runtime envelope containing the detached exact profile and the unchanged
    newline-inclusive Forge artifact identity.  That identity is not derived
    from, or replaced by, the product lifecycle's canonical document hash.
    """

    def __init__(self) -> None:
        self._state_profiles = DreamlandStateProfileCodec()
        self._origins: dict[str, str] = {}
        self._origin_lock = threading.Lock()

    def decode(
        self,
        document: Mapping[str, object],
        *,
        checkpoint_namespace: str,
    ) -> ProductRuntimeState:
        copied = deepcopy(dict(document))
        kind = copied.get("kind")
        if kind == _FORGE_REVISION_KIND:
            origin, profile = _initial_revision(copied)
            self._remember_origin(origin)
        elif kind == FORGE_RUNTIME_STATE_KIND:
            origin, profile = _detached_runtime(copied)
            self._require_known_origin(origin)
        else:
            raise ValueError("Forge runtime document kind is invalid")

        runtime = self._state_profiles.decode(
            profile.to_document(),
            checkpoint_namespace=checkpoint_namespace,
        )
        return ProductRuntimeState(
            profile_name=profile.name,
            session=runtime.session,
            codec_context=ForgeRuntimeCodecContext(origin, profile),
        )

    def _remember_origin(self, origin: ForgeRuntimeOrigin) -> None:
        with self._origin_lock:
            known = self._origins.get(origin.revision_id)
            if known is not None and known != origin.digest:
                raise ValueError(
                    "Forge runtime origin conflicts with verified identity"
                )
            if known is None:
                if len(self._origins) >= _MAX_TRACKED_ORIGINS:
                    raise ValueError("Forge runtime origin capacity is exhausted")
                self._origins[origin.revision_id] = origin.digest

    def _require_known_origin(self, origin: ForgeRuntimeOrigin) -> None:
        with self._origin_lock:
            if self._origins.get(origin.revision_id) != origin.digest:
                raise ValueError("Forge runtime origin is not verified")

    def encode(self, state: ProductRuntimeState) -> Mapping[str, object]:
        context = state.codec_context
        if (
            not isinstance(context, ForgeRuntimeCodecContext)
            or context.profile.name != state.profile_name
        ):
            raise ValueError("Forge runtime codec context is invalid")
        nested_state = ProductRuntimeState(
            profile_name=state.profile_name,
            session=state.session,
            codec_context=context.profile,
        )
        profile_document = dict(self._state_profiles.encode(nested_state))
        profile = _require_detached_profile(profile_document, context.origin)
        return {
            "kind": FORGE_RUNTIME_STATE_KIND,
            "schema_revision": FORGE_RUNTIME_STATE_SCHEMA,
            "origin": context.origin.to_document(),
            "profile": profile.to_document(),
        }


def forge_runtime_origin(state: ProductRuntimeState) -> ForgeRuntimeOrigin:
    """Return the verified immutable origin of one Forge-backed runtime."""
    context = state.codec_context
    if not isinstance(context, ForgeRuntimeCodecContext):
        raise ValueError("runtime state is not Forge-backed")
    return context.origin


def _initial_revision(
    document: dict[str, object],
) -> tuple[ForgeRuntimeOrigin, StateProfile]:
    try:
        canonical = _canonical_bytes(document)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("Forge revision document is invalid") from error
    try:
        parsed = json.loads(canonical)
        same_json_types = _same_json_types(parsed, document)
    except (RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Forge revision document is not canonical JSON") from error
    if not isinstance(parsed, dict) or not same_json_types:
        raise ValueError("Forge revision document is not canonical JSON")
    digest_hex = hashlib.sha256(canonical).hexdigest()
    profile = _profile_from_revision_document(document, digest_hex)
    try:
        validation = validate_forge_profile(profile)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("Forge revision document failed validation") from error
    if not validation.valid:
        raise ValueError("Forge revision document failed validation")
    origin = ForgeRuntimeOrigin(
        revision_id=f"world-revision:sha256:{digest_hex}",
        digest=f"sha256:{digest_hex}",
    )
    return origin, profile


def _detached_runtime(
    document: dict[str, object],
) -> tuple[ForgeRuntimeOrigin, StateProfile]:
    _require_json_object(document, "Forge runtime document")
    if set(document) != {"kind", "schema_revision", "origin", "profile"}:
        raise ValueError("Forge runtime document schema is invalid")
    if document.get("schema_revision") != FORGE_RUNTIME_STATE_SCHEMA:
        raise ValueError("Forge runtime document schema is unsupported")
    origin_document = document.get("origin")
    if not isinstance(origin_document, dict) or set(origin_document) != {
        "revision_id",
        "digest",
    }:
        raise ValueError("Forge runtime origin is invalid")
    revision_id = origin_document.get("revision_id")
    digest = origin_document.get("digest")
    if not isinstance(revision_id, str) or not isinstance(digest, str):
        raise ValueError("Forge runtime origin is invalid")
    origin = ForgeRuntimeOrigin(revision_id, digest)
    profile_document = document.get("profile")
    if not isinstance(profile_document, dict):
        raise ValueError("Forge runtime profile is invalid")
    profile = _require_detached_profile(profile_document, origin)
    return origin, profile


def _require_detached_profile(
    document: dict[str, object],
    origin: ForgeRuntimeOrigin,
) -> StateProfile:
    if set(document) != {
        "kind",
        "schema_version",
        "name",
        "level",
        "created_at",
        "state",
    }:
        raise ValueError("Forge runtime profile schema is invalid")
    profile = state_profile_from_document(deepcopy(document))
    digest_hex = _require_matching_revision_identity(
        origin.revision_id,
        origin.digest,
    )
    expected = _profile_from_revision_document(
        {
            "kind": _FORGE_REVISION_KIND,
            "schema_revision": "1",
            "profile": {"level": "world", "state": {}},
        },
        digest_hex,
    )
    if (
        profile.name != expected.name
        or profile.created_at != expected.created_at
        or profile.level != "exact"
        or profile.source_session_id is not None
        or profile.comment is not None
    ):
        raise ValueError("Forge runtime profile metadata is invalid")
    return profile


def _require_json_object(document: dict[str, object], label: str) -> None:
    try:
        encoded = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        parsed = json.loads(encoded)
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(parsed, dict) or not _same_json_types(parsed, document):
        raise ValueError(f"{label} is not canonical JSON")


def _same_json_types(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
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
    "FORGE_RUNTIME_STATE_KIND",
    "FORGE_RUNTIME_STATE_SCHEMA",
    "ForgeRuntimeCodecContext",
    "ForgeRuntimeOrigin",
    "ForgeStateProfileCodec",
    "forge_runtime_origin",
]
