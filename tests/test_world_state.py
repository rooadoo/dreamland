import json
import unittest

from dreamland.world_state import (
    active_host_metadata,
    active_host_ref,
    default_interactive_program_from_world_state,
    format_world_state_for_prompt,
    sync_active_host,
)


class WorldStateTests(unittest.TestCase):
    def test_formats_world_state_as_json_for_prompts(self):
        rendered = format_world_state_for_prompt(
            {
                "name": "default-world",
                "entry_host": "ubuntu",
                "active_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {"hostname": "ubuntu"}},
            }
        )

        parsed = json.loads(rendered)
        self.assertEqual(parsed["name"], "default-world")
        self.assertEqual(parsed["entry_host"], "ubuntu")
        self.assertIn("hosts_by_name", parsed)

    def test_active_host_helpers_use_host_registry_keys(self):
        state = {
            "entry_host": "ubuntu",
            "active_host": "ubuntu",
            "hosts_by_name": {"ubuntu": {"hostname": "ubuntu"}},
        }

        synced = sync_active_host(state, "jump01")

        self.assertEqual(active_host_ref(synced), "jump01")
        self.assertEqual(active_host_metadata(synced)["hostname"], "jump01")
        self.assertIn("jump01", synced["hosts_by_name"])

    def test_default_interactive_program_uses_active_host_shell(self):
        program = default_interactive_program_from_world_state(
            {
                "name": "lab",
                "active_host": "jump01",
                "hosts_by_name": {
                    "jump01": {
                        "hostname": "jump01",
                        "shell": {
                            "default": {
                                "name": "zsh",
                                "command": "/bin/zsh",
                                "argv": ["zsh", "-l"],
                            }
                        },
                    }
                },
            }
        )

        self.assertEqual(program.name, "zsh")
        self.assertEqual(program.command, "/bin/zsh")
        self.assertEqual(program.argv, ("zsh", "-l"))
        self.assertEqual(program.metadata["host"], "jump01")
        self.assertEqual(program.metadata["world_state"], "lab")

    def test_default_interactive_program_falls_back_to_bash(self):
        program = default_interactive_program_from_world_state({})

        self.assertEqual(program.name, "bash")
        self.assertEqual(program.command, "/bin/bash")
        self.assertEqual(program.argv, ("bash",))


if __name__ == "__main__":
    unittest.main()
