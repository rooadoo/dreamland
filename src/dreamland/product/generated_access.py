"""Provider-free generated-world promotion and reveal authority.

This module is inert until explicitly constructed.  It does not publish a
browser catalog, mint attendee launch authority, construct a provider, or join
normal DREAMLAND/SSH composition.  Its only responsibility is to turn a
current, authoritative World Forge selection into bounded immutable access
truth and to make that truth revealable through an explicit Operator action.

The caller of :meth:`GeneratedWorldAccessService.promote` supplies only a
private Operator authority, current service-epoch evidence, an idempotency key,
and the non-authorizing Forge job reference.  Revision identity and content are
resolved and verified through the trusted in-process source; paths, raw state,
Forge authority, and selected-record authority never enter a projection.
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from dreamland.product.forge import (
    DEFAULT_SELECTED_HANDOFF_POLICY_ID,
    ForgeSelectedRevisionRecord,
    ForgeServiceError,
    WorldForgeService,
)
from dreamland.product.forge_artifacts import (
    ImmutableForgeRevision,
    ImmutableForgeStore,
    canonical_revision_document,
    validate_forge_profile,
)

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_INPUT_PATTERN = re.compile(r"^[\x21-\x7e]{1,512}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_ID_PATTERN = re.compile(r"^world-revision:sha256:[0-9a-f]{64}$")
_EPOCH_PATTERN = re.compile(r"^(?:generated-access-epoch|epoch):[A-Za-z0-9_-]{16,128}$")
_PROMOTION_ID_PATTERN = re.compile(
    r"^generated-access-promotion:[A-Za-z0-9_-]{16,128}$"
)
_ACCESS_RECORD_ID_PATTERN = re.compile(
    r"^generated-access-record:[A-Za-z0-9_-]{16,128}$"
)
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MAX_TOKEN_ATTEMPTS = 16
_SOURCE_ORIGIN = "world_forge_selection"
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


class GeneratedAccessState(StrEnum):
    """Closed states for immutable generated-world access records."""

    HIDDEN = "hidden"
    REVEALED = "revealed"


@dataclass(frozen=True, slots=True)
class GeneratedAccessActorSeed:
    """Private Operator authority installed by trusted service composition."""

    authority: str = field(repr=False)
    actor_id: str = ""
    expires_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class GeneratedAccessPolicy:
    """Hard process-local bounds for an explicitly constructed aggregate."""

    max_actor_authorities: int = 16
    max_active_promotions: int = 32
    max_access_records: int = 64
    max_idempotency_records: int = 128
    max_token_history: int = 256
    access_ttl_ms: int = 60 * 60 * 1000
    idempotency_ttl_ms: int = 60 * 60 * 1000
    failed_operation_ttl_ms: int = 5 * 60 * 1000
    retry_after_ms: int = 250
    accepted_selected_policy_ids: frozenset[str] = field(
        default_factory=lambda: frozenset({DEFAULT_SELECTED_HANDOFF_POLICY_ID})
    )
    accepted_source_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset({"single_attempt_live_vertical_slice"})
    )

    def __post_init__(self) -> None:
        for name in (
            "max_actor_authorities",
            "max_active_promotions",
            "max_access_records",
            "max_idempotency_records",
            "max_token_history",
            "access_ttl_ms",
            "idempotency_ttl_ms",
            "failed_operation_ttl_ms",
            "retry_after_ms",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a positive safe integer")
        if self.max_access_records < 2 * self.max_active_promotions:
            raise ValueError(
                "max_access_records must cover hidden and revealed promotion records"
            )
        if self.max_idempotency_records < 2 * self.max_active_promotions:
            raise ValueError(
                "max_idempotency_records must cover promotion and reveal recovery"
            )
        minimum_token_history = 1 + 3 * self.max_active_promotions
        if self.max_token_history < minimum_token_history:
            raise ValueError(
                "max_token_history must cover epoch and promotion record identities"
            )
        if self.idempotency_ttl_ms < self.access_ttl_ms:
            raise ValueError("idempotency_ttl_ms must cover the access lifetime")
        if self.failed_operation_ttl_ms > self.idempotency_ttl_ms:
            raise ValueError("failed_operation_ttl_ms cannot exceed idempotency_ttl_ms")
        if self.retry_after_ms > 60_000:
            raise ValueError("retry_after_ms cannot exceed 60000")
        for name in (
            "accepted_selected_policy_ids",
            "accepted_source_kinds",
        ):
            values = getattr(self, name)
            if (
                not isinstance(values, frozenset)
                or not values
                or len(values) > 32
                or any(
                    not isinstance(value, str)
                    or _IDENTITY_PATTERN.fullmatch(value) is None
                    for value in values
                )
            ):
                raise ValueError(f"{name} must contain safe identifiers")


class GeneratedAccessError(RuntimeError):
    """Stable bounded error safe for a private transport adapter."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status: int,
        retry: Literal["none", "same_request", "after_delay"] = "none",
        retry_after_ms: int | None = None,
    ) -> None:
        if _IDENTITY_PATTERN.fullmatch(code) is None:
            raise ValueError("generated-access error code is invalid")
        if not _safe_label(message, maximum=160):
            raise ValueError("generated-access error message is invalid")
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 400 <= status <= 599
        ):
            raise ValueError("generated-access error status is invalid")
        if retry not in {"none", "same_request", "after_delay"}:
            raise ValueError("generated-access retry category is invalid")
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


class GeneratedAccessSourceUnavailable(RuntimeError):
    """Trusted source signal for a retryable failure before access mutation."""


class GeneratedAccessPublicationCapacityError(RuntimeError):
    """Trusted catalog callback signal for retryable pre-publication capacity."""


@dataclass(frozen=True, slots=True)
class GeneratedAccessPublicationReceipt:
    """Trusted rollback for publication prepared before access commit."""

    rollback: Callable[[], None] = field(repr=False)

    def __post_init__(self) -> None:
        if not callable(self.rollback):
            raise TypeError("publication rollback must be callable")


class GeneratedAccessForgeSource(Protocol):
    """Narrow trusted seam from access authority to current Forge truth."""

    def resolve_selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        pass

    def load_revision(
        self, revision_id: str, immutable_digest: str
    ) -> ImmutableForgeRevision:
        pass


class WorldForgeAccessSource:
    """Resolve Forge authority and immutable artifacts entirely server-side."""

    def __init__(
        self,
        *,
        forge: WorldForgeService,
        artifacts: ImmutableForgeStore,
        forge_actor_authority: str,
    ) -> None:
        if not isinstance(forge, WorldForgeService):
            raise TypeError("forge must be a WorldForgeService")
        if not isinstance(artifacts, ImmutableForgeStore):
            raise TypeError("artifacts must be an ImmutableForgeStore")
        if _OPAQUE_INPUT_PATTERN.fullmatch(forge_actor_authority) is None:
            raise ValueError("forge_actor_authority is invalid")
        self._forge = forge
        self._artifacts = artifacts
        self._forge_actor_authority = forge_actor_authority

    def resolve_selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        return self._forge.resolve_selected_record(
            actor_authority=self._forge_actor_authority,
            expected_epoch=self._forge.service_epoch,
            job_reference=job_reference,
        )

    def load_revision(
        self, revision_id: str, immutable_digest: str
    ) -> ImmutableForgeRevision:
        return self._artifacts.load(revision_id, immutable_digest)


@dataclass(frozen=True, slots=True)
class GeneratedAccessEligibility:
    """Trusted revealed revision payload; never a transport DTO or authority."""

    service_epoch: str
    promotion_id: str
    access_record_id: str
    revision_id: str
    immutable_digest: str
    expires_at_ms: int
    expires_deadline_ms: int = field(repr=False)
    canonical_json: bytes = field(repr=False)

    def allocate_document(self) -> dict[str, object]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise RuntimeError("generated access revision is not a JSON object")
        return value


@dataclass(frozen=True, slots=True)
class _ActorRecord:
    key: tuple[str, str] = field(repr=False)
    expires_at_ms: int
    expires_deadline_ms: int


@dataclass(frozen=True, slots=True)
class _AccessRecord:
    service_epoch: str
    promotion_id: str
    access_record_id: str
    previous_access_record_id: str | None
    revision_number: int
    state: GeneratedAccessState
    owner_key: tuple[str, str] = field(repr=False)
    source_job_reference_sha256: str = field(repr=False)
    selected_record_sha256: str = field(repr=False)
    revision_id: str = ""
    immutable_digest: str = ""
    canonical_json: bytes = field(default=b"", repr=False)
    created_at_ms: int = 0
    changed_at_ms: int = 0
    expires_at_ms: int = 0
    expires_deadline_ms: int = 0


@dataclass(frozen=True, slots=True)
class _RecordedError:
    code: str
    message: str
    status: int
    retry: Literal["none", "same_request", "after_delay"]
    retry_after_ms: int | None

    @classmethod
    def capture(cls, error: GeneratedAccessError) -> _RecordedError:
        return cls(
            error.code,
            error.message,
            error.status,
            error.retry,
            error.retry_after_ms,
        )

    def restore(self) -> GeneratedAccessError:
        return GeneratedAccessError(
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
    result: dict[str, object] | None = field(default=None, repr=False)
    error: _RecordedError | None = field(default=None, repr=False)


class GeneratedWorldAccessService:
    """Bounded immutable promotion and explicit reveal aggregate."""

    def __init__(
        self,
        *,
        actors: Iterable[GeneratedAccessActorSeed],
        source: GeneratedAccessForgeSource,
        policy: GeneratedAccessPolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        token_factory: Callable[[str], str] | None = None,
        service_epoch: str | None = None,
    ) -> None:
        self._policy = policy or GeneratedAccessPolicy()
        self._source = source
        if not callable(getattr(source, "resolve_selected", None)) or not callable(
            getattr(source, "load_revision", None)
        ):
            raise TypeError("source must resolve selections and load revisions")
        self._clock_ms = clock_ms or _system_clock_ms
        self._monotonic_ms = monotonic_ms or (
            clock_ms if clock_ms is not None else _system_monotonic_ms
        )
        self._token_factory = token_factory or _secure_token
        wall_ms = _clock_value(self._clock_ms(), "clock_ms")
        deadline_ms = _clock_value(self._monotonic_ms(), "monotonic_ms")
        self._last_monotonic_ms = deadline_ms
        actor_values = tuple(actors)
        if not actor_values:
            raise ValueError("generated access requires at least one Operator actor")
        if len(actor_values) > self._policy.max_actor_authorities:
            raise ValueError("generated access actor capacity is exceeded")
        self._actors: dict[str, _ActorRecord] = {}
        actor_keys: set[tuple[str, str]] = set()
        for seed in actor_values:
            authority_digest, actor_id, expires_at_ms = _validate_actor_seed(seed)
            key = (actor_id, authority_digest)
            if authority_digest in self._actors or key in actor_keys:
                raise ValueError("generated access actor authorities must be unique")
            remaining_ms = max(0, expires_at_ms - wall_ms)
            self._actors[authority_digest] = _ActorRecord(
                key=key,
                expires_at_ms=expires_at_ms,
                expires_deadline_ms=_safe_add(deadline_ms, remaining_ms),
            )
            actor_keys.add(key)
        self._lock = threading.RLock()
        self._used_token_sha256s: set[str] = set()
        if service_epoch is None:
            (self._service_epoch,) = self._new_tokens(
                (("generated-access-epoch", _EPOCH_PATTERN),)
            )
        else:
            if _EPOCH_PATTERN.fullmatch(service_epoch) is None:
                raise ValueError("generated access service_epoch is invalid")
            self._service_epoch = service_epoch
            self._used_token_sha256s.add(_credential_sha256(service_epoch))
        self._records_by_id: dict[str, _AccessRecord] = {}
        self._current_by_promotion: dict[str, _AccessRecord] = {}
        self._promotion_by_source_sha256: dict[str, str] = {}
        self._idempotency: dict[
            tuple[str, tuple[str, str], str, str], _IdempotencyRecord
        ] = {}

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    def promote(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        idempotency_key: str,
        job_reference: str,
    ) -> dict[str, object]:
        """Create hidden access truth from one authoritative Forge selection."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _wall_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, _wall_ms, deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            reference_sha256 = _opaque_input_sha256(
                job_reference, field_name="job_reference"
            )
            body_sha256 = _canonical_sha256({"job_reference_sha256": reference_sha256})
            self._prune(_wall_ms, deadline_ms)
            scope = (self._service_epoch, actor.key, "promote", key)
            replay = self._replay(scope, body_sha256)
            if replay is not None:
                _LIFECYCLE_LOGGER.info(
                    "generated_access_promotion_replayed state=%s",
                    replay["state"],
                )
                return replay
            self._require_idempotency_capacity()
            claim = _IdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=_safe_add(
                    deadline_ms, self._policy.failed_operation_ttl_ms
                ),
            )
            self._idempotency[scope] = claim
            try:
                existing_id = self._promotion_by_source_sha256.get(reference_sha256)
                if existing_id is not None:
                    existing = self._current_by_promotion.get(existing_id)
                    if existing is None:
                        raise RuntimeError(
                            "generated access source index is inconsistent"
                        )
                    if existing.owner_key != actor.key:
                        raise _selection_unavailable()
                    result = self._projection(existing, replayed=False)
                    claim.result = _copy_document(result)
                    claim.retain_until_deadline_ms = self._success_retain_until(
                        deadline_ms, existing
                    )
                    _LIFECYCLE_LOGGER.info(
                        "generated_access_promotion_reused state=%s",
                        result["state"],
                    )
                    return result
                self._require_promotion_capacity(
                    additional_promotions=1, additional_records=1
                )
                selected, canonical_json = self._resolve_source(job_reference)
                commit_wall_ms, commit_deadline_ms = self._now()
                actor = self._require_actor(
                    actor_authority,
                    commit_wall_ms,
                    commit_deadline_ms,
                )
                selected_expires_at_ms = _timestamp_ms(selected.expires_at)
                remaining_access_ms = min(
                    self._policy.access_ttl_ms,
                    selected_expires_at_ms - commit_wall_ms,
                )
                if remaining_access_ms <= 0:
                    raise _selection_unavailable()
                promotion_id, access_record_id = self._new_tokens(
                    (
                        ("generated-access-promotion", _PROMOTION_ID_PATTERN),
                        ("generated-access-record", _ACCESS_RECORD_ID_PATTERN),
                    )
                )
                expires_at_ms = min(
                    _safe_add(commit_wall_ms, remaining_access_ms),
                    _MAX_TIMESTAMP_MS,
                )
                expires_deadline_ms = _safe_add(
                    commit_deadline_ms,
                    remaining_access_ms,
                )
                record = _AccessRecord(
                    service_epoch=self._service_epoch,
                    promotion_id=promotion_id,
                    access_record_id=access_record_id,
                    previous_access_record_id=None,
                    revision_number=1,
                    state=GeneratedAccessState.HIDDEN,
                    owner_key=actor.key,
                    source_job_reference_sha256=reference_sha256,
                    selected_record_sha256=_selected_record_sha256(selected),
                    revision_id=selected.revision_id,
                    immutable_digest=selected.immutable_digest,
                    canonical_json=canonical_json,
                    created_at_ms=commit_wall_ms,
                    changed_at_ms=commit_wall_ms,
                    expires_at_ms=expires_at_ms,
                    expires_deadline_ms=expires_deadline_ms,
                )
                self._records_by_id[record.access_record_id] = record
                self._current_by_promotion[record.promotion_id] = record
                self._promotion_by_source_sha256[reference_sha256] = record.promotion_id
                result = self._projection(record, replayed=False)
                claim.result = _copy_document(result)
                claim.retain_until_deadline_ms = self._success_retain_until(
                    commit_deadline_ms, record
                )
                _LIFECYCLE_LOGGER.info(
                    "generated_access_promotion_created state=hidden"
                )
                return result
            except GeneratedAccessSourceUnavailable:
                self._idempotency.pop(scope, None)
                raise self._capacity_error(
                    code="source_temporarily_unavailable",
                    message="The selected World Forge revision is temporarily unavailable.",
                ) from None
            except GeneratedAccessError as error:
                _LIFECYCLE_LOGGER.warning(
                    "generated_access_promotion_failed code=%s retry=%s",
                    error.code,
                    error.retry,
                )
                if error.retry == "after_delay":
                    self._idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedError.capture(error)
                    self._refresh_failed_retain(claim)
                raise
            except BaseException:
                self._idempotency.pop(scope, None)
                raise

    def reveal(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        idempotency_key: str,
        promotion_id: str,
        expected_access_record_id: str,
        publish: Callable[
            [GeneratedAccessEligibility], GeneratedAccessPublicationReceipt | None
        ],
    ) -> dict[str, object]:
        """Reveal one current hidden record under explicit compare-and-swap."""
        if not callable(publish):
            raise TypeError("publish must be callable")
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            wall_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, wall_ms, deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            promotion_id = _require_token(
                promotion_id, _PROMOTION_ID_PATTERN, "promotion_id"
            )
            expected_access_record_id = _require_token(
                expected_access_record_id,
                _ACCESS_RECORD_ID_PATTERN,
                "expected_access_record_id",
            )
            body_sha256 = _canonical_sha256(
                {
                    "promotion_id_sha256": _credential_sha256(promotion_id),
                    "expected_access_record_id_sha256": _credential_sha256(
                        expected_access_record_id
                    ),
                }
            )
            self._prune(wall_ms, deadline_ms)
            scope = (self._service_epoch, actor.key, "reveal", key)
            replay = self._replay(scope, body_sha256)
            if replay is not None:
                _LIFECYCLE_LOGGER.info(
                    "generated_access_reveal_replayed state=%s",
                    replay["state"],
                )
                return replay
            self._require_idempotency_capacity()
            claim = _IdempotencyRecord(
                body_sha256=body_sha256,
                retain_until_deadline_ms=_safe_add(
                    deadline_ms, self._policy.failed_operation_ttl_ms
                ),
            )
            self._idempotency[scope] = claim
            try:
                current = self._owned_current(actor, promotion_id)
                if current.access_record_id != expected_access_record_id:
                    raise _error(
                        "access_record_changed",
                        "The generated-world access record has changed.",
                        status=409,
                    )
                if current.state is GeneratedAccessState.REVEALED:
                    result = self._projection(current, replayed=False)
                    claim.result = _copy_document(result)
                    claim.retain_until_deadline_ms = self._success_retain_until(
                        deadline_ms, current
                    )
                    _LIFECYCLE_LOGGER.info(
                        "generated_access_reveal_reused state=revealed"
                    )
                    return result
                self._require_promotion_capacity(
                    additional_promotions=0, additional_records=1
                )
                (access_record_id,) = self._new_tokens(
                    (("generated-access-record", _ACCESS_RECORD_ID_PATTERN),)
                )
                revealed = _AccessRecord(
                    service_epoch=current.service_epoch,
                    promotion_id=current.promotion_id,
                    access_record_id=access_record_id,
                    previous_access_record_id=current.access_record_id,
                    revision_number=current.revision_number + 1,
                    state=GeneratedAccessState.REVEALED,
                    owner_key=current.owner_key,
                    source_job_reference_sha256=(current.source_job_reference_sha256),
                    selected_record_sha256=current.selected_record_sha256,
                    revision_id=current.revision_id,
                    immutable_digest=current.immutable_digest,
                    canonical_json=current.canonical_json,
                    created_at_ms=current.created_at_ms,
                    changed_at_ms=wall_ms,
                    expires_at_ms=current.expires_at_ms,
                    expires_deadline_ms=current.expires_deadline_ms,
                )
                publication = publish(_eligibility(revealed))
                if publication is not None and not isinstance(
                    publication, GeneratedAccessPublicationReceipt
                ):
                    raise TypeError("publish returned an invalid publication receipt")
                try:
                    commit_wall_ms, commit_deadline_ms = self._now()
                    if (
                        commit_wall_ms >= current.expires_at_ms
                        or commit_deadline_ms >= current.expires_deadline_ms
                    ):
                        raise _error(
                            "reveal_unavailable",
                            "The generated-world access record cannot be revealed.",
                            status=409,
                        )
                except BaseException:
                    if publication is not None:
                        publication.rollback()
                    raise
                revealed = replace(revealed, changed_at_ms=commit_wall_ms)
                self._records_by_id[revealed.access_record_id] = revealed
                self._current_by_promotion[revealed.promotion_id] = revealed
                result = self._projection(revealed, replayed=False)
                claim.result = _copy_document(result)
                claim.retain_until_deadline_ms = self._success_retain_until(
                    deadline_ms, revealed
                )
                _LIFECYCLE_LOGGER.info("generated_access_revealed state=revealed")
                return result
            except GeneratedAccessPublicationCapacityError:
                # This trusted signal promises that the publication callback
                # made no offering visible. The candidate access identity was
                # therefore never published and must not consume the bounded
                # identity history on each retryable capacity attempt.
                self._used_token_sha256s.discard(_credential_sha256(access_record_id))
                self._idempotency.pop(scope, None)
                _LIFECYCLE_LOGGER.warning(
                    "generated_access_reveal_failed "
                    "code=publication_capacity_exhausted retry=after_delay"
                )
                raise self._capacity_error(
                    code="publication_capacity_exhausted",
                    message=(
                        "Generated-world publication capacity is temporarily exhausted."
                    ),
                ) from None
            except GeneratedAccessError as error:
                _LIFECYCLE_LOGGER.warning(
                    "generated_access_reveal_failed code=%s retry=%s",
                    error.code,
                    error.retry,
                )
                if error.retry == "after_delay":
                    self._idempotency.pop(scope, None)
                else:
                    claim.error = _RecordedError.capture(error)
                    self._refresh_failed_retain(claim)
                raise
            except BaseException:
                self._idempotency.pop(scope, None)
                raise

    def status(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        promotion_id: str,
    ) -> dict[str, object]:
        """Return the current safe access projection for its Operator owner."""
        with self._lock:
            self._require_epoch(expected_epoch)
            wall_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, wall_ms, deadline_ms)
            promotion_id = _require_token(
                promotion_id, _PROMOTION_ID_PATTERN, "promotion_id"
            )
            self._prune(wall_ms, deadline_ms)
            return self._projection(
                self._owned_current(actor, promotion_id), replayed=False
            )

    def resolve_revealed_revision(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        promotion_id: str,
        access_record_id: str,
    ) -> GeneratedAccessEligibility:
        """Resolve exact current reveal truth for trusted catalog composition.

        The returned canonical bytes are detached immutable source material,
        not a launch selection or runtime grant.  A coordinator must still
        register its own bounded catalog offering and exchange that locator for
        narrow attendee authority.
        """
        with self._lock:
            self._require_epoch(expected_epoch)
            wall_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, wall_ms, deadline_ms)
            promotion_id = _require_token(
                promotion_id, _PROMOTION_ID_PATTERN, "promotion_id"
            )
            access_record_id = _require_token(
                access_record_id, _ACCESS_RECORD_ID_PATTERN, "access_record_id"
            )
            self._prune(wall_ms, deadline_ms)
            current = self._owned_current(actor, promotion_id)
            if (
                current.access_record_id != access_record_id
                or current.state is not GeneratedAccessState.REVEALED
            ):
                raise _access_unavailable()
            return _eligibility(current)

    def preview_rotate_epoch(self) -> str:
        """Simulate process loss; no old access truth is recovered or rebound."""
        with self._lock:
            (new_epoch,) = self._new_tokens(
                (("generated-access-epoch", _EPOCH_PATTERN),)
            )
            self._service_epoch = new_epoch
            self._records_by_id.clear()
            self._current_by_promotion.clear()
            self._promotion_by_source_sha256.clear()
            self._idempotency.clear()
            _LIFECYCLE_LOGGER.warning("generated_access_epoch_rotated state_lost=true")
            return new_epoch

    def _resolve_source(
        self, job_reference: str
    ) -> tuple[ForgeSelectedRevisionRecord, bytes]:
        try:
            selected = self._source.resolve_selected(job_reference)
        except GeneratedAccessSourceUnavailable:
            raise
        except ForgeServiceError:
            raise _selection_unavailable() from None
        if not isinstance(selected, ForgeSelectedRevisionRecord):
            raise _selection_unavailable()
        self._validate_selected(selected, job_reference)
        try:
            revision = self._source.load_revision(
                selected.revision_id, selected.immutable_digest
            )
        except GeneratedAccessSourceUnavailable:
            raise
        except (OSError, ValueError):
            raise _revision_unavailable() from None
        if not isinstance(revision, ImmutableForgeRevision):
            raise _revision_unavailable()
        try:
            canonical_json = _canonical_revision_json(revision.document)
            report = validate_forge_profile(revision.profile)
            exact_document = canonical_revision_document(revision.profile)
        except (RecursionError, TypeError, UnicodeError, ValueError):
            raise _revision_unavailable() from None
        if (
            revision.revision_id != selected.revision_id
            or revision.digest != selected.immutable_digest
            or not report.valid
            or exact_document != revision.document
            or f"sha256:{hashlib.sha256(canonical_json).hexdigest()}"
            != selected.immutable_digest
        ):
            raise _revision_unavailable()
        return selected, canonical_json

    def _validate_selected(
        self, selected: ForgeSelectedRevisionRecord, job_reference: str
    ) -> None:
        try:
            expires_at_ms = _timestamp_ms(selected.expires_at)
            _timestamp_ms(selected.selected_at)
        except ValueError:
            raise _selection_unavailable() from None
        wall_ms = _clock_value(self._clock_ms(), "clock_ms")
        safe_values = (
            selected.selected_record_id,
            selected.service_epoch,
            selected.job_id,
            selected.job_reference,
            selected.actor_id,
            selected.receipt_correlation_id,
        )
        if (
            selected.status != "selected"
            or selected.job_reference != job_reference
            or any(
                not isinstance(value, str)
                or _OPAQUE_INPUT_PATTERN.fullmatch(value) is None
                for value in safe_values
            )
            or selected.policy_id not in self._policy.accepted_selected_policy_ids
            or selected.source_kind not in self._policy.accepted_source_kinds
            or _REVISION_ID_PATTERN.fullmatch(selected.revision_id) is None
            or _SHA256_PATTERN.fullmatch(selected.immutable_digest) is None
            or _SHA256_PATTERN.fullmatch(selected.source_fingerprint) is None
            or _SHA256_PATTERN.fullmatch(selected.receipt_fingerprint) is None
            or expires_at_ms <= wall_ms
        ):
            raise _selection_unavailable()

    def _require_epoch(
        self, expected_epoch: str | None, *, mutation: bool = False
    ) -> None:
        if (
            not isinstance(expected_epoch, str)
            or _EPOCH_PATTERN.fullmatch(expected_epoch) is None
        ):
            raise _error(
                "expected_epoch_required",
                "Expected generated-access service epoch evidence is required.",
                status=400,
            )
        if not secrets.compare_digest(expected_epoch, self._service_epoch):
            if mutation:
                raise _error(
                    "outcome_unknown",
                    "The generated-access mutation outcome cannot be reconciled.",
                    status=410,
                )
            raise _error(
                "service_epoch_changed",
                "The generated-access service epoch has changed.",
                status=410,
            )

    def _require_actor(
        self, authority: str, wall_ms: int, deadline_ms: int
    ) -> _ActorRecord:
        digest = (
            _credential_sha256(authority)
            if isinstance(authority, str)
            and _OPAQUE_INPUT_PATTERN.fullmatch(authority) is not None
            else _credential_sha256("invalid-generated-access-authority")
        )
        actor = self._actors.get(digest)
        if (
            actor is None
            or wall_ms >= actor.expires_at_ms
            or deadline_ms >= actor.expires_deadline_ms
        ):
            raise _error(
                "authority_denied",
                "The requested generated-access resource is unavailable.",
                status=403,
            )
        return actor

    def _owned_current(self, actor: _ActorRecord, promotion_id: str) -> _AccessRecord:
        current = self._current_by_promotion.get(promotion_id)
        if current is None or current.owner_key != actor.key:
            raise _access_unavailable()
        return current

    def _projection(
        self, record: _AccessRecord, *, replayed: bool
    ) -> dict[str, object]:
        return {
            "kind": "generated_world_access",
            "service_epoch": self._service_epoch,
            "promotion_id": record.promotion_id,
            "access_record_id": record.access_record_id,
            "revision_number": record.revision_number,
            "state": record.state.value,
            "origin": {"kind": _SOURCE_ORIGIN},
            "revision": {
                "revision_id": record.revision_id,
                "sha256": record.immutable_digest.removeprefix("sha256:"),
            },
            "created_at": _format_timestamp(record.created_at_ms),
            "changed_at": _format_timestamp(record.changed_at_ms),
            "expires_at": _format_timestamp(record.expires_at_ms),
            "catalog_eligible": record.state is GeneratedAccessState.REVEALED,
            "replayed": replayed,
        }

    def _replay(
        self,
        scope: tuple[str, tuple[str, str], str, str],
        body_sha256: str,
    ) -> dict[str, object] | None:
        record = self._idempotency.get(scope)
        if record is None:
            return None
        if record.body_sha256 != body_sha256:
            raise _error(
                "idempotency_conflict",
                "The idempotency key is already bound to another request.",
                status=409,
            )
        if record.error is not None:
            raise record.error.restore()
        if record.result is None:
            raise RuntimeError("generated access idempotency replay is incomplete")
        result = _copy_document(record.result)
        result["replayed"] = True
        return result

    def _require_idempotency_capacity(self) -> None:
        if len(self._idempotency) >= self._policy.max_idempotency_records:
            raise self._capacity_error(
                code="idempotency_capacity_exhausted",
                message="Generated-access replay capacity is temporarily exhausted.",
            )

    def _require_promotion_capacity(
        self, *, additional_promotions: int, additional_records: int
    ) -> None:
        if (
            len(self._current_by_promotion) + additional_promotions
            > self._policy.max_active_promotions
            or len(self._records_by_id) + additional_records
            > self._policy.max_access_records
        ):
            raise self._capacity_error(
                code="capacity_exhausted",
                message="Generated-world access capacity is temporarily exhausted.",
            )

    def _capacity_error(self, *, code: str, message: str) -> GeneratedAccessError:
        return _error(
            code,
            message,
            status=503,
            retry="after_delay",
            retry_after_ms=self._policy.retry_after_ms,
        )

    def _success_retain_until(self, deadline_ms: int, record: _AccessRecord) -> int:
        return min(
            record.expires_deadline_ms,
            _safe_add(deadline_ms, self._policy.idempotency_ttl_ms),
        )

    def _refresh_failed_retain(self, claim: _IdempotencyRecord) -> None:
        try:
            _wall_ms, deadline_ms = self._now()
        except RuntimeError:
            deadline_ms = self._last_monotonic_ms
        claim.retain_until_deadline_ms = _safe_add(
            deadline_ms,
            self._policy.failed_operation_ttl_ms,
        )

    def _new_tokens(
        self, requests: tuple[tuple[str, re.Pattern[str]], ...]
    ) -> tuple[str, ...]:
        if len(self._used_token_sha256s) + len(requests) > (
            self._policy.max_token_history
        ):
            raise self._capacity_error(
                code="identity_capacity_exhausted",
                message="Generated-access identity capacity is temporarily exhausted.",
            )
        pending: list[tuple[str, str]] = []
        pending_digests: set[str] = set()
        for purpose, pattern in requests:
            for _attempt in range(_MAX_TOKEN_ATTEMPTS):
                value = self._token_factory(purpose)
                if not isinstance(value, str) or pattern.fullmatch(value) is None:
                    continue
                digest = _credential_sha256(value)
                if digest in self._used_token_sha256s or digest in pending_digests:
                    continue
                pending.append((value, digest))
                pending_digests.add(digest)
                break
            else:
                raise self._capacity_error(
                    code="identity_capacity_exhausted",
                    message="Generated-access identity capacity is temporarily exhausted.",
                )
        self._used_token_sha256s.update(digest for _value, digest in pending)
        return tuple(value for value, _digest in pending)

    def _now(self) -> tuple[int, int]:
        wall_ms = _clock_value(self._clock_ms(), "clock_ms")
        deadline_ms = _clock_value(self._monotonic_ms(), "monotonic_ms")
        if deadline_ms < self._last_monotonic_ms:
            raise RuntimeError("monotonic_ms must not move backward")
        self._last_monotonic_ms = deadline_ms
        return wall_ms, deadline_ms

    def _prune(self, wall_ms: int, deadline_ms: int) -> None:
        expired_promotions = tuple(
            promotion_id
            for promotion_id, record in self._current_by_promotion.items()
            if wall_ms >= record.expires_at_ms
            or deadline_ms >= record.expires_deadline_ms
        )
        for promotion_id in expired_promotions:
            current = self._current_by_promotion.pop(promotion_id)
            if (
                self._promotion_by_source_sha256.get(
                    current.source_job_reference_sha256
                )
                == promotion_id
            ):
                self._promotion_by_source_sha256.pop(
                    current.source_job_reference_sha256, None
                )
            for access_record_id, record in tuple(self._records_by_id.items()):
                if record.promotion_id == promotion_id:
                    self._records_by_id.pop(access_record_id, None)
        for scope, idempotency_record in tuple(self._idempotency.items()):
            if deadline_ms >= idempotency_record.retain_until_deadline_ms:
                self._idempotency.pop(scope, None)


def _eligibility(record: _AccessRecord) -> GeneratedAccessEligibility:
    return GeneratedAccessEligibility(
        service_epoch=record.service_epoch,
        promotion_id=record.promotion_id,
        access_record_id=record.access_record_id,
        revision_id=record.revision_id,
        immutable_digest=record.immutable_digest,
        expires_at_ms=record.expires_at_ms,
        expires_deadline_ms=record.expires_deadline_ms,
        canonical_json=record.canonical_json,
    )


def _validate_actor_seed(seed: GeneratedAccessActorSeed) -> tuple[str, str, int]:
    if not isinstance(seed, GeneratedAccessActorSeed):
        raise TypeError("generated access actor seed is invalid")
    if _OPAQUE_INPUT_PATTERN.fullmatch(seed.authority) is None:
        raise ValueError("generated access actor authority is invalid")
    if _IDENTITY_PATTERN.fullmatch(seed.actor_id) is None:
        raise ValueError("generated access actor ID is invalid")
    expires_at_ms = _clock_value(seed.expires_at_ms, "actor expires_at_ms")
    return _credential_sha256(seed.authority), seed.actor_id, expires_at_ms


def _canonical_revision_json(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(document),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _selected_record_sha256(record: ForgeSelectedRevisionRecord) -> str:
    return _canonical_sha256(
        {
            "selected_record_id_sha256": _credential_sha256(record.selected_record_id),
            "service_epoch_sha256": _credential_sha256(record.service_epoch),
            "job_reference_sha256": _credential_sha256(record.job_reference),
            "policy_id": record.policy_id,
            "revision_id": record.revision_id,
            "immutable_digest": record.immutable_digest,
            "source_fingerprint": record.source_fingerprint,
            "receipt_fingerprint": record.receipt_fingerprint,
        }
    )


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _copy_document(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(dict(value)))
    if not isinstance(copied, dict):
        raise RuntimeError("generated access projection copy is invalid")
    return copied


def _credential_sha256(value: object) -> str:
    if not isinstance(value, str):
        return hashlib.sha256(b"invalid-generated-access-credential").hexdigest()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque_input_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_INPUT_PATTERN.fullmatch(value) is None:
        raise _error(
            "invalid_request",
            f"The generated-access {field_name} is invalid.",
            status=400,
        )
    return _credential_sha256(value)


def _require_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise _error(
            "invalid_request",
            "The generated-access idempotency key is invalid.",
            status=400,
        )
    return value


def _require_token(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _error(
            "invalid_request",
            f"The generated-access {field_name} is invalid.",
            status=400,
        )
    return value


def _timestamp_ms(value: object) -> int:
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp is invalid")
    milliseconds = int(parsed.timestamp() * 1_000)
    if not 0 <= milliseconds <= _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp is invalid")
    return milliseconds


def _format_timestamp(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1_000, tz=UTC).isoformat(
        timespec="milliseconds"
    )


def _clock_value(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_TIMESTAMP_MS
    ):
        raise RuntimeError(f"{name} must return a valid integer millisecond value")
    return value


def _safe_add(value: int, increment: int) -> int:
    if value > _MAX_TIMESTAMP_MS - increment:
        return _MAX_TIMESTAMP_MS
    return value + increment


def _safe_label(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(
            character == " " or 0x21 <= ord(character) <= 0x7E for character in value
        )
    )


def _error(
    code: str,
    message: str,
    *,
    status: int,
    retry: Literal["none", "same_request", "after_delay"] = "none",
    retry_after_ms: int | None = None,
) -> GeneratedAccessError:
    return GeneratedAccessError(
        code=code,
        message=message,
        status=status,
        retry=retry,
        retry_after_ms=retry_after_ms,
    )


def _selection_unavailable() -> GeneratedAccessError:
    return _error(
        "selection_unavailable",
        "The selected World Forge revision is unavailable for promotion.",
        status=404,
    )


def _revision_unavailable() -> GeneratedAccessError:
    return _error(
        "revision_unavailable",
        "The selected World Forge artifact is unavailable for promotion.",
        status=409,
    )


def _access_unavailable() -> GeneratedAccessError:
    return _error(
        "access_unavailable",
        "The generated-world access record is unavailable.",
        status=404,
    )


def _system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _system_monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _secure_token(purpose: str) -> str:
    return f"{purpose}:{secrets.token_urlsafe(24)}"
