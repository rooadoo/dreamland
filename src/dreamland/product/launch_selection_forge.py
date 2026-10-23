"""Trusted registration of authoritative Forge results for launch selection.

The registrar is inert until an explicit live composition constructs it.  It
accepts only a selected record resolved through ``WorldForgeService`` and
reopens the exact content-addressed artifact before publishing a browser-safe,
non-authorizing launch option.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime

from dreamland.product.forge import ForgeSelectedRevisionRecord, WorldForgeService
from dreamland.product.forge_artifacts import ImmutableForgeStore
from dreamland.product.forge_stage import (
    ForgeStageHandoffPolicy,
    forge_stage_display_metadata,
)
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionOrigin,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import ProductOperation, ProductRole

_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
_MAX_GRANT_TTL_MS = 30 * 60 * 1_000


class ForgeLaunchSelectionRegistrar:
    """Bridge selected live Forge records into one source-neutral registry."""

    def __init__(
        self,
        *,
        forge: WorldForgeService,
        artifacts: ImmutableForgeStore,
        registry: LaunchSelectionRegistry,
        actor_authority: str,
        actor_id: str,
        stage_policy: ForgeStageHandoffPolicy,
        locator_key: bytes | None = None,
        clock_ms: Callable[[], int] | None = None,
        maximum_launch_grant_ttl_ms: int = _MAX_GRANT_TTL_MS,
    ) -> None:
        if not isinstance(forge, WorldForgeService):
            raise TypeError("forge must be WorldForgeService")
        if not isinstance(artifacts, ImmutableForgeStore):
            raise TypeError("artifacts must be ImmutableForgeStore")
        if not isinstance(registry, LaunchSelectionRegistry):
            raise TypeError("registry must be LaunchSelectionRegistry")
        if not isinstance(stage_policy, ForgeStageHandoffPolicy):
            raise TypeError("stage_policy must be ForgeStageHandoffPolicy")
        if not isinstance(actor_authority, str) or not actor_authority:
            raise ValueError("Forge registration actor authority is invalid")
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("Forge registration actor identity is invalid")
        key = secrets.token_bytes(32) if locator_key is None else locator_key
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Forge registration locator key is invalid")
        if (
            isinstance(maximum_launch_grant_ttl_ms, bool)
            or not isinstance(maximum_launch_grant_ttl_ms, int)
            or maximum_launch_grant_ttl_ms <= 0
        ):
            raise ValueError("Forge launch grant TTL must be positive")
        self._forge = forge
        self._artifacts = artifacts
        self._registry = registry
        self._actor_authority = actor_authority
        self._actor_id = actor_id
        self._stage_policy = stage_policy
        self._locator_key = key
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._maximum_launch_grant_ttl_ms = maximum_launch_grant_ttl_ms

    def register_selected(self, job_reference: str) -> dict[str, object]:
        """Resolve, verify, and atomically register one selected Forge result."""
        selected = self._forge.resolve_selected_record(
            actor_authority=self._actor_authority,
            expected_epoch=self._forge.service_epoch,
            job_reference=job_reference,
        )
        if selected.actor_id != self._actor_id:
            raise ValueError(
                "selected Forge actor is incompatible with launch selection"
            )
        artifact = self._artifacts.load(
            selected.revision_id,
            selected.immutable_digest,
        )
        if (
            artifact.revision_id != selected.revision_id
            or artifact.digest != selected.immutable_digest
        ):
            raise ValueError("selected Forge artifact identity changed")
        display = forge_stage_display_metadata(selected, self._stage_policy)
        expires_at_ms = _timestamp_ms(selected.expires_at)
        remaining_ms = expires_at_ms - self._clock_ms()
        if remaining_ms <= 0:
            raise ValueError("selected Forge record expired before registration")
        offering_id, launch_selection_id = self._locators(selected)
        result = self._registry.register_trusted_offering(
            revision=LaunchRevisionSeed(
                revision_id=artifact.revision_id,
                document=artifact.document,
            ),
            offering=LaunchOfferingSeed(
                offering_id=offering_id,
                launch_selection_id=launch_selection_id,
                revision_id=artifact.revision_id,
                origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                title=display.title,
                source_label=display.source_label,
                revision_label=display.revision_label,
                role=ProductRole.PRESENTER,
                allowed_operations=frozenset(
                    {ProductOperation.CREATE, ProductOperation.SWITCH}
                ),
                limits=self._stage_policy.launch_limits,
                policy_id=selected.policy_id,
                notice_version=self._stage_policy.notice_version,
                expires_at_ms=expires_at_ms,
                # Keep trusted registration replay byte-stable as time advances.
                # Exchange independently caps each grant at offering expiry.
                launch_grant_ttl_ms=self._maximum_launch_grant_ttl_ms,
            ),
        )
        _LIFECYCLE_LOGGER.info(
            "world_forge_launch_option_registered offering_id=%s revision_id=%s",
            offering_id,
            selected.revision_id,
        )
        return result

    def _locators(
        self,
        selected: ForgeSelectedRevisionRecord,
    ) -> tuple[str, str]:
        material = "\0".join(
            (
                "dreamland.forge-launch-selection.v1",
                selected.service_epoch,
                selected.selected_record_id,
                selected.revision_id,
            )
        ).encode("utf-8")
        digest = hmac.new(self._locator_key, material, hashlib.sha256).hexdigest()
        return (
            f"offering:forge-{digest[:32]}",
            f"launch-selection:forge_{digest}",
        )


def _timestamp_ms(value: str) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError("selected Forge expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("selected Forge expiry is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("selected Forge expiry is invalid")
    milliseconds = int(parsed.timestamp() * 1_000)
    if milliseconds <= 0:
        raise ValueError("selected Forge expiry is invalid")
    return milliseconds


__all__ = ["ForgeLaunchSelectionRegistrar"]
