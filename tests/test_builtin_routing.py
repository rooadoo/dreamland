import unittest

from dreamland.agent_builtins import (
    handle_builtin_command_if_deterministic,
    is_builtin_command,
)
from dreamland.models import InteractiveProgram, default_interactive_program


class BuiltinRoutingTests(unittest.TestCase):
    def state(self, command, *, history=()):
        return {
            "username": "alice",
            "cwd": "/home/alice",
            "current_command": command,
            "interactive_program": {"name": "bash", "command": "/bin/bash"},
            "shell_environment": {
                "variables": {
                    "HOME": "/home/alice",
                    "PWD": "/home/alice",
                    "SHELL": "/bin/bash",
                    "X": "expanded",
                },
                "exported": ["HOME", "PWD", "SHELL", "X"],
            },
            "world_state": {
                "active_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {"command_history": list(history)}},
            },
        }

    def test_supported_forms_remain_local(self):
        for command in (
            "pwd",
            "whoami",
            "cd /tmp",
            "cd -",
            "env",
            "printenv HOME",
            "export X=one",
            "unset X",
            "X=one",
            "set",
            "declare",
            "declare -p X",
            "declare -x X=one",
            "exit",
            "logout",
            "X=one env",
            "env X=one env printenv X",
            'export X=";"',
        ):
            with self.subTest(command=command):
                self.assertTrue(
                    is_builtin_command(command, default_interactive_program())
                )

    def test_compound_and_unsupported_forms_do_not_dispatch_first_builtin(self):
        for command in (
            "pwd; whoami",
            "pwd && ls",
            "pwd | cat",
            "pwd > path",
            "pwd &",
            "pwd\nwhoami",
            "pwd # comment",
            "pwd 'unfinished",
            "env ls",
            "env env ls",
            "env X=one touch file",
            "env -i",
            "pwd -P",
            "whoami --help",
            "cd -P /tmp",
            "set -e",
            "declare -a X",
            "export -n X",
            "unset -f X",
            "printenv --null",
            "exit 7",
            "export X=$(pwd)",
            "export X=`pwd`",
            "cd *",
            "cd ${X:-/tmp}",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    is_builtin_command(command, default_interactive_program())
                )
                self.assertIsNone(
                    handle_builtin_command_if_deterministic(self.state(command))
                )

    def test_literal_expansions_defer_without_changing_their_meaning(self):
        for command in ("export X='$HOME'", r"export X=\$HOME", 'cd "~"', "cd '$HOME'"):
            with self.subTest(command=command):
                self.assertIsNone(
                    handle_builtin_command_if_deterministic(self.state(command))
                )

    def test_quoted_separator_value_is_preserved_by_builtin(self):
        result = handle_builtin_command_if_deterministic(self.state('export X="a; b"'))

        self.assertIsNotNone(result)
        self.assertEqual(result.state_update.shell_environment.variables["X"], "a; b")

    def test_shell_commands_are_not_local_inside_other_programs(self):
        program = InteractiveProgram(name="python", command="python3")
        for command in ("pwd", "cd /tmp", "env", "exit"):
            with self.subTest(command=command):
                self.assertFalse(is_builtin_command(command, program))

    def test_ambiguous_cd_defers_and_known_cd_is_deterministic(self):
        self.assertIsNone(
            handle_builtin_command_if_deterministic(self.state("cd unseen"))
        )
        result = handle_builtin_command_if_deterministic(self.state("cd /tmp"))

        self.assertEqual(result.state_update.cwd, "/tmp")

    def test_compound_or_redirected_history_does_not_supply_path_hints(self):
        for command, stdout in (
            ("pwd; whoami", "/home/alice/unseen\n"),
            ("pwd > output", "/home/alice/unseen\n"),
            ("cd unseen; pwd", "/home/alice/unseen\n"),
            (
                "ls -l; echo extra",
                "drwxr-xr-x 2 alice users 4096 Sep 11 12:00 unseen\n",
            ),
            ("ls -l | cat", "drwxr-xr-x 2 alice users 4096 Sep 11 12:00 unseen\n"),
        ):
            with self.subTest(command=command):
                state = self.state(
                    "cd unseen",
                    history=[
                        {
                            "cwd": "/home/alice",
                            "command": command,
                            "stdout": stdout,
                            "exit_status": 0,
                        }
                    ],
                )
                self.assertIsNone(handle_builtin_command_if_deterministic(state))

    def test_simple_history_still_supplies_path_hints(self):
        state = self.state(
            "cd unseen",
            history=[
                {
                    "cwd": "/home/alice",
                    "command": "ls -l",
                    "stdout": "drwxr-xr-x 2 alice users 4096 Sep 11 12:00 unseen\n",
                    "exit_status": 0,
                }
            ],
        )

        result = handle_builtin_command_if_deterministic(state)

        self.assertEqual(result.state_update.cwd, "/home/alice/unseen")


if __name__ == "__main__":
    unittest.main()
