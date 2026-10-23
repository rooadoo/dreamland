from __future__ import annotations

import unittest

from dreamland.background_agents.event_bus import (
    SessionEventBus,
    new_session_event,
)
from dreamland.background_agents.events import EventSource, SessionEventType


class SessionEventBusTests(unittest.TestCase):
    def test_subscribers_receive_the_same_immutable_event(self) -> None:
        bus = SessionEventBus()
        received: list[tuple[str, object]] = []
        first = bus.subscribe("first", lambda event: received.append(("first", event)))
        bus.subscribe("second", lambda event: received.append(("second", event)))
        event = new_session_event(
            session_id="session-1",
            event_type=SessionEventType.COMMAND_COMPLETED,
            source=EventSource.COMMAND_EMULATOR,
            command_id="command-1",
            target_host="app",
            payload={"exit_status": 0},
            occurred_at_ms=100,
        )

        failures = bus.publish(event)

        self.assertEqual(failures, ())
        self.assertEqual([name for name, _ in received], ["first", "second"])
        self.assertIs(received[0][1], event)
        self.assertIs(received[1][1], event)
        first.close()
        self.assertEqual(bus.subscriber_ids(), ("second",))

    def test_failing_subscriber_does_not_block_other_subscribers(self) -> None:
        bus = SessionEventBus()
        received = []

        def fail(_event: object) -> None:
            raise RuntimeError("boom")

        bus.subscribe("broken", fail)
        bus.subscribe("healthy", received.append)
        event = new_session_event(
            session_id="session-1",
            event_type=SessionEventType.SESSION_IDLE,
            source=EventSource.SESSION_RUNTIME,
            occurred_at_ms=100,
        )

        with self.assertLogs("dreamland.enrichment.events", level="WARNING"):
            failures = bus.publish(event)

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].subscriber_id, "broken")
        self.assertEqual(received, [event])

    def test_duplicate_subscriber_ids_are_rejected(self) -> None:
        bus = SessionEventBus()
        bus.subscribe("agent", lambda _event: None)
        with self.assertRaisesRegex(ValueError, "already registered"):
            bus.subscribe("agent", lambda _event: None)


if __name__ == "__main__":
    unittest.main()
