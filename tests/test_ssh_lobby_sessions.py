import queue
import threading
import unittest

from dreamland.agent import LangGraphCommandAgent
from dreamland.backend import AgentBackend
from dreamland.lobby_input import BoundaryFilter
from dreamland.models import CommandOutputChunk, CommandResult, CommandStreamResult
from dreamland.profile import server_profile
from dreamland.ssh_sessions import ShellSession
from tests import test_ssh_sessions
from tests.test_backend import RecordingModel, langgraph_available
from tests.test_ssh_sessions import FakeChannel, SudoShellAgent


class QueueChannel(FakeChannel):
    def __init__(self, initial=b""):
        super().__init__(b"")
        self.incoming = queue.Queue()
        if initial:
            self.incoming.put(initial)
        self.prompt_seen = threading.Event()
        self.data_received = threading.Event()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, count):
        try:
            result = self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError from None
        if not result:
            self.closed = True
        self.data_received.set()
        return result

    def recv_ready(self):
        return not self.incoming.empty()

    def send(self, data):
        super().send(data)
        if b"$ " in data:
            self.prompt_seen.set()


class BlockingAgent:
    def __init__(self):
        self.started = threading.Event()
        self.finish = threading.Event()
        self.released = threading.Event()
        self.control = None
        self.contexts = []
        self.releases = []

    def stream_respond(self, context, control=None):
        self.contexts.append(context)
        self.control = control
        yield CommandOutputChunk("visible before close\n")
        self.started.set()
        self.finish.wait(5)
        yield CommandOutputChunk("FORBIDDEN LATE OUTPUT\n")
        yield CommandStreamResult(CommandResult(stdout="FORBIDDEN LATE RESULT\n"))

    def release_session(self, session_id):
        self.releases.append(session_id)
        self.released.set()


class SSHLobbySessionTests(unittest.TestCase):
    def test_busy_drained_partial_key_preserves_wire_order_after_buffered_exit(self):
        agent = BlockingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = QueueChannel(b"agent-check\nexit\r")
        shell = ShellSession(channel, "bob", backend, lobby_mode=True)
        outcomes = []
        runner = threading.Thread(target=lambda: outcomes.append(shell.run()))
        runner.start()
        self.addCleanup(agent.finish.set)
        self.assertTrue(agent.started.wait(2))
        channel.data_received.clear()
        channel.incoming.put(b"\n\x1b[")
        self.assertTrue(channel.data_received.wait(2))
        agent.finish.set()
        runner.join(2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcomes[0].reason, "world_exit")
        boundary = BoundaryFilter()
        for byte in shell.boundary_input:
            boundary.consume(byte)
        self.assertTrue(boundary.suppress(ord("A")))
        self.assertFalse(boundary.suppress(ord("1")))

    def test_long_boundary_input_compacts_without_losing_unclosed_osc(self):
        shell = ShellSession(
            FakeChannel(b""),
            "bob",
            AgentBackend(server_profile()),
            lobby_mode=True,
        )
        try:
            shell._remember_input(b"\x1b]")
            for _ in range(100):
                shell._remember_input(b"x" * 1024)
            self.assertLessEqual(len(shell.boundary_input), 4102)
            boundary = BoundaryFilter()
            for byte in shell.boundary_input:
                boundary.consume(byte)
            self.assertTrue(boundary.suppress(ord("1")))
            self.assertTrue(boundary.suppress(7))
            self.assertFalse(boundary.suppress(ord("1")))
        finally:
            shell.close()

    def test_outer_exit_preserves_channel_and_reports_discarded_crlf_tail(self):
        backend = AgentBackend(server_profile())
        channel = FakeChannel(b"exit\r\n1\nwhoami\n\x1b[")
        shell = ShellSession(channel, "bob", backend, lobby_mode=True)

        outcome = shell.run()

        self.assertEqual(outcome.reason, "world_exit")
        self.assertTrue(shell.boundary_input.endswith(b"\r\n1\nwhoami\n\x1b["))
        self.assertIsNone(channel.exit_status)
        self.assertFalse(channel.closed)
        self.assertFalse(shell.cleanup_pending)
        self.assertEqual(len(shell.session.history), 1)

    def test_nested_ctrl_d_returns_to_parent_before_outer_exit(self):
        backend = AgentBackend(server_profile(), agent=SudoShellAgent())
        channel = FakeChannel(b"sudo -i\n\x04exit\n")
        shell = ShellSession(channel, "bob", backend, lobby_mode=True)

        self.assertEqual(shell.run().reason, "world_exit")
        self.assertIn("root@ubuntu:~$ ", channel.output)
        self.assertIn("\r\nbob@ubuntu:~$ ", channel.output)
        self.assertIsNone(channel.exit_status)

    def test_actual_channel_eof_is_disconnect(self):
        backend = AgentBackend(server_profile())
        shell = ShellSession(FakeChannel(b""), "bob", backend, lobby_mode=True)

        self.assertEqual(shell.run().reason, "disconnect")

    def test_operator_close_stops_idle_intake_without_closing_parent(self):
        backend = AgentBackend(server_profile())
        channel = QueueChannel()
        console = test_ssh_sessions.ShellSessionTests()._console()
        shell = ShellSession(channel, "bob", backend, console=console, lobby_mode=True)
        outcomes = []
        runner = threading.Thread(target=lambda: outcomes.append(shell.run()))
        runner.start()
        self.assertTrue(channel.prompt_seen.wait(2))

        console.session_registry.unregister(shell.session.langfuse_trace_id)
        channel.incoming.put(b"whoami\n")
        runner.join(2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcomes[0].reason, "operator_close")
        self.assertEqual(shell.session.history, [])
        self.assertFalse(channel.closed)
        self.assertIsNone(channel.exit_status)

    def test_busy_close_fences_provider_and_reclaims_only_after_owner_finishes(self):
        agent = BlockingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = QueueChannel(b"agent-check\nqueued-command\n")
        console = test_ssh_sessions.ShellSessionTests()._console()
        shell = ShellSession(channel, "bob", backend, console=console, lobby_mode=True)
        outcomes = []
        runner = threading.Thread(target=lambda: outcomes.append(shell.run()))
        runner.start()
        self.addCleanup(agent.finish.set)
        self.assertTrue(agent.started.wait(2))

        console.session_registry.unregister(shell.session.langfuse_trace_id)
        runner.join(2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcomes[0].reason, "operator_close")
        self.assertTrue(agent.control.cancelled())
        self.assertTrue(shell.cleanup_pending)
        self.assertEqual(agent.releases, [])
        self.assertIsNone(console.session_registry.get(shell.session.langfuse_trace_id))
        self.assertEqual(len(agent.contexts), 1)
        self.assertIn(b"queued-command", shell.boundary_input)

        channel.send(b"NEXT PICKER\r\n")
        agent.finish.set()
        self.assertTrue(agent.released.wait(2))
        self.assertEqual(agent.releases, [shell.session.langfuse_trace_id])
        self.assertFalse(shell.cleanup_pending)
        self.assertNotIn("FORBIDDEN", channel.output)
        self.assertTrue(channel.output.endswith("NEXT PICKER\r\n"))
        shell.close()
        self.assertEqual(agent.releases, [shell.session.langfuse_trace_id])

    def test_shutdown_unblocks_idle_lobby_world(self):
        stop_event = threading.Event()
        channel = QueueChannel()
        shell = ShellSession(
            channel,
            "bob",
            AgentBackend(server_profile()),
            lobby_mode=True,
            stop_event=stop_event,
        )
        outcomes = []
        runner = threading.Thread(target=lambda: outcomes.append(shell.run()))
        runner.start()
        self.assertTrue(channel.prompt_seen.wait(2))
        stop_event.set()
        runner.join(2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcomes[0].reason, "shutdown")

    def test_precreated_session_identity_is_registered_before_start(self):
        backend = AgentBackend(server_profile())
        prepared = backend.create_session("bob")
        console = test_ssh_sessions.ShellSessionTests()._console()
        shell = ShellSession(
            FakeChannel(b""),
            "bob",
            backend,
            session=prepared,
            console=console,
            lobby_mode=True,
        )

        self.assertIs(shell.session, prepared)
        self.assertIs(
            console.session_registry.get(prepared.langfuse_trace_id).session, prepared
        )
        shell.close()
        self.assertIsNone(console.session_registry.get(prepared.langfuse_trace_id))

    @unittest.skipUnless(langgraph_available, "LangGraph is not installed")
    def test_repeated_visits_reclaim_real_checkpoints(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("ok"),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        for _ in range(8):
            channel = FakeChannel(b"agent-check\nexit\n")
            shell = ShellSession(channel, "bob", backend, lobby_mode=True)
            self.assertEqual(shell.run().reason, "world_exit")
            self.assertFalse(shell.cleanup_pending)
            self.assertEqual(list(agent.graph.checkpointer.list(None)), [])


if __name__ == "__main__":
    unittest.main()
