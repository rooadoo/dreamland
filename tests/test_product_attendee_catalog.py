from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from dreamland.product.admission import ProductAdmissionLedger, ProductAdmissionLimits
from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeCatalogEligibilityProof,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
    mint_attendee_catalog_eligibility_proof,
)
from dreamland.product.attendee_catalog import (
    GeneratedWorldCatalogPolicy,
    GeneratedWorldCatalogService,
)
from dreamland.product.attendee_security import (
    AttendeeRequestAdmissionLedger,
    AttendeeRequestAdmissionPolicy,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.execution import ProductExecutionResult
from dreamland.product.forge import ForgeSelectedRevisionRecord
from dreamland.product.forge_artifacts import (
    ImmutableForgeRevision,
    ImmutableForgeStore,
)
from dreamland.product.generated_access import (
    GeneratedAccessActorSeed,
    GeneratedAccessEligibility,
    GeneratedAccessError,
    GeneratedAccessPolicy,
    GeneratedAccessPublicationCapacityError,
    GeneratedWorldAccessService,
)
from dreamland.product.launch_selection import (
    LaunchGrantProvisioningReceipt,
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LifecyclePolicy,
    ProductOperation,
    ProductRole,
)
from dreamland.state_profiles import StateProfile

SHARED_EPOCH = "epoch:phase8a_catalog_00000001"
OPERATOR_AUTHORITY = "operator-authority:phase8a-catalog-00000001"
BFF_AUTHORITY = "bff-authority:phase8a-catalog-gateway-00000001"
NOTICE_VERSION = "notice:phase8a-world-access-v1"
FORGE_REFERENCE = "forge-reference:phase8a-selected-00000001"
ELIGIBILITY_KEY = b"phase8a catalog eligibility proof key"
LOCATOR_KEY = b"phase8a catalog locator derivation key"


class _Clock:
    def __init__(self) -> None:
        self.wall_ms = 1_800_000_000_000
        self.deadline_ms = 50_000

    def wall(self) -> int:
        return self.wall_ms

    def monotonic(self) -> int:
        return self.deadline_ms

    def advance(self, milliseconds: int) -> None:
        self.wall_ms += milliseconds
        self.deadline_ms += milliseconds


class _RegistryTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return SHARED_EPOCH
        self.index += 1
        return f"launch-grant:phase8a_catalog_candidate_{self.index:08d}"


class _CoreTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return SHARED_EPOCH
        self.index += 1
        return f"{purpose}:phase8a-catalog-{self.index:08d}"


class _AccessTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}:phase8a_catalog_{self.index:08d}"


class _Tasks:
    def __init__(self) -> None:
        self.values: list[Callable[[], None]] = []

    def __call__(self, task: Callable[[], None]) -> None:
        self.values.append(task)


class _Executor:
    def execute(self, _request, _emit):
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class _SelectedSource:
    def __init__(
        self,
        *,
        clock: _Clock,
        store: ImmutableForgeStore,
        artifact: ImmutableForgeRevision,
    ) -> None:
        self.clock = clock
        self.store = store
        self.artifact = artifact
        self.resolve_calls = 0

    def resolve_selected(self, job_reference: str) -> ForgeSelectedRevisionRecord:
        self.resolve_calls += 1
        if job_reference != FORGE_REFERENCE:
            raise ValueError("selected fixture is unavailable")
        selected_at = datetime.fromtimestamp(self.clock.wall_ms / 1_000, UTC)
        expires_at = datetime.fromtimestamp((self.clock.wall_ms + 300_000) / 1_000, UTC)
        return ForgeSelectedRevisionRecord(
            selected_record_id="forge-selected-record:phase8a-catalog-00000001",
            status="selected",
            service_epoch="forge-epoch:phase8a-catalog-00000001",
            job_id="forge-job:phase8a-catalog-00000001",
            job_reference=FORGE_REFERENCE,
            actor_id="forge-presenter:phase8a-catalog",
            selected_at=selected_at.isoformat(),
            expires_at=expires_at.isoformat(),
            policy_id="forge-to-stage-handoff:v1",
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            source_kind="single_attempt_live_vertical_slice",
            source_fingerprint="sha256:" + "1" * 64,
            receipt_correlation_id="forge-receipt:phase8a-catalog-00000001",
            receipt_fingerprint="sha256:" + "2" * 64,
        )

    def load_revision(
        self, revision_id: str, immutable_digest: str
    ) -> ImmutableForgeRevision:
        return self.store.load(revision_id, immutable_digest)


class _ObservedPublicationCatalog(GeneratedWorldCatalogService):
    def __init__(self, *, publication_reached: threading.Event, **kwargs) -> None:
        self._publication_reached = publication_reached
        super().__init__(**kwargs)

    def _prepare_publication(self, eligibility):
        self._publication_reached.set()
        return super()._prepare_publication(eligibility)


class _ObservedActivationCatalog(GeneratedWorldCatalogService):
    def __init__(
        self,
        *,
        activation_reached: threading.Event,
        release_activation: threading.Event,
        **kwargs,
    ) -> None:
        self._activation_reached = activation_reached
        self._release_activation = release_activation
        self.activation_calls = 0
        super().__init__(**kwargs)

    def _activate_publication(self, access_record_id: str) -> None:
        self.activation_calls += 1
        if self.activation_calls == 1:
            self._activation_reached.set()
            if not self._release_activation.wait(timeout=2):
                raise RuntimeError("test activation release timed out")
        super()._activate_publication(access_record_id)


class _PostRegistrationCatalog(GeneratedWorldCatalogService):
    def __init__(self, *, after_prepare: Callable[[], None], **kwargs) -> None:
        self._after_prepare = after_prepare
        self.preparation_calls = 0
        self.prepared_locator: tuple[str, str, str] | None = None
        super().__init__(**kwargs)

    def _prepare_publication(self, eligibility):
        receipt = super()._prepare_publication(eligibility)
        self.preparation_calls += 1
        self.prepared_locator = self._locators(eligibility)
        self._after_prepare()
        return receipt


class GeneratedWorldCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = _Clock()
        self.tasks = _Tasks()
        self.store = ImmutableForgeStore(Path(self.temporary.name) / "revisions")
        self.artifact = self.store.save(_forge_profile())
        self.source = _SelectedSource(
            clock=self.clock,
            store=self.store,
            artifact=self.artifact,
        )
        self.attendee, self.registry, self.core, self.actors = self._attendee()
        self.access = GeneratedWorldAccessService(
            actors=(
                GeneratedAccessActorSeed(
                    authority=OPERATOR_AUTHORITY,
                    actor_id="operator:phase8a-catalog",
                    expires_at_ms=self.clock.wall_ms + 3_600_000,
                ),
            ),
            source=self.source,
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=4,
                max_access_records=8,
                max_idempotency_records=16,
                max_token_history=32,
                access_ttl_ms=1_800_000,
                idempotency_ttl_ms=1_800_000,
                failed_operation_ttl_ms=60_000,
            ),
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_AccessTokens(),
            service_epoch=SHARED_EPOCH,
        )
        self.catalog = GeneratedWorldCatalogService(
            access=self.access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
                maximum_generated_offerings=2,
                maximum_catalog_items=4,
                launch_grant_ttl_ms=600_000,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        self.context = self.attendee.bootstrap_context(
            bff_authority=BFF_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id="gateway-context:" + "a" * 32,
            idempotency_key="bootstrap-phase8a-catalog",
        )
        self.actor = str(self.context["actor_context"])

    def test_hidden_then_revealed_catalog_launches_exact_generated_revision(
        self,
    ) -> None:
        hidden = self._promote()
        authored_only = self._catalog()
        self.assertEqual(len(authored_only["offerings"]), 1)
        self.assertEqual(
            authored_only["offerings"][0]["origin"],
            {"kind": "authored_profile_snapshot"},
        )

        revealed = self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-phase8a-catalog",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        self.assertEqual(revealed["state"], "revealed")
        mixed = self._catalog()
        self.assertNotEqual(
            mixed["catalog_position"], authored_only["catalog_position"]
        )
        generated = next(
            value
            for value in mixed["offerings"]
            if value["origin"]["kind"] == "world_forge_selection"
        )
        generated_record = self.registry._offerings[generated["offering_id"]]
        access_record = self.access._current_by_promotion[revealed["promotion_id"]]
        self.assertLessEqual(
            generated_record.expires_deadline_ms,
            access_record.expires_deadline_ms,
        )
        encoded = json.dumps(mixed, sort_keys=True)
        for forbidden in (
            "launch_selection_id",
            "launch_grant",
            "immutable_digest",
            str(self.artifact.path),
            FORGE_REFERENCE,
            OPERATOR_AUTHORITY,
            "hosts_by_name",
        ):
            self.assertNotIn(forbidden, encoded)

        exchanged = self._exchange(generated, "exchange-generated")
        replay = self._exchange(generated, "exchange-generated")
        self.assertFalse(exchanged["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(exchanged["launch_grant"], replay["launch_grant"])
        self.assertRegex(exchanged["launch_binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            exchanged["launch_binding_sha256"], replay["launch_binding_sha256"]
        )
        self.assertNotIn("revision", exchanged["selection"])

        created = self.attendee.create(
            actor_authority=self.actor,
            launch_grant=exchanged["launch_grant"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-generated-session",
        )
        created_replay = self.attendee.create(
            actor_authority=self.actor,
            launch_grant=exchanged["launch_grant"],
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-generated-session",
        )
        self.assertFalse(created["replayed"])
        self.assertTrue(created_replay["replayed"])
        self.assertEqual(created["snapshot"], created_replay["snapshot"])
        self.assertEqual(
            created["snapshot"]["starting_revision"]["revision_id"],
            self.artifact.revision_id,
        )
        self.assertEqual(
            exchanged["launch_binding_sha256"],
            created["snapshot"]["launch_binding"]["sha256"],
        )
        self.assertEqual(
            exchanged["launch_binding_sha256"],
            created_replay["snapshot"]["launch_binding"]["sha256"],
        )
        session = self.core._sessions_by_reference[created["snapshot"]["reference"]]
        self.assertEqual(session.runtime_document, self.artifact.document)
        self.assertIsNot(session.runtime_document, self.artifact.document)
        self.assertIn(self.artifact.revision_id, self.core._revisions)

        second = self.attendee.bootstrap_context(
            bff_authority=BFF_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id="gateway-context:" + "b" * 32,
            idempotency_key="bootstrap-authored-correlation",
        )
        second_actor = str(second["actor_context"])
        authored = authored_only["offerings"][0]
        authored_exchange = self.catalog.exchange(
            actor_authority=second_actor,
            expected_epoch=SHARED_EPOCH,
            offering_id=str(authored["offering_id"]),
            offering_revision=str(authored["offering_revision"]),
            notice_version_presented=str(authored["notice_version"]),
            idempotency_key="exchange-authored-correlation",
        )
        authored_created = self.attendee.create(
            actor_authority=second_actor,
            launch_grant=str(authored_exchange["launch_grant"]),
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-authored-correlation",
        )
        self.assertEqual(
            authored_exchange["launch_binding_sha256"],
            authored_created["snapshot"]["launch_binding"]["sha256"],
        )
        self.assertNotEqual(
            authored_exchange["launch_binding_sha256"],
            exchanged["launch_binding_sha256"],
        )

    def test_launch_binding_correlation_is_not_accepted_as_authority(self) -> None:
        authored = self._catalog()["offerings"][0]
        exchanged = self._exchange(authored, "exchange-nonauthorizing-binding")
        correlation = str(exchanged["launch_binding_sha256"])
        before = (
            dict(self.registry._grants),
            dict(self.core._sessions_by_reference),
            dict(self.core._session_authorities),
        )

        with self.assertRaises(LaunchSelectionError) as actor_denied:
            self.catalog.catalog(
                actor_authority=correlation,
                expected_epoch=SHARED_EPOCH,
            )
        self.assertEqual(actor_denied.exception.code, "authority_denied")
        with self.assertRaises(LaunchSelectionError) as grant_denied:
            self.registry.resolve_launch_grant(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                launch_grant=correlation,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(grant_denied.exception.code, "launch_grant_unavailable")
        self.assertEqual(
            (
                self.registry._grants,
                self.core._sessions_by_reference,
                self.core._session_authorities,
            ),
            before,
        )

    def test_catalog_exchange_requires_a_valid_product_binding_receipt(self) -> None:
        authored = self._catalog()["offerings"][0]
        before = (
            dict(self.registry._grants),
            dict(self.core._launch_grants),
            dict(self.attendee._catalog_exchange_idempotency),
        )

        for key, returned, message in (
            ("catalog-binding-missing", None, "receipt is missing"),
            ("catalog-binding-invalid", object(), "receipt is invalid"),
            (
                "catalog-binding-uncommitted",
                LaunchGrantProvisioningReceipt("a" * 64),
                "commit is missing",
            ),
        ):
            with self.subTest(key=key):
                with patch.object(
                    self.core,
                    "provision_launch_selection",
                    return_value=returned,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, f"provisioning {message}"
                    ):
                        self._exchange(authored, key)
                self.assertEqual(
                    (
                        self.registry._grants,
                        self.core._launch_grants,
                        self.attendee._catalog_exchange_idempotency,
                    ),
                    before,
                )

        recovered = self._exchange(authored, "catalog-binding-missing")
        self.assertRegex(recovered["launch_binding_sha256"], r"^[0-9a-f]{64}$")

    def test_committed_product_receipt_prevents_callback_split_truth(self) -> None:
        authored = self._catalog()["offerings"][0]

        def provision_then_discard_receipt(issue, resolved):
            receipt = self.core.provision_launch_selection(
                actor_authority=self.actor,
                expected_epoch=issue.service_epoch,
                issue=issue,
                resolved=resolved,
            )
            self.assertEqual(
                issue.launch_binding_sha256,
                receipt.launch_binding_sha256,
            )
            return object()

        issue = self.registry.exchange_offering(
            actor_authority=self.actor,
            expected_epoch=SHARED_EPOCH,
            offering_id=str(authored["offering_id"]),
            idempotency_key="discarded-committed-receipt",
            provision=provision_then_discard_receipt,
            required_role=ProductRole.ATTENDEE,
            require_launch_binding_receipt=True,
        )
        self.assertRegex(issue.launch_binding_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(len(self.registry._grants), 1)
        self.assertEqual(len(self.core._launch_grants), 1)

        created = self.attendee.create(
            actor_authority=self.actor,
            launch_grant=issue.launch_grant,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="create-after-discarded-receipt",
        )
        self.assertEqual(
            issue.launch_binding_sha256,
            created["snapshot"]["launch_binding"]["sha256"],
        )

    def test_reveal_linearizes_access_truth_and_catalog_activation(self) -> None:
        activation_reached = threading.Event()
        release_activation = threading.Event()
        catalog = _ObservedActivationCatalog(
            activation_reached=activation_reached,
            release_activation=release_activation,
            access=self.access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
                maximum_generated_offerings=2,
                maximum_catalog_items=4,
                launch_grant_ttl_ms=600_000,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        hidden = self._promote()
        reveal_results: list[dict[str, object]] = []
        status_results: list[dict[str, object]] = []
        catalog_results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def reveal() -> None:
            try:
                reveal_results.append(
                    catalog.reveal(
                        actor_authority=OPERATOR_AUTHORITY,
                        expected_epoch=SHARED_EPOCH,
                        idempotency_key="linearized-reveal",
                        promotion_id=str(hidden["promotion_id"]),
                        expected_access_record_id=str(hidden["access_record_id"]),
                    )
                )
            except BaseException as error:
                errors.append(error)

        reader_barrier = threading.Barrier(3)

        def status() -> None:
            try:
                reader_barrier.wait(timeout=1)
                status_results.append(
                    self.access.status(
                        actor_authority=OPERATOR_AUTHORITY,
                        expected_epoch=SHARED_EPOCH,
                        promotion_id=str(hidden["promotion_id"]),
                    )
                )
            except BaseException as error:
                errors.append(error)

        def read_catalog() -> None:
            try:
                reader_barrier.wait(timeout=1)
                catalog_results.append(
                    catalog.catalog(
                        actor_authority=self.actor,
                        expected_epoch=SHARED_EPOCH,
                    )
                )
            except BaseException as error:
                errors.append(error)

        reveal_thread = threading.Thread(target=reveal)
        status_thread = threading.Thread(target=status)
        catalog_thread = threading.Thread(target=read_catalog)
        reveal_thread.start()
        status_thread.start()
        catalog_thread.start()
        try:
            self.assertTrue(activation_reached.wait(timeout=1))
            reader_barrier.wait(timeout=1)
            status_thread.join(timeout=0.05)
            catalog_thread.join(timeout=0.05)
            self.assertTrue(status_thread.is_alive())
            self.assertTrue(catalog_thread.is_alive())
            self.assertEqual(status_results, [])
            self.assertEqual(catalog_results, [])
        finally:
            release_activation.set()

        for thread in (reveal_thread, status_thread, catalog_thread):
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(reveal_results), 1)
        self.assertEqual(reveal_results[0]["state"], "revealed")
        self.assertEqual(len(status_results), 1)
        self.assertEqual(status_results[0]["state"], "revealed")
        self.assertTrue(status_results[0]["catalog_eligible"])
        self.assertEqual(len(catalog_results), 1)
        generated = [
            offering
            for offering in catalog_results[0]["offerings"]
            if offering["origin"]["kind"] == "world_forge_selection"
        ]
        self.assertEqual(len(generated), 1)
        self.assertEqual(catalog.activation_calls, 1)
        self.assertEqual(catalog._catalog_sequence, 1)

        replay = catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="linearized-reveal",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(catalog.activation_calls, 2)
        self.assertEqual(catalog._catalog_sequence, 1)
        self.assertEqual(len(catalog._generated_by_offering), 1)

    def test_authored_options_snapshot_is_stable_during_pending_rollback(self) -> None:
        hidden = self._promote()
        captured = []

        def capture_eligibility(eligibility) -> None:
            captured.append(eligibility)
            raise RuntimeError("capture publication eligibility")

        with self.assertRaisesRegex(RuntimeError, "capture publication eligibility"):
            self.access.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="capture-pending-options-eligibility",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
                publish=capture_eligibility,
            )
        self.assertEqual(len(captured), 1)
        receipt = self.catalog._prepare_publication(captured[0])
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(len(self.catalog._pending_by_access_record), 1)

        snapshot_reached = threading.Event()
        release_snapshot = threading.Event()
        rollback_started = threading.Event()
        option_results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        original_offerings = self.registry.offerings

        def paused_offerings(**kwargs):
            result = original_offerings(**kwargs)
            snapshot_reached.set()
            if not release_snapshot.wait(timeout=2):
                raise RuntimeError("test options snapshot release timed out")
            return result

        def read_options() -> None:
            try:
                option_results.append(
                    self.attendee.options(
                        actor_authority=self.actor,
                        expected_epoch=SHARED_EPOCH,
                    )
                )
            except BaseException as error:
                errors.append(error)

        def rollback() -> None:
            rollback_started.set()
            try:
                receipt.rollback()
            except BaseException as error:
                errors.append(error)

        options_thread = threading.Thread(target=read_options)
        rollback_thread = threading.Thread(target=rollback)
        with patch.object(self.registry, "offerings", side_effect=paused_offerings):
            options_thread.start()
            try:
                self.assertTrue(snapshot_reached.wait(timeout=1))
                rollback_thread.start()
                self.assertTrue(rollback_started.wait(timeout=1))
                rollback_thread.join(timeout=0.05)
                self.assertTrue(rollback_thread.is_alive())
                self.assertEqual(option_results, [])
            finally:
                release_snapshot.set()

            for thread in (options_thread, rollback_thread):
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(option_results), 1)
        options = option_results[0]["options"]
        self.assertEqual(len(options), 1)
        self.assertTrue(
            all(
                option["origin"] == {"kind": "authored_profile_snapshot"}
                for option in options
            )
        )
        self.assertEqual(self.catalog._pending_by_access_record, {})
        self.assertFalse(
            any(
                offering.origin is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
                for offering in self.registry._offerings.values()
            )
        )

    def test_unpromoted_registry_forge_offering_is_hidden_and_cannot_grant(
        self,
    ) -> None:
        selection_id = "launch-selection:unpromoted_generated_00000001"
        self.registry.register_trusted_offering(
            revision=LaunchRevisionSeed(
                revision_id=self.artifact.revision_id,
                document=self.artifact.document,
            ),
            offering=LaunchOfferingSeed(
                offering_id="offering:unpromoted-generated",
                launch_selection_id=selection_id,
                revision_id=self.artifact.revision_id,
                origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                title="Unpromoted",
                source_label="Untrusted composition",
                revision_label="Hidden",
                role=ProductRole.ATTENDEE,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits=_launch_limits(),
                policy_id="policy:unpromoted-generated",
                notice_version=NOTICE_VERSION,
                expires_at_ms=self.clock.wall_ms + 600_000,
                launch_grant_ttl_ms=300_000,
            ),
        )
        catalog = self._catalog()
        self.assertNotIn(
            "offering:unpromoted-generated",
            {value["offering_id"] for value in catalog["offerings"]},
        )
        with self.assertRaises(LaunchSelectionError) as unavailable:
            self.catalog.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                offering_id="offering:unpromoted-generated",
                offering_revision="offering-revision:unknown-0000000000000000",
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="unpromoted-generated",
            )
        self.assertEqual(unavailable.exception.code, "launch_selection_unavailable")
        self.assertEqual(self.registry._grants, {})
        self.assertNotIn(self.artifact.revision_id, self.core._revisions)

    def test_published_product_methods_remain_authored_only(self) -> None:
        hidden = self._promote()
        self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-authored-compatibility",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        options = self.attendee.options(
            actor_authority=self.actor,
            expected_epoch=SHARED_EPOCH,
        )
        self.assertEqual(len(options["options"]), 1)
        self.assertEqual(
            options["options"][0]["origin"],
            {"kind": "authored_profile_snapshot"},
        )
        generated = next(
            offering
            for offering in self.registry._offerings.values()
            if offering.origin is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
        )
        with self.assertRaises(LaunchSelectionError) as unavailable:
            self.attendee.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                launch_selection_id=generated.launch_selection_id,
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="frozen-product-generated-denied",
            )
        self.assertEqual(unavailable.exception.code, "launch_selection_unavailable")

    def test_catalog_exchange_binds_revision_notice_and_idempotency(self) -> None:
        hidden = self._promote()
        self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-catalog-binding",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        generated = next(
            value
            for value in self._catalog()["offerings"]
            if value["origin"]["kind"] == "world_forge_selection"
        )
        with self.assertRaises(LaunchSelectionError) as stale:
            self.catalog.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                offering_id=generated["offering_id"],
                offering_revision="offering-revision:stale-000000000000000000",
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="catalog-binding",
            )
        self.assertEqual(stale.exception.code, "launch_selection_unavailable")
        with self.assertRaises(LaunchSelectionError) as conflict:
            self._exchange(generated, "catalog-binding")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

        with self.assertRaises(LaunchSelectionError) as notice:
            self.catalog.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                offering_id=generated["offering_id"],
                offering_revision=generated["offering_revision"],
                notice_version_presented="notice:wrong-v1",
                idempotency_key="notice-mismatch",
            )
        self.assertEqual(notice.exception.code, "notice_version_mismatch")
        with self.assertRaises(LaunchSelectionError) as notice_replay:
            self.catalog.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                offering_id=generated["offering_id"],
                offering_revision=generated["offering_revision"],
                notice_version_presented="notice:wrong-v1",
                idempotency_key="notice-mismatch",
            )
        self.assertEqual(notice_replay.exception.code, "notice_version_mismatch")

    def test_both_grant_issuing_exchanges_share_logical_admission(self) -> None:
        admission = AttendeeRequestAdmissionLedger(
            AttendeeRequestAdmissionPolicy(
                window_ms=1_000,
                max_fresh_operations_per_context=1,
                max_fresh_operations_global=4,
                max_tracked_contexts=2,
            )
        )
        # The production composition supplies this at construction. Attaching
        # after setUp isolates admission charging to the two exchange calls in
        # this owning catalog test.
        self.attendee._request_admission = admission
        authored = self.attendee.exchange(
            actor_authority=self.actor,
            expected_epoch=SHARED_EPOCH,
            launch_selection_id="launch-selection:phase8a_authored_00000001",
            notice_version_presented=NOTICE_VERSION,
            idempotency_key="rate-authored-exchange",
        )
        self.assertEqual(authored["kind"], "attendee_launch_selection_granted")

        hidden = self._promote()
        self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-rate-generated",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        generated = next(
            value
            for value in self._catalog()["offerings"]
            if value["origin"]["kind"] == "world_forge_selection"
        )
        with self.assertRaises(LaunchSelectionError) as limited:
            self._exchange(generated, "rate-generated-exchange")
        self.assertEqual(limited.exception.code, "logical_rate_limited")
        self.assertEqual(
            self.attendee._catalog_exchange_idempotency,
            {},
        )

        self.clock.advance(1_000)
        granted = self._exchange(generated, "rate-generated-exchange")
        replayed = self._exchange(generated, "rate-generated-exchange")
        self.assertFalse(granted["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(admission.snapshot()["global_accepted"], 1)

    def test_reveal_reserves_total_catalog_capacity_before_access_commit(self) -> None:
        limited = GeneratedWorldCatalogService(
            access=self.access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
                maximum_generated_offerings=1,
                maximum_catalog_items=1,
                launch_grant_ttl_ms=600_000,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        hidden = self._promote()
        with self.assertRaises(GeneratedAccessError) as capacity:
            limited.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="reveal-total-catalog-capacity",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
            )
        self.assertEqual(capacity.exception.code, "publication_capacity_exhausted")
        self.assertEqual(capacity.exception.retry, "after_delay")
        self.assertEqual(
            self.access.status(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                promotion_id=str(hidden["promotion_id"]),
            )["state"],
            "hidden",
        )
        self.assertEqual(
            len(
                limited.catalog(
                    actor_authority=self.actor,
                    expected_epoch=SHARED_EPOCH,
                )["offerings"]
            ),
            1,
        )
        self.assertFalse(
            any(
                offering.origin is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
                for offering in self.registry._offerings.values()
            )
        )

        revealed = self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-total-catalog-capacity",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        self.assertEqual(revealed["state"], "revealed")

    def test_expiry_hides_publication_without_reclaiming_lifetime_capacity(
        self,
    ) -> None:
        limited = GeneratedWorldCatalogService(
            access=self.access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
                maximum_generated_offerings=1,
                maximum_catalog_items=2,
                launch_grant_ttl_ms=50,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        canonical = (
            json.dumps(
                self.artifact.document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        first = GeneratedAccessEligibility(
            service_epoch=SHARED_EPOCH,
            promotion_id="generated-access-promotion:lifetime_00000001",
            access_record_id="generated-access-record:lifetime_00000001",
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            expires_at_ms=self.clock.wall_ms + 100,
            expires_deadline_ms=self.clock.deadline_ms + 100,
            canonical_json=canonical,
        )
        receipt = limited._prepare_publication(first)
        self.assertIsNotNone(receipt)
        limited._activate_publication(first.access_record_id)
        self.assertEqual(
            limited.publication_accounting(),
            {
                "retention": "service_epoch_lifetime",
                "capacity": 1,
                "used": 1,
                "remaining": 0,
            },
        )

        self.clock.advance(101)
        self.assertEqual(
            len(
                limited.catalog(
                    actor_authority=self.actor,
                    expected_epoch=SHARED_EPOCH,
                )["offerings"]
            ),
            1,
        )
        second = replace(
            first,
            promotion_id="generated-access-promotion:lifetime_00000002",
            access_record_id="generated-access-record:lifetime_00000002",
            expires_at_ms=self.clock.wall_ms + 100,
            expires_deadline_ms=self.clock.deadline_ms + 100,
        )
        with self.assertRaises(GeneratedAccessPublicationCapacityError):
            limited._prepare_publication(second)
        self.assertEqual(limited.publication_accounting()["used"], 1)

    def test_exact_exchange_replays_after_offering_and_grant_expire(self) -> None:
        authored_revision_id = next(
            revision_id
            for revision_id, origin in self.core._attendee_revision_origins.items()
            if origin is LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
        )
        self.registry.register_offering(
            LaunchOfferingSeed(
                offering_id="offering:phase8a-short-authored",
                launch_selection_id=("launch-selection:phase8a_short_authored_0001"),
                revision_id=authored_revision_id,
                origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                title="Authored baseline",
                source_label="Authored",
                revision_label="Pinned A1",
                role=ProductRole.ATTENDEE,
                allowed_operations=frozenset(
                    {ProductOperation.CREATE, ProductOperation.SWITCH}
                ),
                limits=_launch_limits(),
                policy_id="policy:phase8a-short-authored",
                notice_version=NOTICE_VERSION,
                expires_at_ms=self.clock.wall_ms + 100,
                launch_grant_ttl_ms=50,
            )
        )
        offering = next(
            value
            for value in self._catalog()["offerings"]
            if value["offering_id"] == "offering:phase8a-short-authored"
        )
        first = self._exchange(offering, "replay-expired-offering")

        self.clock.advance(101)
        self.assertNotIn(
            offering["offering_id"],
            {value["offering_id"] for value in self._catalog()["offerings"]},
        )
        replay = self._exchange(offering, "replay-expired-offering")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["launch_grant"], first["launch_grant"])
        self.assertEqual(replay["grant_expires_at"], first["grant_expires_at"])
        self.assertEqual(replay["selection"], first["selection"])

    def test_publication_waiting_past_cutoff_does_not_commit_reveal(self) -> None:
        reached = threading.Event()
        observed = _ObservedPublicationCatalog(
            publication_reached=reached,
            access=self.access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 100,
                maximum_generated_offerings=1,
                maximum_catalog_items=2,
                launch_grant_ttl_ms=50,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        hidden = self._promote()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def reveal() -> None:
            try:
                results.append(
                    observed.reveal(
                        actor_authority=OPERATOR_AUTHORITY,
                        expected_epoch=SHARED_EPOCH,
                        idempotency_key="reveal-after-publication-cutoff",
                        promotion_id=str(hidden["promotion_id"]),
                        expected_access_record_id=str(hidden["access_record_id"]),
                    )
                )
            except BaseException as error:
                errors.append(error)

        with self.registry._lock:
            thread = threading.Thread(target=reveal)
            thread.start()
            self.assertTrue(reached.wait(timeout=1))
            self.clock.advance(101)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], GeneratedAccessError)
        self.assertEqual(errors[0].code, "reveal_unavailable")
        current = self.access.status(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            promotion_id=str(hidden["promotion_id"]),
        )
        self.assertEqual(current["state"], "hidden")
        self.assertFalse(
            any(
                offering.origin is LaunchSelectionOrigin.WORLD_FORGE_SELECTION
                for offering in self.registry._offerings.values()
            )
        )

    def test_post_registration_expiry_rolls_back_exact_publication(self) -> None:
        access, catalog = self._post_registration_catalog(
            after_prepare=lambda: self.clock.advance(100),
        )
        hidden = access.promote(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="promote-short-publication",
            job_reference=FORGE_REFERENCE,
        )
        revisions_before = dict(self.registry._revisions)
        offerings_before = dict(self.registry._offerings)
        selections_before = dict(self.registry._offering_by_selection_sha256)
        displays_before = dict(self.registry._display_by_revision)
        tokens_before = set(self.registry._used_token_sha256s)

        def reveal() -> None:
            catalog.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="post-registration-expiry",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=str(hidden["access_record_id"]),
            )

        with self.assertRaises(GeneratedAccessError) as unavailable:
            reveal()
        self.assertEqual(unavailable.exception.code, "reveal_unavailable")
        self.assertEqual(catalog.preparation_calls, 1)
        self.assertEqual(catalog._pending_by_access_record, {})
        self.assertEqual(catalog._generated_by_access_record, {})
        self.assertEqual(self.registry._revisions, revisions_before)
        self.assertEqual(self.registry._offerings, offerings_before)
        self.assertEqual(
            self.registry._offering_by_selection_sha256,
            selections_before,
        )
        self.assertEqual(self.registry._display_by_revision, displays_before)
        self.assertEqual(self.registry._used_token_sha256s, tokens_before)
        self.assertFalse(
            any(
                offering["origin"]["kind"] == "world_forge_selection"
                for offering in catalog.catalog(
                    actor_authority=self.actor,
                    expected_epoch=SHARED_EPOCH,
                )["offerings"]
            )
        )

        with self.assertRaises(GeneratedAccessError) as replay:
            reveal()
        self.assertEqual(replay.exception.code, "reveal_unavailable")
        self.assertEqual(catalog.preparation_calls, 1)
        with self.assertRaises(GeneratedAccessError) as conflict:
            access.reveal(
                actor_authority=OPERATOR_AUTHORITY,
                expected_epoch=SHARED_EPOCH,
                idempotency_key="post-registration-expiry",
                promotion_id=str(hidden["promotion_id"]),
                expected_access_record_id=(
                    "generated-access-record:changed_record_00000001"
                ),
                publish=catalog._prepare_publication,
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

        self.assertIsNotNone(catalog.prepared_locator)
        offering_id, _selection_id, offering_revision = catalog.prepared_locator
        with self.assertRaises(LaunchSelectionError) as absent:
            catalog.exchange(
                actor_authority=self.actor,
                expected_epoch=SHARED_EPOCH,
                offering_id=offering_id,
                offering_revision=offering_revision,
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="rolled-back-publication-exchange",
            )
        self.assertEqual(absent.exception.code, "launch_selection_unavailable")
        self.assertEqual(self.registry._grants, {})

    def test_post_registration_monotonic_rollback_undoes_publication(self) -> None:
        def move_monotonic_backwards() -> None:
            self.clock.deadline_ms -= 1

        access, catalog = self._post_registration_catalog(
            after_prepare=move_monotonic_backwards,
        )
        hidden = access.promote(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="promote-monotonic-rollback",
            job_reference=FORGE_REFERENCE,
        )
        registry_before = (
            dict(self.registry._revisions),
            dict(self.registry._offerings),
            dict(self.registry._offering_by_selection_sha256),
            dict(self.registry._display_by_revision),
            set(self.registry._used_token_sha256s),
        )

        try:
            with self.assertRaisesRegex(RuntimeError, "monotonic_ms must not move"):
                catalog.reveal(
                    actor_authority=OPERATOR_AUTHORITY,
                    expected_epoch=SHARED_EPOCH,
                    idempotency_key="post-registration-monotonic-rollback",
                    promotion_id=str(hidden["promotion_id"]),
                    expected_access_record_id=str(hidden["access_record_id"]),
                )
        finally:
            self.clock.deadline_ms += 1

        self.assertEqual(catalog.preparation_calls, 1)
        self.assertEqual(catalog._pending_by_access_record, {})
        self.assertEqual(catalog._generated_by_access_record, {})
        self.assertEqual(
            (
                self.registry._revisions,
                self.registry._offerings,
                self.registry._offering_by_selection_sha256,
                self.registry._display_by_revision,
                self.registry._used_token_sha256s,
            ),
            registry_before,
        )

    def test_signed_catalog_eligibility_tampering_fails_uniformly(self) -> None:
        hidden = self._promote()
        revealed = self.catalog.reveal(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="reveal-proof-tampering",
            promotion_id=str(hidden["promotion_id"]),
            expected_access_record_id=str(hidden["access_record_id"]),
        )
        generated = next(
            offering
            for offering in self._catalog()["offerings"]
            if offering["origin"]["kind"] == "world_forge_selection"
        )
        binding = self.catalog._generated_by_offering[generated["offering_id"]]
        proof = mint_attendee_catalog_eligibility_proof(
            key=ELIGIBILITY_KEY,
            service_epoch=SHARED_EPOCH,
            offering_id=str(generated["offering_id"]),
            offering_revision=str(generated["offering_revision"]),
            access_record_id=str(revealed["access_record_id"]),
            origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            expires_at_ms=binding.expires_at_ms,
        )
        expired = mint_attendee_catalog_eligibility_proof(
            key=ELIGIBILITY_KEY,
            service_epoch=SHARED_EPOCH,
            offering_id=str(generated["offering_id"]),
            offering_revision=str(generated["offering_revision"]),
            access_record_id=str(revealed["access_record_id"]),
            origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
            revision_id=self.artifact.revision_id,
            immutable_digest=self.artifact.digest,
            expires_at_ms=self.clock.wall_ms,
        )
        tampered: tuple[AttendeeCatalogEligibilityProof, ...] = (
            replace(proof, verification_tag="sha256:" + "0" * 64),
            replace(
                proof,
                access_record_id="generated-access-record:tampered_00000001",
            ),
            replace(proof, origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT),
            replace(
                proof,
                offering_revision="offering-revision:tampered-0000000000000000",
            ),
            replace(
                proof,
                revision_id="world-revision:sha256:" + "f" * 64,
            ),
            replace(proof, immutable_digest="sha256:" + "f" * 64),
            expired,
        )
        for index, candidate in enumerate(tampered):
            with self.subTest(index=index):
                with self.assertRaises(LaunchSelectionError) as denied:
                    self.attendee.exchange_catalog_offering(
                        actor_authority=self.actor,
                        expected_epoch=SHARED_EPOCH,
                        offering_id=str(generated["offering_id"]),
                        offering_revision=str(generated["offering_revision"]),
                        notice_version_presented=NOTICE_VERSION,
                        idempotency_key=f"tampered-proof-{index}",
                        catalog_eligibility_proof=candidate,
                    )
                self.assertEqual(
                    denied.exception.code,
                    "launch_selection_unavailable",
                )
                self.assertEqual(self.registry._grants, {})
                self.assertEqual(self.core._launch_grants, {})
                self.assertNotIn(self.artifact.revision_id, self.core._revisions)

    def test_generated_seams_fail_closed_when_constructor_opt_in_is_absent(
        self,
    ) -> None:
        attendee, _registry, core, _actors = self._attendee(
            generated_world_access=False
        )
        context = attendee.bootstrap_context(
            bff_authority=BFF_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            gateway_context_id="gateway-context:" + "b" * 32,
            idempotency_key="bootstrap-default-off",
        )
        with self.assertRaises(LaunchSelectionError) as denied:
            attendee.exchange_catalog_offering(
                actor_authority=str(context["actor_context"]),
                expected_epoch=SHARED_EPOCH,
                offering_id="offering:phase8a-authored",
                offering_revision="offering-revision:default-off-000000000000",
                notice_version_presented=NOTICE_VERSION,
                idempotency_key="default-off-catalog-exchange",
            )
        self.assertEqual(denied.exception.code, "launch_selection_unavailable")
        with self.assertRaisesRegex(ValueError, "generated-world attendee access"):
            core.provision_generated_launch_selection(
                actor_authority=str(context["actor_context"]),
                expected_epoch=SHARED_EPOCH,
                issue=None,
                resolved=None,
            )
        self.assertEqual(core._launch_grants, {})
        self.assertEqual(len(core._revisions), 1)

    def test_catalog_composition_rejects_incompatible_policy_at_startup(self) -> None:
        with self.assertRaisesRegex(ValueError, "idle_ttl_ms"):
            GeneratedWorldCatalogPolicy(
                launch_limits={"absolute_ttl_ms": 1, "idle_ttl_ms": 2},
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_000,
            )

        with self.assertRaisesRegex(ValueError, "grant TTL"):
            GeneratedWorldCatalogService(
                access=self.access,
                registry=self.registry,
                attendee=self.attendee,
                policy=GeneratedWorldCatalogPolicy(
                    launch_limits=_launch_limits(),
                    notice_version=NOTICE_VERSION,
                    admission_closes_at_ms=self.clock.wall_ms + 1_000,
                    maximum_generated_offerings=2,
                    maximum_catalog_items=4,
                    launch_grant_ttl_ms=(
                        self.registry._policy.max_launch_grant_ttl_ms + 1
                    ),
                ),
                locator_key=LOCATOR_KEY,
                eligibility_key=ELIGIBILITY_KEY,
                clock_ms=self.clock.wall,
            )

        allowed = self.registry._allowed_operation_families
        self.registry._allowed_operation_families = frozenset({ProductOperation.CREATE})
        try:
            with self.assertRaisesRegex(ValueError, "operations"):
                GeneratedWorldCatalogService(
                    access=self.access,
                    registry=self.registry,
                    attendee=self.attendee,
                    policy=GeneratedWorldCatalogPolicy(
                        launch_limits=_launch_limits(),
                        notice_version=NOTICE_VERSION,
                        admission_closes_at_ms=self.clock.wall_ms + 1_000,
                        maximum_generated_offerings=2,
                        maximum_catalog_items=4,
                    ),
                    locator_key=LOCATOR_KEY,
                    eligibility_key=ELIGIBILITY_KEY,
                    clock_ms=self.clock.wall,
                )
        finally:
            self.registry._allowed_operation_families = allowed

    def _attendee(self, *, generated_world_access: bool = True):
        document = _authored_document()
        revision = _launch_revision(document)
        actors = tuple(
            LaunchSelectionActorSeed(
                authority=f"attendee-authority:phase8a-slot-{index:08d}",
                actor_id=f"attendee:phase8a-slot-{index}",
                role=ProductRole.ATTENDEE,
                expires_at_ms=self.clock.wall_ms + 3_600_000,
            )
            for index in range(2)
        )
        registry = LaunchSelectionRegistry(
            revisions=(revision,),
            offerings=(
                LaunchOfferingSeed(
                    offering_id="offering:phase8a-authored",
                    launch_selection_id=("launch-selection:phase8a_authored_00000001"),
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title="Authored baseline",
                    source_label="Authored",
                    revision_label="Pinned A1",
                    role=ProductRole.ATTENDEE,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits=_launch_limits(),
                    policy_id="policy:phase8a-authored",
                    notice_version=NOTICE_VERSION,
                    expires_at_ms=self.clock.wall_ms + 1_200_000,
                    launch_grant_ttl_ms=600_000,
                ),
            ),
            actors=actors,
            policy=LaunchSelectionPolicy(
                max_revisions=4,
                max_offerings=4,
                max_actor_authorities=2,
                max_active_launch_grants=8,
                max_idempotency_records=32,
                max_token_history=64,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=600_000,
                idempotency_ttl_ms=900_000,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_RegistryTokens(),
        )
        admission = ProductAdmissionLedger(
            ProductAdmissionLimits(
                max_active_sessions=3,
                max_logical_jobs=8,
                max_physical_workers=4,
                token_budget=2_000,
                spend_budget_units=2_000,
                presenter_stage_reserved_sessions=1,
                presenter_stage_reserved_logical_jobs=2,
                presenter_stage_reserved_physical_workers=1,
                presenter_stage_reserved_token_budget=400,
                presenter_stage_reserved_spend_budget_units=400,
            )
        )
        core = AttendeeSessionCoreService(
            revisions=(ImmutableRevisionSeed(revision.revision_id, document),),
            actors=tuple(
                ActorAuthoritySeed(
                    token=actor.authority,
                    actor_id=actor.actor_id,
                    role=actor.role,
                    expires_at_ms=actor.expires_at_ms,
                )
                for actor in actors
            ),
            executor=_Executor(),
            submit_task=self.tasks,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=600_000,
                idle_ttl_ms=300_000,
                tombstone_ttl_ms=60_000,
                failed_operation_ttl_ms=30_000,
                max_active_sessions=2,
                max_tombstones=4,
                max_idempotency_records=32,
                max_launch_grants=8,
                max_actor_authorities=2,
                max_revisions=4,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=4,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=16 * 1024,
                max_output_chunks_per_command=32,
                max_runtime_document_bytes=4 * 1024 * 1024,
                reserved_tokens_per_command=100,
                reserved_spend_units_per_command=100,
            ),
            admission=admission,
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_CoreTokens(),
            generated_world_access=generated_world_access,
        )
        attendee = PublicAttendeeSessionService(
            registry=registry,
            core=core,
            context_actors=actors,
            bff_authorities=(
                AttendeeBffAuthoritySeed(
                    authority=BFF_AUTHORITY,
                    authority_id="bff:phase8a-catalog",
                    expires_at_ms=self.clock.wall_ms + 3_600_000,
                ),
            ),
            policy=AttendeeContextPolicy(
                max_contexts=2,
                context_ttl_ms=1_800_000,
                max_bootstrap_idempotency_records=8,
                max_exchange_idempotency_records=32,
                admission_opens_at_ms=self.clock.wall_ms,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
            ),
            generated_world_access=generated_world_access,
            generated_eligibility_key=(
                ELIGIBILITY_KEY if generated_world_access else None
            ),
        )
        return attendee, registry, core, actors

    def _post_registration_catalog(
        self,
        *,
        after_prepare: Callable[[], None],
    ) -> tuple[GeneratedWorldAccessService, _PostRegistrationCatalog]:
        access = GeneratedWorldAccessService(
            actors=(
                GeneratedAccessActorSeed(
                    authority=OPERATOR_AUTHORITY,
                    actor_id="operator:phase8a-short-publication",
                    expires_at_ms=self.clock.wall_ms + 3_600_000,
                ),
            ),
            source=self.source,
            policy=GeneratedAccessPolicy(
                max_actor_authorities=1,
                max_active_promotions=1,
                max_access_records=2,
                max_idempotency_records=4,
                max_token_history=8,
                access_ttl_ms=100,
                idempotency_ttl_ms=100,
                failed_operation_ttl_ms=50,
            ),
            clock_ms=self.clock.wall,
            monotonic_ms=self.clock.monotonic,
            token_factory=_AccessTokens(),
            service_epoch=SHARED_EPOCH,
        )
        catalog = _PostRegistrationCatalog(
            after_prepare=after_prepare,
            access=access,
            registry=self.registry,
            attendee=self.attendee,
            policy=GeneratedWorldCatalogPolicy(
                launch_limits=_launch_limits(),
                notice_version=NOTICE_VERSION,
                admission_closes_at_ms=self.clock.wall_ms + 1_200_000,
                maximum_generated_offerings=1,
                maximum_catalog_items=2,
                launch_grant_ttl_ms=50,
            ),
            locator_key=LOCATOR_KEY,
            eligibility_key=ELIGIBILITY_KEY,
            clock_ms=self.clock.wall,
        )
        return access, catalog

    def _promote(self) -> dict[str, object]:
        return self.access.promote(
            actor_authority=OPERATOR_AUTHORITY,
            expected_epoch=SHARED_EPOCH,
            idempotency_key="promote-phase8a-catalog",
            job_reference=FORGE_REFERENCE,
        )

    def _catalog(self) -> dict[str, object]:
        return self.catalog.catalog(
            actor_authority=self.actor,
            expected_epoch=SHARED_EPOCH,
        )

    def _exchange(self, offering: dict[str, object], key: str) -> dict[str, object]:
        return self.catalog.exchange(
            actor_authority=self.actor,
            expected_epoch=SHARED_EPOCH,
            offering_id=str(offering["offering_id"]),
            offering_revision=str(offering["offering_revision"]),
            notice_version_presented=str(offering["notice_version"]),
            idempotency_key=key,
        )


def _launch_limits() -> dict[str, int]:
    return {
        "absolute_ttl_ms": 600_000,
        "idle_ttl_ms": 300_000,
        "max_commands": 4,
        "max_command_bytes": 4_096,
        "command_reserved_tokens": 100,
        "command_reserved_spend_units": 100,
        "command_max_attempts": 1,
        "command_deadline_ms": 10_000,
    }


def _authored_document() -> dict[str, object]:
    return {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "authored baseline",
                    "active_host": "authored-console",
                    "entry_host": "authored-console",
                    "hosts_by_name": {
                        "authored-console": {"hostname": "authored-console"}
                    },
                }
            },
        },
    }


def _launch_revision(document: dict[str, object]) -> LaunchRevisionSeed:
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


def _forge_profile() -> StateProfile:
    return StateProfile(
        name="phase8a-generated-candidate",
        level="world",
        created_at="2026-08-30T12:00:00+00:00",
        state={
            "world_state": {
                "name": "phase8a generated world",
                "active_host": "tracking-console",
                "entry_host": "tracking-console",
                "purpose": {"theme": "Generated attendee catalog test"},
                "hosts_by_name": {
                    "tracking-console": {
                        "login_banner": "Test console\n",
                        "hostname": "tracking-console",
                        "purpose": {"theme": "Track generated access"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )


if __name__ == "__main__":
    unittest.main()
