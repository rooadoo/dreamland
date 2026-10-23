from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from dreamland.product.launch_selection import (
    LaunchGrantProvisioningCapacityError,
    LaunchGrantProvisioningReceipt,
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import ProductOperation, ProductRole

ATTENDEE_AUTHORITY = "authority:launch-selection-attendee-0001"
PRESENTER_AUTHORITY = "authority:launch-selection-presenter-0001"
SECOND_AUTHORITY = "authority:launch-selection-attendee-0002"
AUTHORED_SELECTION = "launch-selection:authored-opaque-00000001"
FORGED_SELECTION = "launch-selection:forged-opaque-0000000002"
LAUNCH_GRANT_PATTERN = r"^launch-grant:[0-9a-f]{16}_[0-9a-f]{64}$"
LAUNCH_GRANT_DERIVATION_DOMAIN = b"dreamland.launch-selection.launch-grant.v1"
MAX_LAUNCH_GRANT_SERIAL = 0xFFFF_FFFF_FFFF_FFFF


class ManualClock:
    def __init__(self, value: int = 10_000) -> None:
        self.wall = value
        self.monotonic = value

    def clock_ms(self) -> int:
        return self.wall

    def monotonic_ms(self) -> int:
        return self.monotonic

    def advance(self, milliseconds: int) -> None:
        self.wall += milliseconds
        self.monotonic += milliseconds


class SequenceTokens:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self, purpose: str) -> str:
        self.index += 1
        return f"{purpose}:launch-selection-test-{self.index:08d}"


def _revision_seed(document: dict[str, object]) -> LaunchRevisionSeed:
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(f"world-revision:sha256:{digest}", document)


def _expected_launch_grant(*, service_epoch: str, serial: int, candidate: str) -> str:
    epoch_bytes = service_epoch.encode("ascii")
    candidate_bytes = candidate.encode("ascii")
    material = b"".join(
        (
            LAUNCH_GRANT_DERIVATION_DOMAIN,
            b"\x00",
            len(epoch_bytes).to_bytes(2, "big"),
            epoch_bytes,
            serial.to_bytes(8, "big"),
            len(candidate_bytes).to_bytes(2, "big"),
            candidate_bytes,
        )
    )
    return f"launch-grant:{serial:016x}_{hashlib.sha256(material).hexdigest()}"


def _offering_seed(
    revision: LaunchRevisionSeed,
    *,
    offering_id: str,
    launch_selection_id: str,
    origin: LaunchSelectionOrigin,
    role: ProductRole = ProductRole.ATTENDEE,
    expires_at_ms: int | None = 100_000,
    launch_grant_ttl_ms: int = 1_000,
) -> LaunchOfferingSeed:
    return LaunchOfferingSeed(
        offering_id=offering_id,
        launch_selection_id=launch_selection_id,
        revision_id=revision.revision_id,
        origin=origin,
        title="Authored operations"
        if origin.value.startswith("authored")
        else "Audience world",
        source_label="Authored"
        if origin is LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT
        else "World Forge",
        revision_label="Pinned snapshot"
        if origin.value.startswith("authored")
        else "Forge selection",
        role=role,
        allowed_operations=frozenset(
            {ProductOperation.CREATE, ProductOperation.SWITCH}
        ),
        limits={"absolute_ttl_ms": 60_000, "max_commands": 12},
        policy_id="policy:launch-development-v1",
        notice_version="notice:launch-development-v1",
        expires_at_ms=expires_at_ms,
        launch_grant_ttl_ms=launch_grant_ttl_ms,
    )


def _raise_provisioning_capacity() -> None:
    raise LaunchGrantProvisioningCapacityError()


def _commit_product_binding(issue, binding_sha256: str):
    receipt = LaunchGrantProvisioningReceipt(binding_sha256)
    issue._prepare_provisioning_receipt(receipt)
    issue._mark_provisioning_committed()
    return receipt


class LaunchSelectionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.tokens = SequenceTokens()
        self.document = {
            "kind": "dreamland.authored_world_revision",
            "schema_revision": "1",
            "profile": {
                "level": "world",
                "state": {
                    "name": "private source name",
                    "profile_path": "/operator/private.json",
                    "source_session": "session:private",
                    "comment": "operator-only note",
                    "world_state": {"hosts_by_name": {"console": {}}},
                },
            },
        }
        self.authored_revision = _revision_seed(self.document)
        self.forged_revision = _revision_seed(
            {
                "kind": "dreamland.world_forge_revision",
                "schema_revision": "1",
                "profile": {
                    "level": "world",
                    "state": {"world_state": {"hosts_by_name": {}}},
                },
            }
        )
        self.registry = self._registry()

    def _registry(
        self,
        *,
        revisions=None,
        offerings=None,
        actors=None,
        policy: LaunchSelectionPolicy | None = None,
        tokens=None,
    ) -> LaunchSelectionRegistry:
        if revisions is None:
            revisions = (self.authored_revision, self.forged_revision)
        if offerings is None:
            offerings = (
                _offering_seed(
                    self.authored_revision,
                    offering_id="offering:authored-a",
                    launch_selection_id=AUTHORED_SELECTION,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                ),
                _offering_seed(
                    self.forged_revision,
                    offering_id="offering:forged-b",
                    launch_selection_id=FORGED_SELECTION,
                    origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                ),
            )
        if actors is None:
            actors = (
                LaunchSelectionActorSeed(
                    ATTENDEE_AUTHORITY,
                    "browser:attendee-a",
                    ProductRole.ATTENDEE,
                    100_000,
                ),
                LaunchSelectionActorSeed(
                    PRESENTER_AUTHORITY,
                    "browser:attendee-a",
                    ProductRole.PRESENTER,
                    100_000,
                ),
                LaunchSelectionActorSeed(
                    SECOND_AUTHORITY,
                    "browser:attendee-b",
                    ProductRole.ATTENDEE,
                    100_000,
                ),
            )
        return LaunchSelectionRegistry(
            revisions=revisions,
            offerings=offerings,
            actors=actors,
            policy=policy,
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=self.clock.clock_ms,
            monotonic_ms=self.clock.monotonic_ms,
            token_factory=tokens or self.tokens,
        )

    def _options(self, *, authority: str = ATTENDEE_AUTHORITY):
        return self.registry.offerings(
            actor_authority=authority,
            expected_epoch=self.registry.service_epoch,
        )

    def _exchange(
        self,
        *,
        authority: str = ATTENDEE_AUTHORITY,
        selection: str = AUTHORED_SELECTION,
        key: str = "exchange-selection-0001",
        provision=lambda _issue, _resolved: None,
    ):
        return self.registry.exchange_selection(
            actor_authority=authority,
            expected_epoch=self.registry.service_epoch,
            launch_selection_id=selection,
            idempotency_key=key,
            provision=provision,
        )

    def test_import_is_inert(self) -> None:
        code = """
import json
import threading
before = [thread.name for thread in threading.enumerate()]
from dreamland.product.launch_selection import LaunchSelectionOrigin
after = [thread.name for thread in threading.enumerate()]
print(json.dumps({"before": before, "after": after, "origin": LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["before"], payload["after"])
        self.assertEqual(payload["origin"], "authored_profile_snapshot")

    def test_options_are_authorized_role_filtered_safe_and_nonauthorizing(self) -> None:
        options = self._options()
        self.assertEqual(len(options), 2)
        self.assertEqual(
            set(options[0]),
            {
                "launch_selection_id",
                "offering_id",
                "display",
                "origin",
                "revision",
                "expires_at",
            },
        )
        self.assertEqual(
            {option["launch_selection_id"] for option in options},
            {AUTHORED_SELECTION, FORGED_SELECTION},
        )
        self.assertEqual(
            {option["origin"]["kind"] for option in options},
            {"authored_profile_snapshot", "world_forge_selection"},
        )
        self.assertEqual(self._options(authority=PRESENTER_AUTHORITY), [])
        serialized = json.dumps(options, sort_keys=True)
        for private_value in (
            "private source name",
            "/operator/private.json",
            "session:private",
            "operator-only note",
            ATTENDEE_AUTHORITY,
        ):
            self.assertNotIn(private_value, serialized)
        for forbidden_key in (
            "document",
            "path",
            "profile",
            "profile_name",
            "profile_path",
            "source_session",
            "comment",
            "authority",
        ):
            self.assertNotIn(f'"{forbidden_key}"', serialized)
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.offerings(
                actor_authority="authority:launch-selection-unknown-0000",
                expected_epoch=self.registry.service_epoch,
            )
        self.assertEqual(caught.exception.code, "authority_denied")
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch="launch-selection-epoch:stale-0000",
            )
        self.assertEqual(caught.exception.code, "service_epoch_changed")

    def test_required_role_denies_before_listing_or_claiming_exchange_key(self) -> None:
        with self.assertRaises(LaunchSelectionError) as listed:
            self.registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                required_role=ProductRole.PRESENTER,
            )
        self.assertEqual(listed.exception.code, "authority_denied")

        for selection in (AUTHORED_SELECTION, FORGED_SELECTION):
            with self.subTest(selection=selection):
                with self.assertRaises(LaunchSelectionError) as exchanged:
                    self.registry.exchange_selection(
                        actor_authority=ATTENDEE_AUTHORITY,
                        expected_epoch=self.registry.service_epoch,
                        launch_selection_id=selection,
                        idempotency_key="required-role-unclaimed-0001",
                        provision=lambda _issue, _resolved: None,
                        required_role=ProductRole.PRESENTER,
                    )
                self.assertEqual(exchanged.exception.code, "authority_denied")

        issue = self.registry.exchange_selection(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            launch_selection_id=AUTHORED_SELECTION,
            idempotency_key="required-role-unclaimed-0001",
            provision=lambda _issue, _resolved: None,
            required_role=ProductRole.ATTENDEE,
        )
        self.assertFalse(issue.replayed)
        with self.assertRaises(ValueError):
            self.registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                required_role="presenter",  # type: ignore[arg-type]
            )

    def test_content_identity_matches_artifact_newline_invariant(self) -> None:
        revision = self.registry.register_revision(
            _revision_seed(
                {
                    "profile": {"state": {"z": 1, "a": 2}, "level": "world"},
                    "schema_revision": "1",
                    "kind": "dreamland.authored_world_revision",
                }
            )
        )
        self.assertTrue(revision.canonical_json.endswith(b"\n"))
        digest = hashlib.sha256(revision.canonical_json).hexdigest()
        self.assertEqual(revision.immutable_digest, f"sha256:{digest}")
        self.assertEqual(revision.revision_id, f"world-revision:sha256:{digest}")
        with self.assertRaises(ValueError):
            self.registry.register_revision(
                LaunchRevisionSeed(
                    "world-revision:sha256:" + "0" * 64,
                    {"kind": "mismatched"},
                )
            )

    def test_revision_is_detached_and_allocate_is_defensive(self) -> None:
        original = {"kind": "revision", "values": [1, {"value": "initial"}]}
        seed = _revision_seed(original)
        registry = self._registry(revisions=(), offerings=())
        revision = registry.register_revision(seed)
        original["values"][1]["value"] = "mutated"
        allocated = revision.allocate()
        self.assertEqual(allocated["values"][1]["value"], "initial")
        allocated["values"][1]["value"] = "also-mutated"
        self.assertEqual(revision.allocate()["values"][1]["value"], "initial")
        self.assertNotIn("initial", repr(revision))
        self.assertFalse(hasattr(revision, "origin"))

    def test_exchange_accepts_only_selection_locator_and_offering_id_cannot_substitute(
        self,
    ) -> None:
        with self.assertRaises(LaunchSelectionError) as caught:
            self._exchange(selection="offering:authored-a")
        self.assertEqual(caught.exception.code, "request_invalid")
        issue = self._exchange(key="valid-exchange-0001")
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_grant=AUTHORED_SELECTION,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")
        self.assertNotEqual(issue.launch_grant, AUTHORED_SELECTION)
        with self.assertRaises(TypeError):
            self.registry.exchange_selection(  # type: ignore[call-arg]
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_selection_id=AUTHORED_SELECTION,
                idempotency_key="unexpected-state-0001",
                provision=lambda _issue, _resolved: None,
                profile_path="/operator/private.json",
            )

    def test_exchange_provisions_before_publish_and_binds_all_authority_fields(
        self,
    ) -> None:
        callback_calls = []

        def provision(issue, resolved) -> None:
            callback_calls.append((issue, resolved))
            with self.assertRaises(LaunchSelectionError) as caught:
                self.registry.resolve_launch_grant(
                    actor_authority=ATTENDEE_AUTHORITY,
                    expected_epoch=self.registry.service_epoch,
                    launch_grant=issue.launch_grant,
                    operation=ProductOperation.CREATE,
                )
            self.assertEqual(caught.exception.code, "launch_grant_unavailable")
            self.assertEqual(resolved.revision.allocate(), self.document)

        issue = self._exchange(provision=provision)
        self.assertEqual(len(callback_calls), 1)
        projection = issue.to_projection()
        self.assertEqual(
            set(projection),
            {
                "kind",
                "service_epoch",
                "launch_grant",
                "grant_expires_at",
                "selection",
                "replayed",
            },
        )
        self.assertEqual(projection["selection"], self._options()[0])
        resolved = self.registry.resolve_launch_grant(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            launch_grant=issue.launch_grant,
            operation=ProductOperation.SWITCH,
        )
        self.assertEqual(resolved.actor_id, "browser:attendee-a")
        self.assertEqual(resolved.role, ProductRole.ATTENDEE)
        self.assertEqual(
            resolved.allowed_operations,
            frozenset({ProductOperation.CREATE, ProductOperation.SWITCH}),
        )
        self.assertEqual(
            dict(resolved.limits),
            {"absolute_ttl_ms": 60_000, "max_commands": 12},
        )
        self.assertEqual(resolved.policy_id, "policy:launch-development-v1")
        self.assertEqual(resolved.notice_version, "notice:launch-development-v1")
        self.assertEqual(resolved.expires_at_ms, 11_000)

    def test_exact_replay_returns_same_grant_without_reprovisioning(self) -> None:
        calls = []

        def provision(_issue, _resolved) -> None:
            calls.append(1)

        first = self._exchange(provision=provision)
        replay = self._exchange(provision=provision)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.launch_grant, replay.launch_grant)
        self.assertEqual(calls, [1])
        with self.assertRaises(LaunchSelectionError) as caught:
            self._exchange(selection=FORGED_SELECTION, provision=provision)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_required_product_binding_receipt_is_validated_and_replayed(
        self,
    ) -> None:
        binding_sha256 = "a" * 64
        callbacks = []

        def provision(issue, _resolved) -> LaunchGrantProvisioningReceipt:
            callbacks.append(1)
            return _commit_product_binding(issue, binding_sha256)

        first = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="binding-receipt-0001",
            provision=provision,
            require_launch_binding_receipt=True,
        )
        replay = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="binding-receipt-0001",
            provision=lambda _issue, _resolved: None,
            require_launch_binding_receipt=True,
        )

        self.assertEqual(first.launch_binding_sha256, binding_sha256)
        self.assertEqual(replay.launch_binding_sha256, binding_sha256)
        self.assertTrue(replay.replayed)
        self.assertEqual(callbacks, [1])
        self.assertNotIn("launch_binding_sha256", first.to_projection())

        for invalid in ("A" * 64, "sha256:" + "a" * 64, "a" * 63):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "binding digest is invalid"):
                    LaunchGrantProvisioningReceipt(invalid)

    def test_required_product_binding_receipt_fails_closed_and_leaves_key_reusable(
        self,
    ) -> None:
        before_grants = dict(self.registry._grants)
        for key, returned, message in (
            ("binding-missing-0001", None, "receipt is missing"),
            ("binding-invalid-0001", object(), "receipt is invalid"),
            (
                "binding-uncommitted-0001",
                LaunchGrantProvisioningReceipt("d" * 64),
                "commit is missing",
            ),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(RuntimeError, f"provisioning {message}"):
                    self.registry.exchange_offering(
                        actor_authority=ATTENDEE_AUTHORITY,
                        expected_epoch=self.registry.service_epoch,
                        offering_id="offering:authored-a",
                        idempotency_key=key,
                        provision=lambda _issue, _resolved, value=returned: value,
                        require_launch_binding_receipt=True,
                    )
                self.assertEqual(self.registry._grants, before_grants)

        recovered = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="binding-missing-0001",
            provision=lambda issue, _resolved: _commit_product_binding(issue, "b" * 64),
            require_launch_binding_receipt=True,
        )
        self.assertFalse(recovered.replayed)
        self.assertEqual(recovered.launch_binding_sha256, "b" * 64)
        recovered_uncommitted = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="binding-uncommitted-0001",
            provision=lambda issue, _resolved: _commit_product_binding(issue, "d" * 64),
            require_launch_binding_receipt=True,
        )
        self.assertFalse(recovered_uncommitted.replayed)
        self.assertEqual(recovered_uncommitted.launch_binding_sha256, "d" * 64)

    def test_product_binding_correlation_is_scoped_by_service_epoch(self) -> None:
        class RotatedTokens(SequenceTokens):
            def __call__(self, purpose: str) -> str:
                if purpose == "epoch":
                    return "epoch:rotated_launch_selection_0001"
                return super().__call__(purpose)

        binding_sha256 = "c" * 64
        first = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="epoch-binding-0001",
            provision=lambda issue, _resolved: _commit_product_binding(
                issue, binding_sha256
            ),
            require_launch_binding_receipt=True,
        )
        rotated = self._registry(tokens=RotatedTokens())
        callback_calls = []
        with self.assertRaises(LaunchSelectionError) as stale:
            rotated.exchange_offering(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                offering_id="offering:authored-a",
                idempotency_key="epoch-binding-stale-0001",
                provision=lambda _issue, _resolved: callback_calls.append(1),
                require_launch_binding_receipt=True,
            )
        self.assertEqual(stale.exception.code, "outcome_unknown")
        self.assertEqual(callback_calls, [])
        self.assertEqual(rotated._grants, {})

        second = rotated.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=rotated.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="epoch-binding-0001",
            provision=lambda issue, _resolved: _commit_product_binding(
                issue, binding_sha256
            ),
            require_launch_binding_receipt=True,
        )
        self.assertNotEqual(first.service_epoch, second.service_epoch)
        self.assertNotEqual(
            (first.service_epoch, first.launch_binding_sha256),
            (second.service_epoch, second.launch_binding_sha256),
        )

    def test_committed_product_binding_survives_callback_failure_and_interrupts(
        self,
    ) -> None:
        binding_sha256 = "e" * 64

        def fail_after_commit(issue, _resolved):
            _commit_product_binding(issue, binding_sha256)
            raise RuntimeError("wrapper discarded committed outcome")

        with self.assertLogs("dreamland.lifecycle", level="ERROR") as captured:
            committed = self.registry.exchange_offering(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                offering_id="offering:authored-a",
                idempotency_key="binding-ordinary-failure-0001",
                provision=fail_after_commit,
                require_launch_binding_receipt=True,
            )
        self.assertEqual(committed.launch_binding_sha256, binding_sha256)
        self.assertIn(
            "launch_selection_provision_callback_failed_after_commit",
            captured.output[0],
        )

        def interrupt_after_commit(issue, _resolved):
            _commit_product_binding(issue, "f" * 64)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.registry.exchange_offering(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                offering_id="offering:authored-a",
                idempotency_key="binding-interruption-0001",
                provision=interrupt_after_commit,
                require_launch_binding_receipt=True,
            )
        callbacks = []
        recovered = self.registry.exchange_offering(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=self.registry.service_epoch,
            offering_id="offering:authored-a",
            idempotency_key="binding-interruption-0001",
            provision=lambda _issue, _resolved: callbacks.append(1),
            require_launch_binding_receipt=True,
        )
        self.assertTrue(recovered.replayed)
        self.assertEqual(recovered.launch_binding_sha256, "f" * 64)
        self.assertEqual(callbacks, [])

    def test_success_replay_outlives_grant_without_republishing(self) -> None:
        callbacks = []

        def provision(_issue, _resolved) -> None:
            callbacks.append(1)

        first = self._exchange(key="expired-grant-replay-0001", provision=provision)
        self.clock.advance(1_000)
        replay = self._exchange(key="expired-grant-replay-0001", provision=provision)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.launch_grant, first.launch_grant)
        self.assertEqual(callbacks, [1])
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_grant=replay.launch_grant,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")

    def test_deterministic_unavailable_is_claimed_replayed_and_conflicts(self) -> None:
        unavailable = "launch-selection:unknown-opaque-000000009"
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(LaunchSelectionError) as caught:
                    self._exchange(selection=unavailable, key="unavailable-0001")
                self.assertEqual(caught.exception.code, "launch_selection_unavailable")
        with self.assertRaises(LaunchSelectionError) as caught:
            self._exchange(selection=AUTHORED_SELECTION, key="unavailable-0001")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_retryable_provisioning_capacity_publishes_nothing_and_leaves_key_unclaimed(
        self,
    ) -> None:
        captured_grants = []

        def no_capacity(issue, _resolved) -> None:
            captured_grants.append(issue.launch_grant)
            raise LaunchGrantProvisioningCapacityError()

        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(LaunchSelectionError) as caught:
                    self._exchange(
                        selection=AUTHORED_SELECTION,
                        key="capacity-unclaimed-0001",
                        provision=no_capacity,
                    )
                self.assertEqual(
                    caught.exception.code, "launch_selection_capacity_exhausted"
                )
                self.assertEqual(caught.exception.retry, "after_delay")
                with self.assertRaises(LaunchSelectionError) as unresolved:
                    self.registry.resolve_launch_grant(
                        actor_authority=ATTENDEE_AUTHORITY,
                        expected_epoch=self.registry.service_epoch,
                        launch_grant=captured_grants[-1],
                        operation=ProductOperation.CREATE,
                    )
                self.assertEqual(unresolved.exception.code, "launch_grant_unavailable")
        changed = self._exchange(
            selection=FORGED_SELECTION,
            key="capacity-unclaimed-0001",
        )
        self.assertEqual(changed.offering_id, "offering:forged-b")

    def test_retryable_capacity_burns_unpublished_serial_and_leaves_key_unclaimed(
        self,
    ) -> None:
        authority = "authority:serial-burn-attendee-0001"
        candidate = "launch-grant:constant_private_candidate_00000001"

        class ConstantCandidateTokens:
            def __init__(self) -> None:
                self.grant_calls = 0

            def __call__(self, purpose: str) -> str:
                if purpose == "epoch":
                    return "epoch:serial_burn_epoch_00000001"
                self.grant_calls += 1
                return candidate

        offering = _offering_seed(
            self.authored_revision,
            offering_id="offering:rollback-token",
            launch_selection_id="launch-selection:rollback-token-opaque-00001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        tokens = ConstantCandidateTokens()
        registry = self._registry(
            revisions=(self.authored_revision,),
            offerings=(offering,),
            actors=(
                LaunchSelectionActorSeed(
                    authority,
                    "browser:rollback-token",
                    ProductRole.ATTENDEE,
                    100_000,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=1,
                max_offerings=1,
                max_actor_authorities=1,
                max_active_launch_grants=1,
                max_idempotency_records=1,
                max_token_history=3,
                max_revision_bytes=4096,
                max_launch_grant_ttl_ms=1_000,
                idempotency_ttl_ms=2_000,
            ),
            tokens=tokens,
        )

        rejected_grants = []

        def no_capacity(issue, _resolved) -> None:
            rejected_grants.append(issue.launch_grant)
            _raise_provisioning_capacity()

        with self.assertRaises(LaunchSelectionError) as caught:
            registry.exchange_selection(
                actor_authority=authority,
                expected_epoch=registry.service_epoch,
                launch_selection_id=offering.launch_selection_id,
                idempotency_key="rollback-capacity-0001",
                provision=no_capacity,
            )
        self.assertEqual(caught.exception.code, "launch_selection_capacity_exhausted")
        self.assertEqual(len(rejected_grants), 1)
        self.assertRegex(rejected_grants[0], LAUNCH_GRANT_PATTERN)
        self.assertEqual(rejected_grants[0].split(":", 1)[1][:16], "0000000000000001")
        with self.assertRaises(LaunchSelectionError) as unresolved:
            registry.resolve_launch_grant(
                actor_authority=authority,
                expected_epoch=registry.service_epoch,
                launch_grant=rejected_grants[0],
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(unresolved.exception.code, "launch_grant_unavailable")

        recovered = registry.exchange_selection(
            actor_authority=authority,
            expected_epoch=registry.service_epoch,
            launch_selection_id=offering.launch_selection_id,
            idempotency_key="rollback-capacity-0001",
            provision=lambda _issue, _resolved: None,
        )
        self.assertFalse(recovered.replayed)
        self.assertRegex(recovered.launch_grant, LAUNCH_GRANT_PATTERN)
        self.assertEqual(
            recovered.launch_grant.split(":", 1)[1][:16], "0000000000000002"
        )
        self.assertNotEqual(recovered.launch_grant, rejected_grants[0])
        self.assertNotEqual(recovered.launch_grant, candidate)
        self.assertEqual(tokens.grant_calls, 2)
        self.assertEqual(len(registry._used_token_sha256s), 3)
        self.assertEqual(len(registry._grants), 1)

    def test_unknown_callback_failure_burns_serial_without_publishing(self) -> None:
        rejected = []

        def fail(issue, _resolved) -> None:
            rejected.append(issue.launch_grant)
            raise RuntimeError("adjacent provisioning failed")

        with self.assertRaisesRegex(RuntimeError, "adjacent provisioning failed"):
            self._exchange(key="unknown-callback-0001", provision=fail)
        recovered = self._exchange(key="unknown-callback-0001")

        self.assertEqual(
            rejected[0].removeprefix("launch-grant:").split("_", 1)[0],
            "0000000000000001",
        )
        self.assertEqual(
            recovered.launch_grant.removeprefix("launch-grant:").split("_", 1)[0],
            "0000000000000002",
        )
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_grant=rejected[0],
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")

    def test_wrong_actor_role_epoch_and_operation_never_resolve_grant(self) -> None:
        issue = self._exchange()
        for authority in (SECOND_AUTHORITY, PRESENTER_AUTHORITY):
            with self.subTest(authority=authority):
                with self.assertRaises(LaunchSelectionError) as caught:
                    self.registry.resolve_launch_grant(
                        actor_authority=authority,
                        expected_epoch=self.registry.service_epoch,
                        launch_grant=issue.launch_grant,
                        operation=ProductOperation.CREATE,
                    )
                self.assertEqual(caught.exception.code, "launch_grant_unavailable")
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch="launch-selection-epoch:stale-0000",
                launch_grant=issue.launch_grant,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "service_epoch_changed")
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.exchange_selection(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch="epoch:stale_epoch_value_000001",
                launch_selection_id=AUTHORED_SELECTION,
                idempotency_key="stale-exchange-0001",
                provision=lambda _issue, _resolved: None,
            )
        self.assertEqual(caught.exception.code, "outcome_unknown")

        create_only = _offering_seed(
            self.authored_revision,
            offering_id="offering:create-only",
            launch_selection_id="launch-selection:create-only-opaque-00003",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        create_only = replace(
            create_only,
            allowed_operations=frozenset({ProductOperation.CREATE}),
        )
        registry = self._registry(offerings=(create_only,))
        create_issue = registry.exchange_selection(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=registry.service_epoch,
            launch_selection_id=create_only.launch_selection_id,
            idempotency_key="create-only-0001",
            provision=lambda _issue, _resolved: None,
        )
        with self.assertRaises(LaunchSelectionError) as caught:
            registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
                launch_grant=create_issue.launch_grant,
                operation=ProductOperation.SWITCH,
            )
        self.assertEqual(caught.exception.code, "operation_denied")

    def test_grant_and_option_expiry_use_monotonic_time(self) -> None:
        issue = self._exchange()
        self.clock.wall = 1
        self.clock.monotonic += 1_000
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_grant=issue.launch_grant,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")

        self.clock = ManualClock()
        expiring = _offering_seed(
            self.authored_revision,
            offering_id="offering:expires",
            launch_selection_id="launch-selection:expiring-opaque-00000004",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            expires_at_ms=10_010,
        )
        registry = self._registry(offerings=(expiring,))
        self.clock.advance(10)
        self.assertEqual(
            registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
            ),
            [],
        )

    def test_phase8_expiry_clamp_is_additive_to_legacy_expired_seed_behavior(
        self,
    ) -> None:
        expired = _offering_seed(
            self.authored_revision,
            offering_id="offering:already-expired",
            launch_selection_id="launch-selection:already-expired-00000001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            expires_at_ms=self.clock.wall,
        )
        legacy = self._registry(offerings=(expired,))
        self.assertEqual(
            legacy.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=legacy.service_epoch,
            ),
            [],
        )

        with self.assertRaisesRegex(ValueError, "already expired"):
            self._registry(offerings=(replace(expired, maximum_remaining_ttl_ms=1),))

    def test_forward_wall_adjustment_cannot_extend_grant_or_option(self) -> None:
        issue = self._exchange()
        self.clock.wall = issue.expires_at_ms
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.resolve_launch_grant(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_grant=issue.launch_grant,
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")

        self.clock = ManualClock()
        expiring = _offering_seed(
            self.authored_revision,
            offering_id="offering:forward-wall-expiry",
            launch_selection_id="launch-selection:forward-wall-expiry-0001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            expires_at_ms=10_010,
        )
        registry = self._registry(offerings=(expiring,))
        self.clock.wall = 10_010
        self.assertEqual(
            registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
            ),
            [],
        )

    def test_expired_selection_rejection_is_reserved_and_actor_expiry_denies(
        self,
    ) -> None:
        self.clock = ManualClock()
        expiring = _offering_seed(
            self.authored_revision,
            offering_id="offering:expiring-exchange",
            launch_selection_id="launch-selection:expiring-exchange-000008",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            expires_at_ms=10_010,
        )
        registry = self._registry(offerings=(expiring,))
        self.clock.advance(10)
        for _attempt in range(2):
            with self.assertRaises(LaunchSelectionError) as caught:
                registry.exchange_selection(
                    actor_authority=ATTENDEE_AUTHORITY,
                    expected_epoch=registry.service_epoch,
                    launch_selection_id=expiring.launch_selection_id,
                    idempotency_key="expired-selection-0001",
                    provision=lambda _issue, _resolved: None,
                )
            self.assertEqual(caught.exception.code, "launch_selection_unavailable")
        with self.assertRaises(LaunchSelectionError) as caught:
            registry.exchange_selection(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
                launch_selection_id=FORGED_SELECTION,
                idempotency_key="expired-selection-0001",
                provision=lambda _issue, _resolved: None,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

        self.clock = ManualClock()
        actor_registry = self._registry(
            actors=(
                LaunchSelectionActorSeed(
                    ATTENDEE_AUTHORITY,
                    "browser:expiring",
                    ProductRole.ATTENDEE,
                    10_010,
                ),
            )
        )
        self.clock.advance(10)
        with self.assertRaises(LaunchSelectionError) as caught:
            actor_registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=actor_registry.service_epoch,
            )
        self.assertEqual(caught.exception.code, "authority_denied")

    def test_concurrent_identical_exchange_allocates_and_provisions_once(self) -> None:
        callbacks = []

        def provision(_issue, _resolved) -> None:
            callbacks.append(1)

        def exchange(_index: int):
            return self._exchange(key="concurrent-exchange-0001", provision=provision)

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(exchange, range(64)))
        self.assertEqual(len({result.launch_grant for result in results}), 1)
        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertEqual(sum(result.replayed for result in results), 63)
        self.assertEqual(callbacks, [1])

    def test_concurrent_distinct_exchanges_cannot_exceed_grant_capacity(self) -> None:
        policy = LaunchSelectionPolicy(
            max_revisions=2,
            max_offerings=2,
            max_actor_authorities=3,
            max_active_launch_grants=4,
            max_idempotency_records=32,
            max_token_history=64,
            max_revision_bytes=4096,
            max_launch_grant_ttl_ms=1_000,
            idempotency_ttl_ms=2_000,
        )
        registry = self._registry(policy=policy)
        provisioned = []

        def exchange(index: int) -> str:
            try:
                registry.exchange_selection(
                    actor_authority=ATTENDEE_AUTHORITY,
                    expected_epoch=registry.service_epoch,
                    launch_selection_id=AUTHORED_SELECTION,
                    idempotency_key=f"distinct-exchange-{index:04d}",
                    provision=lambda issue, _resolved: provisioned.append(
                        issue.launch_grant
                    ),
                )
            except LaunchSelectionError as error:
                return error.code
            return "ok"

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(exchange, range(32)))
        self.assertEqual(results.count("ok"), 4)
        self.assertEqual(results.count("launch_selection_capacity_exhausted"), 28)
        self.assertEqual(len(provisioned), 4)
        self.assertEqual(
            {
                int(value.removeprefix("launch-grant:").split("_", 1)[0], 16)
                for value in provisioned
            },
            {1, 2, 3, 4},
        )

    def test_reentrant_provisioning_cannot_exceed_active_grant_capacity(self) -> None:
        policy = LaunchSelectionPolicy(
            max_revisions=2,
            max_offerings=2,
            max_actor_authorities=3,
            max_active_launch_grants=1,
            max_idempotency_records=4,
            max_token_history=8,
            max_revision_bytes=4096,
            max_launch_grant_ttl_ms=1_000,
            idempotency_ttl_ms=2_000,
        )
        registry = self._registry(policy=policy)
        nested_errors = []

        def provision(_issue, _resolved) -> None:
            try:
                registry.exchange_selection(
                    actor_authority=ATTENDEE_AUTHORITY,
                    expected_epoch=registry.service_epoch,
                    launch_selection_id=AUTHORED_SELECTION,
                    idempotency_key="reentrant-inner-0001",
                    provision=lambda _inner_issue, _inner_resolved: None,
                )
            except LaunchSelectionError as error:
                nested_errors.append(error.code)

        result = registry.exchange_selection(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=registry.service_epoch,
            launch_selection_id=AUTHORED_SELECTION,
            idempotency_key="reentrant-outer-0001",
            provision=provision,
        )

        self.assertFalse(result.replayed)
        self.assertEqual(nested_errors, ["launch_selection_capacity_exhausted"])
        self.assertEqual(len(registry._grants), 1)
        self.assertEqual(registry._inflight_launch_grants, 0)
        self.assertEqual(len(registry._idempotency), 1)

    def test_dynamic_registration_replays_exact_and_rejects_collisions_atomically(
        self,
    ) -> None:
        document = {"kind": "dreamland.world_forge_revision", "value": 1}
        revision = _revision_seed(document)
        offering = _offering_seed(
            revision,
            offering_id="offering:dynamic",
            launch_selection_id="launch-selection:dynamic-opaque-000000005",
            origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
        )
        first = self.registry.register_trusted_offering(
            revision=revision, offering=offering
        )
        second = self.registry.register_trusted_offering(
            revision=revision, offering=offering
        )
        self.assertEqual(first, second)
        changed = _revision_seed({"kind": "dreamland.world_forge_revision", "value": 2})
        mismatched_id = LaunchRevisionSeed(revision.revision_id, changed.document)
        with self.assertRaises(ValueError):
            self.registry.register_trusted_offering(
                revision=mismatched_id, offering=offering
            )
        collision = _offering_seed(
            self.forged_revision,
            offering_id="offering:another",
            launch_selection_id="launch-selection:dynamic-opaque-000000005",
            origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
        )
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.register_offering(collision)
        self.assertEqual(caught.exception.code, "registration_conflict")

    def test_constructor_rejects_stage_incompatible_offerings(self) -> None:
        valid = _offering_seed(
            self.authored_revision,
            offering_id="offering:constructor-validation",
            launch_selection_id="launch-selection:constructor-validation-00001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        invalid = (
            replace(
                valid,
                allowed_operations=frozenset({ProductOperation.SWITCH}),
            ),
            replace(valid, origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION),
            replace(valid, limits={"max_commands": 0}),
            replace(
                valid,
                limits={"absolute_ttl_ms": 1_000, "idle_ttl_ms": 1_001},
            ),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    self._registry(
                        revisions=(self.authored_revision,),
                        offerings=(candidate,),
                    )

    def test_runtime_registration_validation_is_atomic_for_both_apis(self) -> None:
        valid = _offering_seed(
            self.authored_revision,
            offering_id="offering:runtime-validation",
            launch_selection_id="launch-selection:runtime-validation-opaque-001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        invalid_changes = (
            {"allowed_operations": frozenset({ProductOperation.SWITCH})},
            {"origin": LaunchSelectionOrigin.WORLD_FORGE_SELECTION},
            {"limits": {"max_commands": 0}},
            {"limits": {"absolute_ttl_ms": 1_000, "idle_ttl_ms": 1_001}},
        )
        for api_name in ("register_offering", "register_trusted_offering"):
            for index, changes in enumerate(invalid_changes):
                with self.subTest(api=api_name, changes=changes):
                    has_startup_revision = api_name == "register_offering"
                    api_slug = api_name.replace("_", "-")
                    registry = self._registry(
                        revisions=(self.authored_revision,)
                        if has_startup_revision
                        else (),
                        offerings=(),
                    )
                    candidate = replace(
                        valid,
                        offering_id=f"offering:runtime-{api_slug}-{index}",
                        launch_selection_id=(
                            "launch-selection:runtime-"
                            f"{api_slug}-{index}-opaque-token-0001"
                        ),
                        **changes,
                    )
                    if has_startup_revision:
                        register = registry.register_offering
                    else:

                        def register(offering, *, target=registry):
                            return target.register_trusted_offering(
                                revision=self.authored_revision,
                                offering=offering,
                            )

                    before = registry.offerings(
                        actor_authority=ATTENDEE_AUTHORITY,
                        expected_epoch=registry.service_epoch,
                    )
                    with self.assertRaises(ValueError):
                        register(candidate)
                    self.assertEqual(
                        registry.offerings(
                            actor_authority=ATTENDEE_AUTHORITY,
                            expected_epoch=registry.service_epoch,
                        ),
                        before,
                    )
                    corrected = replace(
                        valid,
                        offering_id=candidate.offering_id,
                        launch_selection_id=candidate.launch_selection_id,
                    )
                    projection = register(corrected)
                    self.assertEqual(projection["offering_id"], corrected.offering_id)

    def test_one_revision_requires_identical_display_metadata_at_construction(
        self,
    ) -> None:
        first = _offering_seed(
            self.authored_revision,
            offering_id="offering:display-constructor-a",
            launch_selection_id="launch-selection:display-constructor-a-00001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        second = replace(
            first,
            offering_id="offering:display-constructor-b",
            launch_selection_id="launch-selection:display-constructor-b-00001",
            title="Conflicting authored title",
        )

        with self.assertRaisesRegex(ValueError, "share display metadata"):
            self._registry(
                revisions=(self.authored_revision,),
                offerings=(first, second),
            )

    def test_runtime_display_conflicts_are_atomic_for_both_registration_apis(
        self,
    ) -> None:
        original = _offering_seed(
            self.authored_revision,
            offering_id="offering:display-original",
            launch_selection_id="launch-selection:display-original-opaque-0001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        registry = self._registry(
            revisions=(self.authored_revision,), offerings=(original,)
        )
        candidates = (
            (
                "offering:display-register",
                "launch-selection:display-register-opaque-0001",
                "Conflicting title",
                registry.register_offering,
            ),
            (
                "offering:display-trusted",
                "launch-selection:display-trusted-opaque-00001",
                "Conflicting title again",
                lambda offering: registry.register_trusted_offering(
                    revision=self.authored_revision,
                    offering=offering,
                ),
            ),
        )
        corrected = []
        for offering_id, selection_id, title, register in candidates:
            before = registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
            )
            conflict = replace(
                original,
                offering_id=offering_id,
                launch_selection_id=selection_id,
                title=title,
            )
            with self.subTest(offering_id=offering_id):
                with self.assertRaises(LaunchSelectionError) as caught:
                    register(conflict)
                self.assertEqual(caught.exception.code, "registration_conflict")
                self.assertEqual(
                    registry.offerings(
                        actor_authority=ATTENDEE_AUTHORITY,
                        expected_epoch=registry.service_epoch,
                    ),
                    before,
                )
                valid = replace(
                    original,
                    offering_id=offering_id,
                    launch_selection_id=selection_id,
                )
                corrected.append(register(valid))
        self.assertEqual(len(corrected), 2)
        self.assertEqual(
            len(
                registry.offerings(
                    actor_authority=ATTENDEE_AUTHORITY,
                    expected_epoch=registry.service_epoch,
                )
            ),
            3,
        )

    def test_contract_option_count_is_a_hard_policy_and_registration_bound(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "contract option limit"):
            LaunchSelectionPolicy(max_offerings=129)
        LaunchSelectionPolicy(
            max_actor_authorities=1,
            max_offerings=1,
            max_active_launch_grants=128,
            max_token_history=3,
        )
        with self.assertRaisesRegex(ValueError, "actors, offerings, and epoch"):
            LaunchSelectionPolicy(
                max_actor_authorities=1,
                max_offerings=1,
                max_active_launch_grants=1,
                max_token_history=2,
            )

        offerings = tuple(
            _offering_seed(
                self.authored_revision,
                offering_id=f"offering:contract-bound-{index:03d}",
                launch_selection_id=(
                    f"launch-selection:contract-bound-{index:03d}-opaque-token"
                ),
                origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            )
            for index in range(128)
        )
        registry = self._registry(
            revisions=(self.authored_revision,),
            offerings=offerings,
            actors=(
                LaunchSelectionActorSeed(
                    ATTENDEE_AUTHORITY,
                    "browser:contract-bound",
                    ProductRole.ATTENDEE,
                    100_000,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=1,
                max_offerings=128,
                max_actor_authorities=1,
                max_active_launch_grants=1,
                max_idempotency_records=1,
                max_token_history=130,
                max_revision_bytes=4096,
                max_launch_grant_ttl_ms=1_000,
                idempotency_ttl_ms=2_000,
            ),
        )
        options = registry.offerings(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=registry.service_epoch,
        )
        self.assertEqual(len(options), 128)
        extra = replace(
            offerings[0],
            offering_id="offering:contract-bound-extra",
            launch_selection_id="launch-selection:contract-bound-extra-opaque",
        )
        with self.assertRaises(LaunchSelectionError) as caught:
            registry.register_offering(extra)
        self.assertEqual(caught.exception.code, "registry_capacity_exhausted")
        self.assertEqual(
            registry.offerings(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=registry.service_epoch,
            ),
            options,
        )

    def test_registry_bounds_and_launch_capacity_are_hard(self) -> None:
        policy = LaunchSelectionPolicy(
            max_revisions=2,
            max_offerings=2,
            max_actor_authorities=3,
            max_active_launch_grants=1,
            max_idempotency_records=2,
            max_token_history=8,
            max_revision_bytes=4096,
            max_launch_grant_ttl_ms=1_000,
            idempotency_ttl_ms=2_000,
        )
        registry = self._registry(policy=policy)
        registry.exchange_selection(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=registry.service_epoch,
            launch_selection_id=AUTHORED_SELECTION,
            idempotency_key="capacity-first-0001",
            provision=lambda _issue, _resolved: None,
        )
        for selection in (FORGED_SELECTION, AUTHORED_SELECTION):
            with self.subTest(selection=selection):
                with self.assertRaises(LaunchSelectionError) as caught:
                    registry.exchange_selection(
                        actor_authority=SECOND_AUTHORITY,
                        expected_epoch=registry.service_epoch,
                        launch_selection_id=selection,
                        idempotency_key="capacity-unclaimed-0002",
                        provision=lambda _issue, _resolved: None,
                    )
                self.assertEqual(
                    caught.exception.code, "launch_selection_capacity_exhausted"
                )
        extra = _revision_seed({"kind": "extra"})
        with self.assertRaises(LaunchSelectionError) as caught:
            registry.register_revision(extra)
        self.assertEqual(caught.exception.code, "registry_capacity_exhausted")

    def test_supplied_token_collisions_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            self._registry(
                offerings=(
                    _offering_seed(
                        self.authored_revision,
                        offering_id="offering:one",
                        launch_selection_id=AUTHORED_SELECTION,
                        origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    ),
                    _offering_seed(
                        self.forged_revision,
                        offering_id="offering:two",
                        launch_selection_id=AUTHORED_SELECTION,
                        origin=LaunchSelectionOrigin.WORLD_FORGE_SELECTION,
                    ),
                )
            )
        with self.assertRaises(ValueError):
            self._registry(
                actors=(
                    LaunchSelectionActorSeed(
                        ATTENDEE_AUTHORITY,
                        "browser:one",
                        ProductRole.ATTENDEE,
                        100_000,
                    ),
                    LaunchSelectionActorSeed(
                        ATTENDEE_AUTHORITY,
                        "browser:two",
                        ProductRole.ATTENDEE,
                        100_000,
                    ),
                )
            )

    def test_constant_private_candidate_supports_bounded_long_lived_issuance(
        self,
    ) -> None:
        authority = "authority:long-lived-launch-selection-0001"
        service_epoch = "epoch:long_lived_grant_epoch_000001"
        candidate = "launch-grant:constant_high_entropy_candidate_000001"

        class ConstantCandidateTokens:
            def __init__(self) -> None:
                self.grant_calls = 0

            def __call__(self, purpose: str) -> str:
                if purpose == "epoch":
                    return service_epoch
                self.grant_calls += 1
                return candidate

        offering = _offering_seed(
            self.authored_revision,
            offering_id="offering:long-lived",
            launch_selection_id="launch-selection:long-lived-opaque-token-0001",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            expires_at_ms=100_000,
        )
        tokens = ConstantCandidateTokens()
        registry = self._registry(
            revisions=(self.authored_revision,),
            offerings=(offering,),
            actors=(
                LaunchSelectionActorSeed(
                    authority,
                    "browser:long-lived",
                    ProductRole.ATTENDEE,
                    100_000,
                ),
            ),
            policy=LaunchSelectionPolicy(
                max_revisions=1,
                max_offerings=1,
                max_actor_authorities=1,
                max_active_launch_grants=1,
                max_idempotency_records=1,
                max_token_history=3,
                max_revision_bytes=4096,
                max_launch_grant_ttl_ms=1_000,
                idempotency_ttl_ms=2_000,
            ),
            tokens=tokens,
        )

        issued = []
        for index in range(12):
            issue = registry.exchange_selection(
                actor_authority=authority,
                expected_epoch=registry.service_epoch,
                launch_selection_id=offering.launch_selection_id,
                idempotency_key=f"long-lived-{index:04d}",
                provision=lambda _issue, _resolved: None,
            )
            issued.append(issue.launch_grant)
            self.assertRegex(issue.launch_grant, LAUNCH_GRANT_PATTERN)
            self.assertNotEqual(issue.launch_grant, candidate)
            self.assertEqual(len(registry._used_token_sha256s), 3)
            self.assertEqual(len(registry._grants), 1)
            self.clock.advance(2_000)

        self.assertEqual(len(set(issued)), 12)
        self.assertEqual(
            [
                int(value.removeprefix("launch-grant:").split("_", 1)[0], 16)
                for value in issued
            ],
            list(range(1, 13)),
        )
        self.assertEqual(
            issued[0],
            _expected_launch_grant(
                service_epoch=service_epoch,
                serial=1,
                candidate=candidate,
            ),
        )
        self.assertEqual(tokens.grant_calls, 12)
        with self.assertRaises(LaunchSelectionError) as caught:
            registry.resolve_launch_grant(
                actor_authority=authority,
                expected_epoch=registry.service_epoch,
                launch_grant=issued[0],
                operation=ProductOperation.CREATE,
            )
        self.assertEqual(caught.exception.code, "launch_grant_unavailable")

    def test_serial_exhaustion_fails_closed_without_breaking_exact_replay(self) -> None:
        self.registry._launch_grant_serial = MAX_LAUNCH_GRANT_SERIAL - 1
        issue = self._exchange(key="serial-maximum-0001")
        self.assertEqual(
            issue.launch_grant.removeprefix("launch-grant:").split("_", 1)[0],
            "ffffffffffffffff",
        )
        replay = self._exchange(key="serial-maximum-0001")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.launch_grant, issue.launch_grant)

        self.clock.advance(1_000)
        callbacks = []
        with self.assertRaisesRegex(RuntimeError, "issuance serial is exhausted"):
            self._exchange(
                key="serial-exhausted-0002",
                provision=lambda _issue, _resolved: callbacks.append(1),
            )
        self.assertEqual(callbacks, [])
        self.assertEqual(self.registry._launch_grant_serial, MAX_LAUNCH_GRANT_SERIAL)

    def test_inputs_are_not_mutated_and_private_values_are_repr_hidden(self) -> None:
        limits = {"max_commands": 12}
        revision = _revision_seed(
            {
                "kind": "dreamland.authored_world_revision",
                "nested": {"value": 1},
            }
        )
        offering = LaunchOfferingSeed(
            offering_id="offering:input",
            launch_selection_id="launch-selection:input-opaque-00000000006",
            revision_id=revision.revision_id,
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
            title="Input",
            source_label="Authored",
            revision_label="Input",
            role=ProductRole.ATTENDEE,
            allowed_operations=frozenset({ProductOperation.CREATE}),
            limits=limits,
            policy_id="policy:input",
            notice_version="notice:input",
            expires_at_ms=100_000,
            launch_grant_ttl_ms=1_000,
        )
        registry = self._registry(revisions=(revision,), offerings=(offering,))
        limits["max_commands"] = 999
        issue = registry.exchange_selection(
            actor_authority=ATTENDEE_AUTHORITY,
            expected_epoch=registry.service_epoch,
            launch_selection_id=offering.launch_selection_id,
            idempotency_key="input-exchange-0001",
            provision=lambda _issue, _resolved: None,
        )
        self.assertEqual(dict(issue.limits), {"max_commands": 12})
        self.assertNotIn(issue.launch_grant, repr(issue))
        self.assertNotIn(offering.launch_selection_id, repr(issue))
        self.assertNotIn(ATTENDEE_AUTHORITY, repr(issue))
        self.assertNotIn(offering.launch_selection_id, repr(offering))

    def test_invalid_shape_origin_limits_and_errors_are_safe(self) -> None:
        invalid = _offering_seed(
            self.authored_revision,
            offering_id="offering:invalid",
            launch_selection_id="launch-selection:invalid-opaque-000000007",
            origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
        )
        with self.assertRaises(ValueError):
            self._registry(
                offerings=(
                    LaunchOfferingSeed(
                        offering_id=invalid.offering_id,
                        launch_selection_id=invalid.launch_selection_id,
                        revision_id=invalid.revision_id,
                        origin="authored_profile_snapshot",  # type: ignore[arg-type]
                        title=invalid.title,
                        source_label=invalid.source_label,
                        revision_label=invalid.revision_label,
                        role=invalid.role,
                        allowed_operations=invalid.allowed_operations,
                        limits=invalid.limits,
                        policy_id=invalid.policy_id,
                        notice_version=invalid.notice_version,
                        expires_at_ms=invalid.expires_at_ms,
                        launch_grant_ttl_ms=invalid.launch_grant_ttl_ms,
                    ),
                )
            )
        with self.assertRaises(LaunchSelectionError) as caught:
            self.registry.exchange_selection(
                actor_authority=ATTENDEE_AUTHORITY,
                expected_epoch=self.registry.service_epoch,
                launch_selection_id="short",
                idempotency_key="bad-shape-0001",
                provision=lambda _issue, _resolved: None,
            )
        self.assertEqual(caught.exception.code, "request_invalid")
        problem = caught.exception.to_problem()["error"]
        self.assertEqual(set(problem), {"code", "message", "retry", "retry_after_ms"})
        self.assertLessEqual(len(problem["message"]), 160)
        self.assertNotIn(ATTENDEE_AUTHORITY, json.dumps(problem))


if __name__ == "__main__":
    unittest.main()
