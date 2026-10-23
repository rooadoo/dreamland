from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

from dreamland.product.journal import (
    CursorDomain,
    EventCursor,
    JournalLimits,
    JournalSnapshotRequired,
    ProductEventCriticality,
    ProductSessionJournal,
    SnapshotRequiredReason,
    TranscriptClip,
    TranscriptCursor,
    TranscriptKind,
    project_inert_text,
)


class DeterministicIds:
    def __init__(self, namespace: str = "journal") -> None:
        self.namespace = namespace
        self.count = 0

    def __call__(self, purpose: str) -> str:
        self.count += 1
        return f"{self.namespace}_{purpose}_{self.count:04d}"


class SequenceClock:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.calls = 0

    def __call__(self) -> int:
        value = self.values[self.calls]
        self.calls += 1
        return value


class ProductEventJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = _journal()

    def test_events_have_stable_ids_monotonic_order_and_complete_replay(self) -> None:
        initial = self.journal.event_position
        first = self.journal.append_event(
            "command.accepted",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            data={"slot_generation": 1},
            command_id="command:one",
        )
        second = self.journal.append_event(
            "command.output",
            criticality=ProductEventCriticality.INFORMATIONAL,
            data={"channel": "stdout", "chunk_index": 0},
            command_id="command:one",
        )
        third = self.journal.append_event(
            "command.completed",
            criticality=ProductEventCriticality.AUTHORITATIVE,
            data={"exit_status": 0},
            command_id="command:one",
        )

        self.assertEqual(initial.sequence, 0)
        self.assertIsNone(initial.event_id)
        self.assertEqual([first.sequence, second.sequence, third.sequence], [1, 2, 3])
        self.assertEqual(len({first.event_id, second.event_id, third.event_id}), 3)
        self.assertTrue(first.event_id.endswith(":1"))
        self.assertEqual(first.stream_id, third.stream_id)
        self.assertEqual(first.emitted_at_ms, 12_345)

        replay = self.journal.replay_events(first.cursor)
        self.assertEqual(replay.events, (second, third))
        self.assertEqual(replay.position, third.cursor)
        self.assertEqual(replay.retention_floor.sequence, 0)
        self.assertEqual(self.journal.replay_events(third.cursor).events, ())

    def test_event_count_eviction_advances_replay_floor_without_reusing_ids(
        self,
    ) -> None:
        journal = _journal(limits=replace(_limits(), max_events=2))
        first = _event(journal, 1)
        second = _event(journal, 2)
        third = _event(journal, 3)

        snapshot = journal.event_snapshot()
        self.assertEqual(snapshot.events, (second, third))
        self.assertEqual(snapshot.retention_floor, first.cursor)
        self.assertEqual(snapshot.position, third.cursor)

        with self.assertRaises(JournalSnapshotRequired) as raised:
            journal.replay_events(EventCursor(journal.stream_id, 0, None))
        self.assertEqual(raised.exception.domain, CursorDomain.EVENT)
        self.assertEqual(
            raised.exception.reason,
            SnapshotRequiredReason.BEFORE_RETENTION_FLOOR,
        )
        self.assertEqual(raised.exception.retention_floor, first.cursor)
        self.assertEqual(journal.replay_events(first.cursor).events, (second, third))

        fourth = _event(journal, 4)
        self.assertEqual(fourth.sequence, 4)
        self.assertNotIn(
            first.event_id,
            {event.event_id for event in journal.event_snapshot().events},
        )

    def test_event_payload_bytes_are_bounded_during_ingestion(self) -> None:
        journal = _journal(
            limits=replace(
                _limits(),
                max_events=10,
                max_event_bytes=30,
                max_event_payload_bytes=30,
            )
        )
        first = journal.append_event(
            "safe.fact",
            criticality=ProductEventCriticality.INFORMATIONAL,
            data={"value": "1234567890"},
        )
        second = journal.append_event(
            "safe.fact",
            criticality=ProductEventCriticality.INFORMATIONAL,
            data={"value": "abcdefghij"},
        )

        snapshot = journal.event_snapshot()
        self.assertEqual(snapshot.events, (second,))
        self.assertEqual(snapshot.retention_floor, first.cursor)
        self.assertLessEqual(snapshot.retained_payload_bytes, 30)

    def test_wrong_mismatched_and_future_event_cursors_require_snapshot(
        self,
    ) -> None:
        event = _event(self.journal, 1)
        another = _journal(namespace="another")
        other_event = _event(another, 1)
        mismatched = EventCursor(
            self.journal.stream_id,
            1,
            event.event_id.removesuffix(":1") + ":99",
        )
        future = EventCursor(
            self.journal.stream_id,
            99,
            event.event_id.removesuffix(":1") + ":99",
        )

        cases = (
            (other_event.cursor, SnapshotRequiredReason.WRONG_STREAM),
            (mismatched, SnapshotRequiredReason.POSITION_MISMATCH),
            (future, SnapshotRequiredReason.FUTURE_POSITION),
        )
        for cursor, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                with self.assertRaises(JournalSnapshotRequired) as raised:
                    self.journal.replay_events(cursor)
                self.assertEqual(raised.exception.reason, expected_reason)
                self.assertEqual(raised.exception.current_position, event.cursor)

    def test_payload_is_deeply_copied_immutable_and_additive(self) -> None:
        source = {
            "known": "value",
            "future_addition": [{"label": "original"}],
        }
        event = self.journal.append_event(
            "safe.additive",
            criticality=ProductEventCriticality.INFORMATIONAL,
            data=source,
        )
        before = self.journal.event_snapshot()
        source["known"] = "changed"
        source["future_addition"][0]["label"] = "changed"

        self.assertEqual(event.data["known"], "value")
        nested = event.data["future_addition"]
        assert isinstance(nested, tuple)
        nested_mapping = nested[0]
        self.assertEqual(nested_mapping["label"], "original")
        with self.assertRaises(TypeError):
            event.data["known"] = "cannot mutate"  # type: ignore[index]
        with self.assertRaises(TypeError):
            nested_mapping["label"] = "cannot mutate"  # type: ignore[index]

        allocated = event.to_dict()
        allocated_data = allocated["data"]
        assert isinstance(allocated_data, dict)
        allocated_data["known"] = "copy-only"
        self.assertEqual(event.data["known"], "value")

        _event(self.journal, 2)
        self.assertEqual(before.events, (event,))
        with self.assertRaises(FrozenInstanceError):
            before.position.sequence = 99  # type: ignore[misc]

    def test_event_inputs_reject_private_or_non_safe_payloads_before_append(
        self,
    ) -> None:
        invalid_payloads: tuple[object, ...] = (
            {"authority": "opaque"},
            {"admin": {"operation": "inspect"}},
            {"provider": {"response": "raw"}},
            {"provider_request": {"safe": "no"}},
            {"raw_state": {}},
            {"safe_message": "escape\x1b[31m"},
            {"safe_message": "reordered\u202etext"},
            {"value": float("nan")},
            {"value": 2**53},
            {"value": {1, 2}},
            {1: "non-string-key"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    self.journal.append_event(
                        "safe.event",
                        criticality=ProductEventCriticality.INFORMATIONAL,
                        data=payload,  # type: ignore[arg-type]
                    )
                self.assertEqual(self.journal.event_position.sequence, 0)

    def test_event_depth_item_and_entry_byte_limits_reject_without_a_gap(
        self,
    ) -> None:
        depth_journal = _journal(
            namespace="depth",
            limits=replace(_limits(), max_payload_depth=1),
        )
        with self.assertRaisesRegex(ValueError, "max_payload_depth"):
            _append_data(depth_journal, {"one": {"two": 2}})

        item_journal = _journal(
            namespace="items",
            limits=replace(_limits(), max_payload_items=1),
        )
        with self.assertRaisesRegex(ValueError, "max_payload_items"):
            _append_data(item_journal, {"one": 1, "two": 2})

        byte_journal = _journal(
            namespace="bytes",
            limits=replace(_limits(), max_event_bytes=10, max_event_payload_bytes=10),
        )
        with self.assertRaisesRegex(ValueError, "max_event_payload_bytes"):
            _append_data(byte_journal, {"value": "too large"})

        self.assertEqual(depth_journal.event_position.sequence, 0)
        self.assertEqual(item_journal.event_position.sequence, 0)
        self.assertEqual(byte_journal.event_position.sequence, 0)

    def test_concurrent_appends_have_one_gapless_sequence(self) -> None:
        journal = _journal(
            limits=replace(
                _limits(),
                max_events=100,
                max_event_bytes=10_000,
            )
        )

        def append(index: int) -> None:
            _event(journal, index)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append, range(100)))

        events = journal.event_snapshot().events
        self.assertEqual([event.sequence for event in events], list(range(1, 101)))
        self.assertEqual(len({event.event_id for event in events}), 100)


class ProductTranscriptJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = _journal()

    def test_all_transcript_kinds_have_independent_order_and_correlation(
        self,
    ) -> None:
        command = self.journal.append_transcript(
            TranscriptKind.COMMAND,
            "look north",
            command_id="command:one",
        )
        stdout = self.journal.append_transcript(
            TranscriptKind.STDOUT,
            b"archive\n",
            command_id="command:one",
            chunk_index=0,
            final=False,
        )
        stderr = self.journal.append_transcript(
            TranscriptKind.STDERR,
            "warning\n",
            command_id="command:one",
            chunk_index=0,
            final=True,
        )
        safe_error = self.journal.append_transcript(
            TranscriptKind.SAFE_ERROR,
            "The command was interrupted.",
            command_id="command:one",
        )
        system = self.journal.append_transcript(
            TranscriptKind.SYSTEM,
            "Session ready.",
        )

        snapshot = self.journal.transcript_snapshot()
        self.assertEqual(
            snapshot.entries,
            (command, stdout, stderr, safe_error, system),
        )
        self.assertEqual(
            [entry.position for entry in snapshot.entries], [1, 2, 3, 4, 5]
        )
        self.assertEqual(stdout.chunk_index, 0)
        self.assertFalse(stdout.final)
        self.assertEqual(system.command_id, None)
        self.assertEqual(self.journal.event_position.sequence, 0)

        event = _event(self.journal, 1)
        self.assertEqual(event.sequence, 1)
        self.assertEqual(self.journal.transcript_position.position, 5)

    def test_projection_makes_control_bidi_and_zero_width_text_inert(self) -> None:
        source = "a\r\nb\rc\x00d\x1b[31m\u202ee\u200bf"
        entry = self.journal.append_transcript(
            TranscriptKind.SYSTEM,
            source,
        )

        self.assertEqual(entry.text, "a\nb\nc\ufffdd\ufffd[31m\ufffde\ufffdf")
        self.assertTrue(entry.transformed)
        self.assertNotIn("\r", entry.text)
        self.assertNotIn("\x1b", entry.text)
        self.assertNotIn("\u202e", entry.text)
        self.assertEqual(entry.projection, "inert-text-v1")
        self.assertEqual(entry.encoding, "utf-8")

        unchanged, transformed = project_inert_text("snowman ☃\n\t")
        self.assertEqual(unchanged, "snowman ☃\n\t")
        self.assertFalse(transformed)

    def test_entry_clipping_preserves_utf8_boundaries_and_omission_sides(
        self,
    ) -> None:
        journal = _journal(
            limits=replace(
                _limits(),
                max_transcript_bytes=20,
                max_transcript_entry_bytes=5,
            )
        )
        start = journal.append_transcript(
            TranscriptKind.SYSTEM,
            "A😀BC",
            omitted_before_bytes=2,
            clip=TranscriptClip.KEEP_START,
        )
        end = journal.append_transcript(
            TranscriptKind.SYSTEM,
            "abcdef",
            omitted_after_bytes=3,
            clip=TranscriptClip.KEEP_END,
        )

        self.assertEqual(start.text, "A😀")
        self.assertEqual(start.omitted_before_bytes, 2)
        self.assertEqual(start.omitted_after_bytes, 2)
        self.assertTrue(start.clipped)
        self.assertLessEqual(len(start.text.encode("utf-8")), 5)

        self.assertEqual(end.text, "bcdef")
        self.assertEqual(end.omitted_before_bytes, 1)
        self.assertEqual(end.omitted_after_bytes, 3)
        self.assertTrue(end.clipped)
        end.text.encode("utf-8", errors="strict")

    def test_transcript_count_and_bytes_eviction_advance_floor(self) -> None:
        count_journal = _journal(
            namespace="count",
            limits=replace(_limits(), max_transcript_entries=2),
        )
        first = _system(count_journal, "one")
        second = _system(count_journal, "two")
        third = _system(count_journal, "three")

        snapshot = count_journal.transcript_snapshot()
        self.assertEqual(snapshot.entries, (second, third))
        self.assertEqual(snapshot.retention_floor, first.cursor)
        self.assertEqual(snapshot.omitted_before_entries, 1)
        self.assertEqual(snapshot.omitted_after_entries, 0)
        with self.assertRaises(JournalSnapshotRequired) as raised:
            count_journal.replay_transcript(
                TranscriptCursor(count_journal.transcript_id, 0, None)
            )
        self.assertEqual(raised.exception.domain, CursorDomain.TRANSCRIPT)
        self.assertEqual(
            raised.exception.reason,
            SnapshotRequiredReason.BEFORE_RETENTION_FLOOR,
        )
        self.assertEqual(
            count_journal.replay_transcript(first.cursor).entries,
            (second, third),
        )

        byte_journal = _journal(
            namespace="bytecount",
            limits=replace(
                _limits(),
                max_transcript_entries=10,
                max_transcript_bytes=8,
                max_transcript_entry_bytes=8,
            ),
        )
        old = _system(byte_journal, "12345")
        current = _system(byte_journal, "abcde")
        byte_snapshot = byte_journal.transcript_snapshot()
        self.assertEqual(byte_snapshot.entries, (current,))
        self.assertEqual(byte_snapshot.retention_floor, old.cursor)
        self.assertLessEqual(byte_snapshot.retained_text_bytes, 8)

    def test_transcript_cursor_wrong_stream_mismatch_and_future_need_snapshot(
        self,
    ) -> None:
        entry = _system(self.journal, "ready")
        other = _journal(namespace="other")
        other_entry = _system(other, "other")
        mismatched = TranscriptCursor(
            self.journal.transcript_id,
            1,
            entry.entry_id.removesuffix(":1") + ":99",
        )
        future = TranscriptCursor(
            self.journal.transcript_id,
            99,
            entry.entry_id.removesuffix(":1") + ":99",
        )

        cases = (
            (other_entry.cursor, SnapshotRequiredReason.WRONG_STREAM),
            (mismatched, SnapshotRequiredReason.POSITION_MISMATCH),
            (future, SnapshotRequiredReason.FUTURE_POSITION),
        )
        for cursor, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(JournalSnapshotRequired) as raised:
                    self.journal.replay_transcript(cursor)
                self.assertEqual(raised.exception.reason, reason)

    def test_snapshots_and_transport_copies_do_not_expose_mutable_state(self) -> None:
        entry = _system(self.journal, "ready")
        before = self.journal.snapshot()
        allocated = entry.to_dict()
        allocated["text"] = "changed"
        _system(self.journal, "later")

        self.assertEqual(entry.text, "ready")
        self.assertEqual(before.transcript.entries, (entry,))
        self.assertEqual(before.events.events, ())
        with self.assertRaises(FrozenInstanceError):
            before.transcript.position.position = 20  # type: ignore[misc]

    def test_invalid_transcript_inputs_do_not_advance_position(self) -> None:
        invalid_calls = (
            lambda: self.journal.append_transcript(
                TranscriptKind.COMMAND, "missing correlation"
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.STDOUT,
                "missing chunk metadata",
                command_id="command:one",
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.STDERR,
                "bad final",
                command_id="command:one",
                chunk_index=0,
                final=1,  # type: ignore[arg-type]
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.SYSTEM,
                "not output",
                chunk_index=0,
                final=True,
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.SYSTEM,
                b"\xff",
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.SYSTEM,
                "unpaired \ud800",
            ),
            lambda: self.journal.append_transcript(  # type: ignore[arg-type]
                "system", "string enum"
            ),
            lambda: self.journal.append_transcript(
                TranscriptKind.SYSTEM,
                "bad omitted count",
                omitted_before_bytes=True,
            ),
        )
        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call):
                with self.assertRaises((TypeError, ValueError)):
                    invalid_call()
                self.assertEqual(self.journal.transcript_position.position, 0)


class ProductAtomicJournalTests(unittest.TestCase):
    def test_command_acceptance_appends_one_exact_atomic_pair(self) -> None:
        journal = _journal()

        entry, event = journal.append_command_accepted(
            "look north",
            command_id="command:one",
            slot_generation=1,
            accepted_state_revision=0,
        )

        self.assertEqual(entry.kind, TranscriptKind.COMMAND)
        self.assertEqual(entry.position, 1)
        self.assertEqual(entry.command_id, "command:one")
        self.assertEqual(event.event_type, "command.accepted")
        self.assertEqual(event.sequence, 1)
        self.assertEqual(
            event.criticality,
            ProductEventCriticality.AUTHORITATIVE,
        )
        self.assertEqual(
            dict(event.data),
            {
                "slot_generation": 1,
                "accepted_state_revision": 0,
                "transcript_position": entry.position,
            },
        )
        snapshot = journal.snapshot()
        self.assertEqual(snapshot.transcript.entries, (entry,))
        self.assertEqual(snapshot.events.events, (event,))

    def test_tight_event_payload_rejection_exposes_neither_command_record(
        self,
    ) -> None:
        clock = SequenceClock([10, 11])
        journal = _journal(
            namespace="tight-command",
            limits=replace(
                _limits(),
                max_event_bytes=70,
                max_event_payload_bytes=70,
            ),
            clock_ms=clock,
        )
        before = journal.snapshot()

        with self.assertRaisesRegex(ValueError, "max_event_payload_bytes"):
            journal.append_command_accepted(
                "safe command",
                command_id="command:tight",
                slot_generation=1,
                accepted_state_revision=0,
            )

        self.assertEqual(journal.snapshot(), before)
        self.assertEqual(clock.calls, 0)

    def test_output_pair_clips_to_event_bound_and_copies_exact_projection(self) -> None:
        journal = _journal(
            namespace="tight-output",
            limits=replace(
                _limits(),
                max_event_bytes=240,
                max_event_payload_bytes=240,
                max_transcript_bytes=1_000,
                max_transcript_entry_bytes=1_000,
            ),
        )
        source = "prefix\x1b[31m" + ("x" * 400)
        inert_source, transformed = project_inert_text(source)

        entry, event = journal.append_command_output(
            TranscriptKind.STDOUT,
            source,
            command_id="command:output",
            chunk_index=3,
            final=False,
            omitted_before_bytes=5,
        )

        self.assertTrue(transformed)
        self.assertTrue(entry.transformed)
        self.assertTrue(entry.clipped)
        self.assertEqual(entry.omitted_before_bytes, 5)
        self.assertGreater(entry.omitted_after_bytes, 0)
        self.assertEqual(
            len(entry.text.encode("utf-8")) + entry.omitted_after_bytes,
            len(inert_source.encode("utf-8")),
        )
        self.assertEqual(event.event_type, "command.output")
        self.assertEqual(event.criticality, ProductEventCriticality.INFORMATIONAL)
        self.assertEqual(event.command_id, entry.command_id)
        self.assertEqual(event.data["stream"], "stdout")
        self.assertEqual(event.data["chunk_index"], 3)
        self.assertEqual(event.data["text"], entry.text)
        self.assertEqual(event.data["encoding"], entry.encoding)
        self.assertEqual(event.data["final"], entry.final)
        self.assertEqual(event.data["clipped"], entry.clipped)
        self.assertEqual(
            event.data["omitted_before_bytes"],
            entry.omitted_before_bytes,
        )
        self.assertEqual(
            event.data["omitted_after_bytes"],
            entry.omitted_after_bytes,
        )
        self.assertEqual(event.data["transformed"], entry.transformed)
        self.assertEqual(event.data["projection"], entry.projection)
        self.assertEqual(event.data["transcript_position"], entry.position)
        self.assertLessEqual(
            journal.event_snapshot().retained_payload_bytes,
            journal.limits.max_event_payload_bytes,
        )

    def test_empty_final_output_marker_is_one_atomic_pair(self) -> None:
        journal = _journal()

        entry, event = journal.append_command_output(
            TranscriptKind.STDERR,
            "",
            command_id="command:final",
            chunk_index=0,
            final=True,
        )

        self.assertEqual(entry.text, "")
        self.assertTrue(entry.final)
        self.assertEqual(event.data["stream"], "stderr")
        self.assertEqual(event.data["text"], "")
        self.assertTrue(event.data["final"])
        self.assertFalse(event.data["clipped"])

    def test_output_event_clipping_can_keep_the_utf8_safe_end(self) -> None:
        journal = _journal(
            namespace="output-tail",
            limits=replace(
                _limits(),
                max_event_bytes=240,
                max_event_payload_bytes=240,
            ),
        )

        entry, event = journal.append_command_output(
            TranscriptKind.STDERR,
            ("x" * 400) + "TAIL",
            command_id="command:tail",
            chunk_index=0,
            final=False,
            clip=TranscriptClip.KEEP_END,
        )

        self.assertTrue(entry.text.endswith("TAIL"))
        self.assertGreater(entry.omitted_before_bytes, 0)
        self.assertEqual(entry.omitted_after_bytes, 0)
        self.assertEqual(event.data["text"], entry.text)
        self.assertEqual(
            event.data["omitted_before_bytes"],
            entry.omitted_before_bytes,
        )

    def test_output_metadata_too_large_exposes_neither_side(self) -> None:
        journal = _journal(
            namespace="output-metadata",
            limits=replace(
                _limits(),
                max_event_bytes=200,
                max_event_payload_bytes=200,
            ),
        )
        before = journal.snapshot()

        with self.assertRaisesRegex(ValueError, "metadata exceeds"):
            journal.append_command_output(
                TranscriptKind.STDOUT,
                "text",
                command_id="command:metadata",
                chunk_index=0,
                final=False,
            )

        self.assertEqual(journal.snapshot(), before)

    def test_second_object_clock_failure_leaves_both_coordinates_unchanged(
        self,
    ) -> None:
        operations = (
            lambda journal: journal.append_command_accepted(
                "safe command",
                command_id="command:clock",
                slot_generation=1,
                accepted_state_revision=0,
            ),
            lambda journal: journal.append_command_output(
                TranscriptKind.STDOUT,
                "safe output",
                command_id="command:clock",
                chunk_index=0,
                final=False,
            ),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                clock = SequenceClock([100, -1])
                journal = _journal(
                    namespace=f"clock-{index}",
                    clock_ms=clock,
                )
                before = journal.snapshot()

                with self.assertRaisesRegex(ValueError, "clock_ms"):
                    operation(journal)

                self.assertEqual(clock.calls, 2)
                self.assertEqual(journal.snapshot(), before)

    def test_counter_exhaustion_cannot_append_only_one_side(self) -> None:
        event_exhausted = _journal(namespace="event-exhausted")
        event_exhausted._event_sequence = 2**63 - 1
        with self.assertRaises(OverflowError):
            event_exhausted.append_command_accepted(
                "command",
                command_id="command:event-counter",
                slot_generation=1,
                accepted_state_revision=0,
            )
        self.assertEqual(event_exhausted.transcript_snapshot().entries, ())

        transcript_exhausted = _journal(namespace="transcript-exhausted")
        transcript_exhausted._transcript_position = 2**63 - 1
        with self.assertRaises(OverflowError):
            transcript_exhausted.append_command_output(
                TranscriptKind.STDOUT,
                "output",
                command_id="command:transcript-counter",
                chunk_index=0,
                final=False,
            )
        self.assertEqual(transcript_exhausted.event_snapshot().events, ())

    def test_pair_eviction_advances_independent_floors_together(self) -> None:
        journal = _journal(
            namespace="pair-eviction",
            limits=replace(
                _limits(),
                max_events=2,
                max_event_bytes=200,
                max_event_payload_bytes=100,
                max_transcript_entries=2,
            ),
        )
        pairs = tuple(
            journal.append_command_accepted(
                f"command {index}",
                command_id=f"command:{index}",
                slot_generation=index,
                accepted_state_revision=0,
            )
            for index in range(1, 4)
        )

        snapshot = journal.snapshot()
        self.assertEqual(
            snapshot.transcript.entries,
            tuple(pair[0] for pair in pairs[1:]),
        )
        self.assertEqual(
            snapshot.events.events,
            tuple(pair[1] for pair in pairs[1:]),
        )
        self.assertEqual(snapshot.transcript.retention_floor, pairs[0][0].cursor)
        self.assertEqual(snapshot.events.retention_floor, pairs[0][1].cursor)
        self.assertEqual(snapshot.transcript.position.position, 3)
        self.assertEqual(snapshot.events.position.sequence, 3)


class ProductJournalLimitTests(unittest.TestCase):
    def test_every_limit_is_explicitly_validated(self) -> None:
        defaults = _limits()
        for name in (
            "max_events",
            "max_event_bytes",
            "max_event_payload_bytes",
            "max_payload_depth",
            "max_payload_items",
            "max_transcript_entries",
            "max_transcript_bytes",
            "max_transcript_entry_bytes",
        ):
            for invalid in (0, -1, True, 2**63):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        replace(defaults, **{name: invalid})

        with self.assertRaisesRegex(ValueError, "max_event_payload_bytes"):
            replace(defaults, max_event_bytes=8, max_event_payload_bytes=9)
        with self.assertRaisesRegex(ValueError, "max_transcript_entry_bytes"):
            replace(
                defaults,
                max_transcript_bytes=8,
                max_transcript_entry_bytes=9,
            )

    def test_constructor_dependencies_and_generated_ids_are_validated(self) -> None:
        with self.assertRaises(TypeError):
            ProductSessionJournal(limits=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ProductSessionJournal(id_factory="not callable")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ProductSessionJournal(clock_ms=5)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "event_stream id"):
            ProductSessionJournal(id_factory=lambda _purpose: "short")

        journal = _journal(clock_ms=lambda: -1)
        with self.assertRaisesRegex(ValueError, "clock_ms"):
            _event(journal, 1)
        self.assertEqual(journal.event_position.sequence, 0)

    def test_separate_journals_are_isolated_without_global_registration(self) -> None:
        ids = DeterministicIds("sharedfactory")
        first = ProductSessionJournal(id_factory=ids, clock_ms=lambda: 1)
        second = ProductSessionJournal(id_factory=ids, clock_ms=lambda: 1)
        first_event = _event(first, 1)
        second_entry = _system(second, "second only")

        self.assertNotEqual(first.stream_id, second.stream_id)
        self.assertNotEqual(first.transcript_id, second.transcript_id)
        self.assertEqual(first.event_snapshot().events, (first_event,))
        self.assertEqual(first.transcript_snapshot().entries, ())
        self.assertEqual(second.event_snapshot().events, ())
        self.assertEqual(second.transcript_snapshot().entries, (second_entry,))


def _limits() -> JournalLimits:
    return JournalLimits(
        max_events=20,
        max_event_bytes=2_000,
        max_event_payload_bytes=1_000,
        max_payload_depth=5,
        max_payload_items=50,
        max_transcript_entries=20,
        max_transcript_bytes=2_000,
        max_transcript_entry_bytes=1_000,
    )


def _journal(
    *,
    namespace: str = "journal",
    limits: JournalLimits | None = None,
    clock_ms=None,
) -> ProductSessionJournal:
    return ProductSessionJournal(
        limits=limits or _limits(),
        id_factory=DeterministicIds(namespace),
        clock_ms=clock_ms or (lambda: 12_345),
    )


def _event(journal: ProductSessionJournal, number: int):
    return journal.append_event(
        "safe.event",
        criticality=ProductEventCriticality.INFORMATIONAL,
        data={"number": number},
    )


def _append_data(journal: ProductSessionJournal, data: object):
    return journal.append_event(
        "safe.event",
        criticality=ProductEventCriticality.INFORMATIONAL,
        data=data,  # type: ignore[arg-type]
    )


def _system(journal: ProductSessionJournal, text: str):
    return journal.append_transcript(TranscriptKind.SYSTEM, text)


if __name__ == "__main__":
    unittest.main()
