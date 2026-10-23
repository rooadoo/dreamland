"""Protocol-neutral Presenter Stage facade over the shared product core.

The facade is inert until explicitly constructed.  It adds presenter-only
selection-bound launch/resume, an explicit replacement name, bounded display
metadata, and use of the hard Presenter Stage admission partition.  Its optional
trusted provisioning seam installs an already-authoritative Forge selection; it
does not implement browser authentication, Forge selection, or runtime
composition.  Its optional X-ray seam is a bounded allowlist over accepted
product state and remains disabled unless the explicit composition supplies a
policy.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductCapacityClass,
)
from dreamland.product.core import (
    ProductCorePolicy,
    ProductSessionCoreService,
    TaskSubmitter,
    _CoreSessionState,
)
from dreamland.product.execution import ProductCommandExecutor
from dreamland.product.journal import JournalLimits
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
    _ActorAuthorityRecord,
    _build_launch_grant_record,
    _build_revisions,
    _canonical_sha256,
    _credential_sha256,
    _error,
    _IdempotencyRecord,
    _is_bounded_visible_ascii,
    _LaunchGrantRecord,
    _RecordedError,
    _require_idempotency_key,
    _require_identity,
    _require_seed_token,
    _require_timestamp,
    _SessionRecord,
    _SessionTombstone,
)
from dreamland.product.xray import (
    XRAY_SCHEMA_REVISION,
    StageXrayPolicy,
    StageXrayProjection,
    build_stage_xray_projection,
)

_IMMUTABLE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_LAUNCH_SELECTION_GRANT_PATTERN = re.compile(
    r"^launch-grant:(?P<serial>[0-9a-f]{16})_[0-9a-f]{64}$"
)
_MAX_SAFE_INTEGER = 2**53 - 1
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")


class StageLaunchSelectionOrigin(StrEnum):
    """Closed origins accepted by source-neutral Stage provisioning."""

    AUTHORED_PROFILE_SNAPSHOT = "authored_profile_snapshot"
    WORLD_FORGE_SELECTION = "world_forge_selection"


@dataclass(frozen=True, slots=True)
class StageDisplayMetadataSeed:
    """Trusted, bounded display labels for one immutable starting revision."""

    revision_id: str
    title: str
    source_label: str
    revision_label: str

    def __post_init__(self) -> None:
        _require_stage_label(self.revision_id, "revision_id", maximum=128)
        _require_stage_label(self.title, "title", maximum=120)
        _require_stage_label(self.source_label, "source_label", maximum=64)
        _require_stage_label(self.revision_label, "revision_label", maximum=96)


@dataclass(frozen=True, slots=True)
class StageForgeIdentitySeed:
    """Safe identity of one authoritative selected World Forge artifact."""

    selected_record_id: str
    immutable_digest: str
    forge_job_id: str
    forge_reference: str

    def __post_init__(self) -> None:
        _require_identity("selected_record_id", self.selected_record_id)
        if not isinstance(self.immutable_digest, str) or not (
            _IMMUTABLE_DIGEST_PATTERN.fullmatch(self.immutable_digest)
        ):
            raise ValueError("immutable_digest must be a canonical SHA-256 digest")
        _require_identity("forge_job_id", self.forge_job_id)
        _require_identity("forge_reference", self.forge_reference)


@dataclass(frozen=True, slots=True)
class StageAuthoredIdentitySeed:
    """Path-free identity of one configured immutable authored snapshot."""

    selected_record_id: str
    immutable_digest: str

    def __post_init__(self) -> None:
        _require_identity("selected_record_id", self.selected_record_id)
        if not isinstance(self.immutable_digest, str) or not (
            _IMMUTABLE_DIGEST_PATTERN.fullmatch(self.immutable_digest)
        ):
            raise ValueError("immutable_digest must be a canonical SHA-256 digest")


@dataclass(frozen=True, slots=True)
class StageLaunchSelectionIdentitySeed:
    """Path-free identity for one source-neutral registered selection."""

    selected_record_id: str
    immutable_digest: str
    origin: StageLaunchSelectionOrigin

    def __post_init__(self) -> None:
        _require_identity("selected_record_id", self.selected_record_id)
        if not isinstance(self.immutable_digest, str) or not (
            _IMMUTABLE_DIGEST_PATTERN.fullmatch(self.immutable_digest)
        ):
            raise ValueError("immutable_digest must be a canonical SHA-256 digest")
        if not isinstance(self.origin, StageLaunchSelectionOrigin):
            raise ValueError("launch-selection origin is invalid")


@dataclass(frozen=True, slots=True)
class StageSelectionProvisioningResult:
    """Credential-free result of one trusted adjacent provisioning call."""

    service_epoch: str
    selected_record_id: str
    revision_id: str
    immutable_digest: str
    reused: bool


class StageSelectionCapacityError(ValueError):
    """Trusted dynamic provisioning exhausted a bounded Stage registry."""


@dataclass(slots=True)
class _StageXraySession:
    stream_id: str
    sequence: int = 0
    accepted_state_revision: int = -1
    content_sha256: str = ""
    projection: StageXrayProjection | None = None


class PresenterStageService(ProductSessionCoreService):
    """Presenter-only Stage facade with an isolated reserved capacity class."""

    def __init__(
        self,
        *,
        revisions: Iterable[ImmutableRevisionSeed],
        actors: Iterable[ActorAuthoritySeed],
        launch_grants: Iterable[LaunchGrantSeed],
        display_metadata: Iterable[StageDisplayMetadataSeed],
        executor: ProductCommandExecutor,
        submit_task: TaskSubmitter,
        lifecycle_policy: LifecyclePolicy | None = None,
        core_policy: ProductCorePolicy | None = None,
        journal_limits: JournalLimits | None = None,
        admission: ProductAdmissionLedger,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        token_factory: Callable[[str], str] | None = None,
        validate_replacement: Callable[[Mapping[str, object]], None] | None = None,
        validate_state_effect: Callable[[Mapping[str, object]], None] | None = None,
        dynamic_selection_only: bool = False,
        xray_policy: StageXrayPolicy | None = None,
    ) -> None:
        if not isinstance(dynamic_selection_only, bool):
            raise ValueError("dynamic_selection_only must be boolean")
        if xray_policy is not None and not isinstance(xray_policy, StageXrayPolicy):
            raise TypeError("xray_policy must be StageXrayPolicy or None")
        revision_values = tuple(revisions)
        actor_values = tuple(actors)
        grant_values = tuple(launch_grants)
        if not actor_values or any(
            actor.role is not ProductRole.PRESENTER for actor in actor_values
        ):
            raise ValueError("Presenter Stage actors must all use presenter authority")
        metadata_values = tuple(display_metadata)
        if dynamic_selection_only:
            if revision_values or grant_values or metadata_values:
                raise ValueError(
                    "dynamic-selection-only Stage cannot have fixed revisions, "
                    "grants, or display metadata"
                )
            metadata_by_revision: dict[str, StageDisplayMetadataSeed] = {}
        else:
            if not grant_values or any(
                grant.role is not ProductRole.PRESENTER for grant in grant_values
            ):
                raise ValueError(
                    "Presenter Stage grants must all use presenter authority"
                )
            metadata_by_revision = {
                value.revision_id: value for value in metadata_values
            }
            if len(metadata_by_revision) != len(metadata_values):
                raise ValueError(
                    "Presenter Stage display metadata revisions must be unique"
                )
            revision_ids = {revision.revision_id for revision in revision_values}
            if set(metadata_by_revision) != revision_ids:
                raise ValueError(
                    "Presenter Stage requires exact display metadata for every revision"
                )
        limits = admission.limits
        reservations = (
            limits.presenter_stage_reserved_sessions,
            limits.presenter_stage_reserved_logical_jobs,
            limits.presenter_stage_reserved_physical_workers,
            limits.presenter_stage_reserved_token_budget,
            limits.presenter_stage_reserved_spend_budget_units,
        )
        if any(value <= 0 for value in reservations):
            raise ValueError("Presenter Stage requires positive hard reservations")
        self._dynamic_selection_only = dynamic_selection_only
        self._xray_policy = xray_policy
        self._xray_sessions: dict[str, _StageXraySession] = {}
        self._stage_display_metadata = metadata_by_revision
        self._stage_forge_sources: dict[str, StageForgeIdentitySeed] = {}
        self._dynamic_stage_grants: set[str] = set()
        self._source_neutral_stage_grants: set[str] = set()
        self._source_neutral_launch_serial_high_water = 0
        self._dynamic_launch_by_selection: dict[str, str] = {}
        super().__init__(
            revisions=revision_values,
            actors=actor_values,
            launch_grants=grant_values,
            executor=executor,
            submit_task=submit_task,
            lifecycle_policy=lifecycle_policy,
            core_policy=core_policy,
            journal_limits=journal_limits,
            admission=admission,
            clock_ms=clock_ms,
            monotonic_ms=monotonic_ms,
            token_factory=token_factory,
            validate_replacement=validate_replacement,
            validate_state_effect=validate_state_effect,
            allow_empty_revisions=dynamic_selection_only,
        )

    def provision_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        revision: ImmutableRevisionSeed,
        launch_grant: LaunchGrantSeed,
        display_metadata: StageDisplayMetadataSeed,
        forge_identity: StageForgeIdentitySeed,
    ) -> StageSelectionProvisioningResult:
        """Atomically install one exact, presenter-owned Forge selection.

        This is a trusted in-process coordination seam, not a transport operation.
        It never generates authority, resolves a path, or selects a fallback.
        """
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            if actor.role is not ProductRole.PRESENTER:
                raise _error(
                    "authority_denied",
                    "The requested product resource is unavailable.",
                    status=403,
                )
            self._expire_and_prune_locked(deadline_ms)
            if not self._dynamic_selection_only:
                raise ValueError(
                    "dynamic Stage provisioning requires dynamic_selection_only"
                )
            if not isinstance(revision, ImmutableRevisionSeed):
                raise TypeError("revision must be ImmutableRevisionSeed")
            if not isinstance(launch_grant, LaunchGrantSeed):
                raise TypeError("launch_grant must be LaunchGrantSeed")
            if not isinstance(display_metadata, StageDisplayMetadataSeed):
                raise TypeError("display_metadata must be StageDisplayMetadataSeed")
            if not isinstance(forge_identity, StageForgeIdentitySeed):
                raise TypeError("forge_identity must be StageForgeIdentitySeed")

            candidate_revision = _build_revisions((revision,))[revision.revision_id]
            candidate_document = candidate_revision.allocate()
            if candidate_document.get("kind") != "dreamland.world_forge_revision":
                raise ValueError(
                    "dynamic Stage revision must be a World Forge revision"
                )
            forge_digest = (
                "sha256:"
                + hashlib.sha256(candidate_revision.canonical_json + b"\n").hexdigest()
            )
            if forge_identity.immutable_digest != forge_digest:
                raise ValueError("World Forge immutable digest does not match revision")
            if revision.revision_id != f"world-revision:{forge_digest}":
                raise ValueError(
                    "World Forge revision identifier does not match immutable digest"
                )
            if len(candidate_revision.canonical_json) > (
                self._core_policy.max_runtime_document_bytes
            ):
                raise ValueError(
                    "immutable revision must fit the product runtime document limit"
                )
            if display_metadata.revision_id != revision.revision_id:
                raise ValueError("display metadata names another revision")
            if launch_grant.revision_id != revision.revision_id:
                raise ValueError("launch grant names another revision")
            if forge_identity.selected_record_id != launch_grant.selected_record_id:
                raise ValueError("Forge identity names another selected record")
            if (
                launch_grant.role is not ProductRole.PRESENTER
                or (
                    launch_grant.actor_id,
                    launch_grant.role,
                )
                != actor.key
            ):
                raise ValueError("launch grant must bind the authorizing presenter")
            if ProductOperation.CREATE not in launch_grant.allowed_operations:
                raise ValueError("dynamic Stage grant must allow session.create")

            _require_seed_token(launch_grant.token)
            _require_timestamp(
                "launch grant expires_at_ms",
                launch_grant.expires_at_ms,
            )
            candidate_grant = _build_launch_grant_record(
                launch_grant,
                actor_keys={actor.key},
                revision_ids=frozenset(
                    (*self._revisions, candidate_revision.revision_id)
                ),
                launch_operation_families=self._launch_operation_families,
                service_epoch=self._service_epoch,
                expires_deadline_ms=self._deadline_from_projection(
                    launch_grant.expires_at_ms
                ),
            )
            if (
                deadline_ms >= candidate_grant.expires_deadline_ms
                or self._last_wall_ms >= candidate_grant.expires_at_ms
            ):
                raise ValueError("dynamic Stage launch grant is expired")

            launch_sha256 = _credential_sha256(launch_grant.token)
            existing_grant = self._launch_grants.get(launch_sha256)
            existing_revision = self._revisions.get(candidate_revision.revision_id)
            existing_metadata = self._stage_display_metadata.get(
                candidate_revision.revision_id
            )
            existing_source = self._stage_forge_sources.get(
                candidate_grant.selected_record_id
            )
            selected_launch_sha256 = self._dynamic_launch_by_selection.get(
                candidate_grant.selected_record_id
            )

            if launch_sha256 in self._dynamic_stage_grants:
                if not (
                    existing_grant is not None
                    and _same_launch_binding(existing_grant, candidate_grant)
                    and existing_revision == candidate_revision
                    and existing_metadata == display_metadata
                    and existing_source == forge_identity
                    and selected_launch_sha256 == launch_sha256
                ):
                    raise ValueError(
                        "dynamic Stage selection conflicts with its prior provision"
                    )
                return _provisioning_result(
                    service_epoch=self._service_epoch,
                    revision_id=candidate_revision.revision_id,
                    source=forge_identity,
                    reused=True,
                )

            if launch_sha256 in self._used_value_digests or existing_grant is not None:
                raise ValueError("dynamic Stage launch grant token is not unique")
            if selected_launch_sha256 is not None or existing_source is not None:
                raise ValueError("dynamic Stage selected record is already provisioned")
            if (
                existing_revision is not None
                and existing_revision != candidate_revision
            ):
                raise ValueError(
                    "dynamic Stage revision conflicts with existing revision"
                )
            if existing_metadata is not None and existing_metadata != display_metadata:
                raise ValueError(
                    "dynamic Stage display metadata conflicts with existing revision"
                )
            if (
                existing_revision is None
                and len(self._revisions) >= self._policy.max_revisions
            ):
                raise StageSelectionCapacityError(
                    "dynamic Stage immutable revision capacity exhausted"
                )
            if len(self._launch_grants) >= self._policy.max_launch_grants:
                raise StageSelectionCapacityError(
                    "dynamic Stage launch grant capacity exhausted"
                )

            self._revisions[candidate_revision.revision_id] = candidate_revision
            self._launch_grants[launch_sha256] = candidate_grant
            self._stage_display_metadata[candidate_revision.revision_id] = (
                display_metadata
            )
            self._stage_forge_sources[candidate_grant.selected_record_id] = (
                forge_identity
            )
            self._dynamic_launch_by_selection[candidate_grant.selected_record_id] = (
                launch_sha256
            )
            self._dynamic_stage_grants.add(launch_sha256)
            self._used_value_digests.add(launch_sha256)
            return _provisioning_result(
                service_epoch=self._service_epoch,
                revision_id=candidate_revision.revision_id,
                source=forge_identity,
                reused=False,
            )

    def provision_launch_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        revision: ImmutableRevisionSeed,
        launch_grant: LaunchGrantSeed,
        display_metadata: StageDisplayMetadataSeed,
        selection_identity: StageLaunchSelectionIdentitySeed,
        expires_deadline_ms: int | None = None,
    ) -> StageSelectionProvisioningResult:
        """Install one exact registered immutable revision into dynamic Stage.

        The source has already been imported or selected and digest-pinned by
        trusted composition. This seam accepts neither a profile name nor a
        filesystem path and does not synthesize Forge provenance. The existing
        Forge-specific seam remains unchanged for complete Forge source tuples.
        """
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            if actor.role is not ProductRole.PRESENTER:
                raise _error(
                    "authority_denied",
                    "The requested product resource is unavailable.",
                    status=403,
                )
            self._expire_and_prune_locked(deadline_ms)
            if not self._dynamic_selection_only:
                raise ValueError(
                    "dynamic Stage provisioning requires dynamic_selection_only"
                )
            if not isinstance(revision, ImmutableRevisionSeed):
                raise TypeError("revision must be ImmutableRevisionSeed")
            if not isinstance(launch_grant, LaunchGrantSeed):
                raise TypeError("launch_grant must be LaunchGrantSeed")
            if not isinstance(display_metadata, StageDisplayMetadataSeed):
                raise TypeError("display_metadata must be StageDisplayMetadataSeed")
            if not isinstance(selection_identity, StageLaunchSelectionIdentitySeed):
                raise TypeError(
                    "selection_identity must be StageLaunchSelectionIdentitySeed"
                )

            grant_match = _LAUNCH_SELECTION_GRANT_PATTERN.fullmatch(launch_grant.token)
            if grant_match is None:
                raise ValueError(
                    "source-neutral launch grant must use the registered format"
                )
            grant_serial = int(grant_match.group("serial"), 16)
            expected_selected_record_id = (
                "selected-record:"
                + hashlib.sha256(launch_grant.token.encode("utf-8")).hexdigest()
            )
            if (
                launch_grant.selected_record_id != expected_selected_record_id
                or selection_identity.selected_record_id != expected_selected_record_id
            ):
                raise ValueError(
                    "source-neutral selected record must bind the launch grant"
                )

            candidate_revision = _build_revisions((revision,))[revision.revision_id]
            candidate_document = candidate_revision.allocate()
            expected_kind = {
                StageLaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT: (
                    "dreamland.authored_world_revision"
                ),
                StageLaunchSelectionOrigin.WORLD_FORGE_SELECTION: (
                    "dreamland.world_forge_revision"
                ),
            }[selection_identity.origin]
            if candidate_document.get("kind") != expected_kind:
                raise ValueError(
                    "dynamic Stage revision does not match its registered origin"
                )
            immutable_digest = (
                "sha256:"
                + hashlib.sha256(candidate_revision.canonical_json + b"\n").hexdigest()
            )
            if selection_identity.immutable_digest != immutable_digest:
                raise ValueError("immutable digest does not match the revision")
            if revision.revision_id != f"world-revision:{immutable_digest}":
                raise ValueError("revision identifier does not match immutable digest")
            if len(candidate_revision.canonical_json) > (
                self._core_policy.max_runtime_document_bytes
            ):
                raise ValueError(
                    "immutable revision must fit the product runtime document limit"
                )
            if display_metadata.revision_id != revision.revision_id:
                raise ValueError("display metadata names another revision")
            if launch_grant.revision_id != revision.revision_id:
                raise ValueError("launch grant names another revision")
            if selection_identity.selected_record_id != launch_grant.selected_record_id:
                raise ValueError("selection identity names another selected record")
            if (
                launch_grant.role is not ProductRole.PRESENTER
                or (launch_grant.actor_id, launch_grant.role) != actor.key
            ):
                raise ValueError("launch grant must bind the authorizing presenter")
            if ProductOperation.CREATE not in launch_grant.allowed_operations:
                raise ValueError("dynamic Stage grant must allow session.create")

            _require_seed_token(launch_grant.token)
            _require_timestamp(
                "launch grant expires_at_ms",
                launch_grant.expires_at_ms,
            )
            local_expires_deadline_ms = deadline_ms + max(
                0,
                launch_grant.expires_at_ms - self._last_wall_ms,
            )
            if expires_deadline_ms is not None:
                _require_timestamp(
                    "launch grant expires_deadline_ms",
                    expires_deadline_ms,
                )
                local_expires_deadline_ms = min(
                    local_expires_deadline_ms,
                    expires_deadline_ms,
                )
            else:
                local_expires_deadline_ms = min(
                    local_expires_deadline_ms,
                    self._deadline_from_projection(launch_grant.expires_at_ms),
                )
            candidate_grant = _build_launch_grant_record(
                launch_grant,
                actor_keys={actor.key},
                revision_ids=frozenset(
                    (*self._revisions, candidate_revision.revision_id)
                ),
                launch_operation_families=self._launch_operation_families,
                service_epoch=self._service_epoch,
                expires_deadline_ms=local_expires_deadline_ms,
            )
            if (
                deadline_ms >= candidate_grant.expires_deadline_ms
                or self._last_wall_ms >= candidate_grant.expires_at_ms
            ):
                raise ValueError("dynamic Stage launch grant is expired")

            launch_sha256 = _credential_sha256(launch_grant.token)
            existing_grant = self._launch_grants.get(launch_sha256)
            existing_revision = self._revisions.get(candidate_revision.revision_id)
            existing_metadata = self._stage_display_metadata.get(
                candidate_revision.revision_id
            )
            existing_forge_source = self._stage_forge_sources.get(
                candidate_grant.selected_record_id
            )
            selected_launch_sha256 = self._dynamic_launch_by_selection.get(
                candidate_grant.selected_record_id
            )

            if launch_sha256 in self._source_neutral_stage_grants:
                if not (
                    existing_grant is not None
                    and _same_launch_binding(existing_grant, candidate_grant)
                    and existing_revision == candidate_revision
                    and existing_metadata == display_metadata
                    and existing_forge_source is None
                    and selected_launch_sha256 == launch_sha256
                ):
                    raise ValueError(
                        "dynamic Stage selection conflicts with its prior provision"
                    )
                return _registered_provisioning_result(
                    service_epoch=self._service_epoch,
                    revision_id=candidate_revision.revision_id,
                    source=selection_identity,
                    reused=True,
                )

            if grant_serial <= self._source_neutral_launch_serial_high_water:
                raise ValueError(
                    "source-neutral launch grant serial is not newer than prior grants"
                )
            if launch_sha256 in self._used_value_digests or existing_grant is not None:
                raise ValueError("dynamic Stage launch grant token is not unique")
            if selected_launch_sha256 is not None or existing_forge_source is not None:
                raise ValueError("dynamic Stage selected record is already provisioned")
            if (
                existing_revision is not None
                and existing_revision != candidate_revision
            ):
                raise ValueError(
                    "dynamic Stage revision conflicts with existing revision"
                )
            if existing_metadata is not None and existing_metadata != display_metadata:
                raise ValueError(
                    "dynamic Stage display metadata conflicts with existing revision"
                )
            if (
                existing_revision is None
                and len(self._revisions) >= self._policy.max_revisions
            ):
                raise StageSelectionCapacityError(
                    "dynamic Stage immutable revision capacity exhausted"
                )
            if len(self._launch_grants) >= self._policy.max_launch_grants:
                raise StageSelectionCapacityError(
                    "dynamic Stage launch grant capacity exhausted"
                )

            self._revisions[candidate_revision.revision_id] = candidate_revision
            self._launch_grants[launch_sha256] = candidate_grant
            self._stage_display_metadata[candidate_revision.revision_id] = (
                display_metadata
            )
            self._dynamic_launch_by_selection[candidate_grant.selected_record_id] = (
                launch_sha256
            )
            self._dynamic_stage_grants.add(launch_sha256)
            self._source_neutral_stage_grants.add(launch_sha256)
            self._used_value_digests.add(launch_sha256)
            self._source_neutral_launch_serial_high_water = grant_serial
            return _registered_provisioning_result(
                service_epoch=self._service_epoch,
                revision_id=candidate_revision.revision_id,
                source=selection_identity,
                reused=False,
            )

    def provision_authored_selection(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        revision: ImmutableRevisionSeed,
        launch_grant: LaunchGrantSeed,
        display_metadata: StageDisplayMetadataSeed,
        authored_identity: StageAuthoredIdentitySeed,
        expires_deadline_ms: int | None = None,
    ) -> StageSelectionProvisioningResult:
        """Compatibility convenience for one configured authored snapshot."""
        if not isinstance(authored_identity, StageAuthoredIdentitySeed):
            raise TypeError("authored_identity must be StageAuthoredIdentitySeed")
        return self.provision_launch_selection(
            actor_authority=actor_authority,
            expected_epoch=expected_epoch,
            revision=revision,
            launch_grant=launch_grant,
            display_metadata=display_metadata,
            selection_identity=StageLaunchSelectionIdentitySeed(
                selected_record_id=authored_identity.selected_record_id,
                immutable_digest=authored_identity.immutable_digest,
                origin=StageLaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            ),
            expires_deadline_ms=expires_deadline_ms,
        )

    def launch(
        self,
        *,
        actor_authority: str,
        launch_grant: str,
        expected_epoch: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Create or resume only the exact originating Stage selection."""
        with self._lock:
            self._require_epoch(expected_epoch, mutation=True)
            _now_ms, deadline_ms = self._now()
            actor = self._require_actor(actor_authority, deadline_ms)
            if actor.role is not ProductRole.PRESENTER:
                raise _error(
                    "authority_denied",
                    "The requested product resource is unavailable.",
                    status=403,
                )
            self._expire_and_prune_locked(deadline_ms)
            key = _require_idempotency_key(idempotency_key)
            if not _is_bounded_visible_ascii(launch_grant):
                raise _error(
                    "authority_denied",
                    "The requested product resource is unavailable.",
                    status=403,
                )
            launch_sha256 = _credential_sha256(launch_grant)
            body_sha256 = _canonical_sha256({"launch_grant_sha256": launch_sha256})
            scope = self._idempotency_scope(
                actor.key,
                ProductOperation.CREATE,
                key,
            )
            replay = self._idempotency_replay(scope, body_sha256)
            if replay is not None:
                return replay

            current_reference = self._current_by_actor.get(actor.key)
            current = (
                self._sessions_by_reference.get(current_reference)
                if current_reference is not None
                else None
            )
            if current is None:
                result = super().create(
                    actor_authority=actor_authority,
                    launch_grant=launch_grant,
                    expected_epoch=expected_epoch,
                    idempotency_key=key,
                )
                result["kind"] = "stage_session_created"
                record = self._idempotency[scope]
                record.result = deepcopy(result)
                return result

            self._require_idempotency_capacity(additional_records=1)
            try:
                grant = self._stage_launch_grant(
                    launch_sha256=launch_sha256,
                    actor_key=actor.key,
                    deadline_ms=deadline_ms,
                )
            except ProductLifecycleError as authority_error:
                self._record_core_error_locked(
                    scope,
                    body_sha256,
                    authority_error,
                    deadline_ms=deadline_ms,
                    session_id=self._related_launch_session_id(launch_sha256),
                )
                raise
            if self._can_resume(current, grant, launch_sha256):
                result = {
                    "kind": "stage_session_resumed",
                    "service_epoch": self._service_epoch,
                    "replayed": False,
                    "session_authority": current.session_authority,
                    "snapshot": self._core_active_snapshot_locked(
                        current,
                        self._require_core_session_locked(current.session_id),
                    ),
                }
                self._idempotency[scope] = _IdempotencyRecord(
                    body_sha256=body_sha256,
                    result=deepcopy(result),
                    error=None,
                    retain_until_deadline_ms=(
                        current.absolute_deadline_ms + self._policy.tombstone_ttl_ms
                    ),
                    session_id=current.session_id,
                )
                return result

            error = ProductLifecycleError(
                code="current_session_exists",
                message="A current Presenter Stage session already exists.",
                status=409,
                retry="resume_snapshot",
                context={
                    "current_selected_record_id": current.selected_record_id,
                    "requested_selected_record_id": grant.selected_record_id,
                },
            )
            self._idempotency[scope] = _IdempotencyRecord(
                body_sha256=body_sha256,
                result=None,
                error=_RecordedError.capture(error),
                retain_until_deadline_ms=(
                    current.absolute_deadline_ms + self._policy.tombstone_ttl_ms
                ),
                session_id=current.session_id,
            )
            raise error

    def create(
        self,
        *,
        actor_authority: str,
        launch_grant: str,
        expected_epoch: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Expose Stage launch through the shared create-shaped client seam."""
        return self.launch(
            actor_authority=actor_authority,
            launch_grant=launch_grant,
            expected_epoch=expected_epoch,
            idempotency_key=idempotency_key,
        )

    def replace(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
        launch_grant: str,
    ) -> dict[str, object]:
        """Explicitly replace Stage through the shared atomic switch path."""
        result = super().switch(
            actor_authority=actor_authority,
            session_authority=session_authority,
            reference=reference,
            expected_epoch=expected_epoch,
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            launch_grant=launch_grant,
        )
        result["kind"] = "stage_session_replaced"
        return result

    def switch(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        idempotency_key: str,
        expected_control_revision: int,
        launch_grant: str,
    ) -> dict[str, object]:
        """Expose Stage replacement through the shared switch-shaped seam."""
        return self.replace(
            actor_authority=actor_authority,
            session_authority=session_authority,
            reference=reference,
            expected_epoch=expected_epoch,
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            launch_grant=launch_grant,
        )

    def _capacity_class_for_session(
        self,
        session: _SessionRecord,
    ) -> ProductCapacityClass:
        if session.actor_key[1] is not ProductRole.PRESENTER:
            raise RuntimeError("non-presenter session reached Presenter Stage")
        return ProductCapacityClass.PRESENTER_STAGE

    def _expire_and_prune_locked(self, deadline_ms: int) -> int:
        """Release expired dynamic capacity after dependent recovery state.

        Dynamic grants are trusted, adjacent provisioning inputs rather than
        the fixed launch catalog owned by an ordinary Stage. Once a grant has
        expired it can no longer authorize work, so its active-capacity slot
        and selected-record binding can be released. Revision display/source
        data remains until no active grant, session, or retained tombstone can
        project it. Legacy Forge credential digests remain reserved for the
        service epoch. Source-neutral grants release their digest and rely on
        their epoch-local serial high-water mark to reject expired credentials.
        """
        expired = super()._expire_and_prune_locked(deadline_ms)
        if not self._dynamic_selection_only:
            return expired

        pruned_grants = 0
        for launch_sha256 in tuple(self._dynamic_stage_grants):
            grant = self._launch_grants.get(launch_sha256)
            if grant is None:
                self._dynamic_stage_grants.discard(launch_sha256)
                if launch_sha256 in self._source_neutral_stage_grants:
                    self._source_neutral_stage_grants.discard(launch_sha256)
                    self._used_value_digests.discard(launch_sha256)
                continue
            if (
                deadline_ms < grant.expires_deadline_ms
                and self._last_wall_ms < grant.expires_at_ms
            ):
                continue
            self._launch_grants.pop(launch_sha256, None)
            self._dynamic_stage_grants.discard(launch_sha256)
            if launch_sha256 in self._source_neutral_stage_grants:
                self._source_neutral_stage_grants.discard(launch_sha256)
                self._used_value_digests.discard(launch_sha256)
            pruned_grants += 1
            if (
                self._dynamic_launch_by_selection.get(grant.selected_record_id)
                == launch_sha256
            ):
                self._dynamic_launch_by_selection.pop(grant.selected_record_id, None)

        retained_selected_records = {
            grant.selected_record_id for grant in self._launch_grants.values()
        }
        retained_selected_records.update(
            session.selected_record_id
            for session in self._sessions_by_reference.values()
        )
        retained_selected_records.update(
            tombstone.selected_record_id
            for tombstone in self._tombstones_by_reference.values()
        )
        pruned_sources = 0
        for selected_record_id in tuple(self._stage_forge_sources):
            if selected_record_id not in retained_selected_records:
                self._stage_forge_sources.pop(selected_record_id, None)
                pruned_sources += 1

        retained_revisions = {
            grant.revision_id for grant in self._launch_grants.values()
        }
        retained_revisions.update(
            session.revision.revision_id
            for session in self._sessions_by_reference.values()
        )
        retained_revisions.update(
            tombstone.revision_id
            for tombstone in self._tombstones_by_reference.values()
        )
        pruned_revisions = 0
        for revision_id in tuple(self._revisions):
            if revision_id in retained_revisions:
                continue
            self._revisions.pop(revision_id, None)
            self._stage_display_metadata.pop(revision_id, None)
            pruned_revisions += 1
        if pruned_grants or pruned_revisions or pruned_sources:
            _LIFECYCLE_LOGGER.info(
                "presenter_stage_dynamic_registry_pruned grants=%d "
                "revisions=%d sources=%d",
                pruned_grants,
                pruned_revisions,
                pruned_sources,
            )
        return expired

    def _core_active_snapshot_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
    ) -> dict[str, object]:
        snapshot = super()._core_active_snapshot_locked(session, core)
        snapshot["stage"] = self._stage_projection(session)
        if self._xray_policy is not None:
            snapshot["available_controls"].append("xray")
        return snapshot

    def _core_terminal_snapshot_locked(
        self,
        tombstone: _SessionTombstone,
    ) -> dict[str, object]:
        snapshot = super()._core_terminal_snapshot_locked(tombstone)
        metadata = self._stage_display_metadata[tombstone.revision_id]
        stage: dict[str, object] = {
            "reference": tombstone.reference,
            "display": _display_projection(metadata),
            "admission_class": "presenter_stage_reserved",
            "xray_available": False,
        }
        self._add_forge_source(stage, tombstone.selected_record_id)
        snapshot["stage"] = stage
        return snapshot

    def _stage_projection(self, session: _SessionRecord) -> dict[str, object]:
        metadata = self._stage_display_metadata[session.revision.revision_id]
        stage: dict[str, object] = {
            "reference": session.reference,
            "display": _display_projection(metadata),
            "admission_class": "presenter_stage_reserved",
            "xray_available": self._xray_policy is not None,
        }
        self._add_forge_source(stage, session.selected_record_id)
        return stage

    def xray_snapshot(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        """Return one presenter-only, accepted-state-consistent X-ray snapshot."""

        with self._lock:
            self._require_xray_enabled()
            super().snapshot(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            session = self._sessions_by_reference.get(reference)
            if session is None:
                raise _error(
                    "session_closed",
                    "The Presenter Stage session is no longer active.",
                    status=409,
                    retry="resume_snapshot",
                )
            core = self._require_core_session_locked(session.session_id)
            state = self._xray_state_locked(session, core)
            return {
                "kind": "stage_xray_snapshot",
                "service_epoch": self._service_epoch,
                "xray": self._xray_snapshot_projection_locked(core, state),
            }

    def xray_detail(
        self,
        *,
        actor_authority: str,
        session_authority: str,
        reference: str,
        expected_epoch: str | None,
        entity_id: str,
        expected_accepted_state_revision: int,
        expected_xray_stream_id: str,
        expected_xray_sequence: int,
    ) -> dict[str, object]:
        """Read one safe entity only when both X-ray coordinates still match."""

        with self._lock:
            self._require_xray_enabled()
            super().snapshot(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
            session = self._sessions_by_reference.get(reference)
            if session is None:
                raise _error(
                    "session_closed",
                    "The Presenter Stage session is no longer active.",
                    status=409,
                    retry="resume_snapshot",
                )
            core = self._require_core_session_locked(session.session_id)
            state = self._xray_state_locked(session, core)
            if (
                isinstance(expected_accepted_state_revision, bool)
                or not isinstance(expected_accepted_state_revision, int)
                or expected_accepted_state_revision < 0
                or expected_accepted_state_revision > _MAX_SAFE_INTEGER
                or not isinstance(expected_xray_stream_id, str)
                or not expected_xray_stream_id
                or isinstance(expected_xray_sequence, bool)
                or not isinstance(expected_xray_sequence, int)
                or expected_xray_sequence <= 0
                or expected_xray_sequence > _MAX_SAFE_INTEGER
            ):
                raise _error(
                    "invalid_request",
                    "The Stage X-ray detail precondition is invalid.",
                    status=400,
                )
            if (
                expected_accepted_state_revision != core.accepted_state_revision
                or expected_xray_stream_id != state.stream_id
                or expected_xray_sequence != state.sequence
            ):
                raise _error(
                    "xray_resync_required",
                    "The Stage X-ray projection changed; obtain a fresh snapshot.",
                    status=409,
                    retry="resume_xray_snapshot",
                )
            if not isinstance(entity_id, str) or len(entity_id) > 128:
                raise _error(
                    "invalid_request",
                    "The Stage X-ray entity identifier is invalid.",
                    status=400,
                )
            projection = state.projection
            detail = (
                projection.details.get(entity_id) if projection is not None else None
            )
            if detail is None:
                raise _error(
                    "xray_entity_unavailable",
                    "The Stage X-ray entity is unavailable; obtain a fresh snapshot.",
                    status=404,
                    retry="resume_xray_snapshot",
                )
            return {
                "kind": "stage_xray_detail",
                "service_epoch": self._service_epoch,
                "xray": {
                    **self._xray_coordinates_locked(core, state),
                    "detail": deepcopy(dict(detail)),
                },
            }

    def _require_xray_enabled(self) -> StageXrayPolicy:
        policy = self._xray_policy
        if policy is None:
            raise _error(
                "xray_unavailable",
                "Stage X-ray is unavailable for this service.",
                status=404,
                retry="none",
            )
        return policy

    def _xray_state_locked(
        self,
        session: _SessionRecord,
        core: _CoreSessionState,
    ) -> _StageXraySession:
        policy = self._require_xray_enabled()
        if (
            isinstance(core.accepted_state_revision, bool)
            or not isinstance(core.accepted_state_revision, int)
            or not 0 <= core.accepted_state_revision <= _MAX_SAFE_INTEGER
        ):
            raise _error(
                "xray_unavailable",
                "Stage X-ray is unavailable for this service.",
                status=404,
                retry="none",
            )
        active_record = (
            core.commands.get(core.active_command_id)
            if core.active_command_id is not None
            else None
        )
        active_command = (
            {
                "command_id": active_record.command_id,
                "status": active_record.status,
            }
            if active_record is not None
            else None
        )
        projection = build_stage_xray_projection(
            session.runtime_document,
            policy=policy,
            active_command=active_command,
        )
        state = self._xray_sessions.get(session.session_id)
        if state is None:
            stream_id, _digest = self._new_core_identity_locked(
                "stage_xray_stream",
                "xray-stream",
            )
            state = _StageXraySession(stream_id=stream_id)
            self._xray_sessions[session.session_id] = state
        sequence_needs_rotation = (
            isinstance(state.sequence, bool)
            or not isinstance(state.sequence, int)
            or state.sequence < 0
            or state.sequence >= _MAX_SAFE_INTEGER
        )
        position_invalid = state.projection is not None and (
            sequence_needs_rotation
            and state.sequence != _MAX_SAFE_INTEGER
            or state.sequence == 0
        )
        if (
            state.projection is None
            or state.accepted_state_revision != core.accepted_state_revision
            or state.content_sha256 != projection.content_sha256
            or position_invalid
        ):
            if sequence_needs_rotation:
                stream_id, _digest = self._new_core_identity_locked(
                    "stage_xray_stream",
                    "xray-stream",
                )
                state.stream_id = stream_id
                state.sequence = 1
            else:
                state.sequence += 1
            state.accepted_state_revision = core.accepted_state_revision
            state.content_sha256 = projection.content_sha256
            state.projection = projection
        return state

    def _xray_coordinates_locked(
        self,
        core: _CoreSessionState,
        state: _StageXraySession,
    ) -> dict[str, object]:
        event = core.journal.event_position.to_dict()
        return {
            "schema_revision": XRAY_SCHEMA_REVISION,
            "accepted_state_revision": core.accepted_state_revision,
            "projection_position": {
                "stream_id": state.stream_id,
                "sequence": state.sequence,
                "cursor": f"xray-cursor:{state.stream_id.removeprefix('xray-stream:')}:{state.sequence}",
            },
            "stage_event_watermark": event,
        }

    def _xray_snapshot_projection_locked(
        self,
        core: _CoreSessionState,
        state: _StageXraySession,
    ) -> dict[str, object]:
        projection = state.projection
        if projection is None:
            raise RuntimeError("Stage X-ray projection was not initialized")
        snapshot = deepcopy(dict(projection.snapshot))
        snapshot.update(self._xray_coordinates_locked(core, state))
        return snapshot

    def _terminalize_locked(
        self,
        session: _SessionRecord,
        *,
        terminal_at_ms: int,
        terminal_deadline_ms: int,
        terminal_reason: str,
    ) -> _SessionTombstone:
        tombstone = super()._terminalize_locked(
            session,
            terminal_at_ms=terminal_at_ms,
            terminal_deadline_ms=terminal_deadline_ms,
            terminal_reason=terminal_reason,
        )
        self._xray_sessions.pop(session.session_id, None)
        return tombstone

    def _add_forge_source(
        self,
        stage: dict[str, object],
        selected_record_id: str,
    ) -> None:
        source = self._stage_forge_sources.get(selected_record_id)
        if source is not None:
            stage["source"] = _forge_source_projection(source)

    def _require_actor(
        self,
        actor_authority: str,
        deadline_ms: int,
    ) -> _ActorAuthorityRecord:
        actor = super()._require_actor(actor_authority, deadline_ms)
        if self._last_wall_ms >= actor.expires_at_ms:
            raise _error(
                "authority_denied",
                "The requested product resource is unavailable.",
                status=403,
            )
        return actor

    def _require_launch_grant(
        self,
        *,
        launch_sha256: str,
        actor: _ActorAuthorityRecord,
        deadline_ms: int,
        operation: ProductOperation = ProductOperation.CREATE,
    ) -> _LaunchGrantRecord:
        grant = super()._require_launch_grant(
            launch_sha256=launch_sha256,
            actor=actor,
            deadline_ms=deadline_ms,
            operation=operation,
        )
        if self._last_wall_ms >= grant.expires_at_ms:
            raise _error(
                "authority_denied",
                "The requested product resource is unavailable.",
                status=403,
            )
        return grant

    def _stage_launch_grant(
        self,
        *,
        launch_sha256: str,
        actor_key: tuple[str, ProductRole],
        deadline_ms: int,
    ) -> _LaunchGrantRecord:
        grant = self._launch_grants.get(launch_sha256)
        if (
            grant is None
            or grant.service_epoch != self._service_epoch
            or grant.actor_key != actor_key
            or ProductOperation.CREATE not in grant.allowed_operations
            or deadline_ms >= grant.expires_deadline_ms
            or self._last_wall_ms >= grant.expires_at_ms
        ):
            raise _error(
                "authority_denied",
                "The requested product resource is unavailable.",
                status=403,
            )
        return grant

    @staticmethod
    def _can_resume(
        session: _SessionRecord,
        grant: _LaunchGrantRecord,
        launch_sha256: str,
    ) -> bool:
        return (
            session.launch_grant_sha256 == launch_sha256
            and grant.consumed_session_id == session.session_id
            and session.selected_record_id == grant.selected_record_id
            and session.revision.revision_id == grant.revision_id
            and session.grant_binding_sha256 == grant.binding_sha256
            and session.actor_key == grant.actor_key
            and dict(session.limits) == dict(grant.limits)
            and session.policy_id == grant.policy_id
            and session.notice_version == grant.notice_version
            and session.absolute_deadline_ms <= grant.expires_deadline_ms
        )


def _display_projection(metadata: StageDisplayMetadataSeed) -> dict[str, str]:
    return {
        "title": metadata.title,
        "source_label": metadata.source_label,
        "revision_label": metadata.revision_label,
    }


def _forge_source_projection(source: StageForgeIdentitySeed) -> dict[str, str]:
    return {
        "kind": "world_forge_selection",
        "selected_record_id": source.selected_record_id,
        "immutable_digest": source.immutable_digest,
        "forge_job_id": source.forge_job_id,
        "forge_reference": source.forge_reference,
    }


def _same_launch_binding(
    existing: _LaunchGrantRecord,
    candidate: _LaunchGrantRecord,
) -> bool:
    return (
        existing.binding_sha256 == candidate.binding_sha256
        and existing.service_epoch == candidate.service_epoch
        and existing.expires_deadline_ms == candidate.expires_deadline_ms
    )


def _provisioning_result(
    *,
    service_epoch: str,
    revision_id: str,
    source: StageForgeIdentitySeed,
    reused: bool,
) -> StageSelectionProvisioningResult:
    return StageSelectionProvisioningResult(
        service_epoch=service_epoch,
        selected_record_id=source.selected_record_id,
        revision_id=revision_id,
        immutable_digest=source.immutable_digest,
        reused=reused,
    )


def _registered_provisioning_result(
    *,
    service_epoch: str,
    revision_id: str,
    source: StageLaunchSelectionIdentitySeed,
    reused: bool,
) -> StageSelectionProvisioningResult:
    return StageSelectionProvisioningResult(
        service_epoch=service_epoch,
        selected_record_id=source.selected_record_id,
        revision_id=revision_id,
        immutable_digest=source.immutable_digest,
        reused=reused,
    )


def _require_stage_label(value: str, name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(
            ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
            for character in value
        )
    ):
        raise ValueError(f"{name} must be a bounded display-safe label")
