"""Protocol-neutral immutable launch-option and grant exchange.

The registry is inert until explicitly constructed. Trusted composition owns
revision and option registration. A safe option exposes a stable opaque
``launch_selection_id`` which is only a locator: exchange also requires private
actor and epoch evidence, and only the resulting ``launch_grant`` can resolve a
revision for downstream launch. This module has no dependency on profiles,
paths, providers, SSH, or normal DREAMLAND runtime composition.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from itertools import islice
from types import MappingProxyType
from typing import Literal

from dreamland.product.lifecycle import ProductOperation, ProductRole

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LIMIT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OFFERING_ID_PATTERN = re.compile(r"^offering:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LAUNCH_SELECTION_ID_PATTERN = re.compile(r"^launch-selection:[A-Za-z0-9_-]{24,128}$")
_LAUNCH_GRANT_PATTERN = re.compile(r"^launch-grant:[A-Za-z0-9_-]{24,480}$")
_SERVICE_EPOCH_PATTERN = re.compile(r"^epoch:[A-Za-z0-9_-]{16,128}$")
_LAUNCH_BINDING_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MIN_OPAQUE_CHARACTERS = 16
_MAX_OPAQUE_CHARACTERS = 512
_MAX_TOKEN_ATTEMPTS = 16
_MAX_IDEMPOTENCY_KEY_CHARACTERS = 128
_MAX_LIMIT_ENTRIES = 32
_MAX_LIMIT_VALUE = 1_000_000_000
_MAX_CONTRACT_OFFERINGS = 128
_MAX_LAUNCH_GRANT_SERIAL = 0xFFFF_FFFF_FFFF_FFFF
_LAUNCH_GRANT_DERIVATION_DOMAIN = b"dreamland.launch-selection.launch-grant.v1"
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


class LaunchSelectionOrigin(StrEnum):
    """Closed provenance classes understood by generic launch selection."""

    AUTHORED_PROFILE_SNAPSHOT = "authored_profile_snapshot"
    WORLD_FORGE_SELECTION = "world_forge_selection"


@dataclass(frozen=True, slots=True)
class LaunchRevisionSeed:
    """Trusted immutable revision input, detached during registration."""

    revision_id: str
    document: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class LaunchOfferingSeed:
    """One safe option and its trusted, non-authorizing selection binding."""

    offering_id: str
    launch_selection_id: str = field(repr=False)
    revision_id: str = ""
    origin: LaunchSelectionOrigin = LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
    title: str = ""
    source_label: str = ""
    revision_label: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    allowed_operations: frozenset[ProductOperation] = field(
        default_factory=lambda: frozenset({ProductOperation.CREATE})
    )
    limits: Mapping[str, int] = field(default_factory=dict, repr=False)
    policy_id: str = ""
    notice_version: str = ""
    expires_at_ms: int | None = None
    launch_grant_ttl_ms: int = 0
    maximum_remaining_ttl_ms: int | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LaunchSelectionActorSeed:
    """Private actor authority supplied by trusted service composition."""

    authority: str = field(repr=False)
    actor_id: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    expires_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class LaunchSelectionPolicy:
    """Hard process-local bounds for an explicitly constructed registry."""

    max_revisions: int = 64
    max_offerings: int = 64
    max_actor_authorities: int = 256
    max_active_launch_grants: int = 256
    max_idempotency_records: int = 512
    max_token_history: int = 1_024
    max_revision_bytes: int = 4 * 1024 * 1024
    max_launch_grant_ttl_ms: int = 10 * 60 * 1000
    idempotency_ttl_ms: int = 15 * 60 * 1000

    def __post_init__(self) -> None:
        for name in (
            "max_revisions",
            "max_offerings",
            "max_actor_authorities",
            "max_active_launch_grants",
            "max_idempotency_records",
            "max_token_history",
            "max_revision_bytes",
            "max_launch_grant_ttl_ms",
            "idempotency_ttl_ms",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a positive safe integer")
        if self.max_idempotency_records < self.max_active_launch_grants:
            raise ValueError("max_idempotency_records must cover active launch grants")
        if self.max_offerings > _MAX_CONTRACT_OFFERINGS:
            raise ValueError(
                "max_offerings cannot exceed the product contract option limit"
            )
        minimum_token_history = self.max_actor_authorities + self.max_offerings + 1
        if self.max_token_history < minimum_token_history:
            raise ValueError(
                "max_token_history must cover actors, offerings, and epoch"
            )
        if self.idempotency_ttl_ms <= self.max_launch_grant_ttl_ms:
            raise ValueError(
                "idempotency_ttl_ms must outlive the maximum launch grant TTL"
            )


class LaunchSelectionError(RuntimeError):
    """Stable, bounded error safe to translate at a private transport edge."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status: int,
        retry: Literal["none", "same_request", "after_delay"] = "none",
        retry_after_ms: int | None = None,
    ) -> None:
        if not _IDENTITY_PATTERN.fullmatch(code):
            raise ValueError("launch-selection error code is invalid")
        if not _is_safe_label(message, maximum=160):
            raise ValueError("launch-selection error message is invalid")
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 400 <= status <= 599
        ):
            raise ValueError("launch-selection error status is invalid")
        if retry not in {"none", "same_request", "after_delay"}:
            raise ValueError("launch-selection error retry category is invalid")
        if retry == "after_delay":
            if (
                isinstance(retry_after_ms, bool)
                or not isinstance(retry_after_ms, int)
                or not 1 <= retry_after_ms <= 60_000
            ):
                raise ValueError("after_delay errors require retry_after_ms")
        elif retry_after_ms is not None:
            raise ValueError("retry_after_ms is valid only for after_delay errors")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry
        self.retry_after_ms = retry_after_ms

    def to_problem(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retry": self.retry,
                "retry_after_ms": self.retry_after_ms,
            }
        }


class LaunchGrantProvisioningCapacityError(RuntimeError):
    """Trusted callback signal that adjacent provisioning has no capacity.

    A coordinator should translate its owning Stage capacity exception to this
    signal. The registry then publishes no grant and leaves the exchange
    idempotency key unclaimed.
    """


@dataclass(frozen=True, slots=True)
class LaunchGrantProvisioningReceipt:
    """Trusted proof of the Product launch binding installed for one grant.

    The digest is non-authorizing and is accepted only as the return value of
    an adjacent in-process provisioning callback.  Product computes it while
    constructing the launch-grant record; the launch registry never derives a
    second interpretation of that semantic binding.
    """

    launch_binding_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.launch_binding_sha256, str)
            or _LAUNCH_BINDING_SHA256_PATTERN.fullmatch(self.launch_binding_sha256)
            is None
        ):
            raise ValueError("launch provisioning binding digest is invalid")


class _CommittedProvisioningInterruption(BaseException):
    """Carry committed registry truth while preserving process interruption."""

    def __init__(self, result: LaunchGrantIssue, failure: BaseException) -> None:
        super().__init__()
        self.result = result
        self.failure = failure


@dataclass(frozen=True, slots=True)
class ImmutableLaunchRevision:
    """Canonical source-independent revision owned by the registry."""

    revision_id: str
    immutable_digest: str
    canonical_json: bytes = field(repr=False)

    def allocate(self) -> dict[str, object]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):  # Construction enforces this invariant.
            raise RuntimeError("immutable launch revision is not a JSON object")
        return value


@dataclass(frozen=True, slots=True)
class LaunchGrantIssue:
    """Private exchange result; launch authority is deliberately repr-hidden."""

    service_epoch: str
    launch_grant: str = field(repr=False)
    launch_selection_id: str = field(repr=False)
    selection_canonical_json: bytes = field(repr=False)
    offering_id: str = ""
    origin: LaunchSelectionOrigin = LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
    actor_id: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    allowed_operations: frozenset[ProductOperation] = field(default_factory=frozenset)
    limits: Mapping[str, int] = field(default_factory=dict)
    policy_id: str = ""
    notice_version: str = ""
    expires_at_ms: int = 0
    replayed: bool = False
    launch_binding_sha256: str | None = field(default=None, repr=False)
    _provisioning_committed: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def _prepare_provisioning_receipt(
        self,
        receipt: LaunchGrantProvisioningReceipt,
    ) -> None:
        """Validate and retain Product's receipt before it changes state.

        This is a trusted in-process commit marker, not a public mutation seam.
        Product invokes it before changing state, then marks the issue committed
        as its final mutation action. Retaining both facts lets the registry
        distinguish a prepared failure from a completed provisioning commit.
        """
        if not isinstance(receipt, LaunchGrantProvisioningReceipt):
            raise TypeError("launch provisioning receipt is invalid")
        binding = receipt.launch_binding_sha256
        if _LAUNCH_BINDING_SHA256_PATTERN.fullmatch(binding) is None:
            raise ValueError("launch provisioning receipt is invalid")
        existing = self.launch_binding_sha256
        if existing is not None and existing != binding:
            raise RuntimeError("launch provisioning receipt changed")
        object.__setattr__(self, "launch_binding_sha256", binding)

    def _mark_provisioning_committed(self) -> None:
        """Mark a prevalidated receipt committed without another failure seam."""
        object.__setattr__(self, "_provisioning_committed", True)

    def to_projection(self) -> dict[str, object]:
        """Return the bounded private exchange projection for the trusted BFF."""
        return {
            "kind": "presenter_stage_launch_selection_granted",
            "service_epoch": self.service_epoch,
            "launch_grant": self.launch_grant,
            "grant_expires_at": _format_timestamp(self.expires_at_ms),
            "selection": self.selection_projection(),
            "replayed": self.replayed,
        }

    def selection_projection(self) -> dict[str, object]:
        value = json.loads(self.selection_canonical_json)
        if not isinstance(value, dict):  # Construction enforces this invariant.
            raise RuntimeError("launch selection projection is not an object")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedLaunchSelection:
    """Trusted grant resolution result for a downstream launch coordinator."""

    service_epoch: str
    offering_id: str
    origin: LaunchSelectionOrigin
    actor_id: str
    role: ProductRole
    revision: ImmutableLaunchRevision
    allowed_operations: frozenset[ProductOperation]
    limits: Mapping[str, int]
    policy_id: str
    notice_version: str
    expires_at_ms: int
    expires_deadline_ms: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class _OfferingRecord:
    offering_id: str
    launch_selection_id: str = field(repr=False)
    launch_selection_sha256: str = field(repr=False)
    revision_id: str = ""
    origin: LaunchSelectionOrigin = LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
    title: str = ""
    source_label: str = ""
    revision_label: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    allowed_operations: frozenset[ProductOperation] = field(default_factory=frozenset)
    limits: Mapping[str, int] = field(default_factory=dict)
    policy_id: str = ""
    notice_version: str = ""
    expires_at_ms: int | None = None
    expires_deadline_ms: int | None = None
    launch_grant_ttl_ms: int = 0


@dataclass(frozen=True, slots=True)
class _TrustedOfferingRegistration:
    """Exact reversible state installed by one trusted registration."""

    offering: _OfferingRecord = field(repr=False)
    revision: ImmutableLaunchRevision = field(repr=False)
    installed_offering: bool = False
    installed_revision: bool = False


@dataclass(frozen=True, slots=True)
class _ActorRecord:
    actor_id: str
    role: ProductRole
    expires_at_ms: int
    expires_deadline_ms: int
    service_epoch: str

    @property
    def key(self) -> tuple[str, ProductRole]:
        return self.actor_id, self.role


@dataclass(frozen=True, slots=True)
class _GrantRecord:
    grant_sha256: str = field(repr=False)
    launch_selection_sha256: str = field(repr=False)
    offering_id: str = ""
    actor_id: str = ""
    role: ProductRole = ProductRole.ATTENDEE
    allowed_operations: frozenset[ProductOperation] = field(default_factory=frozenset)
    limits: Mapping[str, int] = field(default_factory=dict)
    policy_id: str = ""
    notice_version: str = ""
    expires_at_ms: int = 0
    expires_deadline_ms: int = 0
    service_epoch: str = ""


@dataclass(frozen=True, slots=True)
class _RecordedError:
    code: str
    message: str
    status: int
    retry: Literal["none", "same_request", "after_delay"]
    retry_after_ms: int | None

    @classmethod
    def capture(cls, error: LaunchSelectionError) -> _RecordedError:
        return cls(
            code=error.code,
            message=error.message,
            status=error.status,
            retry=error.retry,
            retry_after_ms=error.retry_after_ms,
        )

    def restore(self) -> LaunchSelectionError:
        return LaunchSelectionError(
            code=self.code,
            message=self.message,
            status=self.status,
            retry=self.retry,
            retry_after_ms=self.retry_after_ms,
        )


@dataclass(slots=True)
class _IdempotencyRecord:
    body_sha256: str = field(repr=False)
    retain_until_deadline_ms: int
    result: LaunchGrantIssue | None = field(default=None, repr=False)
    error: _RecordedError | None = None


class LaunchSelectionRegistry:
    """Bounded source-neutral option registry and private grant exchanger."""

    def __init__(
        self,
        *,
        revisions: Iterable[LaunchRevisionSeed] = (),
        offerings: Iterable[LaunchOfferingSeed] = (),
        actors: Iterable[LaunchSelectionActorSeed],
        policy: LaunchSelectionPolicy | None = None,
        allowed_operation_families: frozenset[ProductOperation] | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        token_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._policy = policy or LaunchSelectionPolicy()
        self._allowed_operation_families = allowed_operation_families or frozenset(
            {ProductOperation.CREATE}
        )
        if not self._allowed_operation_families or any(
            not isinstance(operation, ProductOperation)
            for operation in self._allowed_operation_families
        ):
            raise ValueError("allowed operation families are invalid")
        self._clock_ms = clock_ms or _system_clock_ms
        self._monotonic_ms = monotonic_ms or (
            clock_ms if clock_ms is not None else _system_monotonic_ms
        )
        self._token_factory = token_factory or _secure_token
        self._lock = threading.RLock()
        self._used_token_sha256s: set[str] = set()
        self._grants: dict[str, _GrantRecord] = {}
        self._inflight_launch_grants = 0

        revision_values = _bounded_values(
            revisions,
            maximum=self._policy.max_revisions,
            message="immutable launch revisions exceed configured capacity",
        )
        offering_values = _bounded_values(
            offerings,
            maximum=self._policy.max_offerings,
            message="launch offerings exceed configured capacity",
        )
        actor_values = _bounded_values(
            actors,
            maximum=self._policy.max_actor_authorities,
            message="launch-selection actors exceed configured capacity",
        )
        if not actor_values:
            raise ValueError("at least one launch-selection actor is required")

        started_at_ms = _require_timestamp(self._clock_ms(), "clock_ms")
        started_deadline_ms = _require_timestamp(self._monotonic_ms(), "monotonic_ms")
        self._started_at_ms = started_at_ms
        self._started_deadline_ms = started_deadline_ms
        self._last_deadline_ms = started_deadline_ms
        self._actors = self._build_actors(
            actor_values,
            now_ms=started_at_ms,
            deadline_ms=started_deadline_ms,
        )
        self._revisions: dict[str, ImmutableLaunchRevision] = {}
        for seed in revision_values:
            candidate = self._build_revision(seed)
            if candidate.revision_id in self._revisions:
                raise ValueError("immutable launch revision IDs must be unique")
            self._revisions[candidate.revision_id] = candidate
        self._offerings: dict[str, _OfferingRecord] = {}
        self._offering_by_selection_sha256: dict[str, _OfferingRecord] = {}
        self._display_by_revision: dict[str, tuple[str, str, str]] = {}
        for seed in offering_values:
            candidate = self._build_offering(
                seed,
                now_ms=started_at_ms,
                deadline_ms=started_deadline_ms,
                wall_ms=started_at_ms,
            )
            if candidate.offering_id in self._offerings:
                raise ValueError("launch offering IDs must be unique")
            if candidate.launch_selection_sha256 in self._offering_by_selection_sha256:
                raise ValueError("launch selection IDs must be unique")
            if candidate.revision_id not in self._revisions:
                raise ValueError("launch offering names an unknown revision")
            self._require_revision_origin(
                candidate,
                self._revisions[candidate.revision_id],
            )
            display = _offering_display(candidate)
            prior_display = self._display_by_revision.get(candidate.revision_id)
            if prior_display is not None and prior_display != display:
                raise ValueError(
                    "launch offerings for one revision must share display metadata"
                )
            self._reserve_supplied_token(
                candidate.launch_selection_sha256,
                message="launch selection ID collides with another opaque value",
            )
            self._offerings[candidate.offering_id] = candidate
            self._offering_by_selection_sha256[candidate.launch_selection_sha256] = (
                candidate
            )
            self._display_by_revision[candidate.revision_id] = display
        self._service_epoch = self._new_opaque_value("epoch")
        self._launch_grant_serial = 0
        self._actors = {
            digest: replace(actor, service_epoch=self._service_epoch)
            for digest, actor in self._actors.items()
        }
        self._idempotency: dict[
            tuple[str, tuple[str, ProductRole], str], _IdempotencyRecord
        ] = {}
        self._offering_idempotency: dict[
            tuple[str, tuple[str, ProductRole], str], _IdempotencyRecord
        ] = {}

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    def offerings(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        required_role: ProductRole | None = None,
    ) -> list[dict[str, object]]:
        """Authorize and return role-filtered, unexpired safe options."""
        with self._lock:
            self._require_epoch(expected_epoch)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            self._require_role_boundary(actor, required_role)
            return [
                self._offering_projection(offering)
                for offering in sorted(
                    self._offerings.values(), key=lambda value: value.offering_id
                )
                if not self._offering_expired(
                    offering,
                    deadline_ms=deadline_ms,
                    wall_ms=wall_ms,
                )
                and offering.role is actor.role
            ]

    def offering(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        offering_id: str,
    ) -> dict[str, object]:
        """Authorize one role-bound public navigation metadata lookup."""
        with self._lock:
            self._require_epoch(expected_epoch)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            offering = self._offerings.get(offering_id)
            if (
                offering is None
                or self._offering_expired(
                    offering,
                    deadline_ms=deadline_ms,
                    wall_ms=wall_ms,
                )
                or offering.role is not actor.role
            ):
                raise _offering_unavailable()
            return self._offering_projection(offering)

    def catalog_offerings(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        required_role: ProductRole | None = None,
    ) -> list[dict[str, object]]:
        """Return role-bound safe offerings without private selection locators."""
        with self._lock:
            self._require_epoch(expected_epoch)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            self._require_role_boundary(actor, required_role)
            return [
                self._catalog_offering_projection(offering)
                for offering in sorted(
                    self._offerings.values(), key=lambda value: value.offering_id
                )
                if not self._offering_expired(
                    offering,
                    deadline_ms=deadline_ms,
                    wall_ms=wall_ms,
                )
                and offering.role is actor.role
            ]

    def register_revision(self, seed: LaunchRevisionSeed) -> ImmutableLaunchRevision:
        """Register or replay one exact trusted immutable revision."""
        candidate = self._build_revision(seed)
        with self._lock:
            existing = self._revisions.get(candidate.revision_id)
            if existing is not None:
                if (
                    existing.immutable_digest != candidate.immutable_digest
                    or existing.canonical_json != candidate.canonical_json
                ):
                    raise _registration_conflict()
                return existing
            if len(self._revisions) >= self._policy.max_revisions:
                raise _registry_capacity()
            self._revisions[candidate.revision_id] = candidate
            return candidate

    def register_offering(self, seed: LaunchOfferingSeed) -> dict[str, object]:
        """Register or replay one trusted option and selection locator."""
        with self._lock:
            now_ms, deadline_ms, wall_ms = self._now()
            candidate = self._build_offering(
                seed,
                now_ms=now_ms,
                deadline_ms=deadline_ms,
                wall_ms=wall_ms,
            )
            if candidate.revision_id not in self._revisions:
                raise _error(
                    "revision_unavailable",
                    "The requested immutable launch revision is unavailable.",
                    status=404,
                )
            self._require_revision_origin(
                candidate,
                self._revisions[candidate.revision_id],
            )
            existing = self._offerings.get(candidate.offering_id)
            selected = self._offering_by_selection_sha256.get(
                candidate.launch_selection_sha256
            )
            if existing is not None:
                if not _same_offering(existing, candidate):
                    raise _registration_conflict()
                return self._offering_projection(existing)
            if selected is not None:
                raise _registration_conflict()
            self._require_consistent_revision_display(candidate)
            if len(self._offerings) >= self._policy.max_offerings:
                raise _registry_capacity()
            self._reserve_supplied_token(
                candidate.launch_selection_sha256,
                message="launch selection ID collides with another opaque value",
            )
            self._offerings[candidate.offering_id] = candidate
            self._offering_by_selection_sha256[candidate.launch_selection_sha256] = (
                candidate
            )
            self._display_by_revision[candidate.revision_id] = _offering_display(
                candidate
            )
            return self._offering_projection(candidate)

    def register_trusted_offering(
        self,
        *,
        revision: LaunchRevisionSeed,
        offering: LaunchOfferingSeed,
    ) -> dict[str, object]:
        """Atomically install an exact revision and its safe option."""
        result, _registration = self._register_trusted_offering_reversible(
            revision=revision,
            offering=offering,
        )
        return result

    def _register_trusted_offering_reversible(
        self,
        *,
        revision: LaunchRevisionSeed,
        offering: LaunchOfferingSeed,
    ) -> tuple[dict[str, object], _TrustedOfferingRegistration]:
        """Install trusted state and return an internal exact rollback receipt."""
        revision_candidate = self._build_revision(revision)
        with self._lock:
            now_ms, deadline_ms, wall_ms = self._now()
            offering_candidate = self._build_offering(
                offering,
                now_ms=now_ms,
                deadline_ms=deadline_ms,
                wall_ms=wall_ms,
            )
            if offering_candidate.revision_id != revision_candidate.revision_id:
                raise ValueError("trusted offering and revision IDs must match")
            self._require_revision_origin(offering_candidate, revision_candidate)
            existing_revision = self._revisions.get(revision_candidate.revision_id)
            if existing_revision is not None and (
                existing_revision.immutable_digest
                != revision_candidate.immutable_digest
                or existing_revision.canonical_json != revision_candidate.canonical_json
            ):
                raise _registration_conflict()
            existing_offering = self._offerings.get(offering_candidate.offering_id)
            selected = self._offering_by_selection_sha256.get(
                offering_candidate.launch_selection_sha256
            )
            if existing_offering is not None:
                if not _same_offering(existing_offering, offering_candidate):
                    raise _registration_conflict()
                if existing_revision is None:  # Existing offering enforces this.
                    raise RuntimeError("trusted offering revision disappeared")
                return self._offering_projection(existing_offering), (
                    _TrustedOfferingRegistration(
                        offering=existing_offering,
                        revision=existing_revision,
                    )
                )
            if selected is not None:
                raise _registration_conflict()
            self._require_consistent_revision_display(offering_candidate)
            if (
                existing_revision is None
                and len(self._revisions) >= self._policy.max_revisions
            ) or len(self._offerings) >= self._policy.max_offerings:
                raise _registry_capacity()
            self._reserve_supplied_token(
                offering_candidate.launch_selection_sha256,
                message="launch selection ID collides with another opaque value",
            )
            if existing_revision is None:
                self._revisions[revision_candidate.revision_id] = revision_candidate
            self._offerings[offering_candidate.offering_id] = offering_candidate
            self._offering_by_selection_sha256[
                offering_candidate.launch_selection_sha256
            ] = offering_candidate
            self._display_by_revision[offering_candidate.revision_id] = (
                _offering_display(offering_candidate)
            )
            return self._offering_projection(offering_candidate), (
                _TrustedOfferingRegistration(
                    offering=offering_candidate,
                    revision=(
                        revision_candidate
                        if existing_revision is None
                        else existing_revision
                    ),
                    installed_offering=True,
                    installed_revision=existing_revision is None,
                )
            )

    def _rollback_trusted_offering_registration(
        self,
        registration: _TrustedOfferingRegistration,
    ) -> None:
        """Undo only state installed by an uncommitted trusted publication."""
        if not isinstance(registration, _TrustedOfferingRegistration):
            raise TypeError("trusted offering registration is invalid")
        if not registration.installed_offering:
            return
        with self._lock:
            offering = self._offerings.get(registration.offering.offering_id)
            if offering is None:
                return
            if offering is not registration.offering:
                raise RuntimeError("trusted offering changed before rollback")
            if any(
                grant.offering_id == offering.offering_id
                for grant in self._grants.values()
            ):
                raise RuntimeError("trusted offering was granted before rollback")
            selected = self._offering_by_selection_sha256.get(
                offering.launch_selection_sha256
            )
            if selected is not offering:
                raise RuntimeError("trusted offering selection index changed")
            remaining = tuple(
                candidate
                for candidate in self._offerings.values()
                if candidate is not offering
                and candidate.revision_id == offering.revision_id
            )
            if registration.installed_revision:
                revision = self._revisions.get(registration.revision.revision_id)
                if revision is not registration.revision:
                    raise RuntimeError("trusted revision changed before rollback")
            self._offerings.pop(offering.offering_id)
            self._offering_by_selection_sha256.pop(offering.launch_selection_sha256)
            self._used_token_sha256s.discard(offering.launch_selection_sha256)
            if remaining:
                self._display_by_revision[offering.revision_id] = _offering_display(
                    remaining[0]
                )
            else:
                self._display_by_revision.pop(offering.revision_id, None)
            if registration.installed_revision and not remaining:
                self._revisions.pop(registration.revision.revision_id)

    def exchange_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        launch_selection_id: str,
        idempotency_key: str,
        provision: Callable[
            [LaunchGrantIssue, ResolvedLaunchSelection],
            LaunchGrantProvisioningReceipt | None,
        ],
        required_role: ProductRole | None = None,
        require_launch_binding_receipt: bool = False,
    ) -> LaunchGrantIssue:
        """Provision and exchange one locator for a private grant atomically.

        ``provision`` is a trusted, bounded in-process callback. It runs before
        the grant or idempotent result becomes visible and must return only once
        adjacent Stage provisioning is committed. It must translate retryable
        Stage capacity to :class:`LaunchGrantProvisioningCapacityError`.
        """
        if not callable(provision):
            raise TypeError("provision must be callable")
        if not isinstance(require_launch_binding_receipt, bool):
            raise TypeError("require_launch_binding_receipt must be boolean")
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            self._require_role_boundary(actor, required_role)
            self._expire_and_prune(deadline_ms, wall_ms)
            selection_sha256, key = _validate_exchange_request(
                launch_selection_id=launch_selection_id,
                idempotency_key=idempotency_key,
            )
            body_sha256 = _canonical_sha256(
                {"launch_selection_sha256": selection_sha256}
            )
            scope = (self._service_epoch, actor.key, key)
            existing = self._idempotency.get(scope)
            if existing is not None:
                if existing.body_sha256 != body_sha256:
                    raise _error(
                        "idempotency_conflict",
                        "The idempotency key is already bound to another request.",
                        status=409,
                    )
                if existing.error is not None:
                    raise existing.error.restore()
                if existing.result is None:  # Locking makes this unreachable.
                    raise RuntimeError("launch-selection exchange replay is incomplete")
                return replace(existing.result, replayed=True)
            if (
                len(self._idempotency) + len(self._offering_idempotency)
                >= self._policy.max_idempotency_records
            ):
                raise _capacity_error()
            claim = _IdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=deadline_ms + self._policy.idempotency_ttl_ms,
            )
            self._idempotency[scope] = claim
            try:
                result = self._exchange_locked(
                    actor=actor,
                    selection_sha256=selection_sha256,
                    deadline_ms=deadline_ms,
                    wall_ms=wall_ms,
                    provision=provision,
                    require_launch_binding_receipt=require_launch_binding_receipt,
                )
            except LaunchSelectionError as error:
                if error.retry == "after_delay":
                    # Explicit pre-work capacity failures never claim the key.
                    self._idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedError.capture(error)
                raise
            except _CommittedProvisioningInterruption as interruption:
                # Product and registry truth are already committed. Preserve the
                # exact recovery result, but never consume process-control
                # interruption on behalf of the caller.
                claim.result = interruption.result
                raise interruption.failure from None
            except BaseException:
                self._idempotency.pop(scope, None)
                raise
            claim.result = result
            return result

    def exchange_offering(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        offering_id: str,
        idempotency_key: str,
        provision: Callable[
            [LaunchGrantIssue, ResolvedLaunchSelection],
            LaunchGrantProvisioningReceipt | None,
        ],
        required_role: ProductRole | None = None,
        require_launch_binding_receipt: bool = False,
    ) -> LaunchGrantIssue:
        """Exchange a safe offering locator without revealing its selection ID.

        The offering identifier remains non-authorizing: a current private actor,
        service epoch, idempotency claim, role match, and successful adjacent
        provisioning are still required before a launch grant becomes visible.
        This additive operation has an idempotency family independent from the
        existing selection-ID exchange used by published Product contracts.
        """
        if not callable(provision):
            raise TypeError("provision must be callable")
        if not isinstance(require_launch_binding_receipt, bool):
            raise TypeError("require_launch_binding_receipt must be boolean")
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            self._require_role_boundary(actor, required_role)
            self._expire_and_prune(deadline_ms, wall_ms)
            locator, key = _validate_offering_exchange_request(
                offering_id=offering_id,
                idempotency_key=idempotency_key,
            )
            body_sha256 = _canonical_sha256({"offering_id": locator})
            scope = (self._service_epoch, actor.key, key)
            existing = self._offering_idempotency.get(scope)
            if existing is not None:
                if existing.body_sha256 != body_sha256:
                    raise _error(
                        "idempotency_conflict",
                        "The idempotency key is already bound to another request.",
                        status=409,
                    )
                if existing.error is not None:
                    raise existing.error.restore()
                if existing.result is None:  # Locking makes this unreachable.
                    raise RuntimeError("offering exchange replay is incomplete")
                return replace(existing.result, replayed=True)
            if (
                len(self._idempotency) + len(self._offering_idempotency)
                >= self._policy.max_idempotency_records
            ):
                raise _capacity_error()
            claim = _IdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=deadline_ms + self._policy.idempotency_ttl_ms,
            )
            self._offering_idempotency[scope] = claim
            try:
                offering = self._offerings.get(locator)
                if (
                    offering is None
                    or offering.role is not actor.role
                    or self._offering_expired(
                        offering,
                        deadline_ms=deadline_ms,
                        wall_ms=wall_ms,
                    )
                ):
                    raise _selection_unavailable()
                result = self._exchange_locked(
                    actor=actor,
                    selection_sha256=offering.launch_selection_sha256,
                    deadline_ms=deadline_ms,
                    wall_ms=wall_ms,
                    provision=provision,
                    require_launch_binding_receipt=require_launch_binding_receipt,
                )
            except LaunchSelectionError as error:
                if error.retry == "after_delay":
                    self._offering_idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedError.capture(error)
                raise
            except _CommittedProvisioningInterruption as interruption:
                claim.result = interruption.result
                raise interruption.failure from None
            except BaseException:
                self._offering_idempotency.pop(scope, None)
                raise
            claim.result = result
            return result

    def resolve_launch_grant(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        launch_grant: str,
        operation: ProductOperation,
    ) -> ResolvedLaunchSelection:
        """Resolve only private grant authority; no path/state input exists."""
        with self._lock:
            self._require_epoch(expected_epoch)
            _now_ms, deadline_ms, wall_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms, wall_ms)
            self._expire_and_prune(deadline_ms, wall_ms)
            if not isinstance(operation, ProductOperation):
                raise _request_invalid()
            grant = self._grants.get(_credential_sha256(launch_grant))
            if (
                grant is None
                or grant.service_epoch != self._service_epoch
                or (grant.actor_id, grant.role) != actor.key
            ):
                raise _grant_unavailable()
            if operation not in grant.allowed_operations:
                raise _error(
                    "operation_denied",
                    "The requested launch operation is unavailable.",
                    status=403,
                )
            offering = self._offerings.get(grant.offering_id)
            if (
                offering is None
                or offering.launch_selection_sha256 != grant.launch_selection_sha256
            ):
                raise RuntimeError("resolved launch offering is missing")
            revision = self._revisions.get(offering.revision_id)
            if revision is None:
                raise RuntimeError("resolved immutable launch revision is missing")
            return ResolvedLaunchSelection(
                service_epoch=self._service_epoch,
                offering_id=offering.offering_id,
                origin=offering.origin,
                actor_id=actor.actor_id,
                role=actor.role,
                revision=revision,
                allowed_operations=grant.allowed_operations,
                limits=grant.limits,
                policy_id=grant.policy_id,
                notice_version=grant.notice_version,
                expires_at_ms=grant.expires_at_ms,
                expires_deadline_ms=grant.expires_deadline_ms,
            )

    def _exchange_locked(
        self,
        *,
        actor: _ActorRecord,
        selection_sha256: str,
        deadline_ms: int,
        wall_ms: int,
        provision: Callable[
            [LaunchGrantIssue, ResolvedLaunchSelection],
            LaunchGrantProvisioningReceipt | None,
        ],
        require_launch_binding_receipt: bool,
    ) -> LaunchGrantIssue:
        offering = self._offering_by_selection_sha256.get(selection_sha256)
        if (
            offering is None
            or self._offering_expired(
                offering,
                deadline_ms=deadline_ms,
                wall_ms=wall_ms,
            )
            or offering.role is not actor.role
        ):
            raise _selection_unavailable()
        if (
            len(self._grants) + self._inflight_launch_grants
            >= self._policy.max_active_launch_grants
        ):
            raise _capacity_error()
        grant_value, grant_sha256 = self._new_launch_grant()
        ttl_candidates = [
            offering.launch_grant_ttl_ms,
            self._policy.max_launch_grant_ttl_ms,
            actor.expires_deadline_ms - deadline_ms,
            actor.expires_at_ms - wall_ms,
        ]
        if offering.expires_at_ms is not None:
            ttl_candidates.append(offering.expires_at_ms - wall_ms)
        if offering.expires_deadline_ms is not None:
            ttl_candidates.append(offering.expires_deadline_ms - deadline_ms)
        ttl_ms = min(ttl_candidates)
        if ttl_ms <= 0:
            raise _selection_unavailable()
        expires_at_ms = wall_ms + ttl_ms
        grant = _GrantRecord(
            grant_sha256=grant_sha256,
            launch_selection_sha256=selection_sha256,
            offering_id=offering.offering_id,
            actor_id=actor.actor_id,
            role=actor.role,
            allowed_operations=offering.allowed_operations,
            limits=offering.limits,
            policy_id=offering.policy_id,
            notice_version=offering.notice_version,
            expires_at_ms=expires_at_ms,
            expires_deadline_ms=deadline_ms + ttl_ms,
            service_epoch=self._service_epoch,
        )
        issue = LaunchGrantIssue(
            service_epoch=self._service_epoch,
            launch_grant=grant_value,
            launch_selection_id=offering.launch_selection_id,
            selection_canonical_json=_canonical_json_bytes(
                self._offering_projection(offering)
            ),
            offering_id=offering.offering_id,
            origin=offering.origin,
            actor_id=actor.actor_id,
            role=actor.role,
            allowed_operations=offering.allowed_operations,
            limits=offering.limits,
            policy_id=offering.policy_id,
            notice_version=offering.notice_version,
            expires_at_ms=expires_at_ms,
        )
        revision = self._revisions.get(offering.revision_id)
        if revision is None:
            raise RuntimeError("launch offering revision is missing")
        resolved = ResolvedLaunchSelection(
            service_epoch=self._service_epoch,
            offering_id=offering.offering_id,
            origin=offering.origin,
            actor_id=actor.actor_id,
            role=actor.role,
            revision=revision,
            allowed_operations=offering.allowed_operations,
            limits=offering.limits,
            policy_id=offering.policy_id,
            notice_version=offering.notice_version,
            expires_at_ms=expires_at_ms,
            expires_deadline_ms=deadline_ms + ttl_ms,
        )
        self._inflight_launch_grants += 1
        receipt = None
        interruption: BaseException | None = None
        try:
            try:
                receipt = provision(issue, resolved)
            except BaseException as failure:
                if not (
                    require_launch_binding_receipt
                    and issue._provisioning_committed
                    and _valid_launch_binding_sha256(issue.launch_binding_sha256)
                ):
                    if isinstance(failure, LaunchGrantProvisioningCapacityError):
                        # The unpublished credential owns a burned serial but no
                        # retained digest. A retry therefore receives a distinct
                        # final credential.
                        raise _capacity_error() from None
                    raise
                # Product recorded this issue only after committing its grant.
                # Commit truth wins over an enclosing callback failure so the
                # registry cannot strand a usable Product grant unpublished.
                if isinstance(failure, Exception):
                    _LIFECYCLE_LOGGER.error(
                        "launch_selection_provision_callback_failed_after_commit "
                        "failure_type=%s",
                        type(failure).__name__,
                    )
                else:
                    interruption = failure
        finally:
            self._inflight_launch_grants -= 1
        recorded_binding = issue.launch_binding_sha256
        if issue._provisioning_committed and _valid_launch_binding_sha256(
            recorded_binding
        ):
            # The core-owned commit marker is authoritative. An enclosing
            # callback cannot create split truth by returning another value.
            pass
        elif require_launch_binding_receipt:
            if isinstance(receipt, LaunchGrantProvisioningReceipt):
                raise RuntimeError("launch provisioning commit is missing")
            if receipt is None:
                raise RuntimeError("launch provisioning receipt is missing")
            raise RuntimeError("launch provisioning receipt is invalid")
        elif isinstance(receipt, LaunchGrantProvisioningReceipt):
            # Optional legacy coordinators can return a validated receipt even
            # when their owning service does not yet record the commit marker.
            issue = replace(
                issue,
                launch_binding_sha256=receipt.launch_binding_sha256,
            )
        elif receipt is not None:
            raise RuntimeError("launch provisioning receipt is invalid")
        self._grants[grant_sha256] = grant
        if interruption is not None:
            raise _CommittedProvisioningInterruption(issue, interruption)
        return issue

    def _require_consistent_revision_display(self, candidate: _OfferingRecord) -> None:
        prior = self._display_by_revision.get(candidate.revision_id)
        if prior is not None and prior != _offering_display(candidate):
            raise _registration_conflict()

    @staticmethod
    def _require_revision_origin(
        offering: _OfferingRecord,
        revision: ImmutableLaunchRevision,
    ) -> None:
        expected_kind = {
            LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT: (
                "dreamland.authored_world_revision"
            ),
            LaunchSelectionOrigin.WORLD_FORGE_SELECTION: (
                "dreamland.world_forge_revision"
            ),
        }[offering.origin]
        if revision.allocate().get("kind") != expected_kind:
            raise ValueError("launch offering origin does not match its revision")

    def _build_revision(self, seed: LaunchRevisionSeed) -> ImmutableLaunchRevision:
        if not isinstance(seed, LaunchRevisionSeed):
            raise TypeError("revision must be LaunchRevisionSeed")
        _require_identity(seed.revision_id, "revision_id")
        if not isinstance(seed.document, Mapping):
            raise ValueError("immutable launch revision must be a JSON object")
        try:
            detached = deepcopy(dict(seed.document))
            _require_string_json_keys(detached)
            canonical = (
                json.dumps(
                    detached,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError) as error:
            raise ValueError(
                "immutable launch revision must contain only JSON values"
            ) from error
        if len(canonical) > self._policy.max_revision_bytes:
            raise ValueError("immutable launch revision exceeds configured size")
        parsed = json.loads(canonical)
        if not isinstance(parsed, dict):
            raise ValueError("immutable launch revision must be a JSON object")
        digest = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        if seed.revision_id != f"world-revision:{digest}":
            raise ValueError(
                "immutable launch revision identity does not match content"
            )
        return ImmutableLaunchRevision(
            revision_id=seed.revision_id,
            immutable_digest=digest,
            canonical_json=canonical,
        )

    def _build_offering(
        self,
        seed: LaunchOfferingSeed,
        *,
        now_ms: int,
        deadline_ms: int,
        wall_ms: int,
    ) -> _OfferingRecord:
        if not isinstance(seed, LaunchOfferingSeed):
            raise TypeError("offering must be LaunchOfferingSeed")
        _require_offering_id(seed.offering_id)
        _require_launch_selection_id(seed.launch_selection_id)
        _require_identity(seed.revision_id, "revision_id")
        if not isinstance(seed.origin, LaunchSelectionOrigin):
            raise ValueError("launch offering origin is invalid")
        if not _is_safe_label(seed.title, maximum=120):
            raise ValueError("offering title must be a bounded display-safe label")
        if not _is_safe_label(seed.source_label, maximum=64):
            raise ValueError("source label must be a bounded display-safe label")
        if not _is_safe_label(seed.revision_label, maximum=96):
            raise ValueError("revision label must be a bounded display-safe label")
        if not isinstance(seed.role, ProductRole):
            raise ValueError("launch offering role is invalid")
        operations = _safe_operations(
            seed.allowed_operations,
            allowed_families=self._allowed_operation_families,
        )
        if ProductOperation.CREATE not in operations:
            raise ValueError("launch offering must allow session.create")
        limits = _safe_limits(seed.limits)
        _require_identity(seed.policy_id, "policy_id")
        _require_identity(seed.notice_version, "notice_version")
        expires_at_ms = (
            None
            if seed.expires_at_ms is None
            else _require_timestamp(seed.expires_at_ms, "offering expiry")
        )
        maximum_remaining_ttl_ms = seed.maximum_remaining_ttl_ms
        if maximum_remaining_ttl_ms is not None and (
            isinstance(maximum_remaining_ttl_ms, bool)
            or not isinstance(maximum_remaining_ttl_ms, int)
            or maximum_remaining_ttl_ms <= 0
            or expires_at_ms is None
        ):
            raise ValueError("launch offering remaining TTL is invalid")
        if maximum_remaining_ttl_ms is not None and (
            expires_at_ms is not None
            and (expires_at_ms <= now_ms or expires_at_ms <= wall_ms)
        ):
            raise ValueError("launch offering is already expired")
        grant_ttl_ms = _require_timestamp(seed.launch_grant_ttl_ms, "launch grant TTL")
        if grant_ttl_ms <= 0 or grant_ttl_ms > self._policy.max_launch_grant_ttl_ms:
            raise ValueError("launch grant TTL exceeds configured policy")
        return _OfferingRecord(
            offering_id=seed.offering_id,
            launch_selection_id=seed.launch_selection_id,
            launch_selection_sha256=_credential_sha256(seed.launch_selection_id),
            revision_id=seed.revision_id,
            origin=seed.origin,
            title=seed.title,
            source_label=seed.source_label,
            revision_label=seed.revision_label,
            role=seed.role,
            allowed_operations=operations,
            limits=limits,
            policy_id=seed.policy_id,
            notice_version=seed.notice_version,
            expires_at_ms=expires_at_ms,
            expires_deadline_ms=(
                None
                if expires_at_ms is None
                else deadline_ms
                + min(
                    expires_at_ms - now_ms,
                    (
                        maximum_remaining_ttl_ms
                        if maximum_remaining_ttl_ms is not None
                        else _MAX_SAFE_INTEGER
                    ),
                )
            ),
            launch_grant_ttl_ms=grant_ttl_ms,
        )

    def _build_actors(
        self,
        seeds: tuple[LaunchSelectionActorSeed, ...],
        *,
        now_ms: int,
        deadline_ms: int,
    ) -> dict[str, _ActorRecord]:
        actors: dict[str, _ActorRecord] = {}
        actor_keys: set[tuple[str, ProductRole]] = set()
        for seed in seeds:
            if not isinstance(seed, LaunchSelectionActorSeed):
                raise TypeError("actor must be LaunchSelectionActorSeed")
            _require_opaque(seed.authority, "actor authority")
            _require_identity(seed.actor_id, "actor_id")
            if not isinstance(seed.role, ProductRole):
                raise ValueError("actor role is invalid")
            expires_at_ms = _require_timestamp(seed.expires_at_ms, "actor expiry")
            digest = _credential_sha256(seed.authority)
            if digest in actors or (seed.actor_id, seed.role) in actor_keys:
                raise ValueError("launch-selection actor authorities must be unique")
            self._reserve_supplied_token(
                digest, message="launch-selection authority token collision"
            )
            actor_keys.add((seed.actor_id, seed.role))
            actors[digest] = _ActorRecord(
                actor_id=seed.actor_id,
                role=seed.role,
                expires_at_ms=expires_at_ms,
                expires_deadline_ms=deadline_ms + max(0, expires_at_ms - now_ms),
                service_epoch="",
            )
        return actors

    def _offering_projection(self, offering: _OfferingRecord) -> dict[str, object]:
        revision = self._revisions.get(offering.revision_id)
        if revision is None:
            raise RuntimeError("launch offering revision is missing")
        return {
            "offering_id": offering.offering_id,
            "launch_selection_id": offering.launch_selection_id,
            "origin": {"kind": offering.origin.value},
            "display": {
                "title": offering.title,
                "source_label": offering.source_label,
                "revision_label": offering.revision_label,
            },
            "revision": {
                "revision_id": revision.revision_id,
                "sha256": revision.immutable_digest.removeprefix("sha256:"),
            },
            "expires_at": _format_timestamp(offering.expires_at_ms),
        }

    def _catalog_offering_projection(
        self, offering: _OfferingRecord
    ) -> dict[str, object]:
        projection = self._offering_projection(offering)
        projection.pop("launch_selection_id")
        projection["notice_version"] = offering.notice_version
        return projection

    def _require_epoch(
        self, expected_epoch: str | None, *, mutation: bool = False
    ) -> None:
        if not isinstance(expected_epoch, str) or not expected_epoch:
            raise _error(
                "expected_epoch_required",
                "Expected launch-selection service epoch evidence is required.",
                status=400,
            )
        if expected_epoch != self._service_epoch:
            if mutation:
                raise _error(
                    "outcome_unknown",
                    "The launch-selection exchange outcome cannot be reconciled.",
                    status=410,
                )
            raise _error(
                "service_epoch_changed",
                "The launch-selection service epoch has changed.",
                status=410,
            )

    def _require_actor(
        self,
        actor_authority: str,
        deadline_ms: int,
        wall_ms: int,
    ) -> _ActorRecord:
        actor = self._actors.get(_credential_sha256(actor_authority))
        if (
            actor is None
            or actor.service_epoch != self._service_epoch
            or deadline_ms >= actor.expires_deadline_ms
            or wall_ms >= actor.expires_at_ms
        ):
            raise _error(
                "authority_denied",
                "The requested launch-selection resource is unavailable.",
                status=403,
            )
        return actor

    @staticmethod
    def _require_role_boundary(
        actor: _ActorRecord,
        required_role: ProductRole | None,
    ) -> None:
        if required_role is None:
            return
        if not isinstance(required_role, ProductRole):
            raise ValueError("required launch-selection role is invalid")
        if actor.role is not required_role:
            raise _error(
                "authority_denied",
                "The requested launch-selection resource is unavailable.",
                status=403,
            )

    @staticmethod
    def _offering_expired(
        offering: _OfferingRecord,
        *,
        deadline_ms: int,
        wall_ms: int,
    ) -> bool:
        return (
            offering.expires_deadline_ms is not None
            and deadline_ms >= offering.expires_deadline_ms
        ) or (offering.expires_at_ms is not None and wall_ms >= offering.expires_at_ms)

    def _expire_and_prune(self, deadline_ms: int, wall_ms: int) -> None:
        expired_grants = [
            digest
            for digest, grant in self._grants.items()
            if deadline_ms >= grant.expires_deadline_ms
            or wall_ms >= grant.expires_at_ms
        ]
        for digest in expired_grants:
            self._grants.pop(digest, None)
        expired_idempotency = [
            scope
            for scope, record in self._idempotency.items()
            if deadline_ms >= record.retain_until_deadline_ms
        ]
        for scope in expired_idempotency:
            self._idempotency.pop(scope, None)
        expired_offering_idempotency = [
            scope
            for scope, record in self._offering_idempotency.items()
            if deadline_ms >= record.retain_until_deadline_ms
        ]
        for scope in expired_offering_idempotency:
            self._offering_idempotency.pop(scope, None)

    def _new_opaque_value(self, purpose: str) -> str:
        if purpose != "epoch":
            raise RuntimeError("unsupported launch-selection token purpose")
        if len(self._used_token_sha256s) >= self._policy.max_token_history:
            raise _capacity_error()
        for _attempt in range(_MAX_TOKEN_ATTEMPTS):
            value = self._token_factory(purpose)
            try:
                _require_service_epoch(value)
            except ValueError:
                continue
            digest = _credential_sha256(value)
            if digest in self._used_token_sha256s:
                continue
            self._used_token_sha256s.add(digest)
            return value
        raise _capacity_error()

    def _new_launch_grant(self) -> tuple[str, str]:
        """Derive a never-repeated epoch-local credential from private entropy."""
        for _attempt in range(_MAX_TOKEN_ATTEMPTS):
            if self._launch_grant_serial >= _MAX_LAUNCH_GRANT_SERIAL:
                raise RuntimeError("launch grant issuance serial is exhausted")
            candidate = self._token_factory("launch-grant")
            try:
                _require_launch_grant(candidate)
            except ValueError:
                continue
            serial = self._launch_grant_serial + 1
            value = _derive_launch_grant(
                service_epoch=self._service_epoch,
                serial=serial,
                candidate=candidate,
            )
            # Once a final candidate exists its serial is never reused, even if
            # collision defense or adjacent provisioning rejects publication.
            self._launch_grant_serial = serial
            digest = _credential_sha256(value)
            if (
                value == candidate
                or digest in self._used_token_sha256s
                or digest in self._grants
            ):
                continue
            return value, digest
        raise _capacity_error()

    def _reserve_supplied_token(self, digest: str, *, message: str) -> None:
        if digest in self._used_token_sha256s or digest in self._grants:
            raise ValueError(message)
        if len(self._used_token_sha256s) >= self._policy.max_token_history:
            raise ValueError("opaque token history exceeds configured capacity")
        self._used_token_sha256s.add(digest)

    def _now(self) -> tuple[int, int, int]:
        wall_ms = _require_timestamp(self._clock_ms(), "clock_ms")
        deadline_ms = _require_timestamp(self._monotonic_ms(), "monotonic_ms")
        if deadline_ms < self._last_deadline_ms:
            raise RuntimeError("launch-selection monotonic clock moved backwards")
        self._last_deadline_ms = deadline_ms
        projected_now_ms = self._started_at_ms + (
            deadline_ms - self._started_deadline_ms
        )
        return (
            _require_timestamp(projected_now_ms, "projected clock_ms"),
            deadline_ms,
            wall_ms,
        )


def _offering_display(offering: _OfferingRecord) -> tuple[str, str, str]:
    return offering.title, offering.source_label, offering.revision_label


def _same_offering(left: _OfferingRecord, right: _OfferingRecord) -> bool:
    return (
        left.offering_id == right.offering_id
        and left.launch_selection_id == right.launch_selection_id
        and left.launch_selection_sha256 == right.launch_selection_sha256
        and left.revision_id == right.revision_id
        and left.origin is right.origin
        and left.title == right.title
        and left.source_label == right.source_label
        and left.revision_label == right.revision_label
        and left.role is right.role
        and left.allowed_operations == right.allowed_operations
        and left.limits == right.limits
        and left.policy_id == right.policy_id
        and left.notice_version == right.notice_version
        and left.expires_at_ms == right.expires_at_ms
        and left.launch_grant_ttl_ms == right.launch_grant_ttl_ms
    )


def _validate_exchange_request(
    *, launch_selection_id: object, idempotency_key: object
) -> tuple[str, str]:
    try:
        _require_launch_selection_id(launch_selection_id)
        if not isinstance(idempotency_key, str) or not (
            1 <= len(idempotency_key) <= _MAX_IDEMPOTENCY_KEY_CHARACTERS
        ):
            raise ValueError("idempotency key is invalid")
        if any(not 0x21 <= ord(character) <= 0x7E for character in idempotency_key):
            raise ValueError("idempotency key is invalid")
    except (TypeError, ValueError):
        raise _request_invalid() from None
    return _credential_sha256(launch_selection_id), idempotency_key


def _validate_offering_exchange_request(
    *, offering_id: object, idempotency_key: object
) -> tuple[str, str]:
    try:
        if not isinstance(offering_id, str):
            raise ValueError("offering ID is invalid")
        _require_offering_id(offering_id)
        if not isinstance(idempotency_key, str) or not (
            1 <= len(idempotency_key) <= _MAX_IDEMPOTENCY_KEY_CHARACTERS
        ):
            raise ValueError("idempotency key is invalid")
        if any(not 0x21 <= ord(character) <= 0x7E for character in idempotency_key):
            raise ValueError("idempotency key is invalid")
    except (TypeError, ValueError):
        raise _request_invalid() from None
    return offering_id, idempotency_key


def _safe_operations(
    values: Iterable[ProductOperation],
    *,
    allowed_families: frozenset[ProductOperation],
) -> frozenset[ProductOperation]:
    try:
        operations = tuple(islice(values, len(ProductOperation) + 1))
    except TypeError as error:
        raise ValueError("launch offering operations are invalid") from error
    if (
        not operations
        or len(operations) > len(ProductOperation)
        or any(not isinstance(operation, ProductOperation) for operation in operations)
        or len(frozenset(operations)) != len(operations)
        or not frozenset(operations).issubset(allowed_families)
    ):
        raise ValueError("launch offering operations are invalid")
    return frozenset(operations)


def _safe_limits(value: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or len(value) > _MAX_LIMIT_ENTRIES:
        raise ValueError("launch offering limits are invalid")
    limits: dict[str, int] = {}
    for name, limit in value.items():
        if not isinstance(name, str) or not _LIMIT_NAME_PATTERN.fullmatch(name):
            raise ValueError("launch offering limit name is invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_LIMIT_VALUE
        ):
            raise ValueError("launch offering limit value is invalid")
        limits[name] = limit
    absolute_ttl_ms = limits.get("absolute_ttl_ms")
    idle_ttl_ms = limits.get("idle_ttl_ms")
    if (
        absolute_ttl_ms is not None
        and idle_ttl_ms is not None
        and idle_ttl_ms > absolute_ttl_ms
    ):
        raise ValueError("launch offering idle_ttl_ms cannot exceed absolute_ttl_ms")
    return MappingProxyType(dict(sorted(limits.items())))


def _require_string_json_keys(value: object) -> None:
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError("immutable revision object keys must be strings")
                pending.append(nested)
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(current)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _derive_launch_grant(
    *,
    service_epoch: str,
    serial: int,
    candidate: str,
) -> str:
    epoch_bytes = service_epoch.encode("ascii")
    candidate_bytes = candidate.encode("ascii")
    material = b"".join(
        (
            _LAUNCH_GRANT_DERIVATION_DOMAIN,
            b"\x00",
            len(epoch_bytes).to_bytes(2, "big"),
            epoch_bytes,
            serial.to_bytes(8, "big"),
            len(candidate_bytes).to_bytes(2, "big"),
            candidate_bytes,
        )
    )
    digest = hashlib.sha256(material).hexdigest()
    return f"launch-grant:{serial:016x}_{digest}"


def _credential_sha256(value: object) -> str:
    text = value if _is_opaque(value) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and _MIN_OPAQUE_CHARACTERS <= len(value) <= _MAX_OPAQUE_CHARACTERS
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _require_opaque(value: object, name: str) -> None:
    if not _is_opaque(value):
        raise ValueError(f"{name} must contain 16 to 512 visible ASCII characters")


def _require_offering_id(value: object) -> None:
    if not isinstance(value, str) or not _OFFERING_ID_PATTERN.fullmatch(value):
        raise ValueError("offering_id is invalid")


def _require_launch_selection_id(value: object) -> None:
    if not isinstance(value, str) or not _LAUNCH_SELECTION_ID_PATTERN.fullmatch(value):
        raise ValueError("launch_selection_id is invalid")


def _require_launch_grant(value: object) -> None:
    if not isinstance(value, str) or not _LAUNCH_GRANT_PATTERN.fullmatch(value):
        raise ValueError("generated launch grant is invalid")


def _require_service_epoch(value: object) -> None:
    if not isinstance(value, str) or not _SERVICE_EPOCH_PATTERN.fullmatch(value):
        raise ValueError("generated service epoch is invalid")


def _require_identity(value: object, name: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _valid_launch_binding_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and _LAUNCH_BINDING_SHA256_PATTERN.fullmatch(value) is not None
    )


def _require_timestamp(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_TIMESTAMP_MS
    ):
        raise ValueError(f"{name} must be a non-negative safe integer")
    return value


def _format_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    timespec = "seconds" if value % 1_000 == 0 else "milliseconds"
    return (
        datetime.fromtimestamp(value / 1_000, UTC)
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _is_safe_label(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(
            ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
            for character in value
        )
    )


def _bounded_values(
    values: Iterable[object], *, maximum: int, message: str
) -> tuple[object, ...]:
    result = tuple(islice(values, maximum + 1))
    if len(result) > maximum:
        raise ValueError(message)
    return result


def _error(code: str, message: str, *, status: int) -> LaunchSelectionError:
    return LaunchSelectionError(code=code, message=message, status=status)


def _request_invalid() -> LaunchSelectionError:
    return _error(
        "request_invalid", "The launch-selection request is invalid.", status=400
    )


def _selection_unavailable() -> LaunchSelectionError:
    return _error(
        "launch_selection_unavailable",
        "The requested launch selection is unavailable.",
        status=404,
    )


def _grant_unavailable() -> LaunchSelectionError:
    return _error(
        "launch_grant_unavailable",
        "The requested launch grant is unavailable.",
        status=404,
    )


def _offering_unavailable() -> LaunchSelectionError:
    return _error(
        "offering_unavailable",
        "The requested launch offering is unavailable.",
        status=404,
    )


def _registration_conflict() -> LaunchSelectionError:
    return _error(
        "registration_conflict",
        "The trusted launch-selection registration conflicts with existing state.",
        status=409,
    )


def _registry_capacity() -> LaunchSelectionError:
    return _error(
        "registry_capacity_exhausted",
        "The trusted launch-selection registry has reached its configured capacity.",
        status=503,
    )


def _capacity_error() -> LaunchSelectionError:
    return LaunchSelectionError(
        code="launch_selection_capacity_exhausted",
        message="Launch-selection capacity is temporarily exhausted.",
        status=503,
        retry="after_delay",
        retry_after_ms=1_000,
    )


def _secure_token(purpose: str) -> str:
    return f"{purpose}:{secrets.token_urlsafe(32)}"


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _system_monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000
