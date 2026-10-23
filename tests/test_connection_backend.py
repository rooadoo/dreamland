import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from dreamland.agent_core import LangGraphCommandAgent
from dreamland.agent_state import (
    interactive_program_to_state,
    shell_environment_to_state,
)
from dreamland.backend import AgentBackend
from dreamland.models import (
    CommandExecutionControl,
    CommandResult,
    CommandStateUpdate,
    CommandStreamResult,
    ConnectionEndpoint,
    ConnectionMetadata,
    ShellStackTransition,
    default_shell_frame,
)
from dreamland.product.execution import ProductExecutionOutcome, ProductExecutionRequest
from dreamland.product.runtime_executor import (
    DreamlandStateProfileCodec,
    ProductRuntimeCommandExecutor,
    ProductRuntimeUsage,
)
from dreamland.profile import server_profile
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
    create_state_profile,
)


def _profile():
    world = {
        "entry_host": "host-a",
        "active_host": "host-a",
        "hosts_by_name": {
            name: {
                "hostname": name,
                "login_banner": "",
                "network": {"ipv4_address": address},
            }
            for name, address in (
                ("host-a", "10.72.24.17"),
                ("host-b", "198.51.100.166"),
                ("host-c", "198.51.100.170"),
            )
        },
    }
    return StateProfile(
        name="connection-backend-test",
        level="world",
        state={"world_state": world},
        created_at="2026-09-11T00:00:00Z",
    )


def _ingress():
    return ConnectionMetadata(
        protocol="ssh",
        source=ConnectionEndpoint("198.51.100.23", 47486),
        destination=ConnectionEndpoint("127.0.0.1", 2222),
    )


def _hop_result(hostname):
    frame = default_shell_frame(username="reader", hostname=hostname)
    return CommandResult(
        state_update=CommandStateUpdate(
            host_identity=frame.host_identity,
            cwd=frame.cwd,
            interactive_program=frame.interactive_program,
            shell_environment=frame.shell_environment,
            shell_stack_transition=ShellStackTransition("push", username="reader"),
        )
    )


class _RecordingAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command.startswith("ssh "):
            return _hop_result(context.command.split()[-1])
        return CommandResult(
            stdout=json.dumps(context.environment_metadata.metadata["active_network"])
        )


class _CancellingAgent(_RecordingAgent):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def respond(self, context):
        result = super().respond(context)
        self.control.cancel()
        return result


class _InterruptedStreamAgent:
    def stream_respond(self, context, control=None):
        yield CommandStreamResult(
            replace(
                _hop_result("host-b"),
                interrupted=True,
                signal="SIGINT",
                exit_status=130,
            )
        )


class _GraphModel:
    def __init__(self):
        self.prompts = []

    def invoke(self, messages, **kwargs):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        command = prompt.rsplit("current command: ", 1)[1]
        update = {}
        if command.startswith("ssh "):
            state = _hop_result(command.split()[-1]).state_update
            update = {
                "host_identity": {"hostname": state.host_identity.hostname},
                "cwd": state.cwd,
                "interactive_program": interactive_program_to_state(
                    state.interactive_program
                ),
                "shell_environment": shell_environment_to_state(
                    state.shell_environment
                ),
                "shell_stack_transition": {"operation": "push", "username": "reader"},
            }
        return SimpleNamespace(
            content=json.dumps(
                {"stdout": "", "stderr": "", "exit_status": 0, "state_update": update}
            ),
            additional_kwargs={},
        )


class _ProductAgent(_RecordingAgent):
    def execute(self, context, control, emit):
        return self.respond(context)


class _UsageMeter:
    def begin(self, **kwargs):
        return object()

    def finish(self, measurement):
        return ProductRuntimeUsage()


class _CheckpointReaper:
    def __init__(self):
        self.namespaces = []

    def reap(self, checkpoint_namespace):
        self.namespaces.append(checkpoint_namespace)


class ConnectionBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="dreamland-connection-backend-")
        self.addCleanup(directory.cleanup)
        self.store = StateProfileStore(Path(directory.name))

    def _backend(self, agent):
        profile = _profile()
        return AgentBackend(
            server_profile(profile.state["world_state"]),
            agent=agent,
            state_profile_runtime=StateProfileRuntime(
                store=self.store, current_profile=profile
            ),
            structured_failure_capture_enabled=False,
        )

    def test_backend_context_and_builtin_environment_follow_a_b_c_and_exits(self):
        agent = _RecordingAgent()
        backend = self._backend(agent)
        session = backend.create_session("adam", connection_metadata=_ingress())
        initial = json.loads(backend.execute(session, "ss -nat").stdout)
        self.assertEqual(
            initial["connections"][0]["local"], {"ip": "10.72.24.17", "port": 22}
        )
        for host, expected_peer in (
            ("host-b", "10.72.24.17"),
            ("host-c", "198.51.100.166"),
        ):
            backend.execute(session, f"ssh {host}")
            self.assertTrue(session.force_session_state_next_command)
            network = json.loads(backend.execute(session, "ss -nat").stdout)
            context = agent.contexts[-1]
            self.assertTrue(context.force_session_state)
            self.assertEqual(context.hostname, host)
            self.assertEqual(network["host"], host)
            self.assertEqual(len(network["connections"]), 1)
            self.assertEqual(network["connections"][0]["peer"]["ip"], expected_peer)
            login = context.environment_metadata.connection
            expected = (
                f"{login.source.ip} {login.source.port} {login.destination.ip} 22\n"
            )
            self.assertEqual(
                backend.execute(session, "printenv SSH_CONNECTION").stdout, expected
            )
            self.assertEqual(context.transport_connection, _ingress())
        self.assertEqual(len(session.network_connections.connections), 3)
        backend.execute(session, "exit")
        self.assertEqual(session.host_identity.hostname, "host-b")
        self.assertEqual(len(session.network_connections.connections), 2)
        backend.execute(session, "exit")
        self.assertEqual(session.host_identity.hostname, "host-a")
        self.assertEqual(len(session.network_connections.connections), 1)

    def test_cancelled_respond_fallback_discards_hop_and_stops_command_list(self):
        for command in ("ssh host-b", "ssh host-b; ssh host-c"):
            with self.subTest(command=command):
                control = CommandExecutionControl()
                agent = _CancellingAgent(control)
                backend = self._backend(agent)
                session = backend.create_session("adam", connection_metadata=_ingress())
                original = deepcopy(session.network_connections)
                events = list(backend.execute_stream(session, command, control))
                result = events[-1].result
                self.assertIsInstance(events[-1], CommandStreamResult)
                self.assertTrue(result.interrupted)
                self.assertEqual(result.exit_status, 130)
                self.assertIsNone(result.state_update.shell_stack_transition)
                self.assertEqual(session.host_identity.hostname, "host-a")
                self.assertEqual(session.network_connections, original)
                self.assertEqual([item.command for item in agent.contexts], [command])
                self.assertEqual(len(session.history), 1)
                self.assertTrue(session.history[0].interrupted)

    def test_interrupted_stream_result_cannot_apply_remote_state(self):
        backend = self._backend(_InterruptedStreamAgent())
        session = backend.create_session("adam", connection_metadata=_ingress())
        original = deepcopy(session.network_connections)
        events = list(backend.execute_stream(session, "ssh host-b"))
        result = events[-1].result
        self.assertTrue(result.interrupted)
        self.assertIsNone(result.state_update.shell_stack_transition)
        self.assertEqual(session.host_identity.hostname, "host-a")
        self.assertEqual(session.network_connections, original)
        self.assertEqual(len(session.shell_stack), 1)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None, "langgraph not installed"
    )
    def test_graph_refresh_preserves_runtime_login_and_release_reclaims_all_state(self):
        model = _GraphModel()
        agent = LangGraphCommandAgent(
            model_name="test-model", system_prompt_name="system.md", model=model
        )
        backend = self._backend(agent)
        session = backend.create_session("adam", connection_metadata=_ingress())
        backend.execute(session, "ssh host-b")
        expected = session.shell_environment.variables["SSH_CONNECTION"]
        self.assertEqual(
            backend.execute(session, "printenv SSH_CONNECTION").stdout, expected + "\n"
        )
        backend.execute(session, "ss -nat")
        prompt = model.prompts[-1]
        self.assertIn('"host": "host-b"', prompt)
        self.assertIn(expected, prompt)
        self.assertNotIn("127.0.0.1", prompt)
        self.assertNotIn("2222", prompt)
        config = {"configurable": {"thread_id": session.langfuse_trace_id}}
        self.assertTrue(agent.graph.get_state(config).values)
        backend.release_session(session)
        self.assertFalse(agent.graph.get_state(config).values)
        self.assertEqual(session.network_connections.connections, {})
        self.assertEqual(session.network_connections.frames, [])

    def test_product_command_executor_keeps_hop_across_real_decode_execute_encode(self):
        backend = self._backend(_RecordingAgent())
        session = backend.create_session("adam", connection_metadata=_ingress())
        document = create_state_profile(
            session, name="product-connected", level="exact"
        ).to_document()
        agent = _ProductAgent()
        reaper = _CheckpointReaper()
        executor = ProductRuntimeCommandExecutor(
            agent=agent,
            state_codec=DreamlandStateProfileCodec(),
            checkpoint_reaper=reaper,
            usage_meter=_UsageMeter(),
            clock_ms=lambda: 100,
        )

        def execute(command, document, index):
            return executor.execute(
                ProductExecutionRequest(
                    service_epoch="epoch:test",
                    session_id="session:test",
                    command_id=f"command:{index}",
                    slot_generation=index,
                    canonical_command=command,
                    provisional_document=document,
                    checkpoint_namespace=f"product:test:{index}",
                    deadline_ms=1000,
                    max_tokens=50,
                    max_spend_units=20,
                    max_attempts=1,
                    cancelled=lambda: False,
                ),
                lambda chunk: None,
            )

        first = execute("ssh host-b", document, 1)
        self.assertEqual(first.outcome, ProductExecutionOutcome.COMPLETED)
        snapshot = first.state_effect.provisional_document
        hop = next(
            value
            for value in snapshot["state"]["network_connections"][
                "connections"
            ].values()
            if not value["entry"]
        )
        second = execute("ss -nat", snapshot, 2)
        self.assertEqual(second.outcome, ProductExecutionOutcome.COMPLETED)
        context = agent.contexts[-1]
        self.assertEqual(context.hostname, "host-b")
        self.assertEqual(context.langfuse_trace_id, "product:test:2")
        self.assertTrue(context.force_session_state)
        row = context.environment_metadata.metadata["active_network"]["connections"][0]
        self.assertEqual(row["id"], hop["id"])
        self.assertEqual(row["peer"], hop["source"])
        self.assertEqual(row["local"], hop["destination"])
        self.assertEqual(reaper.namespaces, ["product:test:1", "product:test:2"])


if __name__ == "__main__":
    unittest.main()
