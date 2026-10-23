"""Inert generated-world catalog and offering-exchange coordination.

This module is the only bridge from revealed generated-access truth into the
attendee launch registry.  It exposes no listener, browser authority, provider,
worker, profile path, or normal DREAMLAND runtime hook.  Trusted composition
must construct it explicitly beside the provider-free Product attendee facade.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType

from dreamland.product.attendee import (
    PublicAttendeeSessionService,
    mint_attendee_catalog_eligibility_proof,
)
from dreamland.product.generated_access import (
    GeneratedAccessEligibility,
    GeneratedAccessError,
    GeneratedAccessPublicationCapacityError,
    GeneratedAccessPublicationReceipt,
    GeneratedWorldAccessService,
)
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import ProductOperation, ProductRole

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OFFERING_REVISION_PATTERN = re.compile(
    r"^offering-revision:[A-Za-z0-9][A-Za-z0-9_.-]{15,127}$"
)
_LAUNCH_BINDING_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LIMIT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MAX_CATALOG_ITEMS = 128
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


@dataclass(frozen=True, slots=True)
class GeneratedWorldCatalogPolicy:
    """Bounded display and attendee-launch policy for one explicit catalog."""

    launch_limits: Mapping[str, int]
    notice_version: str
    admission_closes_at_ms: int
    policy_id: str = "policy:generated-world-access-preview-v1"
    title: str = "Audience-created DREAMLAND world"
    source_label: str = "World Forge"
    maximum_generated_offerings: int = 16
    maximum_catalog_items: int = 64
    launch_grant_ttl_ms: int = 20 * 60 * 1000

    def __post_init__(self) -> None:
        limits = dict(self.launch_limits)
        if (
            not limits
            or len(limits) > 32
            or any(
                not isinstance(name, str)
                or _LIMIT_NAME_PATTERN.fullmatch(name) is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 1_000_000_000
                for name, value in limits.items()
            )
        ):
            raise ValueError("generated catalog launch limits are invalid")
        for name in ("notice_version", "policy_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTITY_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{name} must be a safe identifier")
        for name, maximum in (("title", 120), ("source_label", 64)):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= maximum
                or any(
                    ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
                    for character in value
                )
            ):
                raise ValueError(f"{name} must be display-safe")
        if (
            isinstance(self.admission_closes_at_ms, bool)
            or not isinstance(self.admission_closes_at_ms, int)
            or not 1 <= self.admission_closes_at_ms <= _MAX_TIMESTAMP_MS
        ):
            raise ValueError("admission_closes_at_ms is invalid")
        for name, maximum in (
            ("maximum_generated_offerings", _MAX_CATALOG_ITEMS),
            ("maximum_catalog_items", _MAX_CATALOG_ITEMS),
            ("launch_grant_ttl_ms", _MAX_SAFE_INTEGER),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise ValueError(f"{name} is invalid")
        if self.maximum_generated_offerings > self.maximum_catalog_items:
            raise ValueError("generated offering capacity exceeds catalog capacity")
        absolute_ttl_ms = limits.get("absolute_ttl_ms")
        idle_ttl_ms = limits.get("idle_ttl_ms")
        if (
            absolute_ttl_ms is not None
            and idle_ttl_ms is not None
            and idle_ttl_ms > absolute_ttl_ms
        ):
            raise ValueError(
                "generated catalog idle_ttl_ms cannot exceed absolute_ttl_ms"
            )
        object.__setattr__(self, "launch_limits", MappingProxyType(limits))


@dataclass(frozen=True, slots=True)
class _GeneratedCatalogBinding:
    offering_id: str
    offering_revision: str
    promotion_id: str = field(repr=False)
    access_record_id: str = field(repr=False)
    revision_id: str = ""
    immutable_digest: str = ""
    expires_at_ms: int = 0


class GeneratedWorldCatalogService:
    """Publish revealed access records and exchange safe catalog locators."""

    def __init__(
        self,
        *,
        access: GeneratedWorldAccessService,
        registry: LaunchSelectionRegistry,
        attendee: PublicAttendeeSessionService,
        policy: GeneratedWorldCatalogPolicy,
        locator_key: bytes,
        eligibility_key: bytes,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(access, GeneratedWorldAccessService):
            raise TypeError("access must be GeneratedWorldAccessService")
        if not isinstance(registry, LaunchSelectionRegistry):
            raise TypeError("registry must be LaunchSelectionRegistry")
        if not isinstance(attendee, PublicAttendeeSessionService):
            raise TypeError("attendee must be PublicAttendeeSessionService")
        if not isinstance(policy, GeneratedWorldCatalogPolicy):
            raise TypeError("policy must be GeneratedWorldCatalogPolicy")
        for key, name in (
            (locator_key, "locator_key"),
            (eligibility_key, "eligibility_key"),
        ):
            if not isinstance(key, bytes) or len(key) < 32:
                raise ValueError(f"{name} is invalid")
        if not attendee._generated_world_access:
            raise ValueError("attendee generated-world access is disabled")
        attendee_key = attendee._generated_eligibility_key
        if attendee_key is None or not hmac.compare_digest(
            attendee_key, eligibility_key
        ):
            raise ValueError("attendee catalog eligibility keys differ")
        if not (
            access.service_epoch == registry.service_epoch == attendee.service_epoch
        ):
            raise ValueError("world-access components must share one service epoch")
        if policy.admission_closes_at_ms > attendee._policy.admission_closes_at_ms:
            raise ValueError("catalog admission exceeds attendee admission")
        if policy.maximum_catalog_items > registry._policy.max_offerings:
            raise ValueError("catalog capacity exceeds launch registry capacity")
        if not frozenset({ProductOperation.CREATE, ProductOperation.SWITCH}).issubset(
            registry._allowed_operation_families
        ):
            raise ValueError("catalog operations exceed launch registry policy")
        if policy.launch_grant_ttl_ms > registry._policy.max_launch_grant_ttl_ms:
            raise ValueError("catalog grant TTL exceeds launch registry policy")
        self._access = access
        self._registry = registry
        self._attendee = attendee
        self._policy = policy
        self._locator_key = bytes(locator_key)
        self._eligibility_key = bytes(eligibility_key)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._access_clock_ms = access._clock_ms
        self._access_monotonic_ms = access._monotonic_ms
        self._service_epoch = access.service_epoch
        self._lock = threading.RLock()
        self._pending_by_access_record: dict[str, _GeneratedCatalogBinding] = {}
        self._generated_by_offering: dict[str, _GeneratedCatalogBinding] = {}
        self._generated_by_access_record: dict[str, _GeneratedCatalogBinding] = {}
        self._catalog_sequence = 0

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    def publication_accounting(self) -> dict[str, object]:
        """Return safe retained-lifetime publication capacity for this epoch.

        Generated launch bindings remain authoritative after catalog visibility
        expires: an issued grant or an exact exchange replay can still need the
        retained offering and revision.  Capacity therefore measures every
        committed or currently prepared binding in this service epoch rather
        than only the entries visible in the current catalog projection.
        """
        with self._lock:
            used = len(self._generated_by_access_record) + len(
                self._pending_by_access_record
            )
            capacity = self._policy.maximum_generated_offerings
            if used > capacity:
                raise RuntimeError("generated publication accounting exceeded policy")
            return {
                "retention": "service_epoch_lifetime",
                "capacity": capacity,
                "used": used,
                "remaining": capacity - used,
            }

    def reveal(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        idempotency_key: str,
        promotion_id: str,
        expected_access_record_id: str,
    ) -> dict[str, object]:
        """Reveal and publish one exact access revision before returning."""
        with self._access._lock:
            with self._lock:
                result = self._access.reveal(
                    actor_authority=actor_authority,
                    expected_epoch=expected_epoch,
                    idempotency_key=idempotency_key,
                    promotion_id=promotion_id,
                    expected_access_record_id=expected_access_record_id,
                    publish=self._prepare_publication,
                )
                access_record_id = result.get("access_record_id")
                if not isinstance(access_record_id, str):
                    raise RuntimeError("revealed access projection is invalid")
                self._activate_publication(access_record_id)
        _LIFECYCLE_LOGGER.info(
            "generated_world_catalog_reveal_completed replayed=%s",
            bool(result.get("replayed")),
        )
        return result

    def _activate_publication(self, access_record_id: str) -> None:
        """Move one prepared binding active under the access/catalog lock pair."""
        with self._lock:
            active = self._generated_by_access_record.get(access_record_id)
            pending = self._pending_by_access_record.pop(access_record_id, None)
            if active is None:
                if pending is None:
                    raise RuntimeError("revealed access publication is unavailable")
                self._generated_by_access_record[access_record_id] = pending
                self._generated_by_offering[pending.offering_id] = pending
                self._catalog_sequence += 1

    def catalog(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        """Return one bounded context-scoped catalog without launch authority."""
        self._attendee._require_context_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            mutation=False,
        )
        with self._lock:
            self._require_current_epoch()
            generated = dict(self._generated_by_offering)
            position = self._catalog_position(self._catalog_sequence)
        raw = self._registry.catalog_offerings(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            required_role=ProductRole.ATTENDEE,
        )
        offerings: list[dict[str, object]] = []
        for value in raw:
            safe = self._safe_catalog_offering(value, generated)
            if safe is not None:
                offerings.append(safe)
        if len(offerings) > self._policy.maximum_catalog_items:
            raise RuntimeError("launch registry exceeded catalog projection capacity")
        return {
            "kind": "world_access_catalog",
            "service_epoch": self._service_epoch,
            "catalog_position": position,
            "offerings": offerings,
        }

    def exchange(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        offering_id: str,
        offering_revision: str,
        notice_version_presented: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Exchange exact catalog evidence for a Product-compatible grant."""
        catalog = self.catalog(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
        )
        raw_offerings = catalog.get("offerings")
        if not isinstance(raw_offerings, list):
            raise RuntimeError("generated-world catalog offerings are invalid")
        offerings: list[Mapping[str, object]] = []
        for value in raw_offerings:
            if not isinstance(value, Mapping):
                raise RuntimeError("generated-world catalog offering is invalid")
            offerings.append(value)
        selected = next(
            (value for value in offerings if value.get("offering_id") == offering_id),
            None,
        )
        exact = (
            isinstance(selected, dict)
            and selected.get("offering_revision") == offering_revision
            and selected.get("notice_version") == notice_version_presented
        )
        proof = None
        if exact:
            if not isinstance(selected, Mapping):  # Exactness establishes this.
                raise RuntimeError("catalog offering selection is invalid")
            origin = selected.get("origin")
            revision = self._registry_revision_for_offering(offering_id)
            if revision is None or not isinstance(origin, Mapping):
                raise RuntimeError("catalog offering registry binding disappeared")
            origin_kind = origin.get("kind")
            if not isinstance(origin_kind, str):
                raise RuntimeError("catalog offering origin is invalid")
            selected_origin = {
                "authored_profile_snapshot": (
                    LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                ),
                "world_forge_selection": LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
            }.get(origin_kind)
            if selected_origin is None:
                raise RuntimeError("catalog offering origin is invalid")
            with self._lock:
                binding = self._generated_by_offering.get(offering_id)
            access_record_id = (
                binding.access_record_id
                if binding is not None
                else "catalog-authored:" + offering_revision.rsplit(":", 1)[-1]
            )
            expires_at_ms = (
                binding.expires_at_ms
                if binding is not None
                else self._policy.admission_closes_at_ms
            )
            proof = mint_attendee_catalog_eligibility_proof(
                key=self._eligibility_key,
                service_epoch=self._service_epoch,
                offering_id=offering_id,
                offering_revision=offering_revision,
                access_record_id=access_record_id,
                origin=selected_origin,
                revision_id=revision["revision_id"],
                immutable_digest="sha256:" + revision["sha256"],
                expires_at_ms=expires_at_ms,
            )
        result = self._attendee.exchange_catalog_offering(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            offering_id=offering_id,
            offering_revision=offering_revision,
            notice_version_presented=notice_version_presented,
            idempotency_key=idempotency_key,
            catalog_eligibility_proof=proof,
        )
        replayed = bool(result.get("replayed"))
        if exact and selected is not None:
            safe_selection = deepcopy(selected)
        elif replayed:
            safe_selection = self._replayed_catalog_selection(
                result.get("selection"),
                offering_id=offering_id,
                offering_revision=offering_revision,
                notice_version=notice_version_presented,
            )
        else:
            raise RuntimeError("inexact catalog exchange unexpectedly succeeded")
        origin = safe_selection.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, Mapping) else "invalid"
        launch_binding_sha256 = result.get("launch_binding_sha256")
        if (
            not isinstance(launch_binding_sha256, str)
            or _LAUNCH_BINDING_SHA256_PATTERN.fullmatch(launch_binding_sha256) is None
        ):
            raise RuntimeError("catalog launch binding receipt is invalid")
        _LIFECYCLE_LOGGER.info(
            "generated_world_catalog_exchange_completed origin=%s replayed=%s",
            origin_kind,
            bool(result.get("replayed")),
        )
        return {
            "kind": "attendee_launch_selection_granted",
            "service_epoch": result["service_epoch"],
            "replayed": result["replayed"],
            "launch_grant": result["launch_grant"],
            "launch_binding_sha256": launch_binding_sha256,
            "grant_expires_at": result["grant_expires_at"],
            "selection": safe_selection,
        }

    @staticmethod
    def _replayed_catalog_selection(
        value: object,
        *,
        offering_id: str,
        offering_revision: str,
        notice_version: str,
    ) -> dict[str, object]:
        """Recover only the safe catalog fields from an attendee replay."""
        expected = {
            "offering_id",
            "display",
            "origin",
            "revision",
            "expires_at",
            "notice_version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise RuntimeError("attendee catalog replay projection is invalid")
        origin = value.get("origin")
        display = value.get("display")
        if (
            value.get("offering_id") != offering_id
            or value.get("notice_version") != notice_version
            or not isinstance(origin, Mapping)
            or origin.get("kind")
            not in {
                LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT.value,
                LaunchSelectionOrigin.WORLD_FORGE_SELECTION.value,
            }
            or not isinstance(display, Mapping)
        ):
            raise RuntimeError("attendee catalog replay identity changed")
        return {
            "offering_id": offering_id,
            "offering_revision": offering_revision,
            "origin": deepcopy(dict(origin)),
            "display": deepcopy(dict(display)),
            "notice_version": notice_version,
            "expires_at": value["expires_at"],
        }

    def _prepare_publication(
        self, eligibility: GeneratedAccessEligibility
    ) -> GeneratedAccessPublicationReceipt | None:
        if not isinstance(eligibility, GeneratedAccessEligibility):
            raise TypeError("publication eligibility is invalid")
        if eligibility.service_epoch != self._service_epoch:
            raise GeneratedAccessError(
                code="access_epoch_changed",
                message="The generated-world access epoch has changed.",
                status=409,
            )
        now_ms = self._clock_value()
        expires_at_ms = min(
            eligibility.expires_at_ms,
            self._policy.admission_closes_at_ms,
        )
        if now_ms >= expires_at_ms:
            raise GeneratedAccessError(
                code="reveal_unavailable",
                message="The generated-world access record cannot be revealed.",
                status=409,
            )
        offering_id, selection_id, offering_revision = self._locators(eligibility)
        binding = _GeneratedCatalogBinding(
            offering_id=offering_id,
            offering_revision=offering_revision,
            promotion_id=eligibility.promotion_id,
            access_record_id=eligibility.access_record_id,
            revision_id=eligibility.revision_id,
            immutable_digest=eligibility.immutable_digest,
            expires_at_ms=expires_at_ms,
        )
        with self._lock:
            existing = self._generated_by_access_record.get(
                eligibility.access_record_id
            ) or self._pending_by_access_record.get(eligibility.access_record_id)
            if existing is not None:
                if existing != binding:
                    raise RuntimeError("generated catalog binding changed")
                return None
            generated_retained = len(self._generated_by_access_record) + len(
                self._pending_by_access_record
            )
            generated_visible = sum(
                1
                for binding in (
                    *self._generated_by_offering.values(),
                    *self._pending_by_access_record.values(),
                )
                if now_ms < binding.expires_at_ms
            )
            with self._registry._lock:
                _projected_ms, deadline_ms, wall_ms = self._registry._now()
                access_wall_ms = _validated_clock_value(
                    self._access_clock_ms(),
                    "access clock_ms",
                )
                access_deadline_ms = _validated_clock_value(
                    self._access_monotonic_ms(),
                    "access monotonic_ms",
                )
                catalog_wall_ms = self._clock_value()
                remaining_ttl_ms = min(
                    eligibility.expires_at_ms - access_wall_ms,
                    eligibility.expires_deadline_ms - access_deadline_ms,
                    expires_at_ms - wall_ms,
                    expires_at_ms - _projected_ms,
                    expires_at_ms - catalog_wall_ms,
                    self._attendee._admission_closes_deadline_ms - deadline_ms,
                )
                if remaining_ttl_ms <= 0:
                    raise GeneratedAccessError(
                        code="reveal_unavailable",
                        message=(
                            "The generated-world access record cannot be revealed."
                        ),
                        status=409,
                    )
                authored_visible = sum(
                    1
                    for offering in self._registry._offerings.values()
                    if offering.role is ProductRole.ATTENDEE
                    and offering.origin
                    is LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
                    and not self._registry._offering_expired(
                        offering,
                        deadline_ms=deadline_ms,
                        wall_ms=wall_ms,
                    )
                )
                if (
                    generated_retained >= self._policy.maximum_generated_offerings
                    or authored_visible + generated_visible
                    >= self._policy.maximum_catalog_items
                ):
                    raise GeneratedAccessPublicationCapacityError()
                try:
                    (
                        _projection,
                        registration,
                    ) = self._registry._register_trusted_offering_reversible(
                        revision=LaunchRevisionSeed(
                            revision_id=eligibility.revision_id,
                            document=eligibility.allocate_document(),
                        ),
                        offering=LaunchOfferingSeed(
                            offering_id=offering_id,
                            launch_selection_id=selection_id,
                            revision_id=eligibility.revision_id,
                            origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                            title=self._policy.title,
                            source_label=self._policy.source_label,
                            revision_label=(
                                "World Forge "
                                + eligibility.immutable_digest.removeprefix("sha256:")[
                                    :12
                                ]
                            ),
                            role=ProductRole.ATTENDEE,
                            allowed_operations=frozenset(
                                {ProductOperation.CREATE, ProductOperation.SWITCH}
                            ),
                            limits=self._policy.launch_limits,
                            policy_id=self._policy.policy_id,
                            notice_version=self._policy.notice_version,
                            expires_at_ms=expires_at_ms,
                            launch_grant_ttl_ms=self._policy.launch_grant_ttl_ms,
                            maximum_remaining_ttl_ms=remaining_ttl_ms,
                        ),
                    )
                except LaunchSelectionError as error:
                    if error.code in {
                        "registry_capacity_exhausted",
                        "launch_selection_capacity_exhausted",
                    }:
                        raise GeneratedAccessPublicationCapacityError() from None
                    raise GeneratedAccessError(
                        code="publication_conflict",
                        message=(
                            "The generated-world publication conflicts with "
                            "current state."
                        ),
                        status=409,
                    ) from None
            try:
                self._pending_by_access_record[eligibility.access_record_id] = binding
                receipt = GeneratedAccessPublicationReceipt(
                    rollback=lambda: self._rollback_publication(
                        binding=binding,
                        registration=registration,
                    )
                )
                _LIFECYCLE_LOGGER.info(
                    "generated_world_catalog_publication_prepared "
                    "origin=world_forge_selection"
                )
                return receipt
            except BaseException:
                pending = self._pending_by_access_record.get(
                    eligibility.access_record_id
                )
                if pending == binding:
                    self._pending_by_access_record.pop(eligibility.access_record_id)
                self._registry._rollback_trusted_offering_registration(registration)
                raise

    def _rollback_publication(
        self,
        *,
        binding: _GeneratedCatalogBinding,
        registration,
    ) -> None:
        """Undo one exact prepared binding before access reveal has committed."""
        with self._lock:
            active = self._generated_by_access_record.get(binding.access_record_id)
            if active is not None:
                raise RuntimeError(
                    "active generated catalog binding cannot be rolled back"
                )
            pending = self._pending_by_access_record.get(binding.access_record_id)
            if pending is None:
                return
            if pending != binding:
                raise RuntimeError("pending generated catalog binding changed")
            self._registry._rollback_trusted_offering_registration(registration)
            self._pending_by_access_record.pop(binding.access_record_id)
            _LIFECYCLE_LOGGER.info(
                "generated_world_catalog_publication_rolled_back "
                "origin=world_forge_selection"
            )

    def _safe_catalog_offering(
        self,
        value: Mapping[str, object],
        generated: Mapping[str, _GeneratedCatalogBinding],
    ) -> dict[str, object] | None:
        expected = {
            "offering_id",
            "origin",
            "display",
            "revision",
            "notice_version",
            "expires_at",
        }
        if set(value) != expected:
            raise RuntimeError("launch registry returned an invalid catalog offering")
        offering_id = value.get("offering_id")
        origin = value.get("origin")
        revision = value.get("revision")
        if (
            not isinstance(offering_id, str)
            or not isinstance(origin, Mapping)
            or not isinstance(revision, Mapping)
        ):
            raise RuntimeError("launch registry catalog fields are invalid")
        if origin.get("kind") == LaunchSelectionOrigin.WORLD_FORGE_SELECTION.value:
            binding = generated.get(offering_id)
            if (
                binding is None
                or revision.get("revision_id") != binding.revision_id
                or revision.get("sha256")
                != binding.immutable_digest.removeprefix("sha256:")
            ):
                return None
            offering_revision = binding.offering_revision
        elif origin.get("kind") == (
            LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT.value
        ):
            offering_revision = self._authored_offering_revision(value)
        else:
            raise RuntimeError("launch registry catalog origin is invalid")
        return {
            "offering_id": offering_id,
            "offering_revision": offering_revision,
            "origin": deepcopy(dict(origin)),
            "display": deepcopy(value["display"]),
            "notice_version": value["notice_version"],
            "expires_at": value["expires_at"],
        }

    def _registry_revision_for_offering(
        self, offering_id: str
    ) -> dict[str, str] | None:
        with self._registry._lock:
            offering = self._registry._offerings.get(offering_id)
            if offering is None:
                return None
            revision = self._registry._revisions.get(offering.revision_id)
            if revision is None:
                return None
            return {
                "revision_id": revision.revision_id,
                "sha256": revision.immutable_digest.removeprefix("sha256:"),
            }

    def _locators(
        self, eligibility: GeneratedAccessEligibility
    ) -> tuple[str, str, str]:
        digest = self._hmac(
            "generated",
            eligibility.access_record_id,
            eligibility.revision_id,
            eligibility.immutable_digest,
        )
        return (
            f"offering:generated-{digest[:32]}",
            f"launch-selection:generated_{digest}",
            f"offering-revision:generated-{digest[:40]}",
        )

    def _authored_offering_revision(self, value: Mapping[str, object]) -> str:
        revision = value.get("revision")
        if not isinstance(revision, Mapping):
            raise RuntimeError("authored catalog revision is unavailable")
        digest = self._hmac(
            "authored",
            str(value.get("offering_id")),
            str(revision.get("revision_id")),
            str(revision.get("sha256")),
        )
        return f"offering-revision:authored-{digest[:40]}"

    def _catalog_position(self, sequence: int) -> str:
        return f"catalog-position:{self._hmac('position', str(sequence))[:40]}"

    def _hmac(self, *values: str) -> str:
        material = "\0".join(("dreamland.world-access.catalog.v1", *values)).encode(
            "utf-8"
        )
        return hmac.new(self._locator_key, material, hashlib.sha256).hexdigest()

    def _require_current_epoch(self) -> None:
        if self._access.service_epoch != self._service_epoch:
            raise LaunchSelectionError(
                code="service_epoch_changed",
                message="The world-access service epoch has changed.",
                status=410,
            )

    def _clock_value(self) -> int:
        return _validated_clock_value(self._clock_ms(), "clock_ms")


def _validated_clock_value(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_TIMESTAMP_MS
    ):
        raise RuntimeError(f"{name} must return a valid timestamp")
    return value


__all__ = [
    "GeneratedWorldCatalogPolicy",
    "GeneratedWorldCatalogService",
]
