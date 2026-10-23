import threading
import unittest
from unittest.mock import PropertyMock, patch

from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings, LangfuseSettings
from dreamland.console import DreamlandConsole
from dreamland.models import (
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    CommandStreamResult,
    HostIdentity,
    InteractiveProgram,
    ShellEnvironment,
    ShellFrame,
    ShellStackTransition,
    default_shell_environment,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer, configure_session_trace_logging
from dreamland.ssh_sessions import ExecSession, ShellSession
from dreamland.state_profiles import StateProfile
from dreamland.terminal import EDITOR_FACTORIES, EditorEvent


class RecordingAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        return CommandResult(stdout="recorded\n")


class RegistryObservingAgent:
    def __init__(self, registry):
        self.registry = registry
        self.registered_during_command = []
        self.session_ids = []

    def respond(self, context):
        self.session_ids.append(context.langfuse_trace_id)
        self.registered_during_command.append(
            self.registry.get(context.langfuse_trace_id) is not None
        )
        return CommandResult(stdout="observed\n")


class PartialLineStreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield CommandOutputChunk("only partial")
        yield CommandStreamResult(CommandResult(stdout="only partial"))


class ProgramSwitchAgent:
    def respond(self, context):
        if context.command == "redis-cli":
            return CommandResult(
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="redis",
                        command="/usr/bin/redis-cli",
                        argv=("redis-cli",),
                        prompt_template="127.0.0.1:6379> ",
                    )
                ),
            )
        return CommandResult(
            stdout=f"{context.interactive_program.name}: {context.command}\n"
        )


class PythonProgramAgent:
    def respond(self, context):
        if context.command == "python3":
            return CommandResult(
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="python",
                        command="/usr/bin/python3",
                        argv=("python3",),
                        prompt_template=">>> ",
                    )
                ),
            )
        return CommandResult(
            stdout=f"{context.interactive_program.name}: {context.command}\n"
        )


class SudoShellAgent:
    def respond(self, context):
        if context.command == "sudo -i":
            environment = default_shell_environment(
                username="root",
                hostname=context.hostname,
                cwd="/root",
            )
            variables = dict(environment.variables)
            variables.update(
                {
                    "HOME": "/root",
                    "LOGNAME": "root",
                    "PWD": "/root",
                    "SHELL": "/bin/bash",
                    "USER": "root",
                }
            )
            return CommandResult(
                state_update=CommandStateUpdate(
                    shell_stack=(
                        *context.shell_stack,
                        ShellFrame(
                            username="root",
                            cwd="/root",
                            interactive_program=InteractiveProgram(
                                name="bash",
                                command="/bin/bash",
                                argv=("bash",),
                            ),
                            shell_environment=ShellEnvironment(
                                variables=variables,
                                exported=frozenset(environment.exported),
                            ),
                            host_identity=context.host_identity,
                        ),
                    )
                )
            )
        return CommandResult(stdout=f"{context.username}\n")


class SudoPasswordPromptAgent:
    prompt = "[sudo] password for bob: "

    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "sudo systemctl status cam-nvr.service":
            return CommandResult(
                stderr=self.prompt,
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="sudo",
                        command="/usr/bin/sudo",
                        argv=("sudo", "systemctl", "status", "cam-nvr.service"),
                        prompt_template=self.prompt,
                        metadata={"phase": "password_auth"},
                    ),
                    shell_stack_transition=ShellStackTransition(operation="push"),
                ),
            )
        if context.command == "red123":
            environment = default_shell_environment(
                username="root",
                hostname=context.hostname,
                cwd="/root",
            )
            variables = dict(environment.variables)
            variables.update(
                {
                    "HOME": "/root",
                    "LOGNAME": "root",
                    "OLDPWD": context.cwd,
                    "PWD": "/root",
                    "SHELL": "/bin/bash",
                    "USER": "root",
                }
            )
            return CommandResult(
                state_update=CommandStateUpdate(
                    cwd="/root",
                    interactive_program=InteractiveProgram(
                        name="bash",
                        command="/bin/bash",
                        argv=("-bash",),
                        prompt_template="root@ubuntu:~# ",
                    ),
                    shell_environment=ShellEnvironment(
                        variables=variables,
                        exported=frozenset(environment.exported),
                    ),
                    shell_stack_transition=ShellStackTransition(
                        operation="replace_active",
                        username="root",
                    ),
                ),
            )
        if context.command == "whoami":
            return CommandResult(stdout=f"{context.username}\n")
        if context.interactive_program.name == "sudo":
            return CommandResult(
                stderr=f"Sorry, try again.\n{self.prompt}",
            )
        return CommandResult(stdout="unexpected\n")


class NestedBashAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "bash":
            return CommandResult(
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="bash",
                        command="/bin/bash",
                        argv=("bash",),
                    ),
                    shell_stack_transition=ShellStackTransition(operation="push"),
                )
            )
        return CommandResult(stdout=f"seen:{context.command}\n")


class NestedSSHAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "ssh swift-gw01.payments.meridianbank.local":
            environment = default_shell_environment(
                username=context.username,
                hostname="swift-gw01",
                cwd="/home/bob",
            )
            variables = dict(environment.variables)
            variables.update(
                {
                    "SSH_CLIENT": "10.40.0.10 51432 22",
                    "SSH_CONNECTION": "10.40.0.10 51432 10.10.30.23 22",
                }
            )
            return CommandResult(
                stdout="Last login: Tue Jun 17 09:12:44 2026 from 10.40.0.10\n",
                state_update=CommandStateUpdate(
                    host_identity=HostIdentity(hostname="swift-gw01"),
                    interactive_program=InteractiveProgram(
                        name="bash",
                        command="/bin/bash",
                        argv=("bash",),
                        prompt_template="bob@swift-gw01:{cwd}$ ",
                    ),
                    shell_environment=ShellEnvironment(
                        variables=variables,
                        exported=frozenset(environment.exported),
                    ),
                    shell_stack_transition=ShellStackTransition(operation="push"),
                ),
            )
        if context.command == "hostname":
            return CommandResult(stdout=f"{context.host_identity.hostname}\n")
        return CommandResult(stdout=f"{context.host_identity.hostname}\n")


class SSHPasswordPromptAgent:
    prompt = "root@cam-nvr01.portops.local's password: "

    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "ssh root@cam-nvr01.portops.local":
            return CommandResult(
                stdout=self.prompt,
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="ssh",
                        command="/usr/bin/ssh",
                        argv=("ssh", "root@cam-nvr01.portops.local"),
                        prompt_template=self.prompt,
                        metadata={
                            "phase": "password_auth",
                            "target_host": "cam-nvr01.portops.local",
                            "target_username": "root",
                        },
                    ),
                    shell_stack_transition=ShellStackTransition(operation="push"),
                ),
            )
        if context.interactive_program.name == "ssh":
            return CommandResult(
                stdout=f"Permission denied, please try again.\n{self.prompt}",
                state_update=CommandStateUpdate(
                    interactive_program=InteractiveProgram(
                        name="ssh",
                        command="/usr/bin/ssh",
                        argv=("ssh", "root@cam-nvr01.portops.local"),
                        prompt_template=self.prompt,
                        metadata={
                            "phase": "password_auth",
                            "target_host": "cam-nvr01.portops.local",
                            "target_username": "root",
                        },
                    ),
                    shell_stack_transition=ShellStackTransition(
                        operation="replace_active"
                    ),
                ),
            )
        return CommandResult(stdout="unexpected\n")


class InteractiveDialogueAgent:
    def __init__(self, name="ssh-keygen", prompts=None):
        self.name = name
        self.prompts = prompts or (
            (
                "filename",
                "Enter file in which to save the key (/home/bob/.ssh/id_ed25519): ",
            ),
            ("overwrite", "Overwrite (y/n)? "),
            ("passphrase", "Enter passphrase (empty for no passphrase): "),
            ("passphrase_confirmation", "Enter same passphrase again: "),
        )
        self.contexts = []
        self.phase_index = -1

    def respond(self, context):
        self.contexts.append(context)
        if (
            context.command != self.name
            and context.interactive_program.name != self.name
        ):
            return CommandResult(stdout=f"seen:{context.command}\n")
        self.phase_index += 1
        if self.phase_index == len(self.prompts):
            return CommandResult(
                stdout="Complete.\n",
                state_update=CommandStateUpdate(
                    shell_stack_transition=ShellStackTransition(operation="pop")
                ),
            )
        phase, prompt = self.prompts[self.phase_index]
        return CommandResult(
            stdout=prompt,
            state_update=CommandStateUpdate(
                interactive_program=InteractiveProgram(
                    name=self.name,
                    command=f"/usr/bin/{self.name}",
                    argv=(self.name,),
                    prompt_template=prompt,
                    metadata={"phase": phase},
                ),
                shell_stack_transition=ShellStackTransition(
                    operation="push" if self.phase_index == 0 else "replace_active"
                ),
            ),
        )


class RedisTestEditor:
    def __init__(self, send, prompt):
        self._send = send
        self._line = ""

    def start_prompt(self, prompt):
        self._line = ""

    def handle_byte(self, byte):
        if byte in {3, 4}:
            return EditorEvent(should_exit=True)
        if byte in {10, 13}:
            self._send("\r\n")
            command = f"redis-editor:{self._line}"
            self._line = ""
            return EditorEvent(command=command)
        if 32 <= byte <= 126:
            self._line += chr(byte)
            self._send(chr(byte))
        return None


class FakeChannel:
    def __init__(self, input_bytes):
        self._input = bytes(input_bytes)
        self.sent = []
        self.stdout_sent = []
        self.stderr_sent = []
        self.exit_status = None
        self.closed = False

    def recv(self, count):
        if not self._input:
            self.closed = True
            return b""
        chunk = self._input[:count]
        self._input = self._input[count:]
        return chunk

    def recv_ready(self):
        return False

    def send(self, data):
        text = bytes(data).decode("utf-8", errors="replace")
        self.sent.append(text)
        self.stdout_sent.append(text)

    def send_stderr(self, data):
        text = bytes(data).decode("utf-8", errors="replace")
        self.sent.append(text)
        self.stderr_sent.append(text)

    def send_exit_status(self, status):
        self.exit_status = status
        self.closed = True

    @property
    def output(self):
        return "".join(self.sent)

    @property
    def stdout(self):
        return "".join(self.stdout_sent)

    @property
    def stderr(self):
        return "".join(self.stderr_sent)


class ChunkedChannel(FakeChannel):
    def __init__(self, chunks):
        super().__init__(b"")
        self.chunks = list(chunks)

    def recv(self, count):
        if not self.chunks:
            self.closed = True
            return b""
        return self.chunks.pop(0)


class ShellSessionTests(unittest.TestCase):
    def test_direct_banner_and_prompt_use_complete_channel_writes(self):
        class CompleteWriteChannel(FakeChannel):
            def send(self, data):
                raise AssertionError("Initial presentation must use complete writes")

            def sendall(self, data):
                FakeChannel.send(self, data)

        backend = AgentBackend(server_profile(), agent=RecordingAgent())
        channel = CompleteWriteChannel(b"")
        shell = ShellSession(
            channel, "bob", backend, state_profile=self._banner_profile()
        )

        shell.run()

        self.assertTrue(channel.output.startswith("Debian GNU/Linux 12\r\n"))
        self.assertTrue(channel.output.endswith("bob@ubuntu:~$ "))

    def test_saved_banner_precedes_first_prompt_without_model_or_history(self):
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        for lobby_mode in (False, True):
            with self.subTest(lobby_mode=lobby_mode):
                channel = FakeChannel(b"")
                shell = ShellSession(
                    channel,
                    "bob",
                    backend,
                    state_profile=self._banner_profile(),
                    lobby_mode=lobby_mode,
                )

                shell.run()

                self.assertTrue(
                    channel.output.startswith(
                        "Debian GNU/Linux 12\r\nMaintenance console\r\n"
                    )
                )
                self.assertIn("bob@ubuntu:~$ ", channel.output)
                self.assertNotIn("Welcome to Ubuntu", channel.output)
                self.assertEqual(shell.session.history, [])
                self.assertEqual(agent.contexts, [])

    def test_exec_response_does_not_include_saved_banner(self):
        agent = RecordingAgent()
        backend = AgentBackend(
            server_profile(), agent=agent, initial_state_profile=self._banner_profile()
        )
        channel = FakeChannel(b"")

        ExecSession(channel, "bob", backend, "agent-check").run()

        self.assertEqual(channel.output, "recorded\n")
        self.assertEqual(len(agent.contexts), 1)
        self.assertNotIn("Debian GNU/Linux", channel.output)

    def test_later_simulated_login_uses_agent_output_without_replaying_saved_banner(
        self,
    ):
        class ReentryAgent(NestedSSHAgent):
            def respond(self, context):
                if context.command == "ssh ubuntu":
                    self.contexts.append(context)
                    return CommandResult(
                        stdout="Entry host greeting generated by the model\n",
                        state_update=CommandStateUpdate(
                            host_identity=HostIdentity(hostname="ubuntu"),
                            shell_stack_transition=ShellStackTransition(
                                operation="push"
                            ),
                        ),
                    )
                return super().respond(context)

        agent = ReentryAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = FakeChannel(
            b"ssh swift-gw01.payments.meridianbank.local\nssh ubuntu\nexit\nexit\nexit\n"
        )
        shell = ShellSession(
            channel, "bob", backend, state_profile=self._banner_profile()
        )

        shell.run()

        self.assertEqual(channel.output.count("Debian GNU/Linux 12"), 1)
        self.assertEqual(channel.output.count("Last login:"), 1)
        self.assertEqual(
            channel.output.count("Entry host greeting generated by the model"), 1
        )
        self.assertEqual(
            agent.contexts[0].command, "ssh swift-gw01.payments.meridianbank.local"
        )
        self.assertEqual(agent.contexts[1].command, "ssh ubuntu")
        self.assertEqual(agent.contexts[1].host_identity.hostname, "swift-gw01")

    @staticmethod
    def _banner_profile():
        world = server_profile().world_state
        host = world["hosts_by_name"][world["active_host"]]
        host["os"] = {"distribution": "Debian GNU/Linux", "version": "12"}
        host["login_banner"] = "Debian GNU/Linux 12\nMaintenance console\n"
        return StateProfile(
            name="debian-banner",
            level="world",
            created_at="2026-10-20T00:00:00Z",
            state={"world_state": world},
        )

    def test_shell_session_registers_while_running_and_unregisters_after_exit(self):
        console = self._console()
        agent = RegistryObservingAgent(console.session_registry)

        channel = self._run_shell("agent-check\nexit\n", agent=agent, console=console)

        self.assertEqual(agent.registered_during_command, [True])
        self.assertIsNone(console.session_registry.get(agent.session_ids[0]))
        self.assertEqual(channel.exit_status, 0)

    def test_shell_session_records_langfuse_start_before_first_command(self):
        console = self._console(
            langfuse_settings=LangfuseSettings(
                enabled=True,
                public_key="pk-test",
                secret_key="sk-test",
                host="https://langfuse.local",
            )
        )
        profile = server_profile()
        backend = AgentBackend(profile, agent=RecordingAgent())
        channel = FakeChannel(b"")

        with patch("dreamland.ssh_sessions.record_langfuse_session_start") as record:
            ShellSession(channel, "bob", backend, console=console)

        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["enabled"], True)
        self.assertEqual(record.call_args.kwargs["public_key"], "pk-test")
        self.assertNotIn("profile_name", record.call_args.kwargs)
        self.assertEqual(record.call_args.kwargs["session"].history, [])

    def test_exec_session_registers_while_running_and_unregisters_after_exit(self):
        console = self._console()
        agent = RegistryObservingAgent(console.session_registry)

        channel = self._run_exec("agent-check", agent=agent, console=console)

        self.assertEqual(agent.registered_during_command, [True])
        self.assertIsNone(console.session_registry.get(agent.session_ids[0]))
        self.assertEqual(channel.exit_status, 0)

    def test_shell_rejects_operator_command_without_history_or_input_trace(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(self._trace_formatter())
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = FakeChannel(b"  !dreamland logging\n\x1b[Aagent-check\nexit\n")
        shell = ShellSession(
            channel,
            "bob",
            backend,
            console=self._console(trace_logs=trace_logs),
        )

        shell.run()

        denial = "error: Dreamland operator commands are unavailable over SSH\r\n"
        records = [
            record.text
            for record in trace_logs.latest(
                shell.session.langfuse_trace_id,
                20,
            )
        ]
        self.assertEqual(channel.output.count(denial), 1)
        self.assertEqual(
            [context.command for context in agent.contexts],
            ["agent-check"],
        )
        self.assertEqual(agent.contexts[0].history, ())
        self.assertFalse(
            any("!dreamland" in str(item) for item in shell.session.history)
        )
        self.assertFalse(any("!dreamland" in record for record in records))
        self.assertIn("recorded\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.output.count("bob@ubuntu:~$ "), 3)
        self.assertEqual(channel.exit_status, 0)

    def test_exec_rejects_operator_command_on_stderr_without_execution(self):
        commands = (
            "!dreamland logging",
            " \t!dreamland logging",
        )

        for command in commands:
            with self.subTest(command=command):
                trace_logs = configure_session_trace_logging(20)
                trace_logs.setFormatter(self._trace_formatter())
                agent = RecordingAgent()
                backend = AgentBackend(server_profile(), agent=agent)
                channel = FakeChannel(b"")
                session = ExecSession(
                    channel,
                    "bob",
                    backend,
                    command,
                    console=self._console(trace_logs=trace_logs),
                )

                session.run()

                records = [
                    record.text
                    for record in trace_logs.latest(
                        session.session.langfuse_trace_id,
                        20,
                    )
                ]
                self.assertEqual(channel.stdout, "")
                self.assertEqual(
                    channel.stderr,
                    "error: Dreamland operator commands are unavailable over SSH\n",
                )
                self.assertNotEqual(channel.exit_status, 0)
                self.assertEqual(agent.contexts, [])
                self.assertEqual(session.session.history, [])
                self.assertFalse(any("!dreamland" in record for record in records))

    def test_shell_records_session_trace(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(self._trace_formatter())
        agent = RecordingAgent()
        channel = self._run_shell(
            "agent-check\nexit\n",
            agent=agent,
            console=self._console(trace_logs=trace_logs),
        )
        session_id = agent.contexts[0].langfuse_trace_id

        records = [record.text for record in trace_logs.latest(session_id, 20)]

        self.assertIn("type=input text='agent-check'", records)
        self.assertTrue(
            any(
                "type=output stream=stdout text='recorded\\r\\n'" in record
                for record in records
            )
        )
        self.assertIn("recorded\r\n", channel.output)

    def test_exec_records_session_trace(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(self._trace_formatter())
        agent = RecordingAgent()
        self._run_exec(
            "agent-check",
            agent=agent,
            console=self._console(trace_logs=trace_logs),
        )
        session_id = agent.contexts[0].langfuse_trace_id

        records = [record.text for record in trace_logs.latest(session_id, 20)]

        self.assertIn("type=input text='agent-check'", records)
        self.assertIn("type=output stream=stdout text='recorded\\n'", records)

    def test_shell_stream_adds_newline_before_prompt_after_partial_line(self):
        channel = self._run_shell(
            "python manage.py shell\nexit\n",
            agent=PartialLineStreamingAgent(),
        )

        self.assertIn("only partial\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_shell_exit_from_nested_shell_returns_to_parent_prompt(self):
        channel = self._run_shell("sudo -i\nexit\nexit\n", agent=SudoShellAgent())

        self.assertIn("root@ubuntu:~$ ", channel.output)
        self.assertIn("\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_shell_ctrl_d_from_nested_shell_returns_to_parent_prompt(self):
        channel = self._run_shell(
            b"sudo -i\n\x04exit\n",
            agent=SudoShellAgent(),
        )

        self.assertIn("root@ubuntu:~$ ", channel.output)
        self.assertIn("\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_shell_ctrl_d_from_nested_ssh_restores_parent_host_context(self):
        agent = NestedSSHAgent()

        channel = self._run_shell(
            b"ssh swift-gw01.payments.meridianbank.local\n\x04hostname\nexit\n",
            agent=agent,
        )

        self.assertIn("bob@swift-gw01:~$ ", channel.output)
        self.assertIn("\r\nbob@ubuntu:~$ hostname", channel.output)
        self.assertIn("\r\nubuntu\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(agent.contexts[-1].host_identity.hostname, "ubuntu")
        self.assertEqual(len(agent.contexts[-1].shell_stack), 1)
        self.assertTrue(agent.contexts[-1].force_session_state)
        self.assertEqual(channel.exit_status, 0)

    def test_ssh_password_prompt_is_not_duplicated_and_input_is_hidden(self):
        agent = SSHPasswordPromptAgent()

        channel = self._run_shell(
            (
                "ssh root@cam-nvr01.portops.local\n"
                "why the first empty line?\n"
                "Also this text should not be visible\n"
            ),
            agent=agent,
        )

        self.assertEqual(channel.output.count(SSHPasswordPromptAgent.prompt), 3)
        self.assertNotIn(
            f"{SSHPasswordPromptAgent.prompt}{SSHPasswordPromptAgent.prompt}",
            channel.output,
        )
        self.assertNotIn("why the first empty line?", channel.output)
        self.assertNotIn("Also this text should not be visible", channel.output)
        self.assertEqual(
            [context.command for context in agent.contexts],
            [
                "ssh root@cam-nvr01.portops.local",
                "why the first empty line?",
                "Also this text should not be visible",
            ],
        )

    def test_sudo_password_prompt_is_not_duplicated_and_input_is_hidden(self):
        agent = SudoPasswordPromptAgent()

        channel = self._run_shell(
            ("sudo systemctl status cam-nvr.service\nhunter2\nred123\nwhoami\n"),
            agent=agent,
        )

        self.assertEqual(channel.output.count(SudoPasswordPromptAgent.prompt), 2)
        self.assertNotIn(
            f"{SudoPasswordPromptAgent.prompt}{SudoPasswordPromptAgent.prompt}",
            channel.output,
        )
        self.assertNotIn("hunter2", channel.output)
        self.assertNotIn("red123", channel.output)
        self.assertIn("root@ubuntu:~# whoami", channel.output)
        self.assertIn("\r\nroot\r\nroot@ubuntu:~# ", channel.output)
        self.assertEqual(
            [context.command for context in agent.contexts],
            [
                "sudo systemctl status cam-nvr.service",
                "hunter2",
                "red123",
            ],
        )

    def test_nested_shell_has_frame_local_history_and_single_stack_push(self):
        agent = NestedBashAgent()

        channel = self._run_shell(
            "parent-marker\nbash\n\x1b[Achild-marker\nexit\n\x1b[A\x03exit\n",
            agent=agent,
        )

        self.assertNotIn("seen:bashchild-marker", channel.output)
        self.assertEqual(len(agent.contexts[-1].shell_stack), 2)
        self.assertIn("bob@ubuntu:~$ bash", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_keygen_prompts_render_once_and_only_passphrases_are_hidden(self):
        agent = InteractiveDialogueAgent()
        channel = self._run_shell(
            "ssh-keygen\n/home/bob/.ssh/id_demo\ny\nprivate-value\nprivate-value\nexit\n",
            agent=agent,
        )

        for _, prompt in agent.prompts:
            self.assertEqual(channel.output.count(prompt), 1)
        self.assertIn("/home/bob/.ssh/id_demo", channel.output)
        self.assertIn("Overwrite (y/n)? y\r\n", channel.output)
        self.assertNotIn("private-value", channel.output)
        self.assertIn("Complete.\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_crlf_counts_once_across_packets_and_prompt_editor_changes(self):
        for chunks in (
            [b"ssh-keygen\r\n\r\ny\r\n\r\n\r\nexit\r\n"],
            [b"ssh-keygen\r", b"\n\r", b"\ny\r", b"\n\r", b"\n\r", b"\nexit\r", b"\n"],
        ):
            with self.subTest(chunks=chunks):
                agent = InteractiveDialogueAgent()
                channel = ChunkedChannel(chunks)
                backend = AgentBackend(server_profile(), agent=agent)
                ShellSession(channel, "bob", backend).run()

                self.assertEqual(
                    [context.command for context in agent.contexts],
                    ["ssh-keygen", "", "y", "", ""],
                )
                for _, prompt in agent.prompts:
                    self.assertEqual(channel.output.count(prompt), 1)
                self.assertEqual(channel.exit_status, 0)

    def test_repeated_enters_are_preserved_in_interactive_dialogue(self):
        for newline in ("\r", "\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                agent = InteractiveDialogueAgent()
                channel = self._run_shell(
                    newline.join(("ssh-keygen", "", "", "", "", "exit", "")),
                    agent=agent,
                )
                self.assertEqual(
                    [context.command for context in agent.contexts],
                    ["ssh-keygen", "", "", "", ""],
                )
                self.assertEqual(channel.exit_status, 0)

    def test_crlf_tail_drained_during_execution_does_not_swallow_next_enter(self):
        started = threading.Event()
        drained = threading.Event()

        class WaitingDialogueAgent(InteractiveDialogueAgent):
            def respond(self, context):
                if context.command == self.name:
                    started.set()
                    if not drained.wait(2):
                        raise AssertionError("Expected the CRLF tail to be drained")
                return super().respond(context)

        class DrainingChannel(ChunkedChannel):
            def recv_ready(self):
                return started.is_set() and not drained.is_set()

            def recv(self, count):
                data = super().recv(count)
                if data == b"\n" and started.is_set():
                    drained.set()
                return data

        agent = WaitingDialogueAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = DrainingChannel([b"ssh-keygen\r", b"\n", b"\ny\n\n\nexit\n"])

        ShellSession(channel, "bob", backend).run()

        self.assertTrue(drained.is_set())
        self.assertEqual(
            [context.command for context in agent.contexts],
            ["ssh-keygen", "", "y", "", ""],
        )
        self.assertEqual(channel.exit_status, 0)

    def test_ctrl_c_cancels_dialogue_and_restores_parent(self):
        for name, phase, prompt in (
            ("ssh-keygen", "filename", "Enter filename: "),
            ("ssh-keygen", "passphrase", "Enter passphrase: "),
            ("passwd", "new_password", "New password: "),
            ("sudo", "password_auth", "[sudo] password for bob: "),
            ("ssh", "host_key_confirmation", "Continue connecting (yes/no)? "),
        ):
            with self.subTest(name=name, phase=phase):
                agent = InteractiveDialogueAgent(name, ((phase, prompt),))
                backend = AgentBackend(server_profile(), agent=agent)
                channel = FakeChannel(
                    f"{name}\nunfinished\x03after-cancel\nexit\n".encode()
                )
                shell = ShellSession(channel, "bob", backend)
                shell.run()

                self.assertEqual(channel.output.count(prompt), 1)
                self.assertIn("^C\r\nbob@ubuntu:~$ after-cancel", channel.output)
                self.assertEqual(agent.contexts[-1].interactive_program.name, "bash")
                self.assertEqual(len(agent.contexts[-1].shell_stack), 1)
                self.assertTrue(agent.contexts[-1].force_session_state)
                interruption = next(
                    entry for entry in shell.session.history if entry.command == "\x03"
                )
                self.assertEqual(interruption.command, "\x03")
                self.assertEqual(interruption.exit_status, 130)
                self.assertEqual(interruption.signal, "SIGINT")
                self.assertTrue(interruption.interrupted)
                self.assertEqual(channel.exit_status, 0)

    def test_bash_ctrl_c_still_clears_only_the_line(self):
        agent = RecordingAgent()
        channel = self._run_shell("unfinished\x03after-cancel\nexit\n", agent=agent)

        self.assertEqual(
            [context.command for context in agent.contexts], ["after-cancel"]
        )
        self.assertIn("^C\r\nbob@ubuntu:~$ after-cancel", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_dialogue_ctrl_c_does_not_mutate_state_during_pending_cleanup(self):
        agent = InteractiveDialogueAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = FakeChannel(b"ssh-keygen\nunfinished\x03")
        shell = ShellSession(channel, "bob", backend)
        with (
            patch.object(
                ShellSession,
                "cleanup_pending",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch("dreamland.ssh_sessions.pop_nested_shell_for_transport_exit") as pop,
        ):
            shell.run()

        pop.assert_not_called()
        self.assertEqual(len(shell.session.shell_stack), 2)
        self.assertEqual(shell.session.interactive_program.name, "ssh-keygen")
        self.assertIn(f"^C\r\n{agent.prompts[0][1]}", channel.output)

    def test_inflight_dialogue_interrupt_keeps_frame_and_rejects_late_response(self):
        started = threading.Event()
        release = threading.Event()
        interrupt_sent = threading.Event()

        class WaitingDialogueAgent(InteractiveDialogueAgent):
            def respond(self, context):
                if context.interactive_program.name == self.name:
                    started.set()
                    if not release.wait(3):
                        raise AssertionError(
                            "Expected test to release the provider owner"
                        )
                return super().respond(context)

        class InterruptingChannel(ChunkedChannel):
            def recv_ready(self):
                return started.is_set() and not interrupt_sent.is_set()

            def recv(self, count):
                data = super().recv(count)
                if data == b"\x03":
                    interrupt_sent.set()
                return data

        agent = WaitingDialogueAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        channel = InterruptingChannel([b"ssh-keygen\n", b"\n", b"\x03"])
        shell = ShellSession(channel, "bob", backend)
        try:
            shell.run()
            self.assertTrue(interrupt_sent.is_set())
            self.assertTrue(shell.cleanup_pending)
            self.assertEqual(len(shell.session.shell_stack), 2)
            self.assertEqual(
                shell.session.interactive_program.metadata["phase"], "filename"
            )
            self.assertIn(f"^C\r\n{agent.prompts[0][1]}", channel.output)
        finally:
            with shell._workers_lock:
                workers = tuple(shell._workers)
            release.set()
            for worker in workers:
                worker.join(timeout=3)

        self.assertFalse(shell.cleanup_pending)
        self.assertEqual(len(shell.session.shell_stack), 2)
        self.assertEqual(
            shell.session.interactive_program.metadata["phase"], "filename"
        )
        self.assertNotIn(agent.prompts[1][1], channel.output)

    def test_normal_shell_output_that_matches_prompt_is_not_suppressed(self):
        agent = RecordingAgent()
        with patch.object(
            agent, "respond", return_value=CommandResult(stdout="bob@ubuntu:~$ ")
        ):
            channel = self._run_shell("prompt-text\nexit\n", agent=agent)

        self.assertIn("prompt-text\r\nbob@ubuntu:~$ \r\nbob@ubuntu:~$ ", channel.output)

    def test_shell_rebuilds_editor_after_interactive_program_change(self):
        previous_factory = EDITOR_FACTORIES.get("redis")
        EDITOR_FACTORIES["redis"] = RedisTestEditor
        try:
            channel = self._run_shell(
                b"redis-cli\nping\n\x04exit\n",
                agent=ProgramSwitchAgent(),
            )
        finally:
            if previous_factory is None:
                del EDITOR_FACTORIES["redis"]
            else:
                EDITOR_FACTORIES["redis"] = previous_factory

        self.assertIn("redis: redis-editor:ping", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def test_shell_restores_parent_after_python_repl_exit(self):
        channel = self._run_shell(
            b"python3\nprint('hello')\n\x04exit\n",
            agent=PythonProgramAgent(),
        )

        self.assertIn("python: print('hello')", channel.output)
        self.assertIn("\r\nbob@ubuntu:~$ ", channel.output)
        self.assertEqual(channel.exit_status, 0)

    def _run_shell(self, input_data, *, agent, console=None):
        profile = server_profile()
        backend = AgentBackend(profile, agent=agent)
        input_bytes = (
            input_data.encode("utf-8") if isinstance(input_data, str) else input_data
        )
        channel = FakeChannel(input_bytes)

        ShellSession(
            channel,
            "bob",
            backend,
            console=console,
        ).run()

        return channel

    def _run_exec(self, command, *, agent, console=None):
        profile = server_profile()
        backend = AgentBackend(profile, agent=agent)
        channel = FakeChannel(b"")

        ExecSession(
            channel,
            "bob",
            backend,
            command,
            console=console,
        ).run()

        return channel

    def _console(
        self,
        trace_logs=None,
        langfuse_settings=None,
    ):
        settings = ConsoleSettings()
        logs = LogRecordBuffer(settings.log_capacity)
        return DreamlandConsole(
            settings,
            logs,
            lambda: None,
            trace_logs=trace_logs,
            langfuse_settings=langfuse_settings,
        )

    def _trace_formatter(self):
        import logging

        return logging.Formatter("%(message)s")


if __name__ == "__main__":
    unittest.main()
