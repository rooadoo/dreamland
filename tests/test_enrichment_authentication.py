from __future__ import annotations

import unittest
from dataclasses import replace

from dreamland.background_agents.enrichment.authentication import (
    foreground_authentication_paths,
    observe_foreground_authentication_paths,
)
from dreamland.models import InteractiveProgram, SessionState, default_shell_frame


class ForegroundAuthenticationPathTests(unittest.TestCase):
    def setUp(self):
        self.frame = default_shell_frame(username="alice", hostname="workstation")
        self.session = SessionState(
            username="alice",
            shell_stack=[self.frame],
            world_state={
                "active_host": "local",
                "hosts_by_name": {
                    "local": {"hostname": "workstation"},
                    "remote": {"hostname": "remote"},
                },
            },
        )

    def test_home_protection_includes_missing_paths_and_survives_stack_and_history(
        self,
    ):
        observe_foreground_authentication_paths(self.session)
        self.session.shell_stack = [
            default_shell_frame(username="root", hostname="remote")
        ]
        self.session.world_state["active_host"] = "remote"
        self.session.history.clear()
        paths = foreground_authentication_paths(self.session, target_host="local")
        for path in (
            "/home/alice/.ssh",
            "/home/alice/.ssh/config",
            "/home/alice/.ssh/config.d",
            "/home/alice/.ssh/config.d/git",
            "/home/alice/.ssh/id_ed25519_github",
            "/home/alice/.ssh/id_ed25519_github.pub",
            "/home/alice/.ssh/known_hosts",
            "/home/alice/.ssh/sub/../config",
        ):
            with self.subTest(path=path):
                self.assertTrue(paths.protects(path))
        self.assertFalse(paths.protects("/root/.ssh/config"))
        self.assertTrue(
            foreground_authentication_paths(
                self.session, target_host="remote"
            ).protects("/root/.ssh/config")
        )

    def test_cwd_does_not_claim_other_users_home_or_unrelated_artifacts(self):
        self.session.shell_stack = [replace(self.frame, cwd="/home/bob/.ssh")]
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertFalse(paths.protects("/home/bob/.ssh/config"))
        self.assertFalse(paths.protects("/etc/ssh/sshd_config"))
        self.assertFalse(paths.protects("/home/alice/.ssh/maintenance-notes.txt"))
        self.assertFalse(paths.protects("/home/alice/.ssh/authorized_keys"))

    def test_imported_stack_resolves_nonactive_host_aliases_without_guessing(self):
        self.session.world_state = {
            "active_host": "remote",
            "hosts_by_name": {
                "gateway": {"hostname": "gateway.internal", "aliases": ["jump"]},
                "remote": {"hostname": "remote"},
            },
        }
        self.session.shell_stack = [
            default_shell_frame(username="alice", hostname="jump"),
            default_shell_frame(username="root", hostname="remote"),
        ]
        paths = foreground_authentication_paths(self.session, target_host="gateway")
        self.assertTrue(paths.protects("/home/alice/.ssh/config"))
        self.assertFalse(paths.protects("/root/.ssh/config"))

    def test_explicit_key_paths_and_public_halves_survive_program_completion(self):
        for command, expected in (
            ("ssh-keygen -t ed25519 -f ~/a", "/home/alice/a"),
            ("ssh -i 'keys/team key' remote", "/home/alice/keys/team key"),
            ("ssh-keygen -f/tmp/example-key", "/tmp/example-key"),
            ("ssh-keygen -qf ~/clustered-key", "/home/alice/clustered-key"),
        ):
            with self.subTest(command=command):
                observe_foreground_authentication_paths(self.session, command)
                paths = foreground_authentication_paths(
                    self.session, target_host="local"
                )
                self.assertTrue(paths.protects(expected))
                self.assertTrue(paths.protects(expected + ".pub"))

    def test_filename_answer_is_reserved_but_passphrase_never_becomes_a_path(self):
        self.session.shell_stack = [
            replace(
                self.frame,
                interactive_program=InteractiveProgram(
                    name="ssh-keygen",
                    command="ssh-keygen",
                    prompt_template="Enter file in which to save the key (/home/alice/.ssh/id_ed25519): ",
                    metadata={"phase": "filename"},
                ),
            )
        ]
        observe_foreground_authentication_paths(self.session, "a")
        self.session.shell_stack = [
            replace(
                self.frame,
                interactive_program=InteractiveProgram(
                    name="ssh-keygen",
                    command="ssh-keygen",
                    prompt_template="Enter passphrase (empty for no passphrase): ",
                    metadata={"phase": "passphrase", "key_path": "/home/alice/a"},
                ),
            )
        ]
        observe_foreground_authentication_paths(self.session, "private-passphrase")
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertEqual(paths.key_paths, {"/home/alice/a", "/home/alice/a.pub"})

    def test_secret_option_values_and_shell_syntax_are_not_interpreted(self):
        for command in (
            "ssh-keygen -N '-f/private-passphrase'",
            "ssh-keygen -P -f private-passphrase",
            "ssh-keygen -qN -f/private-option-secret",
            "ssh -o '-i/private-option' host",
            "ssh host echo -i /remote-command-argument",
            "echo ssh-keygen -f /wrong",
            "ssh-keygen -f $(private-command)",
            "ssh-keygen -f /wrong; echo next",
        ):
            observe_foreground_authentication_paths(self.session, command)
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertEqual(paths.key_paths, frozenset())

    def test_input_to_authentication_and_shell_read_is_not_parsed_as_invocation(self):
        for program in (
            InteractiveProgram(
                name="ssh", command="ssh remote", metadata={"phase": "password_auth"}
            ),
            InteractiveProgram(name="bash", command="bash", metadata={"phase": "read"}),
            InteractiveProgram(
                name="bash", command="bash", metadata={"input_mode": "line"}
            ),
            InteractiveProgram(
                name="ssh-keygen",
                command="ssh-keygen",
                metadata={"phase": "passphrase"},
                prompt_template="Enter file in which to save the key: ",
            ),
        ):
            with self.subTest(program=program):
                self.session.shell_stack = [
                    replace(self.frame, interactive_program=program)
                ]
                observe_foreground_authentication_paths(
                    self.session, "ssh -i /synthetic-secret"
                )
                paths = foreground_authentication_paths(
                    self.session, target_host="local"
                )
                self.assertEqual(paths.key_paths, frozenset())

    def test_secret_prompt_overrides_stale_filename_phase(self):
        self.session.shell_stack = [
            replace(
                self.frame,
                interactive_program=InteractiveProgram(
                    name="ssh-keygen",
                    command="ssh-keygen",
                    prompt_template="Enter passphrase (empty for no passphrase): ",
                    metadata={"phase": "filename"},
                ),
            )
        ]
        observe_foreground_authentication_paths(self.session, "private-passphrase")
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertEqual(paths.key_paths, frozenset())

    def test_empty_key_metadata_does_not_reserve_cwd_but_space_filename_is_valid(self):
        self.session.shell_stack = [
            replace(
                self.frame,
                interactive_program=InteractiveProgram(
                    name="ssh-keygen",
                    command="ssh-keygen",
                    metadata={"phase": "filename", "filename": "", "key_path": ""},
                ),
            )
        ]
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertEqual(paths.key_paths, frozenset())
        observe_foreground_authentication_paths(self.session, " ")
        paths = foreground_authentication_paths(self.session, target_host="local")
        self.assertEqual(paths.key_paths, {"/home/alice/ ", "/home/alice/ .pub"})


if __name__ == "__main__":
    unittest.main()
