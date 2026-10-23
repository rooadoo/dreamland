"""Inert source-neutral launch-selection coordination for Presenter Stage.

The coordinator joins two explicitly constructed, process-local services that
share one epoch: a :class:`LaunchSelectionRegistry` and a dynamic-only
:class:`PresenterStageService`.  Listing and exchanging selections performs no
provider or command-executor work.  An exchange provisions only the exact
immutable registry result into Stage, and the resulting launch grant remains
the sole authority accepted by Stage launch or replacement operations.

Importing this module creates no provider, listener, worker, file, or runtime.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

from dreamland.product.launch_selection import (
    LaunchGrantIssue,
    LaunchGrantProvisioningCapacityError,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionRegistry,
    ResolvedLaunchSelection,
)
from dreamland.product.lifecycle import (
    MAX_IMMUTABLE_REVISION_BYTES,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    ProductRole,
)
from dreamland.product.stage import (
    PresenterStageService,
    StageDisplayMetadataSeed,
    StageLaunchSelectionIdentitySeed,
    StageLaunchSelectionOrigin,
    StageSelectionCapacityError,
)

_LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
_FAILURE_LOGGER = logging.getLogger("dreamland.failures")

_STAGE_ORIGIN = {
    LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT: (
        StageLaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
    ),
    LaunchSelectionOrigin.WORLD_FORGE_SELECTION: (
        StageLaunchSelectionOrigin.WORLD_FORGE_SELECTION
    ),
}


class PresenterStageLaunchSelectionService:
    """Exchange safe selection locators for Stage-owned launch authority."""

    def __init__(
        self,
        *,
        registry: LaunchSelectionRegistry,
        stage: PresenterStageService,
    ) -> None:
        if not isinstance(registry, LaunchSelectionRegistry):
            raise TypeError("registry must be LaunchSelectionRegistry")
        if not isinstance(stage, PresenterStageService):
            raise TypeError("stage must be PresenterStageService")
        if not stage._dynamic_selection_only:
            raise ValueError("launch-selection Stage must be dynamic-selection-only")
        if registry.service_epoch != stage.service_epoch:
            raise ValueError("launch-selection and Stage service epochs must match")
        _require_compatible_composition(registry, stage)
        self._registry = registry
        self._stage = stage

    @property
    def service_epoch(self) -> str:
        """Return the one epoch shared by selection and Stage."""
        return self._registry.service_epoch

    def options(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
    ) -> dict[str, object]:
        """Return Presenter-visible, non-authorizing immutable launch options."""
        try:
            options = [
                _stage_option(projection)
                for projection in self._registry.offerings(
                    actor_authority=actor_authority,
                    expected_epoch=expected_epoch,
                    required_role=ProductRole.PRESENTER,
                )
            ]
        except LaunchSelectionError as error:
            _LIFECYCLE_LOGGER.info(
                "presenter_stage_launch_options_rejected code=%s", error.code
            )
            raise
        except Exception as failure:
            _FAILURE_LOGGER.error(
                "presenter_stage_launch_options_failed failure_type=%s",
                type(failure).__name__,
            )
            raise
        _LIFECYCLE_LOGGER.info(
            "presenter_stage_launch_options_listed count=%d", len(options)
        )
        return {
            "kind": "presenter_stage_launch_options",
            "service_epoch": self.service_epoch,
            "options": options,
        }

    def exchange(
        self,
        *,
        actor_authority: str,
        expected_epoch: str | None,
        launch_selection_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Provision and return one exact, private, Stage-compatible grant."""

        def provision(
            issue: LaunchGrantIssue,
            resolved: ResolvedLaunchSelection,
        ) -> None:
            projection = _stage_option(issue.selection_projection())
            _require_exact_result(
                service_epoch=self.service_epoch,
                issue=issue,
                resolved=resolved,
                option=projection,
            )
            selected_record_id = _selected_record_id(issue.launch_grant)
            revision = ImmutableRevisionSeed(
                revision_id=resolved.revision.revision_id,
                document=resolved.revision.allocate(),
            )
            launch_grant = LaunchGrantSeed(
                token=issue.launch_grant,
                selected_record_id=selected_record_id,
                actor_id=resolved.actor_id,
                role=resolved.role,
                revision_id=resolved.revision.revision_id,
                expires_at_ms=resolved.expires_at_ms,
                allowed_operations=resolved.allowed_operations,
                limits=resolved.limits,
                policy_id=resolved.policy_id,
                notice_version=resolved.notice_version,
            )
            display = _mapping(projection["display"], "selection display")
            display_metadata = StageDisplayMetadataSeed(
                revision_id=resolved.revision.revision_id,
                title=_string(display["title"], "selection title"),
                source_label=_string(display["source_label"], "selection source label"),
                revision_label=_string(
                    display["revision_label"], "selection revision label"
                ),
            )
            selection_identity = StageLaunchSelectionIdentitySeed(
                selected_record_id=selected_record_id,
                immutable_digest=resolved.revision.immutable_digest,
                origin=_STAGE_ORIGIN[resolved.origin],
            )
            try:
                # This provisioning commit is deliberately the callback's final
                # action.  The registry publishes the grant only after it returns.
                self._stage.provision_launch_selection(
                    actor_authority=actor_authority,
                    expected_epoch=issue.service_epoch,
                    revision=revision,
                    launch_grant=launch_grant,
                    display_metadata=display_metadata,
                    selection_identity=selection_identity,
                    expires_deadline_ms=resolved.expires_deadline_ms,
                )
            except StageSelectionCapacityError:
                raise LaunchGrantProvisioningCapacityError() from None

        try:
            issue = self._registry.exchange_selection(
                actor_authority=actor_authority,
                expected_epoch=expected_epoch,
                launch_selection_id=launch_selection_id,
                idempotency_key=idempotency_key,
                provision=provision,
                required_role=ProductRole.PRESENTER,
            )
            # The issue owns the immutable projection captured at exchange time.
            # Exact replays must not depend on the offering still being listed.
            selected_option = _stage_option(issue.selection_projection())
            _require_issue_projection(
                service_epoch=self.service_epoch,
                issue=issue,
                option=selected_option,
            )
        except LaunchSelectionError as error:
            logger = (
                _LIFECYCLE_LOGGER.warning
                if error.retry == "after_delay"
                else _LIFECYCLE_LOGGER.info
            )
            logger("presenter_stage_launch_selection_rejected code=%s", error.code)
            raise
        except Exception as failure:
            _FAILURE_LOGGER.error(
                "presenter_stage_launch_selection_failed failure_type=%s",
                type(failure).__name__,
            )
            raise

        _LIFECYCLE_LOGGER.info(
            "presenter_stage_launch_selection_granted replayed=%s "
            "offering_id=%s origin=%s revision_id=%s",
            issue.replayed,
            issue.offering_id,
            issue.origin.value,
            _mapping(selected_option["revision"], "selection revision")["revision_id"],
        )
        return {
            "kind": "presenter_stage_launch_selection_granted",
            "service_epoch": issue.service_epoch,
            "replayed": issue.replayed,
            "launch_grant": issue.launch_grant,
            "grant_expires_at": _format_timestamp(issue.expires_at_ms),
            "selection": selected_option,
        }


def _require_compatible_composition(
    registry: LaunchSelectionRegistry,
    stage: PresenterStageService,
) -> None:
    """Fail before publishing options that the adjacent Stage cannot accept."""
    if registry._monotonic_ms != stage._monotonic_clock_ms:
        raise ValueError("launch-selection and Stage must share one monotonic clock")
    if not registry._allowed_operation_families.issubset(
        stage._launch_operation_families
    ):
        raise ValueError(
            "launch-selection operations exceed Presenter Stage operations"
        )
    downstream_revision_bytes = min(
        stage._core_policy.max_runtime_document_bytes,
        MAX_IMMUTABLE_REVISION_BYTES,
    )
    if registry._policy.max_revision_bytes > downstream_revision_bytes + 1:
        raise ValueError(
            "launch-selection revision policy exceeds Presenter Stage capacity"
        )

    presenter_actors = {
        digest: actor
        for digest, actor in registry._actors.items()
        if actor.role is ProductRole.PRESENTER
    }
    if not presenter_actors:
        raise ValueError("launch-selection registry has no Presenter authority")
    for digest, actor in presenter_actors.items():
        stage_actor = stage._actors.get(digest)
        if (
            stage_actor is None
            or stage_actor.key != actor.key
            or stage_actor.expires_at_ms < actor.expires_at_ms
        ):
            raise ValueError(
                "launch-selection Presenter authority is incompatible with Stage"
            )


def _stage_option(projection: Mapping[str, object]) -> dict[str, object]:
    expected_keys = {
        "launch_selection_id",
        "offering_id",
        "display",
        "origin",
        "revision",
        "expires_at",
    }
    if set(projection) != expected_keys:
        raise RuntimeError("launch registry returned an invalid offering projection")
    display = _mapping(projection["display"], "selection display")
    origin = _mapping(projection["origin"], "selection origin")
    revision = _mapping(projection["revision"], "selection revision")
    if set(display) != {"title", "source_label", "revision_label"}:
        raise RuntimeError("launch registry returned invalid display metadata")
    if set(origin) != {"kind"}:
        raise RuntimeError("launch registry returned an invalid selection origin")
    if set(revision) != {"revision_id", "sha256"}:
        raise RuntimeError("launch registry returned an invalid revision projection")
    return {
        "launch_selection_id": _string(
            projection["launch_selection_id"], "launch selection ID"
        ),
        "offering_id": _string(projection["offering_id"], "offering ID"),
        "display": {
            "title": _string(display["title"], "selection title"),
            "source_label": _string(display["source_label"], "selection source label"),
            "revision_label": _string(
                display["revision_label"], "selection revision label"
            ),
        },
        "origin": {"kind": _string(origin["kind"], "selection origin")},
        "revision": {
            "revision_id": _string(revision["revision_id"], "revision ID"),
            "sha256": _string(revision["sha256"], "revision digest"),
        },
        "expires_at": projection["expires_at"],
    }


def _require_exact_result(
    *,
    service_epoch: str,
    issue: LaunchGrantIssue,
    resolved: ResolvedLaunchSelection,
    option: Mapping[str, object],
) -> None:
    origin = _mapping(option["origin"], "selection origin")
    revision = _mapping(option["revision"], "selection revision")
    consistent = (
        not issue.replayed
        and issue.service_epoch == service_epoch == resolved.service_epoch
        and issue.launch_selection_id == option["launch_selection_id"]
        and issue.offering_id == resolved.offering_id == option["offering_id"]
        and issue.origin is resolved.origin
        and issue.origin.value == origin["kind"]
        and issue.actor_id == resolved.actor_id
        and issue.role is resolved.role is ProductRole.PRESENTER
        and issue.allowed_operations == resolved.allowed_operations
        and issue.limits == resolved.limits
        and issue.policy_id == resolved.policy_id
        and issue.notice_version == resolved.notice_version
        and issue.expires_at_ms == resolved.expires_at_ms
        and resolved.revision.revision_id == revision["revision_id"]
        and resolved.revision.immutable_digest.removeprefix("sha256:")
        == revision["sha256"]
    )
    if not consistent:
        raise RuntimeError("launch registry returned an inconsistent exchange result")


def _require_issue_projection(
    *,
    service_epoch: str,
    issue: LaunchGrantIssue,
    option: Mapping[str, object],
) -> None:
    origin = _mapping(option["origin"], "selection origin")
    if not (
        issue.service_epoch == service_epoch
        and issue.launch_selection_id == option["launch_selection_id"]
        and issue.offering_id == option["offering_id"]
        and issue.origin.value == origin["kind"]
        and issue.role is ProductRole.PRESENTER
    ):
        raise RuntimeError("launch registry replay diverged from its selection")


def _selected_record_id(launch_grant: str) -> str:
    digest = hashlib.sha256(launch_grant.encode("utf-8")).hexdigest()
    return f"selected-record:{digest}"


def _format_timestamp(value: int) -> str:
    timespec = "seconds" if value % 1_000 == 0 else "milliseconds"
    return (
        datetime.fromtimestamp(value / 1_000, UTC)
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"launch registry returned an invalid {name}")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"launch registry returned an invalid {name}")
    return value


__all__ = ["PresenterStageLaunchSelectionService"]
