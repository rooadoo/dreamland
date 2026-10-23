from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from collections.abc import Callable
from dataclasses import replace
from unittest.mock import patch

from dreamland.product.admission import (
    ProductAdmissionLedger,
    ProductAdmissionLimits,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductExecutionResult
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.launch_selection_stage import (
    PresenterStageLaunchSelectionService,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)
from dreamland.product.stage import (
    PresenterStageService,
    StageDisplayMetadataSeed,
    StageSelectionCapacityError,
)

PRESENTER_AUTHORITY = "authority:presenter-launch-selection-0001"
PRESENTER_ACTOR_ID = "presenter:launch-selection-a"
SHARED_EPOCH = "epoch:launchselectionstage0001"
AUTHORED_SELECTION = "launch-selection:authored-stage-opaque-00000001"
FORGE_SELECTION = "launch-selection:forged-stage-opaque-000000002"


class ManualClock:
    def __init__(self, value: int = 1_000) -> None:
        self.wall = value
        self.monotonic = value

    def __call__(self) -> int:
        return self.wall

    def monotonic_ms(self) -> int:
        return self.monotonic

    @property
    def value(self) -> int:
        return self.wall

    @value.setter
    def value(self, value: int) -> None:
        self.wall = value
        self.monotonic = value


class RegistryTokens:
    def __init__(self, *, epoch: str = SHARED_EPOCH) -> None:
        self.epoch = epoch
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self.epoch
        self.index += 1
        return f"launch-grant:selection-stage-test-{self.index:08d}"


class StageTokens:
    def __init__(self, *, epoch: str = SHARED_EPOCH) -> None:
        self.epoch = epoch
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return self.epoch
        self.index += 1
        return f"{purpose}:selection-stage-test-{self.index:08d}"


class ManualTasks:
    def __init__(self) -> None:
        self.tasks: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.tasks.append(task)


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _request, _emit):
        self.calls += 1
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class FailFirstProvisionStage(PresenterStageService):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provision_attempts = 0

    def provision_launch_selection(self, **kwargs):
        self.provision_attempts += 1
        if self.provision_attempts == 1:
            raise StageSelectionCapacityError("temporary test capacity")
        return super().provision_launch_selection(**kwargs)


class PresenterStageLaunchSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.tasks = ManualTasks()
        self.executor = CountingExecutor()
        self.authored = _revision(
            {
                "kind": "dreamland.authored_world_revision",
                "schema_revision": "1",
                "profile": {
                    "level": "world",
                    "state": {
                        "world_state": {
                            "name": "private authored state",
                            "profile_path": "/operator/private/colony.json",
                            "active_host": "console",
                            "entry_host": "console",
                            "hosts_by_name": {"console": {"hostname": "console"}},
                        }
                    },
                },
            }
        )
        self.forged = _revision(
            {
                "kind": "dreamland.world_forge_revision",
                "schema_revision": "1",
                "profile": {
                    "level": "world",
                    "state": {
                        "world_state": {
                            "name": "private forged state",
                            "active_host": "bridge",
                            "entry_host": "bridge",
                            "hosts_by_name": {"bridge": {"hostname": "bridge"}},
                        }
                    },
                },
            }
        )
        self.registry = _registry(
            clock=self.clock,
            authored=self.authored,
            forged=self.forged,
        )
        self.stage = _stage(
            clock=self.clock,
            tasks=self.tasks,
            executor=self.executor,
        )
        self.service = PresenterStageLaunchSelectionService(
            registry=self.registry,
            stage=self.stage,
        )

    def test_import_is_inert(self) -> None:
        code = """
import json
import threading
before = [thread.name for thread in threading.enumerate()]
from dreamland.product.launch_selection_stage import PresenterStageLaunchSelectionService
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({"before": before, "after": after, "name": PresenterStageLaunchSelectionService.__name__}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["name"], "PresenterStageLaunchSelectionService")

    def test_options_are_exact_path_free_and_nonauthorizing(self) -> None:
        result = self.service.options(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
        )

        self.assertEqual(set(result), {"kind", "service_epoch", "options"})
        self.assertEqual(result["kind"], "presenter_stage_launch_options")
        self.assertEqual(result["service_epoch"], SHARED_EPOCH)
        options = result["options"]
        self.assertIsInstance(options, list)
        self.assertEqual(len(options), 2)
        for option in options:
            self.assertEqual(
                set(option),
                {
                    "launch_selection_id",
                    "offering_id",
                    "display",
                    "origin",
                    "revision",
                    "expires_at",
                },
            )
        serialized = json.dumps(result, sort_keys=True)
        for private_value in (
            PRESENTER_AUTHORITY,
            "private authored state",
            "private forged state",
            "/operator/private/colony.json",
            "world_state",
            "policy:presenter-selection-v1",
            "absolute_ttl_ms",
            "actor_id",
            "launch_grant",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_authored_exchange_launches_existing_stage(self) -> None:
        exchanged = self._exchange(AUTHORED_SELECTION, "exchange-authored")

        self.assertEqual(
            set(exchanged),
            {
                "kind",
                "service_epoch",
                "replayed",
                "launch_grant",
                "grant_expires_at",
                "selection",
            },
        )
        self.assertFalse(exchanged["replayed"])
        self.assertEqual(
            exchanged["selection"]["origin"]["kind"],
            "authored_profile_snapshot",
        )
        launched = self.stage.launch(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            launch_grant=exchanged["launch_grant"],
            idempotency_key="launch-authored",
        )

        self.assertEqual(launched["kind"], "stage_session_created")
        self.assertEqual(
            launched["snapshot"]["starting_revision"]["revision_id"],
            self.authored.revision_id,
        )
        self.assertEqual(
            launched["snapshot"]["stage"]["display"],
            {
                "title": "Configured Colony",
                "source_label": "Authored",
                "revision_label": "Pinned A1",
            },
        )
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_forge_exchange_replaces_existing_authored_stage(self) -> None:
        authored = self._exchange(AUTHORED_SELECTION, "exchange-before-replace")
        created = self.stage.launch(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            launch_grant=authored["launch_grant"],
            idempotency_key="launch-before-replace",
        )
        forged = self._exchange(FORGE_SELECTION, "exchange-forge")

        snapshot = created["snapshot"]
        replaced = self.stage.replace(
            actor_authority=PRESENTER_AUTHORITY,
            session_authority=created["session_authority"],
            reference=snapshot["reference"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="replace-with-forge",
            expected_control_revision=snapshot["coordinates"]["control_revision"],
            launch_grant=forged["launch_grant"],
        )

        self.assertEqual(replaced["kind"], "stage_session_replaced")
        self.assertEqual(
            replaced["snapshot"]["starting_revision"]["revision_id"],
            self.forged.revision_id,
        )
        self.assertEqual(forged["selection"]["origin"]["kind"], "world_forge_selection")
        self.assertNotIn("source", replaced["snapshot"]["stage"])
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.tasks.tasks, [])

    def test_exact_replay_returns_same_grant_without_reprovisioning(self) -> None:
        fresh = self._exchange(AUTHORED_SELECTION, "same-exchange")
        registry_state = (
            len(self.stage._revisions),
            len(self.stage._launch_grants),
            len(self.stage._dynamic_stage_grants),
            len(self.stage._dynamic_launch_by_selection),
        )

        replay = self._exchange(AUTHORED_SELECTION, "same-exchange")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["launch_grant"], fresh["launch_grant"])
        self.assertEqual(replay["selection"], fresh["selection"])
        self.assertEqual(replay["grant_expires_at"], fresh["grant_expires_at"])
        self.assertEqual(
            (
                len(self.stage._revisions),
                len(self.stage._launch_grants),
                len(self.stage._dynamic_stage_grants),
                len(self.stage._dynamic_launch_by_selection),
            ),
            registry_state,
        )

    def test_exact_replay_survives_grant_and_offering_expiry(self) -> None:
        fresh = self._exchange(FORGE_SELECTION, "expiring-exchange")
        registry_state = (
            len(self.stage._revisions),
            len(self.stage._launch_grants),
            len(self.stage._dynamic_stage_grants),
            len(self.stage._dynamic_launch_by_selection),
        )
        # The Forge option expires at 90 seconds and its grant at 21 seconds;
        # the registry's idempotency result remains retained for 15 minutes.
        self.clock.value = 90_000

        with patch.object(
            self.stage,
            "provision_launch_selection",
            side_effect=AssertionError("replay must not reprovision Stage"),
        ) as provision:
            replay = self._exchange(FORGE_SELECTION, "expiring-exchange")
            with self.assertRaises(LaunchSelectionError) as changed:
                self._exchange(AUTHORED_SELECTION, "expiring-exchange")

        provision.assert_not_called()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["launch_grant"], fresh["launch_grant"])
        self.assertEqual(replay["selection"], fresh["selection"])
        self.assertEqual(replay["grant_expires_at"], fresh["grant_expires_at"])
        self.assertEqual(changed.exception.code, "idempotency_conflict")
        self.assertEqual(
            (
                len(self.stage._revisions),
                len(self.stage._launch_grants),
                len(self.stage._dynamic_stage_grants),
                len(self.stage._dynamic_launch_by_selection),
            ),
            registry_state,
        )

    def test_wall_clock_jumps_do_not_diverge_registry_and_stage_expiry(self) -> None:
        for jumped_wall in (50_000, 100):
            with self.subTest(jumped_wall=jumped_wall):
                clock = ManualClock()
                registry = _registry(
                    clock=clock,
                    authored=self.authored,
                    forged=self.forged,
                )
                stage = _stage(
                    clock=clock,
                    tasks=ManualTasks(),
                    executor=CountingExecutor(),
                )
                service = PresenterStageLaunchSelectionService(
                    registry=registry,
                    stage=stage,
                )
                clock.wall = jumped_wall

                exchanged = service.exchange(
                    actor_authority=PRESENTER_AUTHORITY,
                    expected_epoch=SHARED_EPOCH,
                    launch_selection_id=AUTHORED_SELECTION,
                    idempotency_key=f"clock-jump-{jumped_wall}",
                )
                grant = exchanged["launch_grant"]
                self.assertIsInstance(grant, str)
                digest = hashlib.sha256(grant.encode("utf-8")).hexdigest()
                registry_deadline = registry._grants[digest].expires_deadline_ms
                stage_deadline = stage._launch_grants[digest].expires_deadline_ms
                self.assertEqual(registry_deadline, stage_deadline)

                clock.monotonic = registry_deadline
                with self.assertRaises(LaunchSelectionError) as registry_error:
                    registry.resolve_launch_grant(
                        actor_authority=PRESENTER_AUTHORITY,
                        expected_epoch=SHARED_EPOCH,
                        launch_grant=grant,
                        operation=ProductOperation.CREATE,
                    )
                with self.assertRaises(ProductLifecycleError) as stage_error:
                    stage.launch(
                        actor_authority=PRESENTER_AUTHORITY,
                        expected_epoch=SHARED_EPOCH,
                        launch_grant=grant,
                        idempotency_key=f"expired-clock-jump-{jumped_wall}",
                    )

                self.assertEqual(
                    registry_error.exception.code,
                    "launch_grant_unavailable",
                )
                self.assertEqual(stage_error.exception.code, "authority_denied")

    def test_callback_wall_rollback_keeps_exact_registry_deadline(self) -> None:
        clock = ManualClock()
        registry = _registry(
            clock=clock,
            authored=self.authored,
            forged=self.forged,
        )
        stage = _stage(
            clock=clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
        )
        service = PresenterStageLaunchSelectionService(
            registry=registry,
            stage=stage,
        )
        original_provision = stage.provision_launch_selection

        def provision_after_rollback(**kwargs):
            clock.wall = 100
            return original_provision(**kwargs)

        clock.wall = 50_000
        with patch.object(
            stage,
            "provision_launch_selection",
            side_effect=provision_after_rollback,
        ):
            exchanged = service.exchange(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=AUTHORED_SELECTION,
                idempotency_key="callback-wall-rollback",
            )

        grant = str(exchanged["launch_grant"])
        digest = hashlib.sha256(grant.encode("utf-8")).hexdigest()
        self.assertEqual(
            registry._grants[digest].expires_deadline_ms,
            stage._launch_grants[digest].expires_deadline_ms,
        )

    def test_fresh_keys_issue_distinct_grants_and_internal_selection_records(
        self,
    ) -> None:
        first = self._exchange(AUTHORED_SELECTION, "fresh-key-a")
        second = self._exchange(AUTHORED_SELECTION, "fresh-key-b")

        self.assertNotEqual(first["launch_grant"], second["launch_grant"])
        selected_records = set(self.stage._dynamic_launch_by_selection)
        self.assertEqual(len(selected_records), 2)
        for serial, exchange in enumerate((first, second), start=1):
            launch_grant = exchange["launch_grant"]
            self.assertIsInstance(launch_grant, str)
            self.assertRegex(
                launch_grant,
                rf"^launch-grant:{serial:016x}_[0-9a-f]{{64}}$",
            )
            expected_record = (
                "selected-record:"
                + hashlib.sha256(launch_grant.encode("utf-8")).hexdigest()
            )
            self.assertIn(expected_record, selected_records)
        self.assertNotIn(AUTHORED_SELECTION, selected_records)
        self.assertNotIn("offering:authored-colony-a1", selected_records)
        self.assertEqual(self.stage._source_neutral_launch_serial_high_water, 2)
        serialized = json.dumps((first, second), sort_keys=True)
        self.assertNotIn(PRESENTER_AUTHORITY, serialized)
        self.assertNotIn("actor_id", serialized)
        self.assertNotIn("policy_id", serialized)
        self.assertNotIn("limits", serialized)

    def test_epoch_mismatch_is_rejected_before_stage_provisioning(self) -> None:
        with self.assertRaises(LaunchSelectionError) as options_error:
            self.service.options(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch="epoch:stale-selection-stage-0001",
            )
        self.assertEqual(options_error.exception.code, "service_epoch_changed")

        with self.assertRaises(LaunchSelectionError) as exchange_error:
            self.service.exchange(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch="epoch:stale-selection-stage-0001",
                launch_selection_id=AUTHORED_SELECTION,
                idempotency_key="stale-exchange",
            )
        self.assertEqual(exchange_error.exception.code, "outcome_unknown")
        self.assertEqual(self.stage._revisions, {})
        self.assertEqual(self.stage._launch_grants, {})
        self.assertEqual(self.executor.calls, 0)

    def test_constructor_rejects_distinct_epochs_and_fixed_stage(self) -> None:
        distinct = _registry(
            clock=self.clock,
            authored=self.authored,
            forged=self.forged,
            epoch="epoch:differentselectionstage0001",
        )
        with self.assertRaisesRegex(ValueError, "epochs must match"):
            PresenterStageLaunchSelectionService(
                registry=distinct,
                stage=self.stage,
            )

        fixed = _stage(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            dynamic_selection_only=False,
        )
        with self.assertRaisesRegex(ValueError, "dynamic-selection-only"):
            PresenterStageLaunchSelectionService(
                registry=self.registry,
                stage=fixed,
            )

        other_clock = ManualClock()
        distinct_clock = _registry(
            clock=other_clock,
            authored=self.authored,
            forged=self.forged,
        )
        with self.assertRaisesRegex(ValueError, "share one monotonic clock"):
            PresenterStageLaunchSelectionService(
                registry=distinct_clock,
                stage=self.stage,
            )

    def test_presenter_coordinator_denies_attendee_registry_authority(self) -> None:
        attendee_authority = "authority:attendee-launch-selection-0001"
        attendee_offering = replace(
            _offering(
                revision=self.authored,
                offering_id="offering:attendee-authored-a1",
                selection_id="launch-selection:attendee-stage-opaque-00000001",
                origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                title="Attendee Authored World",
                source_label="Authored",
                revision_label="Attendee A1",
                expires_at_ms=None,
            ),
            role=ProductRole.ATTENDEE,
            allowed_operations=frozenset({ProductOperation.CREATE}),
        )
        registry = LaunchSelectionRegistry(
            revisions=(self.authored,),
            offerings=(attendee_offering,),
            actors=(
                LaunchSelectionActorSeed(
                    authority=attendee_authority,
                    actor_id="attendee:launch-selection-a",
                    role=ProductRole.ATTENDEE,
                    expires_at_ms=100_000,
                ),
            ),
            allowed_operation_families=frozenset({ProductOperation.CREATE}),
            clock_ms=self.clock,
            monotonic_ms=self.clock.monotonic_ms,
            token_factory=RegistryTokens(),
        )
        with self.assertRaisesRegex(ValueError, "no Presenter authority"):
            PresenterStageLaunchSelectionService(
                registry=registry,
                stage=self.stage,
            )
        self.assertEqual(registry._idempotency, {})
        self.assertEqual(self.stage._revisions, {})
        self.assertEqual(self.stage._launch_grants, {})

    def test_constructor_rejects_registry_stage_policy_mismatches(self) -> None:
        mismatched_actor = LaunchSelectionRegistry(
            revisions=(self.authored,),
            offerings=(),
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id="presenter:different-actor",
                    role=ProductRole.PRESENTER,
                    expires_at_ms=100_000,
                ),
            ),
            clock_ms=self.clock,
            monotonic_ms=self.clock.monotonic_ms,
            token_factory=RegistryTokens(),
        )
        unsupported_operations = LaunchSelectionRegistry(
            revisions=(self.authored,),
            offerings=(),
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=100_000,
                ),
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.CLOSE}
            ),
            clock_ms=self.clock,
            monotonic_ms=self.clock.monotonic_ms,
            token_factory=RegistryTokens(),
        )
        oversized_revisions = LaunchSelectionRegistry(
            revisions=(self.authored,),
            offerings=(),
            actors=(
                LaunchSelectionActorSeed(
                    authority=PRESENTER_AUTHORITY,
                    actor_id=PRESENTER_ACTOR_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=100_000,
                ),
            ),
            policy=LaunchSelectionPolicy(max_revision_bytes=4 * 1024 * 1024 + 2),
            clock_ms=self.clock,
            monotonic_ms=self.clock.monotonic_ms,
            token_factory=RegistryTokens(),
        )
        large_core_stage = _stage(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            max_runtime_document_bytes=8 * 1024 * 1024,
        )

        for registry, stage, message in (
            (mismatched_actor, self.stage, "authority is incompatible"),
            (unsupported_operations, self.stage, "operations exceed"),
            (oversized_revisions, large_core_stage, "revision policy exceeds"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    PresenterStageLaunchSelectionService(
                        registry=registry,
                        stage=stage,
                    )

    def test_stage_capacity_leaves_exchange_key_retryable(self) -> None:
        stage = _stage(
            clock=self.clock,
            tasks=ManualTasks(),
            executor=CountingExecutor(),
            stage_type=FailFirstProvisionStage,
        )
        service = PresenterStageLaunchSelectionService(
            registry=self.registry,
            stage=stage,
        )

        with self.assertRaises(LaunchSelectionError) as captured:
            service.exchange(
                actor_authority=PRESENTER_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=AUTHORED_SELECTION,
                idempotency_key="retry-capacity",
            )
        self.assertEqual(captured.exception.code, "launch_selection_capacity_exhausted")
        self.assertEqual(captured.exception.retry, "after_delay")
        self.assertEqual(stage._revisions, {})
        self.assertEqual(stage._launch_grants, {})

        recovered = service.exchange(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            launch_selection_id=AUTHORED_SELECTION,
            idempotency_key="retry-capacity",
        )
        self.assertFalse(recovered["replayed"])
        self.assertEqual(stage.provision_attempts, 2)
        self.assertEqual(len(stage._launch_grants), 1)

    def _exchange(self, selection: str, key: str) -> dict[str, object]:
        return self.service.exchange(
            actor_authority=PRESENTER_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            launch_selection_id=selection,
            idempotency_key=key,
        )


def _revision(document: dict[str, object]) -> LaunchRevisionSeed:
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(
        revision_id=f"world-revision:sha256:{digest}",
        document=document,
    )


def _registry(
    *,
    clock: ManualClock,
    authored: LaunchRevisionSeed,
    forged: LaunchRevisionSeed,
    epoch: str = SHARED_EPOCH,
) -> LaunchSelectionRegistry:
    return LaunchSelectionRegistry(
        revisions=(authored, forged),
        offerings=(
            _offering(
                revision=authored,
                offering_id="offering:authored-colony-a1",
                selection_id=AUTHORED_SELECTION,
                origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                title="Configured Colony",
                source_label="Authored",
                revision_label="Pinned A1",
                expires_at_ms=None,
            ),
            _offering(
                revision=forged,
                offering_id="offering:forge-bridge-b1",
                selection_id=FORGE_SELECTION,
                origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                title="Forged Bridge",
                source_label="World Forge",
                revision_label="Selected B1",
                expires_at_ms=90_000,
            ),
        ),
        actors=(
            LaunchSelectionActorSeed(
                authority=PRESENTER_AUTHORITY,
                actor_id=PRESENTER_ACTOR_ID,
                role=ProductRole.PRESENTER,
                expires_at_ms=100_000,
            ),
        ),
        allowed_operation_families=frozenset(
            {ProductOperation.CREATE, ProductOperation.SWITCH}
        ),
        clock_ms=clock,
        monotonic_ms=clock.monotonic_ms,
        token_factory=RegistryTokens(epoch=epoch),
    )


def _offering(
    *,
    revision: LaunchRevisionSeed,
    offering_id: str,
    selection_id: str,
    origin: LaunchSelectionOrigin,
    title: str,
    source_label: str,
    revision_label: str,
    expires_at_ms: int | None,
) -> LaunchOfferingSeed:
    return LaunchOfferingSeed(
        offering_id=offering_id,
        launch_selection_id=selection_id,
        revision_id=revision.revision_id,
        origin=origin,
        title=title,
        source_label=source_label,
        revision_label=revision_label,
        role=ProductRole.PRESENTER,
        allowed_operations=frozenset(
            {ProductOperation.CREATE, ProductOperation.SWITCH}
        ),
        limits=_stage_limits(),
        policy_id="policy:presenter-selection-v1",
        notice_version="notice:presenter-selection-v1",
        expires_at_ms=expires_at_ms,
        launch_grant_ttl_ms=20_000,
    )


def _stage(
    *,
    clock: ManualClock,
    tasks: ManualTasks,
    executor: CountingExecutor,
    dynamic_selection_only: bool = True,
    max_runtime_document_bytes: int = 4 * 1024 * 1024,
    stage_type=PresenterStageService,
) -> PresenterStageService:
    if dynamic_selection_only:
        revisions = ()
        launch_grants = ()
        display_metadata = ()
    else:
        fixed_document = {"world_state": {"name": "fixed"}}
        # A fixed Stage is used only to prove fail-closed composition.
        revisions = (ImmutableRevisionSeed("fixed-revision", fixed_document),)
        launch_grants = (
            LaunchGrantSeed(
                token="fixed-launch-grant-token-0001",
                selected_record_id="fixed-selected-record-0001",
                actor_id=PRESENTER_ACTOR_ID,
                role=ProductRole.PRESENTER,
                revision_id="fixed-revision",
                expires_at_ms=100_000,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits=_stage_limits(),
                policy_id="fixed-policy-v1",
                notice_version="fixed-notice-v1",
            ),
        )
        display_metadata = (
            StageDisplayMetadataSeed("fixed-revision", "Fixed", "Authored", "Fixed A"),
        )
    return stage_type(
        revisions=revisions,
        actors=(
            ActorAuthoritySeed(
                token=PRESENTER_AUTHORITY,
                actor_id=PRESENTER_ACTOR_ID,
                role=ProductRole.PRESENTER,
                expires_at_ms=100_000,
            ),
        ),
        launch_grants=launch_grants,
        display_metadata=display_metadata,
        executor=executor,
        submit_task=tasks,
        lifecycle_policy=LifecyclePolicy(
            absolute_ttl_ms=60_000,
            idle_ttl_ms=30_000,
            tombstone_ttl_ms=10_000,
            failed_operation_ttl_ms=5_000,
            max_active_sessions=3,
            max_tombstones=6,
            max_idempotency_records=32,
            max_launch_grants=8,
            max_actor_authorities=4,
            max_revisions=4,
        ),
        core_policy=ProductCorePolicy(
            max_commands_per_session=4,
            command_deadline_ms=10_000,
            cancellation_deadline_ms=2_000,
            max_output_bytes_per_command=1_024,
            max_output_chunks_per_command=16,
            max_runtime_document_bytes=max_runtime_document_bytes,
            reserved_tokens_per_command=100,
            reserved_spend_units_per_command=100,
        ),
        admission=ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=3,
                max_logical_jobs=3,
                max_physical_workers=2,
                token_budget=300,
                spend_budget_units=300,
                presenter_stage_reserved_sessions=1,
                presenter_stage_reserved_logical_jobs=1,
                presenter_stage_reserved_physical_workers=1,
                presenter_stage_reserved_token_budget=100,
                presenter_stage_reserved_spend_budget_units=100,
            )
        ),
        clock_ms=clock,
        monotonic_ms=clock.monotonic_ms,
        token_factory=StageTokens(),
        dynamic_selection_only=dynamic_selection_only,
    )


def _stage_limits() -> dict[str, int]:
    return {
        "absolute_ttl_ms": 50_000,
        "idle_ttl_ms": 20_000,
        "max_commands": 4,
        "max_command_bytes": 1_024,
        "command_reserved_tokens": 100,
        "command_reserved_spend_units": 100,
    }


if __name__ == "__main__":
    unittest.main()
