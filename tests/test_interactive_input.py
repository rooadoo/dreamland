import json
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from dreamland.agent_core import LangGraphCommandAgent
from dreamland.backend import AgentBackend
from dreamland.models import (
    CommandStreamResult,
    InteractiveProgram,
    SessionState,
    default_shell_environment,
)
from dreamland.profile import server_profile
from dreamland.session_state import active_shell_frame


def program(name, phase=None, **metadata):
    if phase is not None:
        metadata["phase"] = phase
    return {
        "name": name,
        "command": "/usr/bin/" + name if name != "bash" else "/bin/bash",
        "argv": [name],
        "prompt_template": "prompt: " if name != "bash" else None,
        "metadata": metadata,
    }


def response(update=None, stdout="", exit_status=0):
    return {
        "stdout": stdout,
        "stderr": "",
        "exit_status": exit_status,
        "state_update": update or {},
    }


class DialogueModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []
        self.messages = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages[-1].content)
        self.inputs.append(messages[-1].content.rsplit("\ncurrent command: ", 1)[1])
        return SimpleNamespace(content=json.dumps(next(self.responses)))


class InteractiveInputTests(unittest.TestCase):
    def setup_dialogue(self, responses):
        model = DialogueModel(responses)
        agent = LangGraphCommandAgent("test-model", "system.md", model=model)
        backend = AgentBackend(server_profile(), agent=agent)
        session = SessionState(
            username="alice",
            cwd="/home/alice",
            shell_environment=default_shell_environment(
                username="alice", cwd="/home/alice"
            ),
            world_state={
                "active_host": "ubuntu",
                "entry_host": "ubuntu",
                "hosts_by_name": {"ubuntu": {"hostname": "ubuntu"}},
            },
        )
        session.shell_stack = [active_shell_frame(session)]
        return backend, session, model

    def execute(self, backend, session, command, streaming):
        if streaming:
            events = list(backend.execute_stream(session, command))
            self.assertIsInstance(events[-1], CommandStreamResult)
            return events[-1].result
        return backend.execute(session, command)

    def test_keygen_empty_defaults_complete_in_one_frame(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                backend, session, model = self.setup_dialogue(
                    [
                        response(
                            {"interactive_program": program("ssh-keygen", "filename")}
                        ),
                        response(
                            {
                                "interactive_program": program(
                                    "ssh-keygen",
                                    "passphrase",
                                    key_path="/home/alice/.ssh/id_ed25519",
                                )
                            }
                        ),
                        response(
                            {
                                "interactive_program": {
                                    **program(
                                        "ssh-keygen",
                                        "confirm_passphrase",
                                        key_path="/home/alice/.ssh/id_ed25519",
                                    ),
                                    "command": "ssh-keygen",
                                }
                            }
                        ),
                        response(
                            {"shell_stack_transition": {"operation": "pop"}},
                            "Key saved.\n",
                        ),
                    ]
                )
                parent = deepcopy(session.shell_stack[0])
                for command, depth in (("ssh-keygen", 2), ("", 2), ("", 2), ("", 1)):
                    result = self.execute(backend, session, command, streaming)
                    self.assertEqual(len(session.shell_stack), depth)
                    self.assertEqual(session.shell_stack[0], parent)
                self.assertEqual(result.stdout, "Key saved.\n")
                self.assertEqual(session.interactive_program.name, "bash")
                self.assertEqual(model.inputs, ["ssh-keygen", "", "", ""])
                self.assertEqual([h.command for h in session.history], model.inputs)
                host_history = session.world_state["hosts_by_name"][
                    session.host_identity.hostname
                ]["command_history"]
                self.assertEqual([h["command"] for h in host_history], model.inputs)
                self.assertIn('"phase": "confirm_passphrase"', model.messages[-1])
                self.assertIn(
                    '"key_path": "/home/alice/.ssh/id_ed25519"', model.messages[-1]
                )
                self.assertIn('"command": ""', model.messages[-1])

    def test_sudo_retries_exit_after_three_attempts_without_leaked_frames(self):
        backend, session, model = self.setup_dialogue(
            [
                response(
                    {
                        "interactive_program": program(
                            "sudo", "password_auth", attempts=0
                        )
                    }
                ),
                response(
                    {
                        "interactive_program": program(
                            "sudo", "password_auth", attempts=1
                        )
                    }
                ),
                response(
                    {
                        "interactive_program": program(
                            "sudo", "password_auth", attempts=2
                        ),
                        "shell_stack_transition": {"operation": "push"},
                    }
                ),
                response(
                    {"shell_stack_transition": {"operation": "pop"}},
                    "sudo: 3 incorrect password attempts\n",
                    1,
                ),
            ]
        )
        for command, depth in (("sudo -l", 2), ("", 2), ("  ", 2), ("exit", 1)):
            result = backend.execute(session, command)
            self.assertEqual(len(session.shell_stack), depth)
        self.assertEqual(result.exit_status, 1)
        self.assertEqual(model.inputs, ["sudo -l", "", "  ", "exit"])
        self.assertEqual(backend.execute(session, "whoami").stdout, "alice\n")

    def test_sudo_success_replaces_authentication_with_root_shell(self):
        backend, session, model = self.setup_dialogue(
            [
                response({"interactive_program": program("sudo", "password_auth")}),
                response(
                    {
                        "interactive_program": program("bash"),
                        "shell_stack_transition": {
                            "operation": "push",
                            "username": "root",
                        },
                        "cwd": "/root",
                    }
                ),
            ]
        )
        backend.execute(session, "sudo -i")
        result = backend.execute(session, "test-password")
        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.username, "root")
        self.assertEqual(
            result.metadata["dreamland.transition.requested_operation"], "push"
        )
        self.assertEqual(
            result.metadata["dreamland.transition.applied_operation"], "replace_active"
        )
        backend.execute(session, "exit")
        self.assertEqual(len(session.shell_stack), 1)
        self.assertEqual(session.username, "alice")

    def test_idle_shell_blanks_never_call_model_or_add_history(self):
        for name in ("bash", "sh", "dash", "ksh", "zsh"):
            for streaming in (False, True):
                with self.subTest(name=name, streaming=streaming):
                    backend, session, model = self.setup_dialogue([])
                    current = InteractiveProgram(
                        name=name, command="/bin/" + name, argv=(name,)
                    )
                    session.shell_stack[-1] = replace(
                        session.shell_stack[-1], interactive_program=current
                    )
                    for command in ("", " ", "\t", ""):
                        self.execute(backend, session, command, streaming)
                    self.assertEqual(model.inputs, [])
                    self.assertEqual(session.history, [])

    def test_unknown_program_and_repl_preserve_empty_and_whitespace_input(self):
        for name in ("python3", "custom-dialogue"):
            with self.subTest(name=name):
                backend, session, model = self.setup_dialogue(
                    [
                        response({"interactive_program": program(name)}),
                        response(),
                        response(),
                    ]
                )
                backend.execute(session, name)
                backend.execute(session, "")
                backend.execute(session, "   ")
                self.assertEqual(model.inputs, [name, "", "   "])
                self.assertEqual(len(session.shell_stack), 2)

    def test_shell_read_input_bypasses_builtins_and_resumes_blank_noop(self):
        backend, session, model = self.setup_dialogue(
            [
                response({"interactive_program": program("bash", input_mode="line")}),
                response(
                    {"interactive_program": program("bash", input_mode="command")},
                    "read exit\n",
                ),
                response({"interactive_program": program("bash", input_mode="line")}),
                response(
                    {"interactive_program": program("bash", input_mode="command")},
                    "read empty\n",
                ),
            ]
        )
        for command in ("read value", "exit", "read value", ""):
            backend.execute(session, command)
            self.assertEqual(len(session.shell_stack), 1)
        backend.execute(session, "")
        self.assertEqual(model.inputs, ["read value", "exit", "read value", ""])
        self.assertEqual(session.history[-1].stdout, "read empty\n")

    def test_legitimate_nested_shells_and_repls_keep_explicit_push(self):
        for name in ("bash", "python3"):
            with self.subTest(name=name):
                backend, session, model = self.setup_dialogue(
                    [
                        response(
                            {
                                "interactive_program": program(name),
                                "shell_stack_transition": {"operation": "push"},
                            }
                        ),
                        response(
                            {
                                "interactive_program": program(name),
                                "shell_stack_transition": {"operation": "push"},
                            }
                        ),
                    ]
                )
                backend.execute(session, name)
                backend.execute(session, name)
                self.assertEqual(len(session.shell_stack), 3)

    def test_legacy_equal_depth_program_phase_update_does_not_push(self):
        backend, session, model = self.setup_dialogue(
            [
                response({"interactive_program": program("ssh-keygen", "filename")}),
                response(
                    {
                        "shell_stack": [
                            {
                                "username": "alice",
                                "cwd": "/home/alice",
                                "interactive_program": program("bash"),
                            },
                            {
                                "username": "alice",
                                "cwd": "/home/alice",
                                "interactive_program": program(
                                    "ssh-keygen", "passphrase"
                                ),
                            },
                        ]
                    }
                ),
            ]
        )
        backend.execute(session, "ssh-keygen")
        backend.execute(session, "")
        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.interactive_program.metadata["phase"], "passphrase")


if __name__ == "__main__":
    unittest.main()
