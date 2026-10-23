from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductLifecycleService,
    ProductOperation,
    ProductRole,
)

ALICE_ATTENDEE_AUTHORITY = "actor-alice-attendee-authority-0001"
ALICE_PRESENTER_AUTHORITY = "actor-alice-presenter-authority-0001"
BOB_ATTENDEE_AUTHORITY = "actor-bob-attendee-authority-000001"
ALICE_LAUNCH_A = "launch-alice-attendee-revision-a-0001"
ALICE_LAUNCH_B = "launch-alice-attendee-revision-b-0001"
ALICE_LAUNCH_C = "launch-alice-attendee-revision-c-0001"
ALICE_PRESENTER_LAUNCH = "launch-alice-presenter-revision-b-0001"
BOB_LAUNCH_A = "launch-bob-attendee-revision-a-000001"


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self._now_ms = now_ms
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._now_ms

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self._now_ms += milliseconds


class SequenceTokenFactory:
    def __init__(self, prefix: str = "preview") -> None:
        self.prefix = prefix
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self, purpose: str) -> str:
        with self._lock:
            self._count += 1
            return f"{self.prefix}-{purpose}-{self._count:04d}-opaque-value"


class ProductLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = _service(clock=self.clock)

    def test_current_lookup_never_creates(self) -> None:
        result = self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )

        self.assertEqual(
            result,
            {
                "kind": "no_current_session",
                "service_epoch": self.service.service_epoch,
            },
        )
        self.assertEqual(self.service._sessions_by_reference, {})
        self.assertEqual(self.service._idempotency, {})

    def test_create_returns_safe_lifecycle_snapshot_and_bound_launch_policy(
        self,
    ) -> None:
        result = self._create()
        snapshot = result["snapshot"]
        assert isinstance(snapshot, dict)

        self.assertEqual(result["kind"], "session_created")
        self.assertFalse(result["replayed"])
        self.assertNotEqual(snapshot["session_id"], snapshot["reference"])
        self.assertEqual(snapshot["authority_class"], "attendee")
        self.assertEqual(
            snapshot["starting_revision"],
            {
                "selected_record_id": "selected-record-a",
                "revision_id": "revision-a",
                "sha256": snapshot["starting_revision"]["sha256"],
            },
        )
        self.assertEqual(
            snapshot["launch_binding"]["limits"],
            {"absolute_ttl_ms": 10_000, "idle_ttl_ms": 3_000},
        )
        self.assertEqual(snapshot["launch_binding"]["policy_id"], "policy-a")
        self.assertEqual(snapshot["launch_binding"]["notice_version"], "notice-2026-08")
        self.assertEqual(snapshot["coordinates"], {"control_revision": 1})
        self.assertEqual(snapshot["available_controls"], ["snapshot", "close"])
        self.assertEqual(snapshot["lifecycle"]["state"], "active")
        self.assertNotIn("terminal_at_ms", snapshot["lifecycle"])
        self.assertNotIn("terminal_reason", snapshot["lifecycle"])

        serialized = json.dumps(result, sort_keys=True)
        for forbidden in (
            "world_state",
            "internal-profile-secret",
            "/private/revision/path",
            ALICE_ATTENDEE_AUTHORITY,
            ALICE_LAUNCH_A,
            "actor_id",
            "command_slot_generation",
            "accepted_state_revision",
            "event_position",
            "transcript_position",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_immutable_revision_bytes_and_allocations_are_isolated(self) -> None:
        source_document = _revision_a_document()
        service = _service(clock=self.clock, revision_a_document=source_document)
        source_document["world_state"]["hosts"]["entry"]["hostname"] = "changed"

        alice = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="create-alice-a",
        )
        bob = _create(
            service,
            actor_authority=BOB_ATTENDEE_AUTHORITY,
            launch_grant=BOB_LAUNCH_A,
            key="create-bob-a",
        )
        alice_snapshot = alice["snapshot"]
        bob_snapshot = bob["snapshot"]
        assert isinstance(alice_snapshot, dict)
        assert isinstance(bob_snapshot, dict)
        alice_runtime = service._sessions_by_reference[
            str(alice_snapshot["reference"])
        ].runtime_document
        bob_runtime = service._sessions_by_reference[
            str(bob_snapshot["reference"])
        ].runtime_document

        alice_runtime["world_state"]["hosts"]["entry"]["hostname"] = "alice-only"

        self.assertEqual(
            bob_runtime["world_state"]["hosts"]["entry"]["hostname"],
            "revision-a-host",
        )
        canonical = json.loads(service._revisions["revision-a"].canonical_json)
        self.assertEqual(
            canonical["world_state"]["hosts"]["entry"]["hostname"],
            "revision-a-host",
        )
        self.assertIsNot(alice_runtime, bob_runtime)
        self.assertIsNot(
            alice_runtime["world_state"]["hosts"],
            bob_runtime["world_state"]["hosts"],
        )

    def test_actor_role_has_an_independent_current_binding(self) -> None:
        attendee = self._create()
        presenter = _create(
            self.service,
            actor_authority=ALICE_PRESENTER_AUTHORITY,
            launch_grant=ALICE_PRESENTER_LAUNCH,
            key="create-stage",
        )

        attendee_current = self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )
        presenter_current = self.service.current(
            actor_authority=ALICE_PRESENTER_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )

        self.assertEqual(attendee_current["snapshot"], attendee["snapshot"])
        self.assertEqual(presenter_current["snapshot"], presenter["snapshot"])
        self.assertNotEqual(
            attendee_current["snapshot"]["reference"],
            presenter_current["snapshot"]["reference"],
        )
        self.assertEqual(presenter_current["snapshot"]["authority_class"], "presenter")

    def test_create_replay_is_exact_after_grant_consumption(self) -> None:
        first = self._create()
        replay = self._create()

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["snapshot"], first["snapshot"])
        self.assertEqual(replay["session_authority"], first["session_authority"])
        self.assertEqual(len(self.service._sessions_by_reference), 1)

        error = self._error_code(
            lambda: _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_A,
                key="another-create-key",
            )
        )
        self.assertEqual(error, "authority_denied")

    def test_same_idempotency_key_with_changed_body_conflicts(self) -> None:
        self._create()

        code = self._error_code(
            lambda: _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_B,
                key="create-key",
            )
        )

        self.assertEqual(code, "idempotency_conflict")
        self.assertEqual(len(self.service._sessions_by_reference), 1)

    def test_create_authority_denials_claim_and_replay_the_semantic_body(
        self,
    ) -> None:
        unknown_service = _service(clock=self.clock)
        cross_actor_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("cross-actor"),
        )
        expired_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("expired-grant"),
        )
        expired_grant = next(
            grant
            for grant in expired_service._launch_grants.values()
            if grant.selected_record_id == "selected-record-a"
        )
        expired_grant.expires_deadline_ms = self.clock()
        disallowed_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("disallowed-grant"),
        )
        disallowed_grant = next(
            grant
            for grant in disallowed_service._launch_grants.values()
            if grant.selected_record_id == "selected-record-a"
        )
        disallowed_grant.allowed_operations = frozenset()
        consumed_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("consumed-grant"),
        )
        _create(
            consumed_service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="consume-launch-grant",
        )
        cases = (
            (
                "unknown",
                unknown_service,
                ALICE_ATTENDEE_AUTHORITY,
                "unknown-launch-authority-00000001",
            ),
            (
                "cross_actor",
                cross_actor_service,
                ALICE_ATTENDEE_AUTHORITY,
                BOB_LAUNCH_A,
            ),
            (
                "expired",
                expired_service,
                ALICE_ATTENDEE_AUTHORITY,
                ALICE_LAUNCH_A,
            ),
            (
                "disallowed",
                disallowed_service,
                ALICE_ATTENDEE_AUTHORITY,
                ALICE_LAUNCH_A,
            ),
            (
                "consumed",
                consumed_service,
                ALICE_ATTENDEE_AUTHORITY,
                ALICE_LAUNCH_A,
            ),
        )

        for name, service, actor_authority, denied_grant in cases:
            with self.subTest(name=name):
                key = f"denied-create-{name}"

                def denied(
                    *,
                    service: ProductLifecycleService = service,
                    actor_authority: str = actor_authority,
                    denied_grant: str = denied_grant,
                    key: str = key,
                ) -> dict[str, object]:
                    return _create(
                        service,
                        actor_authority=actor_authority,
                        launch_grant=denied_grant,
                        key=key,
                    )

                first = self._error_signature(denied)
                replay = self._error_signature(denied)

                def changed_body(
                    *,
                    service: ProductLifecycleService = service,
                    actor_authority: str = actor_authority,
                    key: str = key,
                ) -> dict[str, object]:
                    return _create(
                        service,
                        actor_authority=actor_authority,
                        launch_grant=ALICE_LAUNCH_B,
                        key=key,
                    )

                changed = self._error_code(changed_body)

                self.assertEqual(first[0], "authority_denied")
                self.assertEqual(replay, first)
                self.assertEqual(changed, "idempotency_conflict")
                record = next(
                    record
                    for scope, record in service._idempotency.items()
                    if scope[-1] == key
                )
                self.assertNotIn(denied_grant, repr(record))

    def test_unrelated_create_denial_releases_key_at_failure_ttl(self) -> None:
        key = "expiring-denied-create"

        def denied() -> dict[str, object]:
            return _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant="unknown-launch-authority-00000001",
                key=key,
            )

        self.assertEqual(self._error_code(denied), "authority_denied")
        self.clock.advance(999)
        self.assertEqual(self._error_code(denied), "authority_denied")
        self.clock.advance(1)

        created = _create(
            self.service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key=key,
        )

        self.assertEqual(created["kind"], "session_created")
        self.assertFalse(created["replayed"])

    def test_create_preclaim_failures_leave_no_key_reservation(self) -> None:
        malformed_service = _service(clock=self.clock)
        malformed_key = "malformed-launch-reusable"
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    malformed_service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant="too-short",
                    key=malformed_key,
                )
            ),
            "authority_denied",
        )
        self.assertEqual(malformed_service._idempotency, {})
        self.assertEqual(
            _create(
                malformed_service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_A,
                key=malformed_key,
            )["kind"],
            "session_created",
        )

        stale_epoch_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("preclaim-epoch"),
        )
        stale_key = "stale-epoch-reusable"
        self.assertEqual(
            self._error_code(
                lambda: stale_epoch_service.create(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    expected_epoch="stale-service-epoch-evidence-0001",
                    idempotency_key=stale_key,
                )
            ),
            "outcome_unknown",
        )
        self.assertEqual(stale_epoch_service._idempotency, {})
        self.assertEqual(
            _create(
                stale_epoch_service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_A,
                key=stale_key,
            )["kind"],
            "session_created",
        )

        invalid_actor_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("preclaim-actor"),
        )
        actor_key = "invalid-actor-reusable"
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    invalid_actor_service,
                    actor_authority="unknown-actor-authority-00000001",
                    launch_grant=ALICE_LAUNCH_A,
                    key=actor_key,
                )
            ),
            "authority_denied",
        )
        self.assertEqual(invalid_actor_service._idempotency, {})
        self.assertEqual(
            _create(
                invalid_actor_service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_A,
                key=actor_key,
            )["kind"],
            "session_created",
        )

        invalid_key_service = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("preclaim-key"),
        )
        self.assertEqual(
            self._error_code(
                lambda: invalid_key_service.create(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    expected_epoch=invalid_key_service.service_epoch,
                    idempotency_key="",
                )
            ),
            "invalid_request",
        )
        self.assertEqual(invalid_key_service._idempotency, {})

    def test_distinct_create_while_active_is_recorded_conflict(self) -> None:
        self._create()

        first_code = self._error_code(
            lambda: _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_B,
                key="distinct-create",
            )
        )
        replay_code = self._error_code(
            lambda: _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_B,
                key="distinct-create",
            )
        )

        self.assertEqual(first_code, "current_session_exists")
        self.assertEqual(replay_code, "current_session_exists")
        self.assertEqual(len(self.service._sessions_by_reference), 1)

    def test_create_conflict_key_is_retained_through_related_tombstone(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)

        def conflicting_create(grant: str) -> dict[str, object]:
            return _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=grant,
                key="retained-create-conflict",
            )

        self.assertEqual(
            self._error_code(lambda: conflicting_create(ALICE_LAUNCH_B)),
            "current_session_exists",
        )
        self.clock.advance(1_001)
        self.assertEqual(
            self._error_code(lambda: conflicting_create(ALICE_LAUNCH_C)),
            "idempotency_conflict",
        )
        self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-related-session",
            expected_control_revision=1,
        )
        self.assertEqual(
            self._error_code(lambda: conflicting_create(ALICE_LAUNCH_B)),
            "current_session_exists",
        )

    def test_consumed_grant_denial_follows_related_session_retention(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        key = "consumed-grant-denial"

        def denied(grant: str = ALICE_LAUNCH_A) -> dict[str, object]:
            return _create(
                self.service,
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                launch_grant=grant,
                key=key,
            )

        first = self._error_signature(denied)
        record = next(
            record
            for scope, record in self.service._idempotency.items()
            if scope[-1] == key
        )
        self.assertEqual(first[0], "authority_denied")
        self.assertEqual(record.session_id, snapshot["session_id"])
        self.clock.advance(1_001)
        self.assertEqual(self._error_signature(denied), first)
        self.assertEqual(
            self._error_code(lambda: denied(ALICE_LAUNCH_B)),
            "idempotency_conflict",
        )
        self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-consumed-grant-session",
            expected_control_revision=1,
        )
        self.assertEqual(self._error_signature(denied), first)

    def test_cross_actor_role_grant_and_session_authority_are_denied(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)

        attempts = (
            lambda: _create(
                self.service,
                actor_authority=BOB_ATTENDEE_AUTHORITY,
                launch_grant=ALICE_LAUNCH_B,
                key="bob-with-alice-grant",
            ),
            lambda: self.service.snapshot(
                actor_authority=BOB_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
            ),
            lambda: self.service.snapshot(
                actor_authority=ALICE_PRESENTER_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
            ),
            lambda: self.service.snapshot(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["session_id"]),
                expected_epoch=self.service.service_epoch,
            ),
        )

        for attempt in attempts:
            with self.subTest(attempt=attempt):
                self.assertEqual(self._error_code(attempt), "authority_denied")

    def test_snapshot_reads_are_passive_and_authorized(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        reference = str(snapshot["reference"])
        last_activity = self.service._sessions_by_reference[
            reference
        ].last_activity_at_ms
        self.clock.advance(2_000)

        read = self.service.snapshot(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=reference,
            expected_epoch=self.service.service_epoch,
        )
        self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )

        self.assertEqual(read["snapshot"], snapshot)
        self.assertEqual(
            self.service._sessions_by_reference[reference].last_activity_at_ms,
            last_activity,
        )

    def test_close_increments_only_control_revision_and_revokes_authority(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        closed = self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-key",
            expected_control_revision=1,
        )
        terminal = closed["snapshot"]
        assert isinstance(terminal, dict)

        self.assertEqual(terminal["coordinates"], {"control_revision": 2})
        self.assertEqual(terminal["lifecycle"]["state"], "closed")
        self.assertEqual(terminal["lifecycle"]["terminal_reason"], "closed")
        self.assertEqual(terminal["available_controls"], [])
        self.assertNotIn("session_authority", json.dumps(terminal))
        self.assertEqual(self.service._sessions_by_reference, {})

        current = self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )
        self.assertEqual(current["kind"], "current_session_terminal")
        self.assertNotIn("session_authority", current)
        self.assertEqual(
            self._error_code(
                lambda: self.service.snapshot(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    session_authority=str(created["session_authority"]),
                    reference=str(snapshot["reference"]),
                    expected_epoch=self.service.service_epoch,
                )
            ),
            "authority_denied",
        )

    def test_close_exact_replay_survives_session_authority_revocation(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        arguments = {
            "actor_authority": ALICE_ATTENDEE_AUTHORITY,
            "session_authority": str(created["session_authority"]),
            "reference": str(snapshot["reference"]),
            "expected_epoch": self.service.service_epoch,
            "idempotency_key": "close-key",
            "expected_control_revision": 1,
        }

        first = self.service.close(**arguments)
        replay = self.service.close(**arguments)

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["snapshot"], first["snapshot"])
        self.assertEqual(len(self.service._tombstones_by_reference), 1)

    def test_close_changed_body_conflicts_before_revoked_authority(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-key",
            expected_control_revision=1,
        )

        code = self._error_code(
            lambda: self.service.close(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
                idempotency_key="close-key",
                expected_control_revision=2,
            )
        )

        self.assertEqual(code, "idempotency_conflict")

    def test_close_authority_denials_claim_and_replay_the_semantic_body(
        self,
    ) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        session_authority = str(created["session_authority"])
        reference = str(snapshot["reference"])
        cases = (
            (
                "unknown_authority",
                ALICE_ATTENDEE_AUTHORITY,
                "unknown-session-authority-0000001",
                reference,
            ),
            (
                "mismatched_reference",
                ALICE_ATTENDEE_AUTHORITY,
                session_authority,
                "unknown-session-reference-0000001",
            ),
            (
                "cross_actor",
                BOB_ATTENDEE_AUTHORITY,
                session_authority,
                reference,
            ),
        )

        for name, actor_authority, denied_authority, denied_reference in cases:
            with self.subTest(name=name):
                key = f"denied-close-{name}"

                def denied(
                    expected_control_revision: int = 1,
                    *,
                    actor_authority: str = actor_authority,
                    denied_authority: str = denied_authority,
                    denied_reference: str = denied_reference,
                    key: str = key,
                ) -> dict[str, object]:
                    return self.service.close(
                        actor_authority=actor_authority,
                        session_authority=denied_authority,
                        reference=denied_reference,
                        expected_epoch=self.service.service_epoch,
                        idempotency_key=key,
                        expected_control_revision=expected_control_revision,
                    )

                first = self._error_signature(denied)
                replay = self._error_signature(denied)
                changed = self._error_code(lambda: denied(2))

                self.assertEqual(first[0], "authority_denied")
                self.assertEqual(replay, first)
                self.assertEqual(changed, "idempotency_conflict")
                record = next(
                    record
                    for scope, record in self.service._idempotency.items()
                    if scope[-1] == key
                )
                if name == "unknown_authority":
                    self.assertIsNone(record.session_id)
                else:
                    self.assertEqual(record.session_id, snapshot["session_id"])

    def test_unrelated_close_denial_releases_key_at_failure_ttl(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        key = "expiring-denied-close"

        def denied() -> dict[str, object]:
            return self.service.close(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority="unknown-session-authority-0000001",
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
                idempotency_key=key,
                expected_control_revision=1,
            )

        self.assertEqual(self._error_code(denied), "authority_denied")
        record = next(
            record
            for scope, record in self.service._idempotency.items()
            if scope[-1] == key
        )
        self.assertIsNone(record.session_id)
        self.clock.advance(1_000)

        closed = self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key=key,
            expected_control_revision=1,
        )

        self.assertEqual(closed["kind"], "session_closed")
        self.assertFalse(closed["replayed"])

    def test_close_preclaim_failures_leave_key_reusable(self) -> None:
        cases = (
            (
                "invalid_control_revision",
                lambda _created, snapshot: {
                    "session_authority": _created["session_authority"],
                    "reference": snapshot["reference"],
                    "expected_control_revision": 0,
                },
                "invalid_request",
            ),
            (
                "malformed_reference",
                lambda created, _snapshot: {
                    "session_authority": created["session_authority"],
                    "reference": "too-short",
                    "expected_control_revision": 1,
                },
                "authority_denied",
            ),
            (
                "malformed_session_authority",
                lambda _created, snapshot: {
                    "session_authority": "too-short",
                    "reference": snapshot["reference"],
                    "expected_control_revision": 1,
                },
                "authority_denied",
            ),
        )

        for name, invalid_fields, expected_error in cases:
            with self.subTest(name=name):
                clock = FakeClock()
                service = _service(
                    clock=clock,
                    token_factory=SequenceTokenFactory(f"close-preclaim-{name}"),
                )
                created = _create(
                    service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    key=f"create-{name}",
                )
                snapshot = created["snapshot"]
                assert isinstance(snapshot, dict)
                key = f"reusable-close-{name}"
                initial_records = len(service._idempotency)
                fields = invalid_fields(created, snapshot)
                invalid_session_authority = str(fields["session_authority"])
                invalid_reference = str(fields["reference"])
                invalid_control_revision = int(fields["expected_control_revision"])

                def invalid_close(
                    *,
                    service: ProductLifecycleService = service,
                    session_authority: str = invalid_session_authority,
                    reference: str = invalid_reference,
                    control_revision: int = invalid_control_revision,
                    key: str = key,
                ) -> dict[str, object]:
                    return service.close(
                        actor_authority=ALICE_ATTENDEE_AUTHORITY,
                        session_authority=session_authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key=key,
                        expected_control_revision=control_revision,
                    )

                self.assertEqual(
                    self._error_code(invalid_close),
                    expected_error,
                )
                self.assertEqual(len(service._idempotency), initial_records)
                closed = service.close(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    session_authority=str(created["session_authority"]),
                    reference=str(snapshot["reference"]),
                    expected_epoch=service.service_epoch,
                    idempotency_key=key,
                    expected_control_revision=1,
                )
                self.assertEqual(closed["kind"], "session_closed")

    def test_terminal_session_denials_follow_tombstone_retention(self) -> None:
        for terminal_mode in ("closed", "expired"):
            with self.subTest(terminal_mode=terminal_mode):
                clock = FakeClock()
                service = _service(
                    clock=clock,
                    token_factory=SequenceTokenFactory(f"terminal-{terminal_mode}"),
                )
                created = _create(
                    service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    key=f"create-{terminal_mode}",
                )
                snapshot = created["snapshot"]
                assert isinstance(snapshot, dict)
                if terminal_mode == "closed":
                    service.close(
                        actor_authority=ALICE_ATTENDEE_AUTHORITY,
                        session_authority=str(created["session_authority"]),
                        reference=str(snapshot["reference"]),
                        expected_epoch=service.service_epoch,
                        idempotency_key="terminalize-session",
                        expected_control_revision=1,
                    )
                else:
                    clock.advance(3_000)
                    self.assertEqual(service.expire_due(), 1)
                key = f"denied-after-{terminal_mode}"
                session_authority = str(created["session_authority"])
                reference = str(snapshot["reference"])

                def denied(
                    expected_control_revision: int = 1,
                    *,
                    service: ProductLifecycleService = service,
                    session_authority: str = session_authority,
                    reference: str = reference,
                    key: str = key,
                ) -> dict[str, object]:
                    return service.close(
                        actor_authority=ALICE_ATTENDEE_AUTHORITY,
                        session_authority=session_authority,
                        reference=reference,
                        expected_epoch=service.service_epoch,
                        idempotency_key=key,
                        expected_control_revision=expected_control_revision,
                    )

                first = self._error_signature(denied)
                record = next(
                    record
                    for scope, record in service._idempotency.items()
                    if scope[-1] == key
                )
                self.assertEqual(first[0], "authority_denied")
                self.assertEqual(record.session_id, snapshot["session_id"])
                clock.advance(1_001)
                self.assertEqual(self._error_signature(denied), first)
                self.assertEqual(
                    self._error_code(lambda: denied(2)),
                    "idempotency_conflict",
                )
                clock.advance(999)
                service.expire_due()
                self.assertFalse(
                    any(scope[-1] == key for scope in service._idempotency)
                )

    def test_control_revision_conflict_is_stable_and_has_no_effect(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)

        def close_with_wrong_revision() -> dict[str, object]:
            return self.service.close(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
                idempotency_key="bad-close-key",
                expected_control_revision=2,
            )

        self.assertEqual(
            self._error_code(close_with_wrong_revision),
            "control_revision_conflict",
        )
        self.assertEqual(
            self._error_code(close_with_wrong_revision),
            "control_revision_conflict",
        )
        self.assertEqual(len(self.service._sessions_by_reference), 1)
        self.assertEqual(len(self.service._tombstones_by_reference), 0)

    def test_control_conflict_changed_body_is_protected_beyond_failure_ttl(
        self,
    ) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)

        def close(expected_control_revision: int) -> dict[str, object]:
            return self.service.close(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
                idempotency_key="retained-control-conflict",
                expected_control_revision=expected_control_revision,
            )

        self.assertEqual(
            self._error_code(lambda: close(2)), "control_revision_conflict"
        )
        self.clock.advance(1_001)
        self.assertEqual(self._error_code(lambda: close(1)), "idempotency_conflict")
        self.assertEqual(len(self.service._sessions_by_reference), 1)

    def test_fresh_create_after_terminal_requires_new_grant_and_key(self) -> None:
        first = self._create()
        first_snapshot = first["snapshot"]
        assert isinstance(first_snapshot, dict)
        self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(first["session_authority"]),
            reference=str(first_snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="close-first",
            expected_control_revision=1,
        )

        second = _create(
            self.service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_B,
            key="create-second",
        )

        self.assertNotEqual(
            second["snapshot"]["reference"], first_snapshot["reference"]
        )
        self.assertEqual(
            self.service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=self.service.service_epoch,
            )["snapshot"],
            second["snapshot"],
        )
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    self.service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    key="reuse-consumed-grant",
                )
            ),
            "authority_denied",
        )

    def test_passive_reads_do_not_extend_idle_expiry(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        self.clock.advance(2_999)
        self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )
        self.service.snapshot(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(created["session_authority"]),
            reference=str(snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
        )
        self.clock.advance(1)

        current = self.service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=self.service.service_epoch,
        )

        self.assertEqual(current["kind"], "current_session_terminal")
        self.assertEqual(current["snapshot"]["lifecycle"]["state"], "expired")
        self.assertEqual(
            current["snapshot"]["lifecycle"]["terminal_reason"], "idle_expired"
        )
        self.assertEqual(current["snapshot"]["coordinates"], {"control_revision": 2})

    def test_absolute_expiry_wins_at_exact_shared_boundary(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=5_000,
            idle_ttl_ms=20_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=10,
            max_tombstones=20,
            max_idempotency_records=40,
            max_launch_grants=10,
        )
        service = _service(
            clock=self.clock,
            policy=policy,
            launch_limits={"absolute_ttl_ms": 5_000, "idle_ttl_ms": 5_000},
        )
        _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="absolute-create",
        )
        self.clock.advance(5_000)

        self.assertEqual(service.expire_due(), 1)
        current = service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=service.service_epoch,
        )
        self.assertEqual(
            current["snapshot"]["lifecycle"]["terminal_reason"],
            "absolute_expired",
        )

    def test_delayed_expiry_uses_earliest_deadline_truth(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=20_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=10,
            max_tombstones=20,
            max_idempotency_records=40,
            max_launch_grants=10,
        )
        service = _service(clock=self.clock, policy=policy)
        _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="late-expiry-create",
        )
        self.clock.advance(12_000)

        self.assertEqual(service.expire_due(), 1)
        terminal = service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(terminal["lifecycle"]["terminal_reason"], "idle_expired")
        self.assertEqual(terminal["lifecycle"]["terminal_at_ms"], 4_000)
        self.assertEqual(terminal["recovery"]["retain_until_ms"], 24_000)

    def test_first_observation_after_expiry_grace_prunes_immediately(self) -> None:
        initial_digest_count = len(self.service._used_value_digests)
        self._create()
        self.clock.advance(12_000)

        self.assertEqual(self.service.expire_due(), 1)
        self.assertEqual(self.service._sessions_by_reference, {})
        self.assertEqual(self.service._tombstones_by_reference, {})
        self.assertEqual(self.service._idempotency, {})
        self.assertEqual(len(self.service._used_value_digests), initial_digest_count)
        self.assertEqual(
            self.service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=self.service.service_epoch,
            )["kind"],
            "no_current_session",
        )

    def test_launch_lifecycle_limits_cap_policy_expiry(self) -> None:
        service = _service(
            clock=self.clock,
            launch_limits={"absolute_ttl_ms": 5_000, "idle_ttl_ms": 1_000},
        )
        created = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="grant-limits-create",
        )
        lifecycle = created["snapshot"]["lifecycle"]
        self.assertEqual(lifecycle["absolute_expires_at_ms"], 6_000)
        self.assertEqual(lifecycle["idle_expires_at_ms"], 2_000)
        self.clock.advance(1_000)
        terminal = service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=service.service_epoch,
        )["snapshot"]
        self.assertEqual(terminal["lifecycle"]["terminal_reason"], "idle_expired")

    def test_tombstone_and_operation_records_are_pruned_at_bound(self) -> None:
        initial_digest_count = len(self.service._used_value_digests)
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        close_arguments = {
            "actor_authority": ALICE_ATTENDEE_AUTHORITY,
            "session_authority": str(created["session_authority"]),
            "reference": str(snapshot["reference"]),
            "expected_epoch": self.service.service_epoch,
            "idempotency_key": "close-pruned",
            "expected_control_revision": 1,
        }
        self.service.close(**close_arguments)
        self.clock.advance(1_999)
        self.assertEqual(self.service.expire_due(), 0)
        self.assertEqual(len(self.service._tombstones_by_reference), 1)
        self.clock.advance(1)

        self.assertEqual(self.service.expire_due(), 0)
        self.assertEqual(self.service._tombstones_by_reference, {})
        self.assertEqual(len(self.service._used_value_digests), initial_digest_count)
        self.assertEqual(
            self.service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=self.service.service_epoch,
            )["kind"],
            "no_current_session",
        )
        self.assertEqual(
            self._error_code(lambda: self.service.close(**close_arguments)),
            "authority_denied",
        )

    def test_wall_clock_jumps_do_not_change_monotonic_expiry_or_order(self) -> None:
        utc_clock = FakeClock(1_000)
        monotonic_clock = FakeClock(5_000)
        service = _service(
            clock=utc_clock,
            monotonic_clock=monotonic_clock,
        )
        created = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="clock-jump-create",
        )
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        utc_clock.advance(500_000)
        monotonic_clock.advance(2_999)

        self.assertEqual(
            service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=service.service_epoch,
            )["kind"],
            "current_session",
        )
        utc_clock.advance(-400_000)
        monotonic_clock.advance(1)
        terminal = service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=service.service_epoch,
        )["snapshot"]

        self.assertEqual(terminal["lifecycle"]["terminal_at_ms"], 4_000)
        self.assertEqual(terminal["lifecycle"]["idle_expires_at_ms"], 4_000)
        self.assertGreaterEqual(
            terminal["lifecycle"]["terminal_at_ms"],
            snapshot["lifecycle"]["created_at_ms"],
        )

    def test_monotonic_clock_regression_after_observation_is_rejected(self) -> None:
        utc_clock = FakeClock(1_000)
        monotonic_clock = FakeClock(5_000)
        service = _service(clock=utc_clock, monotonic_clock=monotonic_clock)
        monotonic_clock.advance(100)
        service.current(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            expected_epoch=service.service_epoch,
        )
        monotonic_clock.advance(-1)

        with self.assertRaisesRegex(RuntimeError, "must not move backward"):
            service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=service.service_epoch,
            )

    def test_actor_and_launch_authority_expire_at_exact_boundaries(self) -> None:
        actor_clock = FakeClock()
        actor_service = _service(
            clock=actor_clock,
            actor_expires_at_ms=2_000,
        )
        actor_clock.advance(999)
        self.assertEqual(
            actor_service.current(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                expected_epoch=actor_service.service_epoch,
            )["kind"],
            "no_current_session",
        )
        actor_clock.advance(1)
        self.assertEqual(
            self._error_code(
                lambda: actor_service.current(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    expected_epoch=actor_service.service_epoch,
                )
            ),
            "authority_denied",
        )

        grant_clock = FakeClock()
        grant_service = _service(
            clock=grant_clock,
            launch_expires_at_ms=2_000,
        )
        grant_clock.advance(1_000)
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    grant_service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=ALICE_LAUNCH_A,
                    key="expired-grant-create",
                )
            ),
            "authority_denied",
        )

    def test_restart_epoch_is_checked_before_every_authority_or_state_lookup(
        self,
    ) -> None:
        old = self._create()
        old_snapshot = old["snapshot"]
        assert isinstance(old_snapshot, dict)
        replacement = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("replacement"),
        )
        self.assertNotEqual(replacement.service_epoch, self.service.service_epoch)

        mutation_attempts = (
            lambda: replacement.create(
                actor_authority="invalid-actor",
                launch_grant="invalid-grant",
                expected_epoch=self.service.service_epoch,
                idempotency_key="old-create",
            ),
            lambda: replacement.close(
                actor_authority="invalid-actor",
                session_authority="invalid-session",
                reference="invalid-reference",
                expected_epoch=self.service.service_epoch,
                idempotency_key="old-close",
                expected_control_revision=1,
            ),
        )
        read_attempts = (
            lambda: replacement.current(
                actor_authority="invalid-actor",
                expected_epoch=self.service.service_epoch,
            ),
            lambda: replacement.snapshot(
                actor_authority="invalid-actor",
                session_authority=str(old["session_authority"]),
                reference=str(old_snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
            ),
        )

        for attempt in mutation_attempts:
            self.assertEqual(self._error_code(attempt), "outcome_unknown")
        for attempt in read_attempts:
            self.assertEqual(self._error_code(attempt), "session_lost")
        self.assertEqual(replacement._sessions_by_reference, {})
        self.assertEqual(replacement._idempotency, {})

    def test_epoch_rotation_discards_rejection_before_a_fresh_key(self) -> None:
        prior_key = "rejected-before-epoch-rotation"
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    self.service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant="unknown-launch-authority-00000001",
                    key=prior_key,
                )
            ),
            "authority_denied",
        )
        replacement = _service(
            clock=self.clock,
            token_factory=SequenceTokenFactory("rotated-service"),
        )
        fresh_key = "fresh-after-epoch-rotation"

        created = _create(
            replacement,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key=fresh_key,
        )

        self.assertEqual(created["kind"], "session_created")
        self.assertFalse(created["replayed"])
        self.assertEqual(len(replacement._idempotency), 1)
        self.assertNotEqual(prior_key, fresh_key)
        self.assertEqual(next(iter(replacement._idempotency))[-1], fresh_key)

    def test_missing_epoch_is_typed_before_authority(self) -> None:
        self.assertEqual(
            self._error_code(
                lambda: self.service.current(
                    actor_authority="invalid-actor",
                    expected_epoch=None,
                )
            ),
            "expected_epoch_required",
        )
        self.assertEqual(
            self._error_code(
                lambda: self.service.create(
                    actor_authority="invalid-actor",
                    launch_grant="invalid-grant",
                    expected_epoch=None,
                    idempotency_key="missing-epoch",
                )
            ),
            "expected_epoch_required",
        )

    def test_non_ascii_epoch_is_safely_typed_before_authority(self) -> None:
        self.assertEqual(
            self._error_code(
                lambda: self.service.current(
                    actor_authority="invalid-actor",
                    expected_epoch="epoch:\u00e9vidence_is_not_ascii",
                )
            ),
            "session_lost",
        )
        self.assertEqual(
            self._error_code(
                lambda: self.service.create(
                    actor_authority="invalid-actor",
                    launch_grant="invalid-grant",
                    expected_epoch="epoch:\u00e9vidence_is_not_ascii",
                    idempotency_key="non-ascii-epoch",
                )
            ),
            "outcome_unknown",
        )

    def test_same_key_concurrent_create_allocates_once(self) -> None:
        barrier = threading.Barrier(3)

        def create() -> dict[str, object]:
            barrier.wait()
            return self._create()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create) for _ in range(2)]
            barrier.wait()
            results = [future.result() for future in futures]

        self.assertEqual(
            sorted(bool(result["replayed"]) for result in results), [False, True]
        )
        self.assertEqual(
            {result["snapshot"]["session_id"] for result in results},
            {results[0]["snapshot"]["session_id"]},
        )
        self.assertEqual(len(self.service._sessions_by_reference), 1)

    def test_same_key_concurrent_deterministic_denial_claims_one_body(self) -> None:
        barrier = threading.Barrier(3)
        grants = (
            "unknown-concurrent-launch-grant-0001",
            "unknown-concurrent-launch-grant-0002",
        )

        def create(grant: str) -> str:
            barrier.wait()
            return self._error_code(
                lambda: _create(
                    self.service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=grant,
                    key="concurrent-denied-create",
                )
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create, grant) for grant in grants]
            barrier.wait()
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            sorted(outcomes),
            ["authority_denied", "idempotency_conflict"],
        )
        sequential = [
            self._error_code(
                lambda grant=grant: _create(
                    self.service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=grant,
                    key="concurrent-denied-create",
                )
            )
            for grant in grants
        ]
        self.assertEqual(
            sorted(sequential),
            ["authority_denied", "idempotency_conflict"],
        )
        self.assertEqual(len(self.service._idempotency), 1)
        self.assertEqual(self.service._sessions_by_reference, {})

    def test_different_key_concurrent_create_has_one_current_winner(self) -> None:
        barrier = threading.Barrier(3)

        def create(grant: str, key: str) -> tuple[str, object]:
            barrier.wait()
            try:
                return "ok", _create(
                    self.service,
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    launch_grant=grant,
                    key=key,
                )
            except ProductLifecycleError as exc:
                return "error", exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create, ALICE_LAUNCH_A, "race-a"),
                executor.submit(create, ALICE_LAUNCH_B, "race-b"),
            ]
            barrier.wait()
            outcomes = [future.result() for future in futures]

        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["error", "ok"])
        self.assertIn(("error", "current_session_exists"), outcomes)
        self.assertEqual(len(self.service._sessions_by_reference), 1)
        self.assertEqual(len(self.service._current_by_actor), 1)

    def test_same_key_concurrent_close_terminalizes_once(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        barrier = threading.Barrier(3)

        def close() -> dict[str, object]:
            barrier.wait()
            return self.service.close(
                actor_authority=ALICE_ATTENDEE_AUTHORITY,
                session_authority=str(created["session_authority"]),
                reference=str(snapshot["reference"]),
                expected_epoch=self.service.service_epoch,
                idempotency_key="concurrent-close",
                expected_control_revision=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(close) for _ in range(2)]
            barrier.wait()
            results = [future.result() for future in futures]

        self.assertEqual(
            sorted(bool(result["replayed"]) for result in results), [False, True]
        )
        self.assertEqual(
            {
                result["snapshot"]["coordinates"]["control_revision"]
                for result in results
            },
            {2},
        )
        self.assertEqual(len(self.service._tombstones_by_reference), 1)

    def test_different_key_concurrent_close_has_one_terminal_winner(self) -> None:
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        barrier = threading.Barrier(3)

        def close(key: str) -> tuple[str, object]:
            barrier.wait()
            try:
                return "ok", self.service.close(
                    actor_authority=ALICE_ATTENDEE_AUTHORITY,
                    session_authority=str(created["session_authority"]),
                    reference=str(snapshot["reference"]),
                    expected_epoch=self.service.service_epoch,
                    idempotency_key=key,
                    expected_control_revision=1,
                )
            except ProductLifecycleError as exc:
                return "error", exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(close, "concurrent-close-a"),
                executor.submit(close, "concurrent-close-b"),
            ]
            barrier.wait()
            outcomes = [future.result() for future in futures]

        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["error", "ok"])
        self.assertIn(("error", "authority_denied"), outcomes)
        self.assertEqual(len(self.service._tombstones_by_reference), 1)

    def test_idempotency_namespaces_isolate_operation_and_authority_class(self) -> None:
        attendee = _create(
            self.service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="shared-idempotency-key",
        )
        presenter = _create(
            self.service,
            actor_authority=ALICE_PRESENTER_AUTHORITY,
            launch_grant=ALICE_PRESENTER_LAUNCH,
            key="shared-idempotency-key",
        )
        attendee_snapshot = attendee["snapshot"]
        assert isinstance(attendee_snapshot, dict)

        closed = self.service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(attendee["session_authority"]),
            reference=str(attendee_snapshot["reference"]),
            expected_epoch=self.service.service_epoch,
            idempotency_key="shared-idempotency-key",
            expected_control_revision=1,
        )

        self.assertEqual(closed["kind"], "session_closed")
        self.assertNotEqual(
            attendee["snapshot"]["session_id"],
            presenter["snapshot"]["session_id"],
        )

    def test_deterministic_count_bounds_reject_without_evicting_truth(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=1,
            max_tombstones=1,
            max_idempotency_records=2,
            max_launch_grants=5,
        )
        service = _service(clock=self.clock, policy=policy)
        alice = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="bounded-alice",
        )

        self.assertEqual(
            self._error_code(
                lambda: _create(
                    service,
                    actor_authority=BOB_ATTENDEE_AUTHORITY,
                    launch_grant=BOB_LAUNCH_A,
                    key="bounded-bob",
                )
            ),
            "capacity_exhausted",
        )
        self.assertEqual(len(service._sessions_by_reference), 1)
        alice_snapshot = alice["snapshot"]
        assert isinstance(alice_snapshot, dict)
        service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(alice["session_authority"]),
            reference=str(alice_snapshot["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="bounded-close",
            expected_control_revision=1,
        )
        self.assertEqual(len(service._tombstones_by_reference), 1)
        self.assertEqual(len(service._idempotency), 2)
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    service,
                    actor_authority=BOB_ATTENDEE_AUTHORITY,
                    launch_grant=BOB_LAUNCH_A,
                    key="bounded-bob-before-prune",
                )
            ),
            "capacity_exhausted",
        )
        self.clock.advance(2_000)
        service.expire_due()
        bob = _create(
            service,
            actor_authority=BOB_ATTENDEE_AUTHORITY,
            launch_grant=BOB_LAUNCH_A,
            key="bounded-bob-after-prune",
        )
        self.assertEqual(bob["kind"], "session_created")
        self.assertEqual(len(service._sessions_by_reference), 1)

    def test_capacity_rejection_is_unclaimed_and_same_request_can_retry(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=1,
            max_tombstones=2,
            max_idempotency_records=4,
            max_launch_grants=5,
        )
        service = _service(clock=self.clock, policy=policy)
        alice = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="capacity-alice",
        )
        key = "capacity-bob-retry"

        def create_bob() -> dict[str, object]:
            return _create(
                service,
                actor_authority=BOB_ATTENDEE_AUTHORITY,
                launch_grant=BOB_LAUNCH_A,
                key=key,
            )

        with self.assertRaises(ProductLifecycleError) as captured:
            create_bob()
        self.assertEqual(captured.exception.code, "capacity_exhausted")
        self.assertEqual(captured.exception.retry, "after_delay")
        self.assertFalse(any(scope[-1] == key for scope in service._idempotency))
        alice_snapshot = alice["snapshot"]
        assert isinstance(alice_snapshot, dict)
        service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(alice["session_authority"]),
            reference=str(alice_snapshot["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="capacity-close-alice",
            expected_control_revision=1,
        )

        bob = create_bob()

        self.assertEqual(bob["kind"], "session_created")
        self.assertFalse(bob["replayed"])

    def test_capacity_rejection_does_not_bind_a_changed_body(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=1,
            max_tombstones=2,
            max_idempotency_records=6,
            max_launch_grants=5,
        )
        service = _service(clock=self.clock, policy=policy)
        _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="capacity-changed-alice",
        )
        key = "capacity-changed-bob"

        self.assertEqual(
            self._error_code(
                lambda: _create(
                    service,
                    actor_authority=BOB_ATTENDEE_AUTHORITY,
                    launch_grant=BOB_LAUNCH_A,
                    key=key,
                )
            ),
            "capacity_exhausted",
        )
        self.assertFalse(any(scope[-1] == key for scope in service._idempotency))
        self.assertEqual(
            self._error_code(
                lambda: _create(
                    service,
                    actor_authority=BOB_ATTENDEE_AUTHORITY,
                    launch_grant="unknown-bob-launch-authority-000001",
                    key=key,
                )
            ),
            "authority_denied",
        )
        self.assertTrue(any(scope[-1] == key for scope in service._idempotency))

    def test_close_capacity_rejection_leaves_key_and_body_unclaimed(self) -> None:
        policy = LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=1,
            max_tombstones=1,
            max_idempotency_records=2,
            max_launch_grants=5,
        )
        service = _service(clock=self.clock, policy=policy)
        alice = _create(
            service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="close-capacity-alice-create",
        )
        alice_snapshot = alice["snapshot"]
        assert isinstance(alice_snapshot, dict)
        service.close(
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            session_authority=str(alice["session_authority"]),
            reference=str(alice_snapshot["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key="close-capacity-alice-close",
            expected_control_revision=1,
        )
        key = "close-capacity-reusable"

        self.assertEqual(
            self._error_code(
                lambda: service.close(
                    actor_authority=BOB_ATTENDEE_AUTHORITY,
                    session_authority="unknown-session-authority-0000001",
                    reference="unknown-session-reference-0000001",
                    expected_epoch=service.service_epoch,
                    idempotency_key=key,
                    expected_control_revision=1,
                )
            ),
            "capacity_exhausted",
        )
        self.assertFalse(any(scope[-1] == key for scope in service._idempotency))

        self.clock.advance(2_000)
        service.expire_due()
        bob = _create(
            service,
            actor_authority=BOB_ATTENDEE_AUTHORITY,
            launch_grant=BOB_LAUNCH_A,
            key="close-capacity-bob-create",
        )
        bob_snapshot = bob["snapshot"]
        assert isinstance(bob_snapshot, dict)
        closed = service.close(
            actor_authority=BOB_ATTENDEE_AUTHORITY,
            session_authority=str(bob["session_authority"]),
            reference=str(bob_snapshot["reference"]),
            expected_epoch=service.service_epoch,
            idempotency_key=key,
            expected_control_revision=1,
        )

        self.assertEqual(closed["kind"], "session_closed")
        self.assertFalse(closed["replayed"])

    def test_seed_validation_rejects_unsafe_or_unbounded_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON values"):
            ProductLifecycleService(
                revisions=[
                    ImmutableRevisionSeed(
                        revision_id="bad-revision",
                        document={"not_finite": float("nan")},
                    )
                ],
                actors=[],
                launch_grants=[],
            )
        with self.assertRaisesRegex(ValueError, "only session.create"):
            _service(
                clock=self.clock,
                launch_operation=frozenset({ProductOperation.CLOSE}),
            )
        with self.assertRaisesRegex(ValueError, "launch grant seeds exceed"):
            _service(
                clock=self.clock,
                policy=LifecyclePolicy(
                    max_active_sessions=1,
                    max_tombstones=1,
                    max_idempotency_records=2,
                    max_launch_grants=1,
                ),
            )
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            ProductLifecycleService(
                revisions=[
                    ImmutableRevisionSeed(
                        revision_id="bad-object-key",
                        document={"nested": {1: "not-json-object-key"}},
                    )
                ],
                actors=[],
                launch_grants=[],
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            _service(
                clock=self.clock,
                launch_limits={"absolute_ttl_ms": 1_000, "idle_ttl_ms": 2_000},
            )
        with self.assertRaisesRegex(ValueError, "policy_id is invalid"):
            ProductLifecycleService(
                revisions=[
                    ImmutableRevisionSeed(
                        revision_id="safe-revision",
                        document={"safe": True},
                    )
                ],
                actors=[
                    ActorAuthoritySeed(
                        token="safe-actor-authority-0001",
                        actor_id="safe-actor",
                        expires_at_ms=50_000,
                    )
                ],
                launch_grants=[
                    LaunchGrantSeed(
                        token="safe-launch-authority-0001",
                        selected_record_id="safe-selection",
                        actor_id="safe-actor",
                        revision_id="safe-revision",
                        expires_at_ms=40_000,
                        policy_id="policy:\u0085unsafe",
                    )
                ],
                clock_ms=self.clock,
                token_factory=SequenceTokenFactory("safe-label"),
            )
        for generated_value in ("unsafe-token\nvalue", "x" * 513):
            with self.subTest(generated_value_length=len(generated_value)):
                with self.assertRaisesRegex(ValueError, "bounded visible-ASCII"):
                    _service(
                        clock=self.clock,
                        token_factory=lambda _purpose, value=generated_value: value,
                    )

    def test_seed_iterables_are_consumed_only_to_capacity_plus_one(self) -> None:
        policy = LifecyclePolicy(
            max_active_sessions=1,
            max_tombstones=1,
            max_idempotency_records=2,
            max_launch_grants=1,
            max_actor_authorities=1,
            max_revisions=1,
        )
        revision_yields: list[int] = []

        def revisions():
            for index in range(100):
                revision_yields.append(index)
                yield ImmutableRevisionSeed(
                    revision_id=f"revision-{index}",
                    document={"index": index},
                )

        with self.assertRaisesRegex(ValueError, "revision seeds exceed"):
            ProductLifecycleService(
                revisions=revisions(),
                actors=[],
                launch_grants=[],
                policy=policy,
            )
        self.assertEqual(revision_yields, [0, 1])

        actor_yields: list[int] = []

        def actors():
            for index in range(100):
                actor_yields.append(index)
                yield ActorAuthoritySeed(
                    token=f"actor-authority-{index:04d}-opaque",
                    actor_id=f"actor-{index}",
                    expires_at_ms=50_000,
                )

        with self.assertRaisesRegex(ValueError, "actor authority seeds exceed"):
            ProductLifecycleService(
                revisions=[
                    ImmutableRevisionSeed(
                        revision_id="revision-only",
                        document={"safe": True},
                    )
                ],
                actors=actors(),
                launch_grants=[],
                policy=policy,
            )
        self.assertEqual(actor_yields, [0, 1])

    def test_errors_and_seed_repr_do_not_expose_credentials(self) -> None:
        seed = ActorAuthoritySeed(
            token=ALICE_ATTENDEE_AUTHORITY,
            actor_id="alice",
            role=ProductRole.ATTENDEE,
            expires_at_ms=50_000,
        )
        with self.assertRaises(ProductLifecycleError) as captured:
            self.service.current(
                actor_authority="not-a-real-secret-authority",
                expected_epoch=self.service.service_epoch,
            )

        problem = captured.exception.to_problem(request_id="request:valid_0001")
        serialized = json.dumps(problem)
        self.assertNotIn("not-a-real-secret-authority", serialized)
        self.assertNotIn(ALICE_ATTENDEE_AUTHORITY, repr(seed))
        self.assertEqual(problem["error"]["code"], "authority_denied")
        self.assertEqual(problem["error"]["request_id"], "request:valid_0001")
        short = captured.exception.to_problem(request_id="request:1")
        self.assertEqual(short["error"]["request_id"], "request:product-lifecycle")
        hostile = captured.exception.to_problem(request_id="request:bad\nsecret")
        self.assertEqual(hostile["error"]["request_id"], "request:product-lifecycle")

        revision_seed = ImmutableRevisionSeed(
            revision_id="secret-revision",
            document={"world_state": "raw-revision-secret"},
        )
        self.assertNotIn("raw-revision-secret", repr(revision_seed))
        created = self._create()
        snapshot = created["snapshot"]
        assert isinstance(snapshot, dict)
        session = self.service._sessions_by_reference[str(snapshot["reference"])]
        idempotency_record = next(iter(self.service._idempotency.values()))
        self.assertNotIn("internal-profile-secret", repr(session))
        self.assertNotIn(str(created["session_authority"]), repr(session))
        self.assertNotIn(str(created["session_authority"]), repr(idempotency_record))
        self.assertNotIn("internal-profile-secret", repr(self.service._revisions))

        with self.assertRaisesRegex(ValueError, "message is invalid"):
            ProductLifecycleError(
                code="unsafe_message",
                message="unsafe\nmessage",
                status=400,
            )
        with self.assertRaisesRegex(ValueError, "message is invalid"):
            ProductLifecycleError(
                code="oversized_message",
                message="x" * 161,
                status=400,
            )
        with self.assertRaisesRegex(ValueError, "message is invalid"):
            ProductLifecycleError(
                code="c1_message",
                message="unsafe\u0085message",
                status=400,
            )

        contextual = ProductLifecycleError(
            code="selection_conflict",
            message="Another selection is current.",
            status=409,
            context={"label": "A", "localized_label": "München"},
        ).to_problem()
        self.assertEqual(
            contextual["error"]["context"],
            {"label": "A", "localized_label": "München"},
        )
        for invalid_context in (
            {"label": ""},
            {"label": "unsafe\nvalue"},
            {"label": "x" * 513},
            {"Invalid-Key": "value"},
        ):
            with self.assertRaisesRegex(ValueError, "context is invalid"):
                ProductLifecycleError(
                    code="selection_conflict",
                    message="Another selection is current.",
                    status=409,
                    context=invalid_context,
                )

    def test_module_has_no_legacy_runtime_imports(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "dreamland"
            / "product"
            / "lifecycle.py"
        )
        source = path.read_text(encoding="utf-8")

        self.assertNotIn("from dreamland", source)
        self.assertNotIn("import dreamland", source)

    def _create(self) -> dict[str, object]:
        return _create(
            self.service,
            actor_authority=ALICE_ATTENDEE_AUTHORITY,
            launch_grant=ALICE_LAUNCH_A,
            key="create-key",
        )

    def _error_code(self, operation) -> str:
        with self.assertRaises(ProductLifecycleError) as captured:
            operation()
        return captured.exception.code

    def _error_signature(
        self,
        operation,
    ) -> tuple[str, str, int, str, int | None]:
        with self.assertRaises(ProductLifecycleError) as captured:
            operation()
        error = captured.exception
        return (
            error.code,
            error.message,
            error.status,
            error.retry,
            error.retry_after_ms,
        )


def _service(
    *,
    clock: FakeClock,
    policy: LifecyclePolicy | None = None,
    token_factory: Callable[[str], str] | None = None,
    monotonic_clock: FakeClock | None = None,
    revision_a_document: dict[str, object] | None = None,
    launch_operation: frozenset[ProductOperation] | None = None,
    launch_limits: dict[str, int] | None = None,
    actor_expires_at_ms: int = 50_000,
    launch_expires_at_ms: int = 40_000,
) -> ProductLifecycleService:
    return ProductLifecycleService(
        revisions=[
            ImmutableRevisionSeed(
                revision_id="revision-a",
                document=revision_a_document or _revision_a_document(),
            ),
            ImmutableRevisionSeed(
                revision_id="revision-b",
                document={
                    "world_state": {
                        "name": "Revision B",
                        "hosts": {"entry": {"hostname": "revision-b-host"}},
                    }
                },
            ),
            ImmutableRevisionSeed(
                revision_id="revision-c",
                document={"world_state": {"name": "Revision C"}},
            ),
        ],
        actors=[
            ActorAuthoritySeed(
                token=ALICE_ATTENDEE_AUTHORITY,
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                expires_at_ms=actor_expires_at_ms,
            ),
            ActorAuthoritySeed(
                token=ALICE_PRESENTER_AUTHORITY,
                actor_id="alice",
                role=ProductRole.PRESENTER,
                expires_at_ms=actor_expires_at_ms,
            ),
            ActorAuthoritySeed(
                token=BOB_ATTENDEE_AUTHORITY,
                actor_id="bob",
                role=ProductRole.ATTENDEE,
                expires_at_ms=actor_expires_at_ms,
            ),
        ],
        launch_grants=[
            _launch_seed(
                token=ALICE_LAUNCH_A,
                selected_record_id="selected-record-a",
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                revision_id="revision-a",
                operations=launch_operation,
                limits=launch_limits,
                expires_at_ms=launch_expires_at_ms,
            ),
            _launch_seed(
                token=ALICE_LAUNCH_B,
                selected_record_id="selected-record-b",
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                revision_id="revision-b",
                limits=launch_limits,
                expires_at_ms=launch_expires_at_ms,
            ),
            _launch_seed(
                token=ALICE_LAUNCH_C,
                selected_record_id="selected-record-c",
                actor_id="alice",
                role=ProductRole.ATTENDEE,
                revision_id="revision-c",
                limits=launch_limits,
                expires_at_ms=launch_expires_at_ms,
            ),
            _launch_seed(
                token=ALICE_PRESENTER_LAUNCH,
                selected_record_id="selected-stage-b",
                actor_id="alice",
                role=ProductRole.PRESENTER,
                revision_id="revision-b",
                limits=launch_limits,
                expires_at_ms=launch_expires_at_ms,
            ),
            _launch_seed(
                token=BOB_LAUNCH_A,
                selected_record_id="selected-bob-a",
                actor_id="bob",
                role=ProductRole.ATTENDEE,
                revision_id="revision-a",
                limits=launch_limits,
                expires_at_ms=launch_expires_at_ms,
            ),
        ],
        policy=policy
        or LifecyclePolicy(
            absolute_ttl_ms=10_000,
            idle_ttl_ms=3_000,
            tombstone_ttl_ms=2_000,
            failed_operation_ttl_ms=1_000,
            max_active_sessions=10,
            max_tombstones=20,
            max_idempotency_records=40,
            max_launch_grants=10,
        ),
        clock_ms=clock,
        monotonic_ms=monotonic_clock,
        token_factory=token_factory or SequenceTokenFactory(),
    )


def _launch_seed(
    *,
    token: str,
    selected_record_id: str,
    actor_id: str,
    role: ProductRole,
    revision_id: str,
    operations: frozenset[ProductOperation] | None = None,
    limits: dict[str, int] | None = None,
    expires_at_ms: int = 40_000,
) -> LaunchGrantSeed:
    return LaunchGrantSeed(
        token=token,
        selected_record_id=selected_record_id,
        actor_id=actor_id,
        role=role,
        revision_id=revision_id,
        expires_at_ms=expires_at_ms,
        allowed_operations=operations
        if operations is not None
        else frozenset({ProductOperation.CREATE}),
        limits=limits
        if limits is not None
        else {"absolute_ttl_ms": 10_000, "idle_ttl_ms": 3_000},
        policy_id="policy-a",
        notice_version="notice-2026-08",
    )


def _revision_a_document() -> dict[str, object]:
    return {
        "world_state": {
            "name": "Revision A",
            "hosts": {
                "entry": {
                    "hostname": "revision-a-host",
                    "secret": "internal-profile-secret",
                    "path": "/private/revision/path",
                }
            },
        }
    }


def _create(
    service: ProductLifecycleService,
    *,
    actor_authority: str,
    launch_grant: str,
    key: str,
) -> dict[str, object]:
    return service.create(
        actor_authority=actor_authority,
        launch_grant=launch_grant,
        expected_epoch=service.service_epoch,
        idempotency_key=key,
    )


if __name__ == "__main__":
    unittest.main()
