import unittest

from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.background_agents.events import SessionEventType
from dreamland.models import SessionState
from dreamland.session_registry import LiveSessionRegistry


class LiveSessionRegistryTests(unittest.TestCase):
    def test_close_callback_is_exact_once_after_registration_is_retired(self):
        registry = LiveSessionRegistry()
        session = SessionState(username="alice", langfuse_trace_id="session-a")
        observed = []
        registration = registry.register(
            session,
            close_callback=lambda: observed.append(registry.get("session-a")),
        )
        with registry.if_live(registration) as live:
            self.assertTrue(live)
        registry.unregister("session-a", session=session)
        registry.unregister("session-a", session=session)
        with registry.if_live(registration) as live:
            self.assertFalse(live)
        self.assertTrue(registration.closed.is_set())
        self.assertEqual(observed, [None])

    def test_register_get_list_and_unregister_session(self):
        registry = LiveSessionRegistry()
        session = SessionState(username="alice", langfuse_trace_id="session-a")

        registration = registry.register(session)

        self.assertEqual(registration.session_id, "session-a")
        self.assertIs(registration.session, session)
        self.assertIs(registry.get("session-a"), registration)
        self.assertEqual(registry.list_sessions(), (registration,))

        registry.unregister("session-a", session=session)

        self.assertIsNone(registry.get("session-a"))
        self.assertEqual(registry.list_sessions(), ())

    def test_unregister_does_not_remove_replaced_session(self):
        registry = LiveSessionRegistry()
        old_session = SessionState(username="alice", langfuse_trace_id="session-a")
        new_session = SessionState(username="bob", langfuse_trace_id="session-a")
        registry.register(old_session)
        new_registration = registry.register(new_session)

        registry.unregister("session-a", session=old_session)

        self.assertIs(registry.get("session-a"), new_registration)

    def test_register_and_unregister_publish_reusable_session_events(self):
        event_bus = SessionEventBus()
        events = []
        event_bus.subscribe("test", events.append)
        registry = LiveSessionRegistry(event_bus=event_bus)
        session = SessionState(
            username="guest",
            langfuse_trace_id="session-events",
            world_state={
                "active_host": "app",
                "hosts_by_name": {"app": {"hostname": "app"}},
            },
        )

        registry.register(session)
        registry.unregister(session.langfuse_trace_id, session=session)

        self.assertEqual(
            [event.event_type for event in events],
            [
                SessionEventType.SESSION_REGISTERED,
                SessionEventType.SESSION_CLOSING,
            ],
        )
        self.assertEqual(events[0].target_host, "app")


if __name__ == "__main__":
    unittest.main()
