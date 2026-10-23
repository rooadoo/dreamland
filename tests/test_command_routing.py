import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace

from dreamland.agent_core import LangGraphCommandAgent
from dreamland.backend import AgentBackend
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.background_agents.events import SessionEventType
from dreamland.models import (
    CommandExecutionControl,
    CommandOutputChunk,
    CommandResult,
    CommandStateEffect,
    CommandStateUpdate,
    CommandStreamResult,
    InteractiveProgram,
    SessionState,
    default_shell_frame,
)
from dreamland.profile import server_profile


def session_state():
    frame = default_shell_frame(username="alice")
    return SessionState(
        username=frame.username,
        cwd=frame.cwd,
        host_identity=frame.host_identity,
        interactive_program=frame.interactive_program,
        shell_environment=frame.shell_environment,
        shell_stack=[frame],
        world_state={
            "entry_host": "ubuntu",
            "active_host": "ubuntu",
            "hosts_by_name": {"ubuntu": {"hostname": "ubuntu"}},
        },
    )


class RecordingAgent:
    def __init__(self, result=None):
        self.result = result or CommandResult(stdout="model output\n")
        self.contexts = []

    def respond(self, context):
        self.contexts.append(deepcopy(context))
        return self.result


class AsyncAgent(RecordingAgent):
    async def respond(self, context):
        return super().respond(context)


class StreamingAgent(RecordingAgent):
    def stream_respond(self, context, control=None):
        result = self.respond(context)
        yield CommandOutputChunk(result.stdout)
        yield CommandStreamResult(result)


def execute(backend, session, command, mode, control=None):
    if mode == "stream":
        events = list(backend.execute_stream(session, command, control))
        finals = [
            event.result for event in events if isinstance(event, CommandStreamResult)
        ]
        if len(finals) != 1:
            raise AssertionError(f"expected one final result, got {len(finals)}")
        return finals[0]
    result = backend.execute(session, command)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


class WholeCommandRoutingTests(unittest.TestCase):
    def test_mixed_lines_reach_agent_once_unchanged_in_all_execution_modes(self):
        for agent_type, mode in (
            (RecordingAgent, "sync"),
            (AsyncAgent, "async"),
            (RecordingAgent, "stream"),
            (StreamingAgent, "stream"),
        ):
            for command in (
                "id; w",
                "  cd /tmp; pwd ; touch testfile  ",
                'export DEST=/tmp; cd "$DEST"; touch testfile',
                "printenv MISSING && id",
                "pwd || id",
                "pwd > out; whoami",
                "pwd | whoami",
                "env ls; pwd",
                "export VALUE=`id`; pwd",
                'printf "%s\\n" "a; b"; id',
                "; ".join(["id"] * 50),
            ):
                with self.subTest(
                    agent=agent_type.__name__, mode=mode, command=command
                ):
                    agent = agent_type()
                    backend = AgentBackend(server_profile(), agent)
                    session = session_state()
                    result = execute(backend, session, command, mode)
                    self.assertEqual([c.command for c in agent.contexts], [command])
                    self.assertEqual(agent.contexts[0].cwd, "/home/alice")
                    self.assertNotIn(
                        "DEST", agent.contexts[0].shell_environment.variables
                    )
                    self.assertEqual(result.stdout, "model output\n")
                    self.assertEqual(
                        [entry.command for entry in session.history], [command]
                    )
                    self.assertEqual(session.cwd, "/home/alice")

    def test_local_chains_use_detached_state_and_no_agent_calls(self):
        for mode in ("sync", "stream"):
            for command, stdout, cwd, status, history_count in (
                ("cd /tmp ; pwd", "/tmp\n", "/tmp", 0, 2),
                ('export DEST=/tmp; cd "$DEST"; pwd', "/tmp\n", "/tmp", 0, 3),
                (
                    "cd /tmp; cd -; pwd",
                    "/home/alice\n/home/alice\n",
                    "/home/alice",
                    0,
                    3,
                ),
                ("printenv MISSING && whoami", "", "/home/alice", 1, 1),
                ("printenv MISSING || whoami", "alice\n", "/home/alice", 0, 2),
                ("cd /root && pwd || whoami", "alice\n", "/home/alice", 0, 2),
                ("pwd;", "/home/alice\n", "/home/alice", 0, 1),
            ):
                with self.subTest(command=command, mode=mode):
                    agent = RecordingAgent()
                    backend = AgentBackend(server_profile(), agent)
                    session = session_state()
                    result = execute(backend, session, command, mode)
                    self.assertEqual(agent.contexts, [])
                    self.assertEqual(result.stdout, stdout)
                    self.assertEqual(result.exit_status, status)
                    self.assertEqual(session.cwd, cwd)
                    self.assertEqual(len(session.history), history_count)
                    self.assertTrue(session.force_session_state_next_command)

    def test_builtin_defer_discards_the_entire_preflight(self):
        for mode in ("sync", "stream"):
            command = "export DEST=/not-established; cd /tmp; cd $DEST; pwd"
            agent = RecordingAgent()
            events = []
            event_bus = SessionEventBus()
            event_bus.subscribe("test", events.append)
            backend = AgentBackend(server_profile(), agent, event_bus=event_bus)
            session = session_state()
            execute(backend, session, command, mode)
            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(agent.contexts[0].command, command)
            self.assertEqual(agent.contexts[0].cwd, "/home/alice")
            self.assertNotIn("DEST", session.shell_environment.variables)
            self.assertEqual(session.cwd, "/home/alice")
            self.assertEqual(len(session.history), 1)
            self.assertEqual(
                [event.event_type for event in events],
                [SessionEventType.COMMAND_ACCEPTED, SessionEventType.COMMAND_COMPLETED],
            )
            history = (
                agent.contexts[0]
                .world_state["hosts_by_name"]["ubuntu"]
                .get("command_history", [])
            )
            self.assertEqual(history, [])

    def test_non_shell_input_is_not_split_into_shell_builtins(self):
        session = session_state()
        program = InteractiveProgram(name="python3", command="python3")
        frame = default_shell_frame(username="alice", interactive_program=program)
        session.shell_stack = [frame]
        session.interactive_program = program
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent)
        backend.execute(session, "pwd; whoami")
        self.assertEqual([c.command for c in agent.contexts], ["pwd; whoami"])

    def test_interrupted_whole_line_applies_no_prefix_or_model_state(self):
        control = CommandExecutionControl()

        class CancellingAgent(RecordingAgent):
            def respond(self, context):
                control.cancel()
                return super().respond(context)

        agent = CancellingAgent(
            CommandResult(state_update=CommandStateUpdate(cwd="/tmp"))
        )
        backend = AgentBackend(server_profile(), agent)
        session = session_state()
        result = execute(backend, session, "cd /tmp; touch file", "stream", control)
        self.assertTrue(result.interrupted)
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(len(session.history), 1)
        self.assertTrue(session.history[0].interrupted)

    def test_ordered_world_effect_publishes_one_materialization(self):
        update = CommandStateUpdate(
            effects=(
                CommandStateEffect(
                    "touch file",
                    CommandStateUpdate(
                        world_state={
                            "hosts_by_name": {
                                "ubuntu": {
                                    "facts_by_id": {"file": {"summary": "exists"}}
                                }
                            }
                        }
                    ),
                ),
            )
        )
        agent = RecordingAgent(CommandResult(state_update=update))
        events = []
        bus = SessionEventBus()
        bus.subscribe("test", events.append)
        backend = AgentBackend(server_profile(), agent, event_bus=bus)
        session = session_state()
        backend.execute(session, "pwd; touch file")
        self.assertEqual(len(session.history), 1)
        completed = [
            e for e in events if e.event_type == SessionEventType.COMMAND_COMPLETED
        ]
        materialized = [
            e for e in events if e.event_type == SessionEventType.STATE_MATERIALIZED
        ]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].payload["state_update_present"])
        self.assertEqual(len(materialized), 1)
        self.assertIn(
            "hosts_by_name", materialized[0].payload["state_update_root_keys"]
        )


class WholeCommandGraphTests(unittest.TestCase):
    def test_local_chain_refreshes_an_existing_checkpoint_before_one_model_call(self):
        class Model:
            def __init__(self):
                self.prompts = []

            def invoke(self, messages, **kwargs):
                self.prompts.append(messages[-1].content)
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "stdout": "model output\n",
                            "stderr": "",
                            "exit_status": 0,
                            "state_update": {},
                        }
                    ),
                    additional_kwargs={},
                )

        for mode in ("sync", "stream"):
            with self.subTest(mode=mode):
                model = Model()
                agent = LangGraphCommandAgent(
                    model_name="test",
                    system_prompt_name="system.md",
                    model=model,
                    callbacks=(),
                )
                backend = AgentBackend(server_profile(), agent)
                session = session_state()
                execute(backend, session, "id", mode)
                execute(backend, session, "cd /tmp; export TEST_CHAIN=kept; pwd", mode)
                self.assertEqual(len(model.prompts), 1)
                command = "  id; w  "
                execute(backend, session, command, mode)
                self.assertEqual(len(model.prompts), 2)
                prompt = model.prompts[-1]
                self.assertTrue(prompt.endswith("current command: " + command))
                self.assertIn("cwd: /tmp", prompt)
                self.assertIn("TEST_CHAIN", prompt)
                self.assertIn("kept", prompt)
                self.assertEqual(session.history[-1].command, command)


if __name__ == "__main__":
    unittest.main()
