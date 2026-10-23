import logging
import unittest

from dreamland.backend import AgentBackend
from dreamland.models import (
    CommandResult,
    CommandStateEffect,
    CommandStateUpdate,
    HostIdentity,
    InteractiveProgram,
    ShellFrame,
    ShellStackTransition,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import configure_session_trace_logging
from dreamland.ssh_session_state import (
    apply_nested_exit_fallback,
    pop_nested_shell_for_transport_exit,
    should_close_shell_session,
)


class SSHSessionStateTests(unittest.TestCase):
    def test_executed_builtin_root_exit_closes_compound_input(self):
        for command in (
            "pwd; exit",
            "exit; pwd",
            "cd /tmp; logout",
            "printenv __MISSING__ || exit",
        ):
            with self.subTest(command=command):
                backend = AgentBackend(server_profile())
                session = backend.create_session("alice")
                starting_stack = tuple(session.shell_stack)
                result = backend.execute(session, command)
                self.assertTrue(
                    should_close_shell_session(command, result, starting_stack)
                )

    def test_skipped_and_nested_builtin_exits_do_not_close_outer_session(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")
        starting_stack = tuple(session.shell_stack)
        command = "printenv __MISSING__ && exit"
        result = backend.execute(session, command)
        self.assertFalse(should_close_shell_session(command, result, starting_stack))
        session.shell_stack.append(ShellFrame(username="root", cwd="/root"))
        starting_stack = tuple(session.shell_stack)
        result = backend.execute(session, "pwd; exit")
        self.assertEqual(len(session.shell_stack), 1)
        self.assertFalse(
            should_close_shell_session("pwd; exit", result, starting_stack)
        )

    def test_model_root_pop_closes_only_the_outer_shell(self):
        root = ShellFrame(username="alice", cwd="/home/alice")
        nested = ShellFrame(username="root", cwd="/root")
        result = CommandResult(
            state_update=CommandStateUpdate(
                shell_stack_transition=ShellStackTransition("pop")
            )
        )
        self.assertTrue(should_close_shell_session("id; exit", result, (root,)))
        self.assertFalse(should_close_shell_session("id; exit", result, (root, nested)))

    def test_ordered_effects_track_shell_depth_before_final_root_pop(self):
        root = ShellFrame(username="alice", cwd="/home/alice")
        pop = CommandStateUpdate(shell_stack_transition=ShellStackTransition("pop"))
        for effects, closes in (
            ((CommandStateEffect("exit", pop),), True),
            (
                (
                    CommandStateEffect(
                        "bash",
                        CommandStateUpdate(
                            shell_stack_transition=ShellStackTransition("push")
                        ),
                    ),
                    CommandStateEffect("exit", pop),
                ),
                False,
            ),
            (
                (
                    CommandStateEffect(
                        "bash",
                        CommandStateUpdate(
                            shell_stack_transition=ShellStackTransition("push")
                        ),
                    ),
                    CommandStateEffect("exit", pop),
                    CommandStateEffect("exit", pop),
                ),
                True,
            ),
            (
                (
                    CommandStateEffect("exit", pop),
                    CommandStateEffect("cd /tmp", CommandStateUpdate(cwd="/tmp")),
                ),
                False,
            ),
        ):
            with self.subTest(effects=effects):
                result = CommandResult(state_update=CommandStateUpdate(effects=effects))
                self.assertEqual(
                    should_close_shell_session("compound-input", result, (root,)),
                    closes,
                )

    def test_interrupted_or_rejected_exit_results_do_not_close_or_pop(self):
        pop = CommandStateUpdate(shell_stack_transition=ShellStackTransition("pop"))
        effects = CommandStateUpdate(effects=(CommandStateEffect("exit", pop),))
        for result in (
            CommandResult(interrupted=True),
            CommandResult(interrupted=True, state_update=effects),
            CommandResult(interrupted=True, metadata={"dreamland.shell_exit": True}),
            CommandResult(metadata={"dreamland.state_effects.accepted": False}),
            CommandResult(
                state_update=effects,
                metadata={"dreamland.transition.accepted": False},
            ),
        ):
            with self.subTest(result=result):
                backend = AgentBackend(server_profile())
                session = backend.create_session("alice")
                self.assertFalse(
                    should_close_shell_session(
                        "exit", result, tuple(session.shell_stack)
                    )
                )
                session.shell_stack.append(ShellFrame(username="root", cwd="/root"))
                starting_stack = tuple(session.shell_stack)
                apply_nested_exit_fallback(session, "exit", result, starting_stack)
                self.assertEqual(tuple(session.shell_stack), starting_stack)

    def test_ordered_effects_do_not_trigger_legacy_nested_exit_fallback(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")
        session.shell_stack.append(ShellFrame(username="root", cwd="/root"))
        starting_stack = tuple(session.shell_stack)
        result = CommandResult(
            state_update=CommandStateUpdate(
                effects=(CommandStateEffect("exit", CommandStateUpdate()),)
            )
        )
        apply_nested_exit_fallback(session, "exit", result, starting_stack)
        self.assertEqual(tuple(session.shell_stack), starting_stack)

    def test_program_exit_and_replacement_do_not_close_the_shell(self):
        python = ShellFrame(
            username="alice",
            cwd="/home/alice",
            interactive_program=InteractiveProgram("python3", "python3"),
        )
        pop = CommandStateUpdate(shell_stack_transition=ShellStackTransition("pop"))
        for update in (
            pop,
            CommandStateUpdate(effects=(CommandStateEffect("exit()", pop),)),
        ):
            self.assertFalse(
                should_close_shell_session(
                    "exit()", CommandResult(state_update=update), (python,)
                )
            )
        root = ShellFrame(username="alice", cwd="/home/alice")
        self.assertFalse(
            should_close_shell_session(
                "exit",
                CommandResult(
                    state_update=CommandStateUpdate(
                        interactive_program=python.interactive_program,
                        shell_stack_transition=ShellStackTransition("replace_active"),
                    )
                ),
                (root,),
            )
        )

    def test_exit_with_shell_stack_update_does_not_close_shell_session(self):
        result = CommandResult(
            state_update=CommandStateUpdate(
                shell_stack=(ShellFrame(username="alice", cwd="/home/alice"),)
            )
        )

        self.assertFalse(should_close_shell_session("exit", result))
        self.assertFalse(should_close_shell_session("logout", result))
        self.assertTrue(should_close_shell_session("exit", CommandResult()))
        self.assertFalse(
            should_close_shell_session(
                "exit",
                CommandResult(),
                (
                    ShellFrame(username="alice", cwd="/home/alice"),
                    ShellFrame(username="root", cwd="/root"),
                ),
            )
        )

    def test_nested_exit_fallback_restores_parent_when_result_lacks_stack_update(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(_trace_formatter())
        profile = server_profile()
        backend = AgentBackend(profile)
        session = backend.create_session("alice")
        root_frame = ShellFrame(
            username="root",
            cwd="/root",
            host_identity=HostIdentity(hostname="swift-gw01"),
        )
        starting_stack = (*session.shell_stack, root_frame)
        session.shell_stack = list(starting_stack)
        session.username = "root"
        session.cwd = "/root"
        session.host_identity = HostIdentity(hostname="swift-gw01")

        apply_nested_exit_fallback(
            session,
            "exit",
            CommandResult(),
            starting_stack,
        )

        self.assertEqual(session.username, "alice")
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(len(session.shell_stack), 1)
        self.assertTrue(session.force_session_state_next_command)
        records = [
            record.text for record in trace_logs.latest(session.langfuse_trace_id, 20)
        ]
        self.assertTrue(
            any(
                "type=shell_stack event=nested_exit_fallback_applied" in record
                and "marker=shell-stack-exit-v1" in record
                and "command='exit'" in record
                for record in records
            ),
            records,
        )

    def test_nested_exit_fallback_does_not_pop_non_shell_program(self):
        profile = server_profile()
        backend = AgentBackend(profile)
        session = backend.create_session("alice")
        program_frame = ShellFrame(
            username="alice",
            cwd="/home/alice",
            interactive_program=InteractiveProgram(
                name="custom-console",
                command="/opt/custom-console",
                argv=("custom-console",),
            ),
        )
        starting_stack = (*session.shell_stack, program_frame)
        session.shell_stack = list(starting_stack)

        apply_nested_exit_fallback(
            session,
            "exit",
            CommandResult(),
            starting_stack,
        )

        self.assertEqual(tuple(session.shell_stack), starting_stack)
        self.assertFalse(
            should_close_shell_session("exit", CommandResult(), starting_stack)
        )

    def test_transport_ctrl_d_pop_restores_parent_nested_shell(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(_trace_formatter())
        profile = server_profile()
        backend = AgentBackend(profile)
        session = backend.create_session("alice")
        parent_frame = session.shell_stack[-1]
        parent_frame = ShellFrame(
            username=parent_frame.username,
            cwd=parent_frame.cwd,
            interactive_program=parent_frame.interactive_program,
            shell_environment=parent_frame.shell_environment,
            host_identity=HostIdentity(hostname="rpi-admin01"),
        )
        root_frame = ShellFrame(
            username="root",
            cwd="/root",
            host_identity=HostIdentity(hostname="swift-gw01"),
        )
        session.shell_stack = [parent_frame]
        session.host_identity = HostIdentity(hostname="rpi-admin01")
        session.shell_stack.append(root_frame)
        session.username = "root"
        session.cwd = "/root"
        session.host_identity = HostIdentity(hostname="swift-gw01")

        popped = pop_nested_shell_for_transport_exit(session)

        self.assertTrue(popped)
        self.assertEqual(session.username, "alice")
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(session.host_identity.hostname, "rpi-admin01")
        self.assertEqual(len(session.shell_stack), 1)
        self.assertTrue(session.force_session_state_next_command)
        records = [
            record.text for record in trace_logs.latest(session.langfuse_trace_id, 20)
        ]
        self.assertTrue(
            any(
                "type=shell_stack event=transport_exit_pop" in record
                and "marker=shell-stack-exit-v1" in record
                and "previous_host=swift-gw01" in record
                and "next_host=rpi-admin01" in record
                for record in records
            ),
            records,
        )
        self.assertFalse(pop_nested_shell_for_transport_exit(session))

    def test_transport_interrupt_pops_only_active_dialogue_and_logs_transition(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(_trace_formatter())
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")
        parent = ShellFrame(username="root", cwd="/root")
        dialogue = ShellFrame(
            username="root",
            cwd="/root",
            interactive_program=InteractiveProgram(
                "ssh-keygen", "ssh-keygen", metadata={"phase": "passphrase"}
            ),
        )
        session.shell_stack.extend((parent, dialogue))
        session.interactive_program = dialogue.interactive_program
        session.username = "root"
        session.cwd = "/root"

        self.assertTrue(pop_nested_shell_for_transport_exit(session, signal="SIGINT"))

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.shell_stack[-1], parent)
        self.assertEqual(session.interactive_program.name, "bash")
        self.assertEqual(session.username, "root")
        self.assertTrue(session.force_session_state_next_command)
        records = [
            record.text for record in trace_logs.latest(session.langfuse_trace_id, 20)
        ]
        self.assertTrue(
            any(
                "event=transport_interrupt_pop" in record
                and "previous_depth=3 next_depth=2" in record
                for record in records
            ),
            records,
        )


def _trace_formatter():
    return logging.Formatter("%(message)s")


if __name__ == "__main__":
    unittest.main()
