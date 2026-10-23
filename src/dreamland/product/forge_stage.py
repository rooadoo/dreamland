"""Inert protocol-neutral coordination for selected Forge-to-Stage handoff.

The coordinator consumes only an authoritative, presenter-owned selection from
the live World Forge service.  It reopens the exact content-addressed artifact,
provisions a narrow private Stage grant, and delegates all session allocation,
resume, replacement, and command lifecycle semantics to ``PresenterStageService``.

Importing this module creates no provider, listener, worker, file, or runtime.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from dreamland.product.forge import (
    ForgeSelectedRevisionRecord,
    ForgeServiceError,
    WorldForgeService,
)
from dreamland.product.forge_artifacts import ImmutableForgeStore
from dreamland.product.lifecycle import (
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
    _canonical_sha256,
    _credential_sha256,
    _require_idempotency_key,
)
from dreamland.product.stage import (
    PresenterStageService,
    StageDisplayMetadataSeed,
    StageForgeIdentitySeed,
    StageSelectionCapacityError,
)

_MAXIMUM_BINDINGS = 64
_MAXIMUM_IDEMPOTENCY_RECORDS = 256
_MAXIMUM_LIMIT_ENTRIES = 32
_MAXIMUM_LIMIT_VALUE = 1_000_000_000
_LIMIT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
_FAILURE_LOGGER = logging.getLogger("dreamland.failures")


@dataclass(frozen=True, slots=True)
class ForgeStageHandoffPolicy:
    """Bounded effective Stage policy for one explicit handoff composition."""

    launch_limits: Mapping[str, int]
    notice_version: str
    title: str = "Audience-created DREAMLAND world"
    source_label: str = "Live World Forge"
    maximum_bindings: int = 16
    maximum_idempotency_records: int = 128

    def __post_init__(self) -> None:
        limits = dict(self.launch_limits)
        if (
            not limits
            or len(limits) > _MAXIMUM_LIMIT_ENTRIES
            or any(
                not isinstance(name, str)
                or _LIMIT_NAME_PATTERN.fullmatch(name) is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAXIMUM_LIMIT_VALUE
                for name, value in limits.items()
            )
        ):
            raise ValueError("Forge-to-Stage launch limits must be positive integers")
        if (
            not isinstance(self.notice_version, str)
            or _SAFE_IDENTIFIER_PATTERN.fullmatch(self.notice_version) is None
        ):
            raise ValueError("notice_version must be a safe identifier")
        for name, value, maximum in (
            ("title", self.title, 120),
            ("source_label", self.source_label, 64),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(
                    ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
                    for character in value
                )
            ):
                raise ValueError(f"{name} must be a bounded display-safe value")
        if (
            isinstance(self.maximum_bindings, bool)
            or not isinstance(self.maximum_bindings, int)
            or not 1 <= self.maximum_bindings <= _MAXIMUM_BINDINGS
        ):
            raise ValueError("maximum_bindings is outside the supported range")
        if (
            isinstance(self.maximum_idempotency_records, bool)
            or not isinstance(self.maximum_idempotency_records, int)
            or not (
                self.maximum_bindings
                <= self.maximum_idempotency_records
                <= _MAXIMUM_IDEMPOTENCY_RECORDS
            )
        ):
            raise ValueError(
                "maximum_idempotency_records must cover the binding capacity"
            )
        object.__setattr__(self, "launch_limits", MappingProxyType(limits))


def forge_stage_display_metadata(
    selected: ForgeSelectedRevisionRecord,
    policy: ForgeStageHandoffPolicy,
) -> StageDisplayMetadataSeed:
    """Return the one Stage display tuple shared by all Forge launch seams."""
    if not isinstance(selected, ForgeSelectedRevisionRecord):
        raise TypeError("selected must be ForgeSelectedRevisionRecord")
    if not isinstance(policy, ForgeStageHandoffPolicy):
        raise TypeError("policy must be ForgeStageHandoffPolicy")
    return StageDisplayMetadataSeed(
        revision_id=selected.revision_id,
        title=policy.title,
        source_label=policy.source_label,
        revision_label=(
            "World Forge " + selected.immutable_digest.removeprefix("sha256:")[:12]
        ),
    )


@dataclass(frozen=True, slots=True)
class _HandoffBinding:
    selected: ForgeSelectedRevisionRecord
    launch_grant: str


@dataclass(frozen=True, slots=True)
class _RecordedHandoffError:
    code: str
    message: str
    status: int
    retry: str
    retry_after_ms: int | None
    context: Mapping[str, str]

    @classmethod
    def capture(cls, error: ProductLifecycleError) -> _RecordedHandoffError:
        return cls(
            error.code,
            error.message,
            error.status,
            error.retry,
            error.retry_after_ms,
            dict(error.context),
        )

    def restore(self) -> ProductLifecycleError:
        return ProductLifecycleError(
            code=self.code,
            message=self.message,
            status=self.status,
            retry=self.retry,
            retry_after_ms=self.retry_after_ms,
            context=self.context,
        )


@dataclass(slots=True)
class _HandoffClaim:
    body_sha256: str
    selected_record_id: str | None = None
    error: _RecordedHandoffError | None = None


class ForgeStageHandoffService:
    """Resolve one live Forge selection into the exact private Stage session."""

    def __init__(
        self,
        *,
        forge: WorldForgeService,
        artifacts: ImmutableForgeStore,
        stage: PresenterStageService,
        policy: ForgeStageHandoffPolicy,
        token_factory: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(forge, WorldForgeService):
            raise TypeError("forge must be WorldForgeService")
        if not isinstance(artifacts, ImmutableForgeStore):
            raise TypeError("artifacts must be ImmutableForgeStore")
        if not isinstance(stage, PresenterStageService):
            raise TypeError("stage must be PresenterStageService")
        if not isinstance(policy, ForgeStageHandoffPolicy):
            raise TypeError("policy must be ForgeStageHandoffPolicy")
        self._forge = forge
        self._artifacts = artifacts
        self._stage = stage
        self._policy = policy
        self._token_factory = token_factory or (
            lambda _purpose: f"launch-grant:{secrets.token_urlsafe(32)}"
        )
        self._lock = threading.RLock()
        self._bindings: dict[str, _HandoffBinding] = {}
        self._claims: dict[
            tuple[str, str, str, str],
            _HandoffClaim,
        ] = {}

    @property
    def forge_service_epoch(self) -> str:
        return self._forge.service_epoch

    @property
    def stage_service_epoch(self) -> str:
        return self._stage.service_epoch

    def launch(
        self,
        *,
        actor_authority: str,
        forge_expected_epoch: str,
        stage_expected_epoch: str,
        forge_reference: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Create or resume Stage from one exact selected Forge record."""

        body = {
            "forge_expected_epoch": forge_expected_epoch,
            "forge_reference": forge_reference,
            "operation": "launch",
        }
        return self._coordinate(
            actor_authority=actor_authority,
            forge_expected_epoch=forge_expected_epoch,
            stage_expected_epoch=stage_expected_epoch,
            forge_reference=forge_reference,
            idempotency_key=idempotency_key,
            operation="launch",
            body=body,
            session_authority=None,
            stage_reference=None,
            expected_control_revision=None,
        )

    def replace(
        self,
        *,
        actor_authority: str,
        forge_expected_epoch: str,
        stage_expected_epoch: str,
        forge_reference: str,
        idempotency_key: str,
        session_authority: str,
        stage_reference: str,
        expected_control_revision: int,
    ) -> dict[str, object]:
        """Explicitly replace current Stage from a different Forge selection."""

        body = {
            "expected_control_revision": expected_control_revision,
            "forge_expected_epoch": forge_expected_epoch,
            "forge_reference": forge_reference,
            "operation": "replace",
            "stage_reference": stage_reference,
        }
        return self._coordinate(
            actor_authority=actor_authority,
            forge_expected_epoch=forge_expected_epoch,
            stage_expected_epoch=stage_expected_epoch,
            forge_reference=forge_reference,
            idempotency_key=idempotency_key,
            operation="replace",
            body=body,
            session_authority=session_authority,
            stage_reference=stage_reference,
            expected_control_revision=expected_control_revision,
        )

    def _coordinate(
        self,
        *,
        actor_authority: str,
        forge_expected_epoch: str,
        stage_expected_epoch: str,
        forge_reference: str,
        idempotency_key: str,
        operation: str,
        body: Mapping[str, object],
        session_authority: str | None,
        stage_reference: str | None,
        expected_control_revision: int | None,
    ) -> dict[str, object]:
        self._require_stage_epoch(stage_expected_epoch)
        try:
            self._forge.authorize(
                actor_authority=actor_authority,
                expected_epoch=forge_expected_epoch,
            )
        except ForgeServiceError as error:
            raise _forge_error(error) from None
        key = _require_idempotency_key(idempotency_key)
        body_sha256 = _canonical_sha256(body)
        scope = (
            stage_expected_epoch,
            _credential_sha256(actor_authority),
            operation,
            key,
        )
        with self._lock:
            claim = self._claims.get(scope)
            if claim is not None:
                if claim.body_sha256 != body_sha256:
                    _LIFECYCLE_LOGGER.info(
                        "forge_stage_handoff_rejected operation=%s "
                        "code=idempotency_conflict",
                        operation,
                    )
                    raise ProductLifecycleError(
                        code="idempotency_conflict",
                        message=(
                            "The idempotency key is already bound to a different "
                            "request."
                        ),
                        status=409,
                        retry="none",
                    )
                if claim.error is not None:
                    restored = claim.error.restore()
                    _LIFECYCLE_LOGGER.info(
                        "forge_stage_handoff_replayed_rejection operation=%s code=%s",
                        operation,
                        restored.code,
                    )
                    raise restored
                if claim.selected_record_id is None:
                    raise RuntimeError("Forge-to-Stage claim is incomplete")
                binding = self._bindings.get(claim.selected_record_id)
                if binding is None:
                    raise RuntimeError("Forge-to-Stage binding is unavailable")
                result = self._invoke_stage(
                    binding=binding,
                    actor_authority=actor_authority,
                    stage_expected_epoch=stage_expected_epoch,
                    idempotency_key=key,
                    operation=operation,
                    session_authority=session_authority,
                    stage_reference=stage_reference,
                    expected_control_revision=expected_control_revision,
                )
                self._log_success(operation, binding.selected, result)
                return result
            if len(self._claims) >= self._policy.maximum_idempotency_records:
                _LIFECYCLE_LOGGER.warning(
                    "forge_stage_handoff_rejected operation=%s "
                    "code=idempotency_capacity_exhausted",
                    operation,
                )
                raise ProductLifecycleError(
                    code="idempotency_capacity_exhausted",
                    message="Forge-to-Stage replay capacity is exhausted.",
                    status=503,
                    retry="none",
                )
            claim = _HandoffClaim(body_sha256)
            self._claims[scope] = claim
            try:
                selected = self._forge.resolve_selected_record(
                    actor_authority=actor_authority,
                    expected_epoch=forge_expected_epoch,
                    job_reference=forge_reference,
                )
                binding = self._require_binding(
                    selected=selected,
                    actor_authority=actor_authority,
                    stage_expected_epoch=stage_expected_epoch,
                )
                result = self._invoke_stage(
                    binding=binding,
                    actor_authority=actor_authority,
                    stage_expected_epoch=stage_expected_epoch,
                    idempotency_key=key,
                    operation=operation,
                    session_authority=session_authority,
                    stage_reference=stage_reference,
                    expected_control_revision=expected_control_revision,
                )
            except ForgeServiceError as error:
                converted = _forge_error(error)
                claim.error = _RecordedHandoffError.capture(converted)
                _LIFECYCLE_LOGGER.info(
                    "forge_stage_handoff_rejected operation=%s code=%s",
                    operation,
                    converted.code,
                )
                raise converted from None
            except ProductLifecycleError as error:
                claim.error = _RecordedHandoffError.capture(error)
                _LIFECYCLE_LOGGER.info(
                    "forge_stage_handoff_rejected operation=%s code=%s",
                    operation,
                    error.code,
                )
                raise
            except StageSelectionCapacityError:
                capacity_error = ProductLifecycleError(
                    code="handoff_capacity_exhausted",
                    message="Forge-to-Stage selection capacity is exhausted.",
                    status=503,
                    retry="none",
                )
                claim.error = _RecordedHandoffError.capture(capacity_error)
                _LIFECYCLE_LOGGER.warning(
                    "forge_stage_handoff_rejected operation=%s code=%s",
                    operation,
                    capacity_error.code,
                )
                raise capacity_error from None
            except (OSError, TypeError, ValueError) as failure:
                artifact_error = ProductLifecycleError(
                    code="selected_artifact_unavailable",
                    message="The selected World Forge artifact is unavailable.",
                    status=409,
                    retry="none",
                )
                claim.error = _RecordedHandoffError.capture(artifact_error)
                _FAILURE_LOGGER.warning(
                    "forge_stage_handoff_artifact_rejected operation=%s "
                    "failure_type=%s",
                    operation,
                    type(failure).__name__,
                )
                raise artifact_error from None
            except Exception as failure:
                # Unknown implementation/provider failures are not safe,
                # deterministic semantic outcomes.  Do not poison the key with
                # an incomplete claim; the transport maps the original failure
                # to its bounded internal-error response.
                self._claims.pop(scope, None)
                _FAILURE_LOGGER.error(
                    "forge_stage_handoff_failed operation=%s failure_type=%s",
                    operation,
                    type(failure).__name__,
                )
                raise
            claim.selected_record_id = selected.selected_record_id
            self._log_success(operation, selected, result)
            return result

    def _require_binding(
        self,
        *,
        selected: ForgeSelectedRevisionRecord,
        actor_authority: str,
        stage_expected_epoch: str,
    ) -> _HandoffBinding:
        existing = self._bindings.get(selected.selected_record_id)
        if existing is not None:
            if existing.selected != selected:
                raise ValueError("selected record identity changed")
            return existing
        if len(self._bindings) >= self._policy.maximum_bindings:
            raise ProductLifecycleError(
                code="handoff_capacity_exhausted",
                message="Forge-to-Stage selection capacity is exhausted.",
                status=503,
                retry="none",
            )
        artifact = self._artifacts.load(
            selected.revision_id,
            selected.immutable_digest,
        )
        launch_grant = self._token_factory("forge-stage-launch-grant")
        expires_at_ms = _timestamp_ms(selected.expires_at)
        display_metadata = forge_stage_display_metadata(selected, self._policy)
        self._stage.provision_selection(
            actor_authority=actor_authority,
            expected_epoch=stage_expected_epoch,
            revision=ImmutableRevisionSeed(
                revision_id=selected.revision_id,
                document=artifact.document,
            ),
            launch_grant=LaunchGrantSeed(
                token=launch_grant,
                selected_record_id=selected.selected_record_id,
                actor_id=selected.actor_id,
                role=ProductRole.PRESENTER,
                revision_id=selected.revision_id,
                expires_at_ms=expires_at_ms,
                allowed_operations=frozenset(
                    {ProductOperation.CREATE, ProductOperation.SWITCH}
                ),
                limits=self._policy.launch_limits,
                policy_id=selected.policy_id,
                notice_version=self._policy.notice_version,
            ),
            display_metadata=display_metadata,
            forge_identity=StageForgeIdentitySeed(
                selected_record_id=selected.selected_record_id,
                immutable_digest=selected.immutable_digest,
                forge_job_id=selected.job_id,
                forge_reference=selected.job_reference,
            ),
        )
        binding = _HandoffBinding(selected, launch_grant)
        self._bindings[selected.selected_record_id] = binding
        return binding

    def _invoke_stage(
        self,
        *,
        binding: _HandoffBinding,
        actor_authority: str,
        stage_expected_epoch: str,
        idempotency_key: str,
        operation: str,
        session_authority: str | None,
        stage_reference: str | None,
        expected_control_revision: int | None,
    ) -> dict[str, object]:
        if operation == "launch":
            stage_result = self._stage.launch(
                actor_authority=actor_authority,
                launch_grant=binding.launch_grant,
                expected_epoch=stage_expected_epoch,
                idempotency_key=idempotency_key,
            )
        elif (
            operation == "replace"
            and session_authority is not None
            and stage_reference is not None
            and expected_control_revision is not None
        ):
            stage_result = self._stage.replace(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=stage_reference,
                expected_epoch=stage_expected_epoch,
                idempotency_key=idempotency_key,
                expected_control_revision=expected_control_revision,
                launch_grant=binding.launch_grant,
            )
        else:
            raise RuntimeError("Forge-to-Stage operation is invalid")
        return _handoff_projection(binding.selected, stage_result)

    @staticmethod
    def _log_success(
        operation: str,
        selected: ForgeSelectedRevisionRecord,
        result: Mapping[str, object],
    ) -> None:
        _LIFECYCLE_LOGGER.info(
            "forge_stage_handoff_completed operation=%s kind=%s replayed=%s "
            "selected_record_id=%s revision_id=%s",
            operation,
            result.get("kind"),
            result.get("replayed"),
            selected.selected_record_id,
            selected.revision_id,
        )

    def _require_stage_epoch(self, expected: str) -> None:
        if not isinstance(expected, str) or not expected:
            raise ProductLifecycleError(
                code="expected_epoch_required",
                message="Expected product service epoch evidence is required.",
                status=400,
                retry="none",
            )
        if expected != self._stage.service_epoch:
            raise ProductLifecycleError(
                code="outcome_unknown",
                message=(
                    "The product service restarted before this mutation could be "
                    "reconciled."
                ),
                status=410,
                retry="none",
            )


def _handoff_projection(
    selected: ForgeSelectedRevisionRecord,
    stage_result: Mapping[str, object],
) -> dict[str, object]:
    stage_kind = stage_result.get("kind")
    if not isinstance(stage_kind, str):
        raise RuntimeError("Stage returned an invalid handoff result")
    kinds = {
        "stage_session_created": "forge_stage_session_created",
        "stage_session_resumed": "forge_stage_session_resumed",
        "stage_session_replaced": "forge_stage_session_replaced",
    }
    kind = kinds.get(stage_kind)
    if kind is None:
        raise RuntimeError("Stage returned an invalid handoff result")
    result = dict(stage_result)
    result["kind"] = kind
    result["forge_selection"] = {
        "service_epoch": selected.service_epoch,
        "selected_record_id": selected.selected_record_id,
        "job_id": selected.job_id,
        "reference": selected.job_reference,
        "selected_at": selected.selected_at,
        "expires_at": selected.expires_at,
        "policy_id": selected.policy_id,
        "receipt_correlation_id": selected.receipt_correlation_id,
        "revision": {
            "revision_id": selected.revision_id,
            "immutable_digest": selected.immutable_digest,
        },
    }
    return result


def _forge_error(error: ForgeServiceError) -> ProductLifecycleError:
    if error.code == "service_epoch_changed":
        return ProductLifecycleError(
            code="outcome_unknown",
            message=(
                "The World Forge service restarted before this mutation could be "
                "reconciled."
            ),
            status=410,
            retry="none",
        )
    if error.code == "job_not_found":
        return ProductLifecycleError(
            code="selected_record_unavailable",
            message="The selected World Forge revision is unavailable.",
            status=404,
            retry="none",
        )
    retry = {
        "none": "none",
        "same_request": "same_request",
        "resume": "resume_snapshot",
    }.get(error.retry, "none")
    return ProductLifecycleError(
        code=error.code,
        message=error.message,
        status=error.status,
        retry=retry,
    )


def _timestamp_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("selected record expiry is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("selected record expiry requires a timezone")
    milliseconds = int(parsed.timestamp() * 1_000)
    if milliseconds < 0:
        raise ValueError("selected record expiry is invalid")
    return milliseconds
