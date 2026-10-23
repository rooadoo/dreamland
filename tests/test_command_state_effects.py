import importlib.util
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from dreamland.agent_core import (
    DeterministicCommandAgent,
    LangGraphCommandAgent,
    _command_state_updates_from_session,
    _result_from_state,
    _result_to_state,
)
from dreamland.agent_state import shell_environment_to_state
from dreamland.agent_structured import command_result_from_model_response
from dreamland.models import (
    CommandContext,
    CommandExecutionControl,
    CommandStateEffect,
    CommandStateUpdate,
    CommandStreamResult,
    HostIdentity,
    SessionState,
    ShellEnvironment,
    ShellStackTransition,
    default_shell_frame,
)
from dreamland.session_state import apply_command_state_update, pop_active_shell_frame


def _session():
    frame = default_shell_frame(username="alice", hostname="host-a")
    return SessionState(
        username=frame.username,
        cwd=frame.cwd,
        host_identity=frame.host_identity,
        interactive_program=frame.interactive_program,
        shell_environment=frame.shell_environment,
        shell_stack=[frame],
        world_state={
            "entry_host": "host-a",
            "active_host": "host-a",
            "hosts_by_name": {
                "host-a": {
                    "hostname": "host-a",
                    "network": {"ipv4_address": "10.0.0.1"},
                },
                "host-b": {
                    "hostname": "host-b",
                    "network": {"ipv4_address": "10.0.0.2"},
                },
            },
        },
    )


def _parse(update, session=None):
    return command_result_from_model_response(
        json.dumps(
            {"stdout": "", "stderr": "", "exit_status": 0, "state_update": update}
        ),
        _command_state_updates_from_session(session or _session()),
    )


def _effect(command, **update):
    return {"command": command, "state_update": update}


class CommandStateEffectsTests(unittest.TestCase):
    def test_direct_deterministic_agent_executes_builtin_only_list(self):
        session = _session()
        context = CommandContext(
            username="alice",
            cwd=session.cwd,
            hostname="host-a",
            command="cd /tmp ; pwd && whoami",
            langfuse_trace_id="",
            shell_environment=session.shell_environment,
            shell_stack=tuple(session.shell_stack),
            host_identity=session.host_identity,
            world_state=session.world_state,
        )
        result = DeterministicCommandAgent().respond(context)
        self.assertEqual(result.stdout, "/tmp\nalice\n")
        apply_command_state_update(
            session, result.state_update, reconcile_network=False
        )
        self.assertEqual(session.cwd, "/tmp")

    def test_parent_cwd_is_preserved_across_ssh_and_logout(self):
        session = _session()
        environment = shell_environment_to_state(session.shell_environment)
        environment["variables"].update({"PWD": "/tmp", "OLDPWD": session.cwd})
        result = _parse(
            {
                "effects": [
                    _effect("cd /tmp", cwd="/tmp", shell_environment=environment),
                    _effect(
                        "ssh host-b",
                        host_identity={"hostname": "host-b"},
                        cwd="/home/alice",
                        shell_stack_transition={"operation": "push"},
                    ),
                ]
            },
            session,
        )

        apply_command_state_update(
            session, result.state_update, command="cd /tmp; ssh host-b"
        )

        self.assertEqual(session.host_identity, HostIdentity("host-b"))
        self.assertEqual(session.shell_stack[0].cwd, "/tmp")
        self.assertEqual(len(session.network_connections.connections), 1)
        self.assertTrue(pop_active_shell_frame(session))
        self.assertEqual(session.cwd, "/tmp")
        self.assertEqual(session.shell_environment.variables["PWD"], "/tmp")
        self.assertEqual(session.shell_environment.variables["OLDPWD"], "/home/alice")
        self.assertEqual(session.host_identity, HostIdentity("host-a"))
        self.assertFalse(session.network_connections.connections)

    def test_export_before_push_changes_authoritative_parent_environment(self):
        session = _session()
        environment = shell_environment_to_state(session.shell_environment)
        environment["variables"]["X"] = "1"
        environment["exported"].append("X")
        result = _parse(
            {
                "effects": [
                    _effect("export X=1", shell_environment=environment),
                    _effect("bash", shell_stack_transition={"operation": "push"}),
                ]
            },
            session,
        )
        apply_command_state_update(session, result.state_update)
        self.assertEqual(session.shell_environment.variables["X"], "1")
        self.assertTrue(pop_active_shell_frame(session))
        self.assertEqual(session.shell_environment.variables["X"], "1")
        self.assertIn("X", session.shell_environment.exported)

    def test_pop_and_later_effect_update_the_restored_hidden_parent(self):
        session = _session()
        apply_command_state_update(
            session,
            CommandStateUpdate(
                cwd="/root",
                shell_stack_transition=ShellStackTransition("push", username="root"),
            ),
            reconcile_network=False,
        )
        result = _parse(
            {
                "effects": [
                    _effect("exit", shell_stack_transition={"operation": "pop"}),
                    _effect("cd /tmp", cwd="/tmp"),
                ]
            },
            session,
        )
        apply_command_state_update(
            session, result.state_update, reconcile_network=False
        )
        self.assertEqual(session.username, "alice")
        self.assertEqual(session.cwd, "/tmp")
        self.assertEqual(len(session.shell_stack), 1)

    def test_implicit_program_transition_uses_preceding_effect_context(self):
        result = _parse(
            {
                "effects": [
                    _effect(
                        "ssh host-b",
                        interactive_program={"name": "ssh", "command": "/usr/bin/ssh"},
                        shell_stack_transition={"operation": "push"},
                    ),
                    _effect(
                        "authentication",
                        interactive_program={"name": "bash", "command": "/bin/bash"},
                    ),
                ]
            }
        )
        self.assertEqual(
            result.state_update.effects[
                1
            ].state_update.shell_stack_transition.operation,
            "replace_active",
        )

    def test_effect_normalizations_remain_visible_in_result_metadata(self):
        result = _parse(
            {
                "effects": [
                    _effect(
                        "ssh host-b",
                        interactive_program={"name": "ssh", "command": "/usr/bin/ssh"},
                    ),
                    _effect(
                        "authentication",
                        interactive_program={"name": "bash", "command": "/bin/bash"},
                    ),
                ]
            }
        )

        self.assertEqual(
            result.metadata["dreamland.transition.normalized"],
            "effects[0]:implicit_program_push;effects[1]:active_ssh_program_replace_active",
        )
        restored = _result_from_state(_result_to_state(result))
        self.assertEqual(restored.metadata, result.metadata)

        explicit = _parse(
            {
                "effects": [
                    _effect("bash", shell_stack_transition={"operation": "push"}),
                ]
            }
        )
        self.assertNotIn("dreamland.transition.normalized", explicit.metadata)

    def test_checkpoint_codec_preserves_all_effects_and_detaches_mutable_fields(self):
        result = _parse(
            {
                "effects": [
                    _effect("cd /tmp", cwd="/tmp"),
                    _effect(
                        "touch file",
                        world_state={"facts_by_id": {"created": {"path": "/tmp/file"}}},
                    ),
                ]
            }
        )
        state = _result_to_state(result)
        restored = _result_from_state(state)
        self.assertEqual(restored, result)
        state["effects"][1]["state_update"]["world_state"]["facts_by_id"].clear()
        self.assertIn(
            "created",
            restored.state_update.effects[1].state_update.world_state["facts_by_id"],
        )

    def test_legacy_ordinary_state_update_still_parses(self):
        result = _parse(
            {"cwd": "/tmp", "shell_stack_transition": {"operation": "push"}}
        )
        self.assertEqual(result.state_update.cwd, "/tmp")
        self.assertEqual(result.state_update.effects, ())
        self.assertEqual(result.state_update.shell_stack_transition.operation, "push")

    def test_malformed_sequence_rejects_every_effect(self):
        valid = _effect("cd /tmp", cwd="/tmp")
        malformed = [
            {"effects": []},
            {"effects": [valid], "cwd": "/root"},
            {"effects": "cd /tmp"},
            {"effects": [valid, {"command": "next"}]},
            {"effects": [valid, _effect("next", effects=[valid])]},
            {"effects": [valid, _effect("next", shell_stack=[])]},
            {"effects": [valid, _effect("next", cwd=123)]},
            {
                "effects": [
                    valid,
                    _effect("next", shell_stack_transition={"operation": "invalid"}),
                ]
            },
            {
                "effects": [
                    valid,
                    _effect(
                        "next", shell_environment={"variables": [], "exported": []}
                    ),
                ]
            },
            {"effects": [valid, _effect("next", interactive_program={"name": "bash"})]},
            {"effects": [valid, _effect("next", world_state=[])]},
            {"effects": [valid, _effect(" ", cwd="/root")]},
            {"effects": [valid] * 65},
            {
                "effects": [
                    _effect("exit", shell_stack_transition={"operation": "pop"}),
                    valid,
                ]
            },
        ]
        for update in malformed:
            with self.subTest(update=update):
                session = _session()
                before = deepcopy(session)
                result = _parse(update, session)
                self.assertEqual(result.state_update, CommandStateUpdate())
                self.assertFalse(result.metadata["structured_response_accepted"])
                self.assertFalse(result.metadata["dreamland.state_effects.accepted"])
                self.assertEqual(session, before)

    def test_malformed_typed_later_effect_cannot_commit_a_prefix(self):
        invalid_updates = (
            CommandStateUpdate(cwd=123),
            CommandStateUpdate(effects=[]),
            CommandStateUpdate(shell_environment="invalid"),
            CommandStateUpdate(shell_environment=ShellEnvironment(variables={"X": 1})),
            CommandStateUpdate(shell_stack_transition=ShellStackTransition("invalid")),
            CommandStateUpdate(
                effects=(CommandStateEffect("nested", CommandStateUpdate()),)
            ),
        )
        for invalid in invalid_updates:
            with self.subTest(update=invalid):
                session = _session()
                before = deepcopy(session)
                with self.assertRaises(ValueError):
                    apply_command_state_update(
                        session,
                        CommandStateUpdate(
                            effects=(
                                CommandStateEffect(
                                    "cd /tmp", CommandStateUpdate(cwd="/tmp")
                                ),
                                CommandStateEffect("invalid", invalid),
                            )
                        ),
                    )
                self.assertEqual(session, before)

    def test_effect_annotations_do_not_execute_commands(self):
        session = _session()
        result = _parse({"effects": [_effect("cd /tmp; export X=1")]}, session)
        apply_command_state_update(session, result.state_update)
        self.assertEqual(session.cwd, "/home/alice")
        self.assertNotIn("X", session.shell_environment.variables)

    def test_detached_effect_application_does_not_allocate_connections(self):
        session = _session()
        result = _parse(
            {
                "effects": [
                    _effect(
                        "ssh host-b",
                        host_identity={"hostname": "host-b"},
                        shell_stack_transition={"operation": "push"},
                    )
                ]
            },
            session,
        )
        apply_command_state_update(
            session, result.state_update, reconcile_network=False
        )
        self.assertEqual(len(session.shell_stack), 2)
        self.assertFalse(session.network_connections.connections)
        self.assertFalse(session.network_connections.frames)


@unittest.skipUnless(
    importlib.util.find_spec("langgraph"), "LangGraph is not installed"
)
class CommandStateEffectGraphTests(unittest.TestCase):
    def test_direct_graph_builtin_chain_uses_no_model_for_regular_or_streaming(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                session = _session()
                model = _RecordingModel("unexpected model invocation")
                agent = LangGraphCommandAgent(
                    model_name="test", system_prompt_name="system.md", model=model
                )
                context = CommandContext(
                    username="alice",
                    cwd=session.cwd,
                    hostname="host-a",
                    command="cd /tmp ; pwd && whoami",
                    langfuse_trace_id="builtin-effects-test",
                    shell_environment=session.shell_environment,
                    shell_stack=tuple(session.shell_stack),
                    host_identity=session.host_identity,
                    world_state=session.world_state,
                )
                if streaming:
                    events = list(agent.stream_respond(context))
                    result = next(
                        event.result
                        for event in reversed(events)
                        if isinstance(event, CommandStreamResult)
                    )
                else:
                    result = agent.respond(context)
                self.assertFalse(model.messages)
                self.assertEqual(result.stdout, "/tmp\nalice\n")
                self.assertEqual(result.exit_status, 0)
                checkpoint = agent.graph.get_state(
                    {"configurable": {"thread_id": context.langfuse_trace_id}}
                ).values
                self.assertEqual(checkpoint["cwd"], "/tmp")
                self.assertEqual(
                    checkpoint["shell_environment"]["variables"]["PWD"], "/tmp"
                )

    def test_cancelled_stream_discards_every_effect_from_result_and_checkpoint(self):
        session = _session()
        control = CommandExecutionControl()
        model = _RecordingModel(
            json.dumps(
                {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {
                        "effects": [
                            _effect("cd /tmp", cwd="/tmp"),
                            _effect(
                                "bash", shell_stack_transition={"operation": "push"}
                            ),
                        ]
                    },
                }
            )
        )
        model.on_invoke = control.cancel
        agent = LangGraphCommandAgent(
            model_name="test", system_prompt_name="system.md", model=model
        )
        context = CommandContext(
            username="alice",
            cwd=session.cwd,
            hostname="host-a",
            command="cd /tmp; bash",
            langfuse_trace_id="cancelled-effects-test",
            shell_environment=session.shell_environment,
            shell_stack=tuple(session.shell_stack),
            host_identity=session.host_identity,
            world_state=session.world_state,
        )
        events = list(agent.stream_respond(context, control=control))
        result = next(
            event.result
            for event in reversed(events)
            if isinstance(event, CommandStreamResult)
        )
        self.assertTrue(result.interrupted)
        self.assertEqual(result.state_update, CommandStateUpdate())
        checkpoint = agent.graph.get_state(
            {"configurable": {"thread_id": context.langfuse_trace_id}}
        ).values
        self.assertEqual(checkpoint["cwd"], "/home/alice")
        self.assertEqual(len(checkpoint["shell_stack"]), 1)

    def test_regular_and_streaming_graphs_keep_parent_effects_and_one_history_entry(
        self,
    ):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                session = _session()
                environment = shell_environment_to_state(session.shell_environment)
                environment["variables"]["X"] = "1"
                environment["exported"].append("X")
                payload = {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {
                        "effects": [
                            _effect("export X=1", shell_environment=environment),
                            _effect(
                                "bash", shell_stack_transition={"operation": "push"}
                            ),
                        ]
                    },
                }
                model = _RecordingModel(json.dumps(payload))
                agent = LangGraphCommandAgent(
                    model_name="test", system_prompt_name="system.md", model=model
                )
                command = "  export X=1 ; bash  "
                context = CommandContext(
                    username=session.username,
                    cwd=session.cwd,
                    hostname="host-a",
                    command=command,
                    langfuse_trace_id="ordered-effects-test",
                    shell_environment=session.shell_environment,
                    shell_stack=tuple(session.shell_stack),
                    host_identity=session.host_identity,
                    world_state=session.world_state,
                )
                if streaming:
                    events = list(agent.stream_respond(context))
                    result = next(
                        event.result
                        for event in reversed(events)
                        if isinstance(event, CommandStreamResult)
                    )
                else:
                    result = agent.respond(context)
                self.assertEqual(len(model.messages), 1)
                self.assertIn(
                    f"current command: {command}", model.messages[0][-1].content
                )
                self.assertEqual(len(result.state_update.effects), 2)
                checkpoint = agent.graph.get_state(
                    {"configurable": {"thread_id": context.langfuse_trace_id}}
                ).values
                self.assertEqual(
                    checkpoint["shell_stack"][0]["shell_environment"]["variables"]["X"],
                    "1",
                )
                self.assertEqual(len(checkpoint["shell_stack"]), 2)
                history = checkpoint["world_state"]["hosts_by_name"]["host-a"][
                    "command_history"
                ]
                self.assertEqual([entry["command"] for entry in history], [command])
                model.response = json.dumps(
                    {
                        "stdout": "",
                        "stderr": "",
                        "exit_status": 0,
                        "state_update": {
                            "shell_stack_transition": {"operation": "pop"}
                        },
                    }
                )
                # The graph must use its saved parent rather than the stale initial context.
                agent.respond(replace(context, command="exit"))
                checkpoint = agent.graph.get_state(
                    {"configurable": {"thread_id": context.langfuse_trace_id}}
                ).values
                self.assertEqual(len(checkpoint["shell_stack"]), 1)
                self.assertEqual(checkpoint["shell_environment"]["variables"]["X"], "1")


class _RecordingModel:
    def __init__(self, response):
        self.response = response
        self.messages = []
        self.on_invoke = None

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        if self.on_invoke is not None:
            self.on_invoke()
        return SimpleNamespace(content=self.response)


if __name__ == "__main__":
    unittest.main()
