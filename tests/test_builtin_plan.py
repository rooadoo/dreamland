import unittest
from copy import deepcopy
from unittest.mock import patch

from dreamland.command_routing import aggregate_builtin_plan, plan_builtin_command_list
from dreamland.models import (
    SessionState,
    default_shell_environment,
    default_shell_frame,
)
from dreamland.session_state import (
    apply_command_state_update,
    validate_command_state_effects,
)


class BuiltinPlanTests(unittest.TestCase):
    def session(self):
        return SessionState(
            username="alice",
            cwd="/home/alice",
            shell_environment=default_shell_environment(username="alice"),
            world_state={
                "active_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {}},
            },
        )

    def test_preparation_is_detached_and_combines_stateful_output(self):
        session = self.session()
        previous = deepcopy(session)

        with patch(
            "dreamland.session_state.reconcile_session_connections",
            side_effect=AssertionError("preparation must not touch connections"),
        ):
            plan = plan_builtin_command_list(
                session, "cd /tmp; export X=one; printenv X; pwd"
            )

        self.assertEqual(session, previous)
        result = aggregate_builtin_plan(plan)
        self.assertEqual(result.stdout, "one\n/tmp\n")
        self.assertEqual(len(result.state_update.effects), 1)
        self.assertEqual(
            result.state_update.effects[0].command, "cd /tmp; export X=one"
        )
        apply_command_state_update(session, result.state_update)
        self.assertEqual(session.cwd, "/tmp")
        self.assertEqual(session.shell_environment.variables["X"], "one")

    def test_ambiguous_builtin_discards_prior_preparation(self):
        session = self.session()
        previous = deepcopy(session)

        plan = plan_builtin_command_list(session, "export X=one; cd unseen; pwd")

        self.assertIsNone(plan)
        self.assertEqual(session, previous)

    def test_any_syntactic_nonbuiltin_prevents_local_evaluation(self):
        for command in (
            "export X=one; touch file",
            "printenv MISSING && ls",
            "pwd || ls",
            "exit; ls",
            "pwd; env ls",
            "pwd | cat",
        ):
            with self.subTest(command=command):
                with patch(
                    "dreamland.agent_core.handle_builtin_command_context"
                ) as handle:
                    self.assertIsNone(
                        plan_builtin_command_list(self.session(), command)
                    )
                handle.assert_not_called()

    def test_short_circuit_skips_ambiguous_cd_when_not_executed(self):
        plan = plan_builtin_command_list(
            self.session(), "printenv MISSING && cd unseen || whoami"
        )

        self.assertEqual(
            [command for command, _ in plan], ["printenv MISSING", "whoami"]
        )
        self.assertEqual(aggregate_builtin_plan(plan).stdout, "alice\n")

    def test_repeated_directory_changes_keep_last_oldpwd(self):
        session = self.session()
        plan = plan_builtin_command_list(session, "cd /tmp; cd /var/log; cd -; pwd")
        result = aggregate_builtin_plan(plan)

        apply_command_state_update(session, result.state_update)

        self.assertEqual(result.stdout, "/tmp\n/tmp\n")
        self.assertEqual(session.cwd, "/tmp")
        self.assertEqual(session.shell_environment.variables["OLDPWD"], "/var/log")

    def test_outer_exit_stops_list_and_sets_shell_exit_metadata(self):
        plan = plan_builtin_command_list(self.session(), "pwd; exit; whoami")
        result = aggregate_builtin_plan(plan)

        self.assertEqual([command for command, _ in plan], ["pwd", "exit"])
        self.assertEqual(result.stdout, "/home/alice\n")
        self.assertIs(result.metadata.get("dreamland.shell_exit"), True)

    def test_nested_exit_keeps_parent_state_and_does_not_exit_session(self):
        session = self.session()
        parent = default_shell_frame(username="alice", cwd="/var/log")
        child = default_shell_frame(username="alice", cwd="/home/alice")
        session.shell_stack = [parent, child]
        plan = plan_builtin_command_list(session, "export X=one; cd /tmp; exit; pwd")
        result = aggregate_builtin_plan(plan)

        self.assertEqual(len(result.state_update.effects), 2)
        self.assertEqual(
            result.state_update.effects[
                -1
            ].state_update.shell_stack_transition.operation,
            "pop",
        )
        self.assertNotIn("dreamland.shell_exit", result.metadata)
        self.assertEqual(
            [command for command, _ in plan], ["export X=one", "cd /tmp", "exit"]
        )
        apply_command_state_update(session, result.state_update)
        self.assertEqual(session.shell_stack, [parent])
        self.assertEqual(session.cwd, "/var/log")
        self.assertNotIn("X", session.shell_environment.variables)

    def test_long_builtin_list_compacts_state_without_calling_model(self):
        session = self.session()
        command = "; ".join(f"export X{index}={index}" for index in range(80))
        plan = plan_builtin_command_list(session, command)
        result = aggregate_builtin_plan(plan)

        self.assertEqual(len(plan), 80)
        self.assertEqual(len(result.state_update.effects), 1)
        validate_command_state_effects(result.state_update)
        apply_command_state_update(session, result.state_update)
        for index in range(80):
            self.assertEqual(
                session.shell_environment.variables[f"X{index}"], str(index)
            )


if __name__ == "__main__":
    unittest.main()
