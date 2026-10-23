"""Provider-free connection routing, concurrent visits, and cleanup checks."""

import unittest

from dreamland.models import CommandResult
from tests import test_ssh_lobby as fixtures


class TierAgent:
    def __init__(self, tier):
        self.openai_service_tier = tier
        self.sessions = []
        self.released = []

    def respond(self, context):
        self.sessions.append(context.langfuse_trace_id)
        return CommandResult(stdout=f"served-by-{self.openai_service_tier}\n")

    def release_session(self, session_id):
        self.released.append(session_id)


class FastLobbyTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.SSHLobbyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def select(self, fixture, backend, number=b"1\n"):
        offset = len(fixture.channel.output)
        fixture.channel.incoming.put(number)
        fixture.channel.wait_for("@ubuntu:", after=offset)
        return backend.sessions[-1]

    def command(self, fixture, tier):
        offset = len(fixture.channel.output)
        fixture.channel.incoming.put(b"probe-model\n")
        fixture.channel.wait_for(f"served-by-{tier}", after=offset)
        fixture.channel.wait_for("@ubuntu:", after=offset)

    def test_demo_visits_and_concurrent_normal_connection_keep_separate_agents(self):
        for login in ("dreamland_fast", "dreamland_fast_simple"):
            with self.subTest(login=login):
                normal, demo = self.fixture(), self.fixture()
                normal.connection_username = "dreamland"
                demo.connection_username = login
                fast_backend = fixtures.RecordingBackend(normal.store)
                fast_backend.agent = fast_agent = TierAgent("ultrafast")
                normal.backend.agent = normal_agent = TierAgent("fast")
                demo.backend = normal.backend
                demo.lobby = normal.lobby
                normal.lobby.fast_backend = fast_backend

                normal.agree()
                demo.agree()
                normal_session = normal.select()
                with self.assertLogs("dreamland.lifecycle", level="INFO") as logs:
                    for number, username in ((b"1\n", "operator"), (b"2\n", "reader")):
                        session = self.select(demo, fast_backend, number)
                        demo.assert_entry_identity(session, username)
                        self.command(demo, "ultrafast")
                        self.command(normal, "fast")
                        self.assertIn(session.langfuse_trace_id, fast_agent.sessions)
                        self.assertNotIn(
                            session.langfuse_trace_id, normal_agent.sessions
                        )
                        offset = len(demo.channel.output)
                        demo.channel.incoming.put(b"exit\n")
                        demo.channel.wait_for("Enter a number", after=offset)
                        self.assertIn(session.langfuse_trace_id, fast_agent.released)
                self.assertTrue(
                    any("service_tier=ultrafast" in message for message in logs.output)
                )
                self.assertEqual(normal.backend.sessions, [normal_session])
                self.assertEqual(len(fast_backend.sessions), 2)
                self.assertEqual(normal_agent.released, [])
                self.command(normal, "fast")
                offset = len(normal.channel.output)
                normal.channel.incoming.put(b"exit\n")
                normal.channel.wait_for("Enter a number", after=offset)
                self.assertEqual(
                    normal_agent.released, [normal_session.langfuse_trace_id]
                )
                for fixture in (normal, demo):
                    fixture.channel.incoming.put(b"q\n")
                    fixture.runner.join(3)
                    self.assertFalse(fixture.runner.is_alive())

    def test_fast_login_without_backend_fails_closed_before_agreement(self):
        fixture = self.fixture()
        fixture.connection_username = "dreamland_fast"
        fixture.runner.start()
        fixture.runner.join(3)
        self.assertFalse(fixture.runner.is_alive())
        self.assertIn("The lobby is unavailable", fixture.channel.output)
        self.assertNotIn("i agree", fixture.channel.output)
        self.assertEqual(fixture.backend.sessions, [])


if __name__ == "__main__":
    unittest.main()
