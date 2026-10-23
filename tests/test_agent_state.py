import unittest

from dreamland.agent_state import (
    environment_metadata_from_state,
    environment_metadata_to_state,
    interactive_program_from_state,
    interactive_program_to_state,
    shell_environment_from_state,
    shell_environment_to_state,
)
from dreamland.models import (
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    InteractiveProgram,
    ShellEnvironment,
)


class AgentStateTests(unittest.TestCase):
    def test_shell_environment_round_trip(self):
        environment = ShellEnvironment(
            variables={"LOCAL": "one", "EXPORTED": "two"},
            exported=frozenset({"EXPORTED"}),
        )
        restored = shell_environment_from_state(shell_environment_to_state(environment))
        self.assertEqual(restored, environment)

    def test_interactive_program_round_trip_preserves_prompt(self):
        program = InteractiveProgram(
            name="redis",
            command="/usr/bin/redis-cli",
            argv=("redis-cli",),
            prompt_template="127.0.0.1:6379> ",
        )
        restored = interactive_program_from_state(interactive_program_to_state(program))
        self.assertEqual(restored, program)

    def test_environment_metadata_round_trip_preserves_connection(self):
        metadata = EnvironmentMetadata(
            connection=ConnectionMetadata(
                protocol="ssh",
                source=ConnectionEndpoint(ip="203.0.113.5", port=51234),
                destination=ConnectionEndpoint(ip="10.0.0.10", port=2222),
                metadata={"producer": "test"},
            ),
            metadata={"scope": "test"},
        )
        restored = environment_metadata_from_state(
            environment_metadata_to_state(metadata)
        )
        self.assertEqual(restored, metadata)

    def test_model_display_labels_decode_as_interactive_program(self):
        restored = interactive_program_from_state(
            {
                "program name": "python3",
                "program command": "python3",
                "program argv": [],
            }
        )

        self.assertEqual(restored.name, "python")
        self.assertEqual(restored.command, "python3")
        self.assertEqual(restored.argv, ())
        self.assertEqual(restored.prompt_template, ">>> ")

    def test_flat_model_environment_decodes_as_exported_variables(self):
        restored = shell_environment_from_state(
            {
                "HOME": "/root",
                "PWD": "/root",
                "SHELL": "/bin/bash",
                "USER": "root",
            }
        )

        self.assertEqual(restored.variables["HOME"], "/root")
        self.assertEqual(restored.variables["USER"], "root")
        self.assertEqual(
            restored.exported,
            frozenset({"HOME", "PWD", "SHELL", "USER"}),
        )


if __name__ == "__main__":
    unittest.main()
