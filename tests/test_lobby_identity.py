from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from dreamland.agent_tracing import (
    graph_invoke_config,
    record_langfuse_session_end,
    record_langfuse_session_start,
    session_start_trace_metadata,
)
from dreamland.backend import AgentBackend
from dreamland.background_agents.enrichment.graph import EnrichmentJobContext
from dreamland.background_agents.enrichment.role_outputs import (
    enrichment_invoke_config,
    enrichment_trace_metadata,
)
from dreamland.lobby_identity import (
    PUBLIC_ID_ALPHABET,
    PublicSimulationRegistry,
    canonical_public_id,
    generate_public_id,
    is_public_reference,
    public_trace_metadata,
)
from dreamland.models import CommandStateUpdate, EnvironmentMetadata, SessionState
from dreamland.profile import server_profile
from dreamland.session_state import apply_command_state_update
from dreamland.spiritguide import SpiritguideContext, _spiritguide_trace_metadata
from dreamland.state_profiles import (
    apply_state_profile_to_session,
    create_state_profile,
    state_profile_to_session,
)

FIRST_ID = "DL-7M2K-P9XR-W4TZ-C6HN"
SECOND_ID = "DL-2M2K-P9XR-W4TZ-C6HN"


class LobbyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "visits.sqlite3"
        self.now = datetime(2026, 9, 11, tzinfo=UTC)
        self.registry = PublicSimulationRegistry(self.path, clock=lambda: self.now)

    def allocate(self, registry=None):
        return (registry or self.registry).allocate(
            "offshore",
            parent_connection_id="parent-1",
            notice_version="notice-test-v1",
            accepted_at=self.now.isoformat(),
        )

    def bound_session(self):
        record = self.allocate()
        session = SessionState(
            username="same-username", world_state=server_profile().world_state
        )
        self.registry.bind(record.public_id, session)
        return record.public_id, session

    def test_generator_and_complete_case_normalization(self):
        self.assertEqual(len(PUBLIC_ID_ALPHABET), 32)
        for _ in range(32):
            generated = generate_public_id()
            self.assertEqual(generated, canonical_public_id(generated.lower()))
        self.assertEqual(canonical_public_id(f" {FIRST_ID.lower()} "), FIRST_ID)
        for value in (
            "DL",
            "dl-7m2k",
            FIRST_ID[:-1],
            FIRST_ID + "Z",
            "DL-OOOO-OOOO-OOOO-OOOO",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_public_reference(value))
                with self.assertRaises(ValueError):
                    self.registry.get(value)
        self.assertFalse(is_public_reference("abcd1234"))

    def test_forced_collision_retries_without_overwriting(self):
        self.registry._id_factory = iter((FIRST_ID, FIRST_ID, SECOND_ID)).__next__
        first = self.allocate()
        second = self.allocate()
        self.assertEqual(first.public_id, FIRST_ID)
        self.assertEqual(second.public_id, SECOND_ID)
        self.assertEqual(self.registry.get(FIRST_ID), first)

    def test_persistent_collision_has_bounded_failure(self):
        self.registry._id_factory = lambda: FIRST_ID
        self.allocate()
        with self.assertRaisesRegex(RuntimeError, "unique"):
            self.allocate()
        self.assertIsNotNone(self.registry.get(FIRST_ID))

    def test_simultaneous_allocation_and_binding_stay_distinct(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.bound_session(), range(32)))
        self.assertEqual(len({public_id for public_id, _ in results}), 32)
        self.assertEqual(len({session.langfuse_trace_id for _, session in results}), 32)
        for public_id, session in results:
            record = self.registry.get(public_id)
            self.assertEqual(record.session_id, session.langfuse_trace_id)
            self.assertEqual(record.status, "live")
            self.assertEqual(
                self.registry.get_by_session_id(session.langfuse_trace_id), record
            )

    def test_session_or_reference_cannot_be_rebound(self):
        public_id, session = self.bound_session()
        other = self.allocate()
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.registry.bind(other.public_id, session)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.registry.bind(public_id, SessionState(username="other"))
        with self.assertRaisesRegex(ValueError, "binding"):
            self.registry.bind(
                other.public_id,
                SessionState(
                    username="other", langfuse_trace_id=session.langfuse_trace_id
                ),
            )
        self.assertIsNone(self.registry.get(other.public_id).session_id)

    def test_ended_reference_is_retained_and_end_is_idempotent(self):
        public_id, session = self.bound_session()
        ended = self.registry.end(public_id, "world_exit")
        self.now += timedelta(hours=1)
        self.assertEqual(self.registry.end(public_id, "transport_failure"), ended)
        self.assertEqual(ended.status, "ended")
        self.assertEqual(ended.session_id, session.langfuse_trace_id)
        self.assertEqual(ended.end_reason, "world_exit")
        self.assertEqual(self.registry.get(public_id.lower()), ended)

    def test_restart_marks_unfinished_records_interrupted_without_resurrection(self):
        public_id, session = self.bound_session()
        allocated = self.allocate()
        restored = PublicSimulationRegistry(self.path, clock=lambda: self.now)
        for reference in (public_id, allocated.public_id):
            record = restored.get(reference)
            self.assertEqual(record.status, "interrupted")
            self.assertEqual(record.end_reason, "process_restart")
            with self.assertRaises(ValueError):
                restored.bind(reference, SessionState(username="other"))
        self.assertEqual(restored.get(public_id).session_id, session.langfuse_trace_id)

    def test_expired_lookup_and_explicit_cleanup(self):
        ended_id, ended_session = self.bound_session()
        live_id, _ = self.bound_session()
        self.registry.end(ended_id, "world_exit")
        self.now += timedelta(days=31)
        self.assertIsNone(self.registry.get(ended_id))
        self.assertIsNone(
            self.registry.get_by_session_id(ended_session.langfuse_trace_id)
        )
        self.assertEqual(self.registry.get(live_id).status, "live")
        self.assertEqual(self.registry.cleanup(), 1)
        self.assertEqual(self.registry.cleanup(), 0)

    def test_failed_start_records_reference_without_success(self):
        allocated = self.allocate()
        self.assertEqual(
            self.registry.end(allocated.public_id, "failed_start").status, "failed"
        )
        bound_id, _ = self.bound_session()
        self.assertEqual(self.registry.end(bound_id, "failed_start").status, "failed")

    def test_unknown_reference_is_unavailable(self):
        self.assertIsNone(self.registry.get(FIRST_ID))
        self.assertIsNone(self.registry.get_by_session_id("no-such-session"))

    def test_private_storage_permissions_and_symlink_denial(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.with_name("link.sqlite3")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            PublicSimulationRegistry(link)
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            PublicSimulationRegistry(self.path)

    def test_model_and_profile_identity_cannot_change_runtime_binding(self):
        public_id, session = self.bound_session()
        original = session.runtime_provenance
        session.environment_metadata = EnvironmentMetadata(
            metadata={
                "dreamlandPublicId": SECOND_ID,
                "dreamland.lobby.public_id": SECOND_ID,
            }
        )
        apply_command_state_update(
            session,
            CommandStateUpdate(
                world_state={
                    "dreamlandPublicId": SECOND_ID,
                    "runtime_provenance": {"public_id": SECOND_ID},
                }
            ),
        )
        profile = create_state_profile(session, name="spoofed", level="exact")
        self.assertNotIn("runtime_provenance", profile.state)
        self.assertNotIn(
            "dreamlandPublicId", profile.state["environment_metadata"]["metadata"]
        )
        profile.state["runtime_provenance"] = {"public_id": SECOND_ID}
        profile.state["environment_metadata"]["metadata"]["dreamlandPublicId"] = (
            SECOND_ID
        )
        booted = state_profile_to_session(profile, current_session_id="fresh")
        self.assertIsNone(booted.runtime_provenance)
        self.assertEqual(public_trace_metadata(booted), {})
        self.assertNotIn("dreamlandPublicId", booted.environment_metadata.metadata)
        apply_state_profile_to_session(session, profile)
        self.assertIs(session.runtime_provenance, original)
        self.assertEqual(
            public_trace_metadata(session), {"dreamlandPublicId": public_id}
        )
        with self.assertRaises(FrozenInstanceError):
            original.public_id = SECOND_ID
        self.assertEqual(
            self.registry.get(public_id).session_id, session.langfuse_trace_id
        )

    def test_command_and_spiritguide_contexts_capture_originating_identity(self):
        public_id, session = self.bound_session()
        backend = AgentBackend(server_profile())
        command = backend._context_from_session(session, "uname -a")
        guidance = SpiritguideContext(
            instruction="add a host",
            session_id=session.langfuse_trace_id,
            session=session,
        )
        session.runtime_provenance = replace(
            session.runtime_provenance, public_id=SECOND_ID
        )
        self.assertEqual(
            graph_invoke_config((), command)["metadata"]["dreamlandPublicId"], public_id
        )
        self.assertEqual(
            _spiritguide_trace_metadata(guidance)["dreamlandPublicId"], public_id
        )
        self.assertEqual(
            graph_invoke_config((), command)["configurable"]["thread_id"],
            session.langfuse_trace_id,
        )

    def test_enrichment_parent_and_graph_metadata_use_captured_provenance(self):
        public_id, session = self.bound_session()
        context = EnrichmentJobContext(
            session_id=session.langfuse_trace_id,
            target_host="ubuntu",
            trigger_id="manual",
            event_id="event-test",
            runtime_provenance=session.runtime_provenance,
        )
        self.registry.end(public_id, "world_exit")
        session.runtime_provenance = None
        metadata = enrichment_trace_metadata(context, graph_version="test")
        self.assertEqual(metadata["dreamlandPublicId"], public_id)
        self.assertEqual(metadata["langfuse_session_id"], context.session_id)
        config = enrichment_invoke_config(context, graph_version="test", callbacks=())
        self.assertEqual(config["metadata"]["dreamlandPublicId"], public_id)

    def test_session_start_end_events_propagate_scalar_metadata(self):
        public_id, session = self.bound_session()
        client = Mock()
        with (
            patch(
                "dreamland.agent_tracing.initialize_langfuse_client",
                return_value=client,
            ),
            patch("langfuse.propagate_attributes") as propagate,
        ):
            options = dict(
                enabled=True,
                public_key="pk-test",
                secret_key="sk-test",
                host=None,
                session=session,
            )
            record_langfuse_session_start(**options)
            record_langfuse_session_end(**options, reason="world_exit")
        self.assertEqual(client.create_event.call_count, 2)
        for call in client.create_event.call_args_list:
            self.assertEqual(call.kwargs["metadata"]["dreamlandPublicId"], public_id)
            self.assertEqual(
                call.kwargs["trace_context"]["trace_id"], session.langfuse_trace_id
            )
        for call in propagate.call_args_list:
            self.assertEqual(call.kwargs["metadata"]["dreamlandPublicId"], public_id)
        self.assertEqual(
            client.create_event.call_args.kwargs["name"], "dreamland-session-end"
        )
        self.assertEqual(
            client.create_event.call_args.kwargs["metadata"][
                "dreamland.session.end_reason"
            ],
            "world_exit",
        )

    def test_disabled_and_unavailable_tracing_do_not_affect_identity(self):
        public_id, session = self.bound_session()
        options = dict(
            public_key=None,
            secret_key=None,
            host=None,
            session=session,
        )
        with patch("dreamland.agent_tracing.initialize_langfuse_client") as initialize:
            record_langfuse_session_start(enabled=False, **options)
            initialize.assert_not_called()
        with self.assertLogs("dreamland.failures", level="WARNING"):
            record_langfuse_session_end(enabled=True, **options, reason="world_exit")
        self.assertEqual(
            self.registry.get(public_id).session_id, session.langfuse_trace_id
        )
        self.assertEqual(
            session_start_trace_metadata(session)["dreamlandPublicId"],
            public_id,
        )


if __name__ == "__main__":
    unittest.main()
