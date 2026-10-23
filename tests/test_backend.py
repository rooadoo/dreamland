import asyncio
import importlib.util
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dreamland.agent import (
    DeterministicCommandAgent,
    LangGraphCommandAgent,
    _build_langfuse_callbacks,
    _DreamlandLangfuseCallbackHandler,
)
from dreamland.agent_core import (
    _merged_world_state,
    _openai_command_prompt_cache_key,
    _uses_openai_explicit_prompt_cache,
)
from dreamland.agent_tracing import (
    SESSION_START_OBSERVATION_NAME,
    TokenUsageLoggingCallback,
    graph_invoke_config,
    record_langfuse_session_start,
    session_start_trace_metadata,
    transition_trace_metadata,
)
from dreamland.backend import AgentBackend
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.background_agents.events import SessionEventType
from dreamland.emulator_context import project_world_state_for_command_emulator
from dreamland.models import (
    CommandContext,
    CommandExecutionControl,
    CommandHistoryEntry,
    CommandOutputChunk,
    CommandResult,
    CommandStateUpdate,
    CommandStreamResult,
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellEnvironment,
    ShellFrame,
    ShellStackTransition,
    default_shell_environment,
)
from dreamland.profile import server_profile
from dreamland.runtime_logging import configure_session_trace_logging
from dreamland.session_metadata import (
    MCP_SESSION_ORIGIN,
    SESSION_LABEL_METADATA_KEY,
    SESSION_METADATA_METADATA_KEY,
    SESSION_ORIGIN_METADATA_KEY,
    SESSION_TAGS_METADATA_KEY,
)
from dreamland.session_state import (
    append_host_command_history,
    apply_external_state_update,
    merge_world_state,
    replace_shell_stack,
)
from dreamland.state_profiles import (
    STATE_PROFILE_METADATA_LEVEL,
    STATE_PROFILE_METADATA_NAME,
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
    create_state_profile,
)

langgraph_available = importlib.util.find_spec("langgraph") is not None


class RecordingAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "pwd":
            return CommandResult(stdout=f"{context.cwd}\n")
        if context.command.startswith("cd "):
            return CommandResult(
                state_update=CommandStateUpdate(
                    cwd=context.command.split(maxsplit=1)[1]
                )
            )
        return CommandResult(stdout=f"handled {context.command} in {context.cwd}\n")


def _packaged_default_world_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = StateProfileStore(Path(tmpdir)).load("default")
    return profile.state["world_state"]


def _world_state_from_prompt(prompt: str) -> dict[str, object]:
    world_state_text = prompt.split("world state:\n", maxsplit=1)[1].split(
        "\n\ncurrent command:",
        maxsplit=1,
    )[0]
    value = json.loads(world_state_text)
    if not isinstance(value, dict):
        raise AssertionError("model-facing world state must be an object")
    return value


class ChainCdAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "cd portops; ls":
            return CommandResult(
                stdout=f"listing in {context.cwd}/portops\n",
                state_update=CommandStateUpdate(cwd=f"{context.cwd}/portops"),
            )
        if context.command == "ls":
            return CommandResult(stdout=f"listing in {context.cwd}\n")
        return CommandResult(stdout=f"handled {context.command}\n")


class StreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="non-streaming fallback\n")

    def stream_respond(self, context):
        yield CommandOutputChunk("first ")
        yield CommandOutputChunk("line\nsecond")
        yield CommandOutputChunk(" line")
        yield CommandStreamResult(CommandResult(stdout="first line\nsecond line\n"))


class FailingStreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield from ()
        raise RuntimeError("provider failed")


class PartialFailingStreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield CommandOutputChunk("partial output\n")
        raise RuntimeError("provider failed")


class CreditBalanceError(RuntimeError):
    status_code = 429
    body = {
        "error": {
            "code": "credit_balance_exhausted",
            "message": "raw provider detail must not escape",
        }
    }


class ClassifiedFailingStreamingAgent:
    provider = "openai"
    model_name = "gpt-5.6-terra"

    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context):
        yield from ()
        raise CreditBalanceError("raw account secret must not escape")


class ClassifiedFailingNonStreamingAgent:
    provider = "openai"
    model_name = "gpt-5.6-terra"

    def respond(self, context):
        raise CreditBalanceError("raw account secret must not escape")


class ClassifiedFailingAsyncAgent(ClassifiedFailingNonStreamingAgent):
    async def respond(self, context):
        raise CreditBalanceError("raw async account secret must not escape")


class UnsafeAttributedFailingStreamingAgent(ClassifiedFailingStreamingAgent):
    provider = "openai raw provider"
    model_name = "m" * 161


class CancellableStreamingAgent:
    def respond(self, context):
        return CommandResult(stdout="fallback\n")

    def stream_respond(self, context, control=None):
        yield CommandOutputChunk("rendered line\n")
        yield CommandOutputChunk("buffered partial")
        while control is not None and not control.cancelled():
            pass
        if control is not None:
            yield CommandStreamResult(control.rendered_result())


class ShellStackAgent:
    def __init__(self):
        self.contexts = []

    def respond(self, context):
        self.contexts.append(context)
        if context.command == "sudo -i":
            root_environment = default_shell_environment(
                username="root",
                hostname=context.hostname,
                cwd="/root",
            )
            root_variables = dict(root_environment.variables)
            root_variables.update({"HOME": "/root", "PWD": "/root"})
            root_environment = ShellEnvironment(
                variables=root_variables,
                exported=frozenset(root_environment.exported),
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
                                argv=("-bash",),
                            ),
                            shell_environment=root_environment,
                            metadata={"login_shell": True, "via": "sudo -i"},
                        ),
                    )
                )
            )
        return CommandResult(stdout=f"handled as {context.username}\n")


class MetadataAgent:
    def respond(self, context):
        return CommandResult(
            stdout="metadata result\n",
            metadata={"structured_parse_error": "test"},
        )


class ReasoningSummaryAgent:
    def respond(self, context):
        return CommandResult(
            stdout="reasoned result\n",
            metadata={"dreamland.reasoning_summary": "Checked shell context."},
        )


class EventfulAgent:
    def respond(self, context):
        return CommandResult(
            stdout="connected\n",
            state_update=CommandStateUpdate(
                host_identity=HostIdentity(hostname="cache"),
                world_state={
                    "hosts_by_name": {
                        "cache": {
                            "hostname": "cache",
                            "services_by_name": {"redis": {"state": "running"}},
                        }
                    }
                },
            ),
        )


class AgentBackendTests(unittest.TestCase):
    def test_backend_publishes_neutral_command_and_materialization_events(self):
        event_bus = SessionEventBus()
        events = []
        event_bus.subscribe("test", events.append)
        backend = AgentBackend(
            server_profile(),
            agent=EventfulAgent(),
            event_bus=event_bus,
        )
        session = backend.create_session("alice")

        backend.execute(session, "ssh cache")

        self.assertEqual(
            [event.event_type for event in events],
            [
                SessionEventType.COMMAND_ACCEPTED,
                SessionEventType.COMMAND_COMPLETED,
                SessionEventType.ACTIVE_HOST_CHANGED,
                SessionEventType.STATE_MATERIALIZED,
            ],
        )
        self.assertEqual(events[0].surface, "shell")
        self.assertEqual(events[0].correlation_id, events[1].correlation_id)
        self.assertTrue(events[1].payload["active_host_changed"])
        self.assertEqual(events[1].payload["new_hosts"], ("cache",))
        self.assertEqual(events[2].target_host, "cache")
        self.assertEqual(events[3].payload["kind"], "host_discovered")
        self.assertEqual(events[3].payload["new_hosts"], ("cache",))
        self.assertTrue(events[3].payload["active_host_changed"])
        completed_focus = events[1].payload["interaction_focus"]
        self.assertEqual(completed_focus, events[2].payload["interaction_focus"])
        self.assertEqual(completed_focus, events[3].payload["interaction_focus"])
        self.assertEqual(
            completed_focus["artifacts"],
            ({"kind": "network_endpoint", "id": "cache"},),
        )

    def test_completed_event_carries_deterministic_presence_probe_kind(self):
        event_bus = SessionEventBus()
        events = []
        event_bus.subscribe("test", events.append)
        backend = AgentBackend(
            server_profile(),
            agent=RecordingAgent(),
            event_bus=event_bus,
        )
        session = backend.create_session("alice")

        backend.execute(session, "ps -ef")

        completed = next(
            event
            for event in events
            if event.event_type is SessionEventType.COMMAND_COMPLETED
        )
        self.assertEqual(completed.payload["presence_probe"], "process_activity")

    def test_basic_commands_are_deterministic_and_stateful(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        self.assertEqual(backend.execute(session, "whoami").stdout, "alice\n")
        self.assertEqual(backend.execute(session, "pwd").stdout, "/home/alice\n")

        backend.execute(session, "cd /var/log")
        self.assertEqual(backend.execute(session, "pwd").stdout, "/var/log\n")

        result = backend.execute(session, "uname -a")
        self.assertIn("[deterministic-agent]", result.stdout)
        self.assertIn("uname -a", result.stdout)
        self.assertEqual(
            [entry.command for entry in session.history],
            [
                "whoami",
                "pwd",
                "cd /var/log",
                "pwd",
                "uname -a",
            ],
        )

    def test_execute_stream_yields_chunks_and_records_final_result(self):
        backend = AgentBackend(server_profile(), agent=StreamingAgent())
        session = backend.create_session("alice")

        events = list(backend.execute_stream(session, "python manage.py shell"))

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["first ", "line\nsecond", " line"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "first line\nsecond line\n")
        self.assertEqual(session.history[-1].stdout, "first line\nsecond line\n")

    def test_execute_stream_converts_provider_failure_to_result(self):
        backend = AgentBackend(server_profile(), agent=FailingStreamingAgent())
        session = backend.create_session("alice")

        with self.assertLogs("dreamland.failures", level="ERROR") as captured:
            events = list(backend.execute_stream(session, "ls -la /opt"))

        chunks = [event for event in events if isinstance(event, CommandOutputChunk)]
        final = events[-1]

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].stream, "stderr")
        self.assertEqual(
            chunks[0].text,
            "dreamland: transient provider error; command could not be completed\n",
        )
        self.assertIsInstance(final, CommandStreamResult)
        self.assertEqual(final.result.exit_status, 75)
        self.assertEqual(final.result.stdout, "")
        self.assertEqual(final.result.stderr, chunks[0].text)
        self.assertEqual(session.history[-1].command, "ls -la /opt")
        self.assertEqual(session.history[-1].exit_status, 75)
        self.assertEqual(session.history[-1].stderr, chunks[0].text)
        self.assertIn("provider_error=RuntimeError", captured.output[0])
        self.assertIn("failure_kind=provider_error", captured.output[0])
        self.assertIn("failure_disposition=unknown", captured.output[0])
        self.assertNotIn("provider failed", captured.output[0])
        self.assertEqual(
            final.result.metadata,
            {
                "dreamland.failure.type": "provider",
                "dreamland.failure.exception": "RuntimeError",
                "dreamland.failure.kind": "provider_error",
                "dreamland.failure.disposition": "unknown",
            },
        )

    def test_provider_failure_metadata_is_bounded_and_published(self):
        event_bus = SessionEventBus()
        published = []
        event_bus.subscribe("test", published.append)
        backend = AgentBackend(
            server_profile(),
            agent=ClassifiedFailingStreamingAgent(),
            event_bus=event_bus,
        )
        session = backend.create_session("alice")

        with self.assertLogs("dreamland.failures", level="ERROR") as captured:
            events = list(backend.execute_stream(session, "cat /etc/hosts"))

        final = events[-1]
        self.assertIsInstance(final, CommandStreamResult)
        self.assertEqual(final.result.exit_status, 75)
        self.assertEqual(
            final.result.stderr,
            "dreamland: transient provider error; command could not be completed\n",
        )
        self.assertEqual(
            final.result.metadata,
            {
                "dreamland.failure.type": "provider",
                "dreamland.failure.exception": "CreditBalanceError",
                "dreamland.failure.kind": "provider_credit_exhausted",
                "dreamland.failure.disposition": "terminal",
                "dreamland.failure.provider": "openai",
                "dreamland.failure.model": "gpt-5.6-terra",
                "dreamland.failure.code": "credit_balance_exhausted",
                "dreamland.failure.status": 429,
            },
        )
        completed = next(
            event
            for event in published
            if event.event_type is SessionEventType.COMMAND_COMPLETED
        )
        self.assertEqual(
            completed.payload["provider_failure"],
            {
                "kind": "provider_credit_exhausted",
                "disposition": "terminal",
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "code": "credit_balance_exhausted",
                "status": 429,
            },
        )
        output = "\n".join(captured.output)
        self.assertIn("failure_kind=provider_credit_exhausted", output)
        self.assertIn("failure_disposition=terminal", output)
        self.assertIn("provider_code=credit_balance_exhausted", output)
        self.assertNotIn("raw account secret", output)
        self.assertNotIn("raw provider detail", output)
        self.assertNotIn("raw account secret", repr(final.result.metadata))
        self.assertNotIn("raw provider detail", repr(completed.payload))

    def test_non_streaming_provider_failures_are_classified_on_all_entry_paths(
        self,
    ):
        for name, agent in (
            ("sync", ClassifiedFailingNonStreamingAgent()),
            ("async", ClassifiedFailingAsyncAgent()),
        ):
            with self.subTest(name=name):
                event_bus = SessionEventBus()
                published = []
                event_bus.subscribe(f"test-{name}", published.append)
                backend = AgentBackend(
                    server_profile(),
                    agent=agent,
                    event_bus=event_bus,
                )
                session = backend.create_session("alice")

                result = backend.execute(session, "cat /etc/hosts")
                if asyncio.iscoroutine(result):
                    result = asyncio.run(result)

                self.assertEqual(result.exit_status, 75)
                completed = next(
                    event
                    for event in published
                    if event.event_type is SessionEventType.COMMAND_COMPLETED
                )
                self.assertEqual(
                    completed.payload["provider_failure"]["disposition"],
                    "terminal",
                )
                self.assertEqual(
                    completed.payload["provider_failure"]["provider"],
                    "openai",
                )

        backend = AgentBackend(
            server_profile(),
            agent=ClassifiedFailingNonStreamingAgent(),
        )
        session = backend.create_session("alice")

        events = list(backend.execute_stream(session, "cat /etc/hosts"))

        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.exit_status, 75)
        self.assertEqual(
            events[-1].result.metadata["dreamland.failure.kind"],
            "provider_credit_exhausted",
        )

    def test_unsafe_agent_provider_and_model_labels_are_omitted(self):
        event_bus = SessionEventBus()
        published = []
        event_bus.subscribe("test", published.append)
        backend = AgentBackend(
            server_profile(),
            agent=UnsafeAttributedFailingStreamingAgent(),
            event_bus=event_bus,
        )
        session = backend.create_session("alice")

        events = list(backend.execute_stream(session, "cat /etc/hosts"))

        final = events[-1]
        self.assertNotIn("dreamland.failure.provider", final.result.metadata)
        self.assertNotIn("dreamland.failure.model", final.result.metadata)
        completed = next(
            event
            for event in published
            if event.event_type is SessionEventType.COMMAND_COMPLETED
        )
        self.assertEqual(
            completed.payload["provider_failure"],
            {
                "kind": "provider_credit_exhausted",
                "disposition": "terminal",
                "code": "credit_balance_exhausted",
                "status": 429,
            },
        )

    def test_execute_stream_records_partial_output_before_provider_failure(self):
        backend = AgentBackend(server_profile(), agent=PartialFailingStreamingAgent())
        session = backend.create_session("alice")

        events = list(backend.execute_stream(session, "tail -f /var/log/syslog"))
        final = events[-1]

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            [
                "partial output\n",
                "dreamland: transient provider error; command could not be completed\n",
            ],
        )
        self.assertIsInstance(final, CommandStreamResult)
        self.assertEqual(final.result.stdout, "partial output\n")
        self.assertEqual(
            final.result.stderr,
            "dreamland: transient provider error; command could not be completed\n",
        )
        self.assertEqual(session.history[-1].stdout, "partial output\n")

    def test_execute_stream_records_only_rendered_output_after_interrupt(self):
        backend = AgentBackend(server_profile(), agent=CancellableStreamingAgent())
        session = backend.create_session("alice")
        control = CommandExecutionControl()
        events = iter(backend.execute_stream(session, "slow command", control=control))

        first = next(events)
        self.assertEqual(first.text, "rendered line\n")
        control.add_rendered("stdout", first.text)
        second = next(events)
        self.assertEqual(second.text, "buffered partial")

        control.cancel()
        final = next(events)

        self.assertIsInstance(final, CommandStreamResult)
        self.assertEqual(final.result.stdout, "rendered line\n")
        self.assertEqual(final.result.exit_status, 130)
        self.assertTrue(final.result.interrupted)
        self.assertEqual(final.result.signal, "SIGINT")
        self.assertEqual(session.history[-1].stdout, "rendered line\n")
        self.assertTrue(session.history[-1].interrupted)
        self.assertEqual(session.history[-1].signal, "SIGINT")
        self.assertNotIn("buffered partial", session.history[-1].stdout)

    def test_reasoning_summary_metadata_is_traced_to_session(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(_trace_formatter())
        backend = AgentBackend(server_profile(), agent=ReasoningSummaryAgent())
        session = backend.create_session("alice")

        backend.execute(session, "explain")

        records = [
            record.text for record in trace_logs.latest(session.langfuse_trace_id, 20)
        ]
        self.assertIn(
            "type=reasoning_summary command='explain' text='Checked shell context.'",
            records,
        )

    def test_unknown_commands_are_routed_to_agent_with_context(self):
        profile = server_profile()
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")
        backend.execute(session, "cd /opt/app")

        result = backend.execute(session, "python manage.py shell")

        self.assertEqual(result.stdout, "handled python manage.py shell in /opt/app\n")
        self.assertEqual(len(agent.contexts), 1)
        context = agent.contexts[-1]
        self.assertEqual(context.username, "alice")
        self.assertEqual(context.cwd, "/opt/app")
        self.assertEqual(context.hostname, "ubuntu")
        self.assertFalse(hasattr(context, "profile_name"))
        self.assertEqual(context.command, "python manage.py shell")
        self.assertEqual(context.langfuse_trace_id, session.langfuse_trace_id)
        self.assertEqual(context.interactive_program.name, "bash")
        self.assertEqual(context.interactive_program.command, "/bin/bash")
        self.assertEqual(context.interactive_program.argv, ("bash",))
        self.assertEqual(context.world_state["name"], "default-world")
        self.assertEqual(context.active_host, "ubuntu")
        self.assertEqual(context.active_host_metadata["hostname"], "ubuntu")
        self.assertTrue(context.active_host_metadata["users"]["passwordless_sudo"])
        self.assertEqual(
            context.active_host_metadata["filesystem"]["notable_paths"][0]["path"],
            "/home/alice",
        )
        self.assertEqual(context.history, ())
        self.assertEqual(
            [
                entry["command"]
                for entry in context.world_state["hosts_by_name"]["ubuntu"][
                    "command_history"
                ]
            ],
            ["cd /opt/app"],
        )

    def test_session_environment_metadata_reaches_agent_context(self):
        profile = server_profile()
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        connection_metadata = ConnectionMetadata(
            protocol="ssh",
            source=ConnectionEndpoint(ip="203.0.113.5", port=51234),
            destination=ConnectionEndpoint(ip="10.0.0.10", port=2222),
        )
        session = backend.create_session("alice", connection_metadata)

        backend.execute(session, "netstat -ant")

        context = agent.contexts[-1]
        self.assertEqual(context.transport_connection, connection_metadata)
        self.assertEqual(session.environment_metadata.connection, connection_metadata)
        self.assertEqual(
            context.environment_metadata.connection.source, connection_metadata.source
        )
        self.assertEqual(context.environment_metadata.connection.destination.port, 22)
        self.assertNotEqual(
            context.environment_metadata.connection.destination,
            connection_metadata.destination,
        )

    def test_backend_can_boot_session_from_system_state_profile(self):
        source = SessionState(
            username="root",
            login_username="alice",
            host_identity=HostIdentity(hostname="display-81"),
            cwd="/root",
            history=[CommandHistoryEntry(cwd="/root", command="id")],
            world_state={
                "entry_host": "display-81",
                "active_host": "display-81",
                "hosts_by_name": {
                    "display-81": {
                        "hostname": "display-81",
                        "login_banner": "Display control workstation\n",
                        "filesystem": {
                            "artifacts_by_path": {
                                "/opt/telemetry": {"type": "directory"}
                            }
                        },
                        "services_by_name": {
                            "redis": {"description": "Local cache service."}
                        },
                    }
                },
            },
        )
        state_profile = create_state_profile(
            source,
            name="system-boot",
            level="system",
            created_at="2026-06-15T12:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            runtime = StateProfileRuntime(
                store=store,
                current_profile=state_profile,
            )
            backend = AgentBackend(
                server_profile(),
                agent=RecordingAgent(),
                state_profile_runtime=runtime,
            )
            connection_metadata = ConnectionMetadata(
                protocol="ssh",
                source=ConnectionEndpoint(ip="203.0.113.5", port=51234),
                destination=ConnectionEndpoint(ip="10.0.0.10", port=2222),
            )

            session = backend.create_session("ssh-user", connection_metadata)
            indexed = store.session_profile(session.langfuse_trace_id)

        self.assertEqual(session.username, "ssh-user")
        self.assertEqual(session.login_username, "ssh-user")
        self.assertEqual(session.host_identity.hostname, "display-81")
        self.assertEqual(session.cwd, "/home/ssh-user")
        self.assertEqual(session.history, [])
        self.assertIn(
            "/opt/telemetry",
            session.world_state["hosts_by_name"]["display-81"]["filesystem"][
                "artifacts_by_path"
            ],
        )
        self.assertEqual(session.environment_metadata.connection, connection_metadata)
        self.assertEqual(
            session.environment_metadata.metadata[STATE_PROFILE_METADATA_NAME],
            "system-boot",
        )
        self.assertEqual(
            session.environment_metadata.metadata[STATE_PROFILE_METADATA_LEVEL],
            "system",
        )
        self.assertEqual(indexed.label, "system-boot:system")
        self.assertEqual(indexed.source, "boot")
        self.assertTrue(session.force_session_state_next_command)
        backend.execute(session, "diagnose boot")
        context = backend.agent.contexts[-1]
        self.assertEqual(context.username, "ssh-user")
        self.assertEqual(context.login_username, "ssh-user")
        self.assertEqual(context.active_host, "display-81")
        self.assertEqual(context.active_host_metadata["hostname"], "display-81")

    def test_forced_session_state_context_logs_shell_stack_marker(self):
        trace_logs = configure_session_trace_logging(20)
        trace_logs.setFormatter(_trace_formatter())
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")
        session.force_session_state_next_command = True

        list(backend.execute_stream(session, "diagnose stack"))

        records = [
            record.text for record in trace_logs.latest(session.langfuse_trace_id, 20)
        ]
        self.assertTrue(
            any(
                "type=shell_stack event=command_context_force_refresh" in record
                and "marker=shell-stack-exit-v1" in record
                and "command='diagnose stack'" in record
                for record in records
            ),
            records,
        )
        self.assertTrue(agent.contexts[-1].force_session_state)
        self.assertFalse(session.force_session_state_next_command)

    def test_backend_boot_from_world_state_uses_login_user_fallback(self):
        source = SessionState(
            username="root",
            login_username="alice",
            host_identity=HostIdentity(hostname="display-81"),
            world_state={
                "name": "test-world",
                "entry_host": "ubuntu",
                "active_host": "ubuntu",
                "hosts_by_name": {
                    "ubuntu": {
                        "hostname": "ubuntu",
                        "login_banner": "Display control workstation\n",
                        "filesystem": {
                            "artifacts_by_path": {
                                "/opt/telemetry": {"type": "directory"}
                            }
                        },
                        "services_by_name": {
                            "redis": {"description": "Local cache service."}
                        },
                    },
                },
                "world_facts_by_id": {
                    "system-description": {
                        "category": "system",
                        "description": "Display control workstation.",
                    }
                },
            },
        )
        state_profile = create_state_profile(
            source,
            name="world-boot",
            level="world",
            created_at="2026-06-15T12:00:00+00:00",
        )
        backend = AgentBackend(
            server_profile(),
            initial_state_profile=state_profile,
        )

        session = backend.create_session("ssh-user")

        self.assertEqual(session.username, "ssh-user")
        self.assertEqual(session.login_username, "ssh-user")
        self.assertEqual(session.cwd, "/home/ssh-user")
        self.assertEqual(session.host_identity.hostname, "ubuntu")
        self.assertIn(
            "redis",
            session.world_state["hosts_by_name"]["ubuntu"]["services_by_name"],
        )
        self.assertIn(
            "/opt/telemetry",
            session.world_state["hosts_by_name"]["ubuntu"]["filesystem"][
                "artifacts_by_path"
            ],
        )

    def test_backend_reads_runtime_default_for_future_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateProfileStore(Path(tmpdir))
            source = SessionState(
                username="alice",
                login_username="alice",
                host_identity=HostIdentity(hostname="display-81"),
                world_state={
                    "entry_host": "display-81",
                    "active_host": "display-81",
                    "hosts_by_name": {
                        "display-81": {
                            "hostname": "display-81",
                            "login_banner": "Display control workstation\n",
                            "services_by_name": {
                                "redis": {"description": "Local cache service."}
                            },
                        }
                    },
                },
            )
            store.save(
                create_state_profile(
                    source,
                    name="runtime-default",
                    level="system",
                    created_at="2026-06-15T12:00:00+00:00",
                )
            )
            runtime = StateProfileRuntime(store=store)
            backend = AgentBackend(
                server_profile(),
                state_profile_runtime=runtime,
            )

            before = backend.create_session("bob")
            runtime.set_default("runtime-default")
            after = backend.create_session("bob")
            runtime.clear_default()
            cleared = backend.create_session("bob")

        self.assertEqual(before.host_identity.hostname, "ubuntu")
        self.assertEqual(after.host_identity.hostname, "display-81")
        self.assertIn(
            "redis",
            after.world_state["hosts_by_name"]["display-81"]["services_by_name"],
        )
        self.assertEqual(cleared.host_identity.hostname, "ubuntu")

    def test_world_state_patch_is_merged_into_session_context(self):
        profile = server_profile()

        class WorldStateAgent:
            def __init__(self):
                self.contexts = []

            def respond(self, context):
                self.contexts.append(context)
                if context.command == "discover":
                    return CommandResult(
                        state_update=CommandStateUpdate(
                            world_state={
                                "hosts_by_name": {
                                    "ubuntu": {
                                        "services_by_name": {
                                            "redis": {
                                                "state": "running",
                                                "listen": "127.0.0.1:6379",
                                            }
                                        }
                                    }
                                }
                            }
                        )
                    )
                if context.command == "inspect":
                    return CommandResult(
                        state_update=CommandStateUpdate(
                            world_state={
                                "hosts_by_name": {
                                    "ubuntu": {
                                        "facts_by_id": {
                                            "redis-localhost": (
                                                "redis listens on localhost:6379"
                                            )
                                        }
                                    }
                                }
                            }
                        )
                    )
                return CommandResult(stdout="ok\n")

        agent = WorldStateAgent()
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")

        backend.execute(session, "discover")
        backend.execute(session, "inspect")

        self.assertEqual(
            session.world_state["hosts_by_name"]["ubuntu"]["services_by_name"]["redis"],
            {"state": "running", "listen": "127.0.0.1:6379"},
        )
        self.assertEqual(
            session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"],
            {"redis-localhost": "redis listens on localhost:6379"},
        )
        self.assertEqual(
            agent.contexts[-1].world_state["hosts_by_name"]["ubuntu"][
                "services_by_name"
            ]["redis"],
            {"state": "running", "listen": "127.0.0.1:6379"},
        )

    def test_world_state_merge_can_delete_keyed_entries(self):
        current = {
            "hosts_by_name": {
                "ubuntu": {
                    "services_by_name": {
                        "redis": {"state": "running"},
                        "postgres": {"state": "running"},
                    }
                }
            }
        }

        merged = merge_world_state(
            current,
            {
                "hosts_by_name": {
                    "ubuntu": {
                        "services_by_name": {"redis": {"__dreamland_delete__": True}}
                    }
                }
            },
        )

        self.assertEqual(
            merged["hosts_by_name"]["ubuntu"]["services_by_name"],
            {"postgres": {"state": "running"}},
        )
        self.assertIn("redis", current["hosts_by_name"]["ubuntu"]["services_by_name"])

    def test_world_state_merge_can_replace_subtrees(self):
        current = {
            "hosts_by_name": {
                "ubuntu": {
                    "role": "devbox",
                    "services": {"ssh": "running"},
                }
            }
        }

        merged = merge_world_state(
            current,
            {
                "hosts_by_name": {
                    "ubuntu": {
                        "__dreamland_replace__": {
                            "role": "router",
                        }
                    }
                }
            },
        )

        self.assertEqual(
            merged["hosts_by_name"]["ubuntu"],
            {"role": "router"},
        )

    def test_agent_graph_world_state_merge_uses_directive_semantics(self):
        current = {
            "hosts_by_name": {
                "ubuntu": {
                    "services_by_name": {
                        "redis": {"state": "running"},
                        "postgres": {"state": "running"},
                    }
                }
            }
        }

        merged = _merged_world_state(
            current,
            {
                "hosts_by_name": {
                    "ubuntu": {
                        "services_by_name": {"redis": {"__dreamland_delete__": True}}
                    }
                }
            },
        )

        self.assertEqual(
            merged["hosts_by_name"]["ubuntu"]["services_by_name"],
            {"postgres": {"state": "running"}},
        )

    def test_session_starts_with_shell_environment(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        self.assertEqual(session.shell_environment.variables["USER"], "alice")
        self.assertEqual(session.shell_environment.variables["HOME"], "/home/alice")
        self.assertEqual(session.shell_environment.variables["PWD"], "/home/alice")
        self.assertEqual(session.shell_environment.variables["SHELL"], "/bin/bash")
        self.assertIn("PATH", session.shell_environment.exported)

    def test_builtin_assignment_export_unset_and_printenv_are_stateful(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        self.assertEqual(backend.execute(session, "APP_ROOT=/opt/app").stdout, "")
        self.assertEqual(session.shell_environment.variables["APP_ROOT"], "/opt/app")
        self.assertNotIn("APP_ROOT", session.shell_environment.exported)

        self.assertNotIn("APP_ROOT=/opt/app", backend.execute(session, "env").stdout)
        self.assertEqual(backend.execute(session, "printenv APP_ROOT").stdout, "")
        backend.execute(session, "export APP_ROOT")
        self.assertIn("APP_ROOT=/opt/app", backend.execute(session, "env").stdout)
        self.assertEqual(
            backend.execute(session, "printenv APP_ROOT").stdout, "/opt/app\n"
        )

        backend.execute(session, "unset APP_ROOT")
        self.assertNotIn("APP_ROOT", session.shell_environment.variables)
        self.assertEqual(backend.execute(session, "printenv APP_ROOT").exit_status, 1)

    def test_env_supports_temporary_assignments(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        result = backend.execute(session, "APP_ROOT=/opt/app env")

        self.assertIn("APP_ROOT=/opt/app", result.stdout)
        self.assertNotIn("APP_ROOT", session.shell_environment.variables)

    def test_set_and_declare_show_local_shell_variables(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")
        backend.execute(session, "APP_ROOT=/opt/app")

        set_result = backend.execute(session, "set")
        declare_result = backend.execute(session, "declare -p APP_ROOT")

        self.assertIn("APP_ROOT=/opt/app", set_result.stdout)
        self.assertIn('declare -- APP_ROOT="/opt/app"', declare_result.stdout)

    def test_declare_marks_exported_variables(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")
        backend.execute(session, "export APP_ROOT=/opt/app")

        result = backend.execute(session, "declare -p APP_ROOT")

        self.assertEqual(result.stdout, 'declare -x APP_ROOT="/opt/app"\n')

    def test_cd_updates_pwd_and_oldpwd_environment(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        backend.execute(session, "cd /var/log")

        self.assertEqual(session.cwd, "/var/log")
        self.assertEqual(session.shell_environment.variables["PWD"], "/var/log")
        self.assertEqual(session.shell_environment.variables["OLDPWD"], "/home/alice")

    def test_command_list_sequences_stateful_segments(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        result = backend.execute(session, "cd /var/log; pwd")

        self.assertEqual(result.stdout, "/var/log\n")
        self.assertEqual(session.cwd, "/var/log")
        self.assertEqual(
            [entry.command for entry in session.history],
            ["cd /var/log", "pwd"],
        )

    def test_command_list_applies_and_or_short_circuiting(self):
        backend = AgentBackend(server_profile())
        session = backend.create_session("alice")

        skipped = backend.execute(session, "printenv MISSING && whoami")
        fallback = backend.execute(session, "printenv MISSING || whoami")

        self.assertEqual(skipped.stdout, "")
        self.assertEqual(skipped.exit_status, 1)
        self.assertEqual(fallback.stdout, "alice\n")
        self.assertEqual(
            [entry.command for entry in session.history],
            ["printenv MISSING", "printenv MISSING", "whoami"],
        )

    def test_ambiguous_cd_in_command_list_delegates_the_whole_line(self):
        agent = ChainCdAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")

        result = backend.execute(session, "cd portops; ls")

        self.assertEqual(result.stdout, "listing in /home/alice/portops\n")
        self.assertEqual(session.cwd, "/home/alice/portops")
        self.assertEqual(
            [context.command for context in agent.contexts],
            ["cd portops; ls"],
        )

    def test_cd_to_known_file_is_rejected_without_agent(self):
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")
        entry = CommandHistoryEntry(
            cwd="/home/alice",
            command="ls -l",
            stdout="-rw-r--r-- 1 alice alice 12 Jul 10 10:00 README.md\n",
            exit_status=0,
        )
        session.history.append(entry)
        session.world_state = append_host_command_history(
            session.world_state,
            session.host_identity.hostname,
            entry,
        )

        result = backend.execute(session, "cd README.md")

        self.assertIn("Not a directory", result.stderr)
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(agent.contexts, [])

    def test_non_root_cd_to_root_home_is_permission_denied_without_agent(self):
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")

        result = backend.execute(session, "cd /root")

        self.assertIn("Permission denied", result.stderr)
        self.assertEqual(result.exit_status, 1)
        self.assertEqual(session.cwd, "/home/user")
        self.assertEqual(agent.contexts, [])

    def test_cd_rejects_directory_without_execute_permission_from_listing(self):
        agent = RecordingAgent()
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")
        entry = CommandHistoryEntry(
            cwd="/home/alice",
            command="ls -la",
            stdout="drwx------ 2 root root 4096 Jul 10 10:00 private\n",
            exit_status=0,
        )
        session.history.append(entry)
        session.world_state = append_host_command_history(
            session.world_state,
            session.host_identity.hostname,
            entry,
        )

        result = backend.execute(session, "cd private")

        self.assertIn("Permission denied", result.stderr)
        self.assertEqual(result.exit_status, 1)
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(agent.contexts, [])

    def test_echo_is_routed_to_agent_with_shell_environment_context(self):
        profile = server_profile()
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")
        backend.execute(session, "export APP_ROOT=/opt/app")

        backend.execute(session, "echo $APP_ROOT")

        context = agent.contexts[-1]
        self.assertEqual(context.command, "echo $APP_ROOT")
        self.assertEqual(context.shell_environment.variables["APP_ROOT"], "/opt/app")
        self.assertIn("APP_ROOT", context.shell_environment.exported)

    def test_session_uses_state_profile_active_host_default_interactive_program(self):
        profile = server_profile()
        state = {"world_state": _packaged_default_world_state()}
        state["world_state"]["hosts_by_name"]["ubuntu"]["shell"]["default"] = {
            "name": "zsh",
            "command": "/usr/bin/zsh",
            "argv": ["zsh", "-l"],
            "prompt_template": "{username}@zsh:{cwd}% ",
            "metadata": {"line_editor": "local"},
        }
        state_profile = StateProfile(
            name="zsh-world",
            level="world",
            state=state,
            created_at="2026-07-11T00:00:00+00:00",
        )
        agent = RecordingAgent()
        backend = AgentBackend(
            profile,
            agent=agent,
            state_profile_runtime=StateProfileRuntime(current_profile=state_profile),
        )
        session = backend.create_session("alice")

        backend.execute(session, "echo hello")

        self.assertEqual(session.username, "alice")
        self.assertEqual(session.interactive_program.name, "zsh")
        self.assertEqual(session.interactive_program.command, "/usr/bin/zsh")
        self.assertEqual(session.interactive_program.argv, ("zsh", "-l"))
        self.assertEqual(backend.prompt(session), "alice@zsh:~% ")
        self.assertEqual(agent.contexts[-1].interactive_program.name, "zsh")

    def test_shell_stack_update_changes_active_context_without_bloating_history(self):
        profile = server_profile()
        agent = ShellStackAgent()
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")

        result = backend.execute(session, "sudo -i")
        whoami_result = backend.execute(session, "whoami")
        agent_result = backend.execute(session, "id")
        exit_result = backend.execute(session, "exit")

        self.assertEqual(result.stdout, "")
        self.assertEqual(session.login_username, "alice")
        self.assertEqual(whoami_result.stdout, "root\n")
        self.assertEqual(agent_result.stdout, "handled as root\n")
        self.assertEqual(agent.contexts[-1].login_username, "alice")
        self.assertEqual(agent.contexts[-1].username, "root")
        self.assertEqual(len(agent.contexts[-1].shell_stack), 2)
        self.assertEqual(exit_result.exit_status, 0)
        self.assertEqual(session.username, "alice")
        self.assertEqual(session.cwd, "/home/alice")
        self.assertEqual(session.shell_environment.variables["USER"], "alice")
        self.assertEqual(len(session.shell_stack), 1)
        self.assertIn("alice@ubuntu:~", backend.prompt(session))
        self.assertEqual(session.history[-2].command, "id")
        self.assertEqual(session.history[-1].command, "exit")
        self.assertFalse(hasattr(session.history[-1], "shell_stack"))

    def test_result_metadata_does_not_bloat_command_history(self):
        backend = AgentBackend(server_profile(), agent=MetadataAgent())
        session = backend.create_session("alice")

        result = backend.execute(session, "bad structured output")

        self.assertEqual(result.metadata["structured_parse_error"], "test")
        self.assertEqual(session.history[-1].stdout, "metadata result\n")
        self.assertFalse(hasattr(session.history[-1], "metadata"))

    def test_agent_context_uses_one_trace_id_per_session(self):
        profile = server_profile()
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        first_session = backend.create_session("alice")
        second_session = backend.create_session("alice")

        backend.execute(first_session, "first command")
        backend.execute(first_session, "second command")
        backend.execute(second_session, "third command")

        self.assertEqual(
            agent.contexts[0].langfuse_trace_id,
            agent.contexts[1].langfuse_trace_id,
        )
        self.assertEqual(
            agent.contexts[0].langfuse_trace_id, first_session.langfuse_trace_id
        )
        self.assertEqual(
            agent.contexts[2].langfuse_trace_id, second_session.langfuse_trace_id
        )
        self.assertNotEqual(
            first_session.langfuse_trace_id, second_session.langfuse_trace_id
        )

    def test_agent_receives_previous_commands_and_outputs(self):
        profile = server_profile()
        agent = RecordingAgent()
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")

        backend.execute(session, "pwd")
        backend.execute(session, "first agent command")
        backend.execute(session, "second agent command")

        second_context = agent.contexts[-1]
        host_history = second_context.world_state["hosts_by_name"]["ubuntu"][
            "command_history"
        ]
        self.assertEqual(second_context.history, ())
        self.assertEqual(
            [(entry["command"], entry["stdout"]) for entry in host_history],
            [
                ("pwd", "/home/alice\n"),
                ("first agent command", "handled first agent command in /home/alice\n"),
            ],
        )
        self.assertEqual(session.history[-1].command, "second agent command")
        self.assertEqual(
            session.history[-1].stdout,
            "handled second agent command in /home/alice\n",
        )

    def test_command_history_collapses_accidental_structured_result_envelope(self):
        class EnvelopeAgent:
            handles_builtins = True

            def respond(self, context):
                return CommandResult(
                    stdout=json.dumps(
                        {
                            "stdout": "real output\n",
                            "stderr": "",
                            "exit_status": 0,
                            "state_update": {},
                        }
                    )
                    + "\n"
                )

        profile = server_profile()
        backend = AgentBackend(profile, agent=EnvelopeAgent())
        session = backend.create_session("alice")

        result = backend.execute(session, "wrapped")

        self.assertTrue(result.stdout.startswith('{"stdout":'))
        self.assertEqual(session.history[-1].stdout, "real output\n")
        host_history = session.world_state["hosts_by_name"]["ubuntu"]["command_history"]
        self.assertEqual(host_history[-1]["stdout"], "real output\n")

    def test_deterministic_command_agent_is_deterministic(self):
        profile = server_profile()
        backend = AgentBackend(profile, agent=DeterministicCommandAgent())
        session = backend.create_session("alice")

        first = backend.execute(session, "uname -a")
        second = backend.execute(session, "uname -a")

        self.assertEqual(first, second)


@unittest.skipUnless(langgraph_available, "LangGraph is not installed")
class LangGraphCommandAgentTests(unittest.TestCase):
    def test_agent_returns_model_output(self):
        model = RecordingModel("agent handled input")
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo hello",
                langfuse_trace_id="trace-test",
                world_state=append_host_command_history(
                    _packaged_default_world_state(),
                    "ubuntu",
                    CommandHistoryEntry(
                        cwd="/home/alice",
                        command="pwd",
                        stdout="/home/alice\n",
                        exit_status=130,
                        interrupted=True,
                        signal="SIGINT",
                    ),
                ),
            )
        )

        self.assertEqual(result.stdout, "agent handled input\n")
        self.assertIn(
            'The authoritative active program is "bash" ("/bin/bash").',
            model.messages[-1][0].content,
        )
        self.assertIn(
            "Return a pop transition only when processing the current input "
            "causes the active program itself to exit.",
            model.messages[-1][0].content,
        )
        self.assertIn("current command: echo hello", model.messages[-1][1].content)
        self.assertIn("program name: bash", model.messages[-1][1].content)
        self.assertIn("program command: /bin/bash", model.messages[-1][1].content)
        self.assertIn("program argv: bash", model.messages[-1][1].content)
        self.assertIn("shell environment:", model.messages[-1][1].content)
        self.assertIn("HOME exported: /home/alice", model.messages[-1][1].content)
        self.assertIn("environment metadata:", model.messages[-1][1].content)
        self.assertNotIn("active host:", model.messages[-1][1].content)
        self.assertEqual(
            _world_state_from_prompt(model.messages[-1][1].content)["active_host"],
            "ubuntu",
        )
        self.assertNotIn("active host metadata:", model.messages[-1][1].content)
        self.assertIn("hostname: ubuntu", model.messages[-1][1].content)
        self.assertNotIn("profile:", model.messages[-1][1].content)
        self.assertNotIn("ubuntu-openssh", model.messages[-1][1].content)
        self.assertIn('"passwordless_sudo": true', model.messages[-1][1].content)
        self.assertIn('"command_history":', model.messages[-1][1].content)
        self.assertIn('"command": "pwd"', model.messages[-1][1].content)
        self.assertIn('"stdout": "/home/alice', model.messages[-1][1].content)
        self.assertIn('"interrupted": true', model.messages[-1][1].content)
        self.assertIn('"signal": "SIGINT"', model.messages[-1][1].content)
        self.assertNotIn("prior host command history:", model.messages[-1][1].content)
        self.assertNotIn("output is partial", model.messages[-1][1].content)

    def test_agent_prompt_projection_follows_active_host_across_pivot(self):
        pivot_response = json.dumps(
            {
                "stdout": "connected\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "host_identity": {"hostname": "db01"},
                    "cwd": "/home/alice",
                    "shell_stack_transition": {"operation": "push"},
                },
            }
        )
        model = RecordingModel(pivot_response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")
        workstation = session.world_state["hosts_by_name"]["ubuntu"]
        workstation["filesystem"]["projection_test"] = "WORKSTATION-FULL"
        workstation["command_history"] = [
            {"command": "before-pivot", "stdout": "WORKSTATION-HISTORY\n"}
        ]
        session.world_state["hosts_by_name"]["db01"] = {
            "hostname": "db01",
            "aliases": ["database"],
            "ip": "10.40.12.21",
            "purpose": {"summary": "Clinical database."},
            "services_by_name": {
                "postgresql": {
                    "address": "10.40.12.21",
                    "port": 5432,
                    "protocol": "tcp",
                    "status": "running",
                    "private_detail": "PEER-SERVICE-HIDDEN",
                }
            },
            "filesystem": {"projection_test": "DATABASE-FULL"},
            "command_history": [
                {"command": "before-login", "stdout": "DATABASE-HISTORY\n"}
            ],
            "users_by_username": {"dba": {"private_detail": "DATABASE-USER-HIDDEN"}},
        }

        backend.execute(session, "connect database")
        model.response = json.dumps(
            {
                "stdout": "database ready\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        backend.execute(session, "inspect database")

        first_prompt = model.messages[0][1].content
        second_prompt = model.messages[1][1].content
        first_world = _world_state_from_prompt(first_prompt)
        second_world = _world_state_from_prompt(second_prompt)

        self.assertNotIn("active host:", first_prompt)
        self.assertNotIn("active host:", second_prompt)
        self.assertEqual(first_world["active_host"], "ubuntu")
        self.assertEqual(second_world["active_host"], "db01")
        self.assertEqual(first_world["entry_host"], "ubuntu")
        self.assertEqual(second_world["entry_host"], "ubuntu")
        self.assertNotIn("active host metadata:", first_prompt)
        self.assertNotIn("active host metadata:", second_prompt)

        self.assertEqual(
            first_world["hosts_by_name"]["ubuntu"]["filesystem"]["projection_test"],
            "WORKSTATION-FULL",
        )
        self.assertNotIn("filesystem", first_world["hosts_by_name"]["db01"])
        self.assertNotIn("command_history", first_world["hosts_by_name"]["db01"])
        self.assertNotIn("users_by_username", first_world["hosts_by_name"]["db01"])
        self.assertEqual(first_world["hosts_by_name"]["db01"]["ip"], "10.40.12.21")
        self.assertEqual(
            first_world["hosts_by_name"]["db01"]["services_by_name"]["postgresql"],
            {
                "address": "10.40.12.21",
                "port": 5432,
                "protocol": "tcp",
                "status": "running",
            },
        )

        self.assertNotIn("filesystem", second_world["hosts_by_name"]["ubuntu"])
        self.assertNotIn("command_history", second_world["hosts_by_name"]["ubuntu"])
        self.assertEqual(
            second_world["hosts_by_name"]["db01"]["filesystem"]["projection_test"],
            "DATABASE-FULL",
        )
        self.assertEqual(
            second_world["hosts_by_name"]["db01"]["command_history"][0]["stdout"],
            "DATABASE-HISTORY\n",
        )

        self.assertEqual(session.world_state["active_host"], "db01")
        self.assertEqual(
            session.world_state["hosts_by_name"]["ubuntu"]["filesystem"][
                "projection_test"
            ],
            "WORKSTATION-FULL",
        )
        self.assertEqual(
            session.world_state["hosts_by_name"]["db01"]["filesystem"][
                "projection_test"
            ],
            "DATABASE-FULL",
        )

        backend.execute(session, "exit")
        backend.execute(session, "inspect workstation")
        returning_prompt = model.messages[-1][1].content
        returning_world = _world_state_from_prompt(returning_prompt)
        self.assertNotIn("active host:", returning_prompt)
        self.assertEqual(returning_world["active_host"], "ubuntu")
        self.assertEqual(returning_world["entry_host"], "ubuntu")
        self.assertEqual(session.world_state["active_host"], "ubuntu")
        self.assertEqual(
            returning_world["hosts_by_name"]["ubuntu"]["filesystem"]["projection_test"],
            "WORKSTATION-FULL",
        )
        self.assertNotIn("filesystem", returning_world["hosts_by_name"]["db01"])

    def test_agent_focuses_system_prompt_on_authoritative_active_program(self):
        response = json.dumps(
            {
                "stdout": "2\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        python = InteractiveProgram(
            name="python",
            command="/usr/bin/python3",
            argv=("python3",),
            prompt_template=">>> ",
        )
        root_environment = default_shell_environment(
            username="root",
            hostname="ubuntu",
            cwd="/root",
        )

        result = agent.respond(
            CommandContext(
                username="root",
                login_username="user",
                cwd="/root",
                hostname="ubuntu",
                command="1+1",
                langfuse_trace_id="trace-test",
                interactive_program=python,
                shell_environment=root_environment,
                shell_stack=(
                    ShellFrame(
                        username="root",
                        cwd="/root",
                        interactive_program=python,
                        shell_environment=root_environment,
                    ),
                ),
            )
        )

        system_prompt = model.messages[-1][0].content
        self.assertIn(
            'The authoritative active program is "python" ("/usr/bin/python3").',
            system_prompt,
        )
        self.assertIn(
            "Ordinary input and completed evaluations do not end the program.",
            system_prompt,
        )
        self.assertIn("Otherwise, do not pop the stack.", system_prompt)
        self.assertEqual(result.stdout, "2\n")
        self.assertIsNone(result.state_update.shell_stack_transition)

    def test_active_program_instruction_tracks_model_driven_stack_push(self):
        model = RecordingModel(
            json.dumps(
                {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {
                        "interactive_program": {
                            "name": "example-repl",
                            "command": "/usr/bin/example-repl",
                            "argv": ["example-repl"],
                            "prompt_template": "example> ",
                            "metadata": {},
                        },
                        "shell_stack_transition": {"operation": "push"},
                    },
                }
            )
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")

        backend.execute(session, "start-example-repl")
        model.response = json.dumps(
            {
                "stdout": "evaluated\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        backend.execute(session, "ordinary-input")

        self.assertIn(
            'The authoritative active program is "example-repl" '
            '("/usr/bin/example-repl").',
            model.messages[-1][0].content,
        )
        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.interactive_program.name, "example-repl")

    def test_python_exit_is_delegated_to_model_transition(self):
        model = RecordingModel(
            json.dumps(
                {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {
                        "shell_stack_transition": {"operation": "pop"},
                    },
                }
            )
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        parent_frame = session.shell_stack[-1]
        python_frame = ShellFrame(
            username="user",
            cwd="/home/user",
            interactive_program=InteractiveProgram(
                name="python",
                command="/usr/bin/python3",
                argv=("python3",),
                prompt_template=">>> ",
            ),
            shell_environment=session.shell_environment,
        )
        replace_shell_stack(session, (parent_frame, python_frame))

        result = backend.execute(session, "exit")

        self.assertEqual(len(model.messages), 1)
        self.assertIn(
            'The authoritative active program is "python" ("/usr/bin/python3").',
            model.messages[-1][0].content,
        )
        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="pop"),
        )
        self.assertEqual(session.shell_stack, [parent_frame])

    def test_unknown_program_exit_without_model_transition_remains_active(self):
        model = RecordingModel(
            json.dumps(
                {
                    "stdout": "still active\n",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                }
            )
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        parent_frame = session.shell_stack[-1]
        program_frame = ShellFrame(
            username="user",
            cwd="/home/user",
            interactive_program=InteractiveProgram(
                name="custom-console",
                command="/opt/custom-console",
                argv=("custom-console",),
            ),
            shell_environment=session.shell_environment,
        )
        replace_shell_stack(session, (parent_frame, program_frame))

        result = backend.execute(session, "exit")

        self.assertEqual(result.stdout, "still active\n")
        self.assertEqual(len(model.messages), 1)
        self.assertEqual(session.shell_stack, [parent_frame, program_frame])

    def test_bash_exit_uses_builtin_stack_pop_without_model_call(self):
        model = RecordingModel("model should not be called")
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        parent_frame = session.shell_stack[-1]
        nested_bash = ShellFrame(
            username="user",
            cwd="/home/user",
            interactive_program=InteractiveProgram(
                name="bash",
                command="/bin/bash",
                argv=("bash",),
            ),
            shell_environment=session.shell_environment,
        )
        replace_shell_stack(session, (parent_frame, nested_bash))

        backend.execute(session, "exit")

        self.assertEqual(model.messages, [])
        self.assertEqual(session.shell_stack, [parent_frame])

    def test_agent_passes_langfuse_callbacks_and_metadata_to_graph(self):
        callback = RecordingCallback()
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
            callbacks=(callback,),
        )
        graph = RecordingGraph()
        agent.graph = graph
        world_state = append_host_command_history(
            _packaged_default_world_state(),
            "ubuntu",
            CommandHistoryEntry(
                cwd="/home/alice",
                command="pwd",
                stdout="/home/alice\n",
            ),
        )
        world_state["hosts_by_name"]["peer01"] = {
            "hostname": "peer01",
            "ip": "192.0.2.20",
            "command_history": [
                {"command": "secret", "stdout": "PEER-HISTORY-SENTINEL\n"}
            ],
            "filesystem": {"private": "PEER-FILESYSTEM-SENTINEL"},
        }
        projected_world_state = project_world_state_for_command_emulator(
            world_state,
            detailed_host_refs=("ubuntu",),
        )

        with self.assertLogs("dreamland.tokens", level="INFO") as logs:
            result = agent.respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="uptime",
                    langfuse_trace_id="trace-test",
                    world_state=world_state,
                )
            )

        self.assertEqual(result.stdout, "traced output\n")
        self.assertIn("context_projection_prepared=active-host-v1", logs.output[0])
        self.assertNotIn("PEER-HISTORY-SENTINEL", logs.output[0])
        self.assertNotIn("PEER-FILESYSTEM-SENTINEL", logs.output[0])
        self.assertEqual(graph.config["callbacks"][1:], [callback])
        self.assertIsInstance(
            graph.config["callbacks"][0],
            TokenUsageLoggingCallback,
        )
        self.assertEqual(graph.config["run_name"], "dreamland-command-agent")
        self.assertEqual(graph.config["metadata"]["dreamland.command"], "uptime")
        self.assertEqual(
            graph.config["metadata"]["dreamland.interactive_program"], "bash"
        )
        self.assertEqual(graph.config["metadata"]["dreamland.history_length"], "1")
        self.assertEqual(graph.config["metadata"]["dreamland.shell_stack_depth"], "1")
        projection_metadata = graph.config["metadata"]
        self.assertTrue(
            all(
                isinstance(value, str)
                for key, value in projection_metadata.items()
                if key.startswith("dreamland.context_projection.")
            )
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.mode"],
            "active-host-v1",
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.active_host"],
            "ubuntu",
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.detailed_hosts"],
            "ubuntu",
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.host_count"],
            "2",
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.detailed_host_count"],
            "1",
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.compact_host_count"],
            "1",
        )
        expected_full_chars = len(json.dumps(world_state, indent=2, sort_keys=False))
        expected_projected_chars = len(
            json.dumps(projected_world_state, indent=2, sort_keys=False)
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.full_world_chars"],
            str(expected_full_chars),
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.projected_world_chars"],
            str(expected_projected_chars),
        )
        self.assertEqual(
            projection_metadata["dreamland.context_projection.saved_world_chars"],
            str(expected_full_chars - expected_projected_chars),
        )
        self.assertEqual(graph.config["metadata"]["langfuse_session_id"], "trace-test")
        self.assertEqual(graph.config["metadata"]["langfuse_user_id"], "alice")
        self.assertEqual(
            graph.config["metadata"]["langfuse_trace_name"],
            "dreamland:alice@ubuntu",
        )
        self.assertEqual(graph.config["tags"], ["dreamland"])
        self.assertNotIn("profile_name", graph.state)
        self.assertNotIn("dreamland.profile", graph.config["metadata"])
        self.assertEqual(graph.state["interactive_program"]["name"], "bash")
        self.assertEqual(graph.state["interactive_program"]["command"], "/bin/bash")
        self.assertEqual(graph.state["interactive_program"]["argv"], ["bash"])
        self.assertIsNone(graph.state["interactive_program"]["prompt_template"])
        self.assertEqual(
            graph.state["shell_environment"]["variables"]["HOME"], "/home/alice"
        )
        self.assertIn("HOME", graph.state["shell_environment"]["exported"])
        self.assertEqual(graph.state["active_host"], "ubuntu")
        self.assertEqual(graph.state["active_host_metadata"]["hostname"], "ubuntu")
        self.assertEqual(
            graph.state["world_state"]["hosts_by_name"]["peer01"]["command_history"][0][
                "stdout"
            ],
            "PEER-HISTORY-SENTINEL\n",
        )

    def test_agent_parses_structured_model_response_with_state_delta(self):
        root_environment = default_shell_environment(
            username="root",
            hostname="ubuntu",
            cwd="/root",
        )
        root_variables = dict(root_environment.variables)
        root_variables.update({"HOME": "/root", "PWD": "/root"})
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack": [
                        {
                            "username": "alice",
                            "cwd": "/home/alice",
                            "interactive_program": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                                "prompt_template": None,
                                "metadata": {},
                            },
                            "shell_environment": {
                                "variables": {
                                    "HOME": "/home/alice",
                                    "USER": "alice",
                                    "PWD": "/home/alice",
                                    "SHELL": "/bin/bash",
                                },
                                "exported": ["HOME", "USER", "PWD", "SHELL"],
                            },
                            "metadata": {},
                        },
                        {
                            "username": "root",
                            "cwd": "/root",
                            "interactive_program": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["-bash"],
                                "prompt_template": None,
                                "metadata": {"login_shell": True},
                            },
                            "shell_environment": {
                                "variables": root_variables,
                                "exported": sorted(root_environment.exported),
                            },
                            "metadata": {"via": "sudo -i"},
                        },
                    ]
                },
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        base_context = {
            "username": "alice",
            "login_username": "alice",
            "cwd": "/home/alice",
            "hostname": "ubuntu",
            "langfuse_trace_id": "structured-thread",
        }

        sudo_result = agent.respond(CommandContext(command="sudo -i", **base_context))
        whoami_result = agent.respond(CommandContext(command="whoami", **base_context))

        self.assertEqual(sudo_result.stdout, "")
        self.assertEqual(sudo_result.state_update.shell_stack[-1].username, "root")
        self.assertEqual(sudo_result.state_update.shell_stack[-1].cwd, "/root")
        self.assertEqual(
            sudo_result.state_update.shell_stack[-1].interactive_program.argv,
            ("-bash",),
        )
        self.assertEqual(whoami_result.stdout, "root\n")

    def test_agent_normalizes_incomplete_structured_shell_stack(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack": [
                        {
                            "username": "guest",
                            "cwd": "/home/user",
                            "interactive_program": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                                "prompt_template": None,
                                "metadata": {},
                            },
                            "shell_environment": {
                                "variables": {},
                                "exported": [],
                            },
                            "metadata": {},
                        },
                        {
                            "username": "guest",
                            "cwd": "/root",
                            "interactive_program": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                                "prompt_template": None,
                                "metadata": {},
                            },
                            "shell_environment": {
                                "variables": {},
                                "exported": [],
                            },
                            "metadata": {},
                        },
                    ]
                },
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        base_context = {
            "username": "user",
            "login_username": "user",
            "cwd": "/home/user",
            "hostname": "ubuntu",
            "langfuse_trace_id": "malformed-structured-thread",
        }

        sudo_result = agent.respond(CommandContext(command="sudo -i", **base_context))
        whoami_result = agent.respond(CommandContext(command="whoami", **base_context))

        self.assertEqual(sudo_result.state_update.shell_stack[0].username, "user")
        self.assertEqual(
            sudo_result.state_update.shell_stack[0].shell_environment.variables["USER"],
            "user",
        )
        self.assertEqual(sudo_result.state_update.shell_stack[-1].username, "root")
        self.assertEqual(
            sudo_result.state_update.shell_stack[-1].shell_environment.variables[
                "USER"
            ],
            "root",
        )
        self.assertEqual(
            sudo_result.state_update.shell_stack[-1].shell_environment.variables[
                "HOME"
            ],
            "/root",
        )
        self.assertEqual(whoami_result.stdout, "root\n")

    def test_agent_preserves_parent_when_model_returns_only_nested_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack": [
                        {
                            "username": "root",
                            "cwd": "/root",
                            "interactive_program": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                                "prompt_template": None,
                                "metadata": {},
                            },
                            "shell_environment": {
                                "variables": {
                                    "HOME": "/root",
                                    "USER": "root",
                                    "LOGNAME": "root",
                                    "PWD": "/root",
                                    "SHELL": "/bin/bash",
                                },
                                "exported": ["HOME", "USER", "LOGNAME", "PWD", "SHELL"],
                            },
                            "metadata": {},
                        },
                    ]
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        base_context = {
            "username": "user",
            "login_username": "user",
            "cwd": "/home/user",
            "hostname": "ubuntu",
            "langfuse_trace_id": "single-frame-sudo-thread",
        }

        sudo_result = agent.respond(CommandContext(command="sudo -i", **base_context))
        exit_result = agent.respond(CommandContext(command="exit", **base_context))

        self.assertEqual(len(sudo_result.state_update.shell_stack), 2)
        self.assertEqual(sudo_result.state_update.shell_stack[0].username, "user")
        self.assertEqual(sudo_result.state_update.shell_stack[-1].username, "root")
        self.assertEqual(exit_result.state_update.shell_stack[-1].username, "user")
        self.assertEqual(exit_result.state_update.shell_stack[-1].cwd, "/home/user")

    def test_agent_preserves_parent_when_model_returns_only_python_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack": [
                        {
                            "username": "user",
                            "cwd": "/home/user",
                            "interactive_program": {
                                "name": "python",
                                "command": "/usr/bin/python3",
                                "argv": ["python3"],
                                "prompt_template": ">>> ",
                                "metadata": {},
                            },
                            "shell_environment": {
                                "variables": {
                                    "HOME": "/home/user",
                                    "USER": "user",
                                    "LOGNAME": "user",
                                    "PWD": "/home/user",
                                    "SHELL": "/bin/bash",
                                },
                                "exported": [
                                    "HOME",
                                    "USER",
                                    "LOGNAME",
                                    "PWD",
                                    "SHELL",
                                ],
                            },
                            "metadata": {},
                        },
                    ]
                },
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        base_context = {
            "username": "user",
            "login_username": "user",
            "cwd": "/home/user",
            "hostname": "ubuntu",
            "langfuse_trace_id": "single-frame-python-thread",
        }

        python_result = agent.respond(CommandContext(command="python3", **base_context))
        model.response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack_transition": {"operation": "pop"},
                },
            }
        )
        exit_result = agent.respond(CommandContext(command="exit", **base_context))

        self.assertEqual(len(python_result.state_update.shell_stack), 2)
        self.assertEqual(
            python_result.state_update.shell_stack[-1].interactive_program.name,
            "python",
        )
        self.assertEqual(
            exit_result.state_update.shell_stack_transition,
            ShellStackTransition(operation="pop"),
        )
        model.response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        agent.respond(CommandContext(command="next-input", **base_context))
        self.assertIn(
            'The authoritative active program is "bash" ("/bin/bash").',
            model.messages[-1][0].content,
        )

    def test_agent_repairs_nested_python_stack_from_trace_payload(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "interactive_program": {
                        "program name": "python3",
                        "program command": "python3",
                        "program argv": [],
                    },
                    "shell_stack": [
                        {
                            "username": "user",
                            "cwd": "/home/user",
                            "interactive_program": {
                                "program name": "bash",
                                "program command": "/bin/bash",
                                "program argv": [],
                            },
                            "shell_environment": {
                                "HOME": "/home/user",
                                "PWD": "/home/user",
                                "SHELL": "/bin/bash",
                                "USER": "user",
                            },
                        },
                        {
                            "username": "root",
                            "cwd": "/root",
                            "interactive_program": {
                                "program name": "python3",
                                "program command": "python3",
                                "program argv": [],
                            },
                            "shell_environment": {
                                "HOME": "/root",
                                "PWD": "/root",
                                "SHELL": "/bin/bash",
                                "USER": "root",
                            },
                        },
                    ],
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        user_frame = ShellFrame(
            username="user",
            cwd="/home/user",
            interactive_program=InteractiveProgram(
                name="bash",
                command="/bin/bash",
                argv=("bash",),
            ),
            shell_environment=default_shell_environment(
                username="user",
                hostname="ubuntu",
                cwd="/home/user",
            ),
        )
        root_frame = ShellFrame(
            username="root",
            cwd="/root",
            interactive_program=InteractiveProgram(
                name="bash",
                command="/bin/bash",
                argv=("-bash",),
            ),
            shell_environment=default_shell_environment(
                username="root",
                hostname="ubuntu",
                cwd="/root",
            ),
        )
        context = CommandContext(
            username="root",
            login_username="user",
            cwd="/root",
            hostname="ubuntu",
            command="python3",
            langfuse_trace_id="nested-python-trace-thread",
            shell_stack=(user_frame, root_frame),
        )

        python_result = agent.respond(context)

        self.assertEqual(len(python_result.state_update.shell_stack), 3)
        self.assertEqual(
            [
                frame.interactive_program.name
                for frame in python_result.state_update.shell_stack
            ],
            ["bash", "bash", "python"],
        )
        self.assertEqual(
            python_result.state_update.shell_stack[:2],
            (user_frame, root_frame),
        )
        self.assertEqual(
            python_result.state_update.shell_stack[
                -1
            ].interactive_program.prompt_template,
            ">>> ",
        )
        self.assertEqual(
            python_result.state_update.shell_stack[-1].shell_environment.variables[
                "USER"
            ],
            "root",
        )

    def test_model_driven_pop_restores_hidden_parent_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack_transition": {"operation": "pop"},
                },
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        parent_frame = ShellFrame(
            username="root",
            cwd="/root/private-parent",
            interactive_program=InteractiveProgram(
                name="bash",
                command="/bin/bash",
                argv=("-bash",),
                prompt_template="root-parent$ ",
            ),
            shell_environment=ShellEnvironment(
                variables={
                    "HOME": "/root",
                    "PARENT_SECRET": "not-for-model",
                    "PWD": "/root/private-parent",
                    "SHELL": "/bin/bash",
                    "USER": "root",
                },
                exported=frozenset({"HOME", "PARENT_SECRET", "PWD", "SHELL", "USER"}),
            ),
            metadata={"source": "authoritative-parent"},
        )
        active_frame = ShellFrame(
            username="root",
            cwd="/srv/console",
            interactive_program=InteractiveProgram(
                name="acme-console",
                command="/usr/bin/acme-console",
                argv=("acme-console",),
                prompt_template="acme> ",
            ),
            shell_environment=ShellEnvironment(
                variables={
                    "HOME": "/root",
                    "PWD": "/srv/console",
                    "SHELL": "/bin/bash",
                    "USER": "root",
                },
                exported=frozenset({"HOME", "PWD", "SHELL", "USER"}),
            ),
        )
        replace_shell_stack(session, (parent_frame, active_frame))

        result = backend.execute(session, "exit()")

        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="pop"),
        )
        self.assertEqual(session.shell_stack, [parent_frame])
        self.assertEqual(session.interactive_program, parent_frame.interactive_program)
        prompt = model.messages[-1][1].content
        self.assertIn("shell stack depth: 2", prompt)
        self.assertIn("program name: acme-console", prompt)
        self.assertNotIn("PARENT_SECRET", prompt)
        self.assertNotIn("/root/private-parent", prompt)
        self.assertIn(
            "shell_stack_transition for stack lifecycle changes",
            model.messages[-1][0].content,
        )

        backend.execute(session, "next-input")

        next_prompt = model.messages[-1][1].content
        self.assertIn("shell stack depth: 1", next_prompt)
        self.assertIn("program name: bash", next_prompt)
        self.assertIn("cwd: /root/private-parent", next_prompt)

    def test_model_driven_push_preserves_authoritative_parent_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "interactive_program": {
                        "name": "custom-repl",
                        "command": "/opt/custom-repl",
                        "argv": ["custom-repl"],
                        "prompt_template": "custom> ",
                        "metadata": {},
                    },
                    "shell_stack_transition": {
                        "operation": "push",
                        "username": "root",
                    },
                },
            }
        )
        model = RecordingModel(response)
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        parent_frame = session.shell_stack[0]

        result = backend.execute(session, "start-custom-repl")

        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="push", username="root"),
        )
        self.assertEqual(session.shell_stack[0], parent_frame)
        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.username, "root")
        self.assertEqual(session.interactive_program.name, "custom-repl")
        self.assertEqual(session.interactive_program.prompt_template, "custom> ")

    def test_model_push_normalizes_frame_state_misplaced_in_transition(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack_transition": {
                        "operation": "push",
                        "interactive_program": {
                            "name": "python3",
                            "command": "python3",
                            "argv": ["python3"],
                            "prompt_template": ">>> ",
                            "metadata": {},
                        },
                        "cwd": "/root",
                        "shell_environment": {
                            "variables": {
                                "HOME": "/root",
                                "PWD": "/root",
                                "SHELL": "/bin/bash",
                                "USER": "root",
                            },
                            "exported": ["HOME", "PWD", "SHELL", "USER"],
                        },
                    },
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("root")

        result = backend.execute(session, "python3")

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.interactive_program.name, "python")
        self.assertEqual(session.interactive_program.command, "python3")
        self.assertEqual(session.interactive_program.prompt_template, ">>> ")
        self.assertEqual(result.state_update.cwd, "/root")
        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="push"),
        )
        self.assertEqual(
            result.metadata["dreamland.transition.normalized"],
            "cwd,interactive_program,shell_environment",
        )
        self.assertEqual(
            result.metadata["dreamland.transition.requested_operation"], "push"
        )
        self.assertEqual(
            result.metadata["dreamland.transition.applied_operation"], "push"
        )
        self.assertEqual(
            result.metadata["dreamland.transition.resulting_stack_depth"], 2
        )
        self.assertEqual(
            result.metadata["dreamland.transition.resulting_active_program"],
            "python",
        )

    def test_model_bare_push_clones_active_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack_transition": {
                        "operation": "push",
                    },
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        original_frame = session.shell_stack[-1]

        result = backend.execute(session, "start-something")

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.shell_stack[0], original_frame)
        self.assertEqual(session.shell_stack[1], original_frame)
        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="push"),
        )
        self.assertTrue(result.metadata["dreamland.transition.accepted"])
        self.assertEqual(
            result.metadata["dreamland.transition.requested_operation"], "push"
        )
        self.assertEqual(
            result.metadata["dreamland.transition.applied_operation"], "push"
        )

    def test_model_push_merges_opaque_metadata_into_cloned_frame(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "shell_stack_transition": {
                        "operation": "push",
                        "metadata": {
                            "context_id": "nested-1",
                            "shared": "transition",
                        },
                    },
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        session.shell_stack[-1].metadata.update(
            {"parent_id": "root-1", "shared": "parent"}
        )

        backend.execute(session, "start-something")

        self.assertEqual(
            session.shell_stack[-1].metadata,
            {
                "parent_id": "root-1",
                "shared": "transition",
                "context_id": "nested-1",
            },
        )

    def test_model_program_only_update_is_normalized_to_push(self):
        response = json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "interactive_program": {
                        "name": "python3",
                        "command": "python3",
                        "argv": ["python3", "-i"],
                        "prompt_template": ">>> ",
                        "metadata": {},
                    },
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")

        result = backend.execute(session, "python3 -i")

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.interactive_program.name, "python")
        self.assertEqual(session.interactive_program.command, "python3")
        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="push"),
        )
        self.assertEqual(
            result.metadata["dreamland.transition.normalized"],
            "implicit_program_push",
        )
        self.assertEqual(
            result.metadata["dreamland.transition.applied_operation"], "push"
        )

    def test_active_ssh_program_only_update_replaces_active_frame(self):
        response = json.dumps(
            {
                "stdout": "\r\nPermission denied, please try again.\r\n"
                "historian@hmi-core01's password: ",
                "stderr": "",
                "exit_status": 0,
                "state_update": {
                    "interactive_program": {
                        "name": "ssh",
                        "command": "/usr/bin/ssh",
                        "argv": ["ssh", "hmi-core01"],
                        "prompt_template": "historian@hmi-core01's password: ",
                        "metadata": {
                            "phase": "password_auth",
                            "target_host": "hmi-core01",
                            "target_username": "historian",
                            "attempts": 2,
                        },
                    },
                },
            }
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(response),
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("historian")
        parent_frame = session.shell_stack[-1]
        ssh_frame = ShellFrame(
            username="historian",
            cwd="/home/historian",
            interactive_program=InteractiveProgram(
                name="ssh",
                command="/usr/bin/ssh",
                argv=("ssh", "hmi-core01"),
                prompt_template="historian@hmi-core01's password: ",
                metadata={
                    "phase": "password_auth",
                    "target_host": "hmi-core01",
                    "target_username": "historian",
                    "attempts": 1,
                },
            ),
            shell_environment=default_shell_environment(
                username="historian",
                hostname="scada-hist01",
                cwd="/home/historian",
            ),
            host_identity=HostIdentity(hostname="scada-hist01"),
        )
        replace_shell_stack(session, (parent_frame, ssh_frame))

        result = backend.execute(session, "ssh res-tele01")

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.shell_stack[0], parent_frame)
        self.assertEqual(session.interactive_program.name, "ssh")
        self.assertEqual(session.interactive_program.metadata["attempts"], 2)
        self.assertEqual(
            result.state_update.shell_stack_transition,
            ShellStackTransition(operation="replace_active"),
        )
        self.assertEqual(
            result.metadata["dreamland.transition.normalized"],
            "active_ssh_program_replace_active",
        )
        self.assertEqual(
            result.metadata["dreamland.transition.applied_operation"],
            "replace_active",
        )

    def test_transition_audit_metadata_is_trace_visible(self):
        metadata = transition_trace_metadata(
            {
                "result": {
                    "metadata": {
                        "dreamland.transition.requested_operation": "push",
                        "dreamland.transition.applied_operation": "push",
                        "dreamland.transition.resulting_stack_depth": 3,
                        "dreamland.reasoning_summary": "Checked shell state.",
                        "unrelated": "ignored",
                    }
                }
            }
        )

        self.assertEqual(
            metadata,
            {
                "dreamland.transition.requested_operation": "push",
                "dreamland.transition.applied_operation": "push",
                "dreamland.transition.resulting_stack_depth": 3,
                "dreamland.reasoning_summary": "Checked shell state.",
            },
        )

    def test_model_without_transition_does_not_pop_arbitrary_program(self):
        model = RecordingModel(
            json.dumps(
                {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                }
            )
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("user")
        active_frame = ShellFrame(
            username="user",
            cwd="/home/user",
            interactive_program=InteractiveProgram(
                name="unknown-repl",
                command="/opt/unknown-repl",
                argv=("unknown-repl",),
                prompt_template="unknown> ",
            ),
            shell_environment=session.shell_environment,
        )
        replace_shell_stack(session, (*session.shell_stack, active_frame))

        backend.execute(session, "leave")

        self.assertEqual(len(session.shell_stack), 2)
        self.assertEqual(session.interactive_program.name, "unknown-repl")

    def test_agent_records_invalid_structured_json_metadata(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel('{"stdout": "unterminated"'),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="bad json",
                langfuse_trace_id="bad-json-thread",
            )
        )

        self.assertEqual(result.stdout, '{"stdout": "unterminated"\n')
        self.assertIn("invalid JSON", result.metadata["structured_parse_error"])
        self.assertFalse(result.metadata["structured_response_accepted"])

    def test_agent_records_missing_structured_keys_metadata(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(json.dumps({"stdout": "partial\n"})),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="bad envelope",
                langfuse_trace_id="bad-envelope-thread",
            )
        )

        self.assertEqual(result.stdout, '{"stdout": "partial\\n"}\n')
        self.assertEqual(
            result.metadata["structured_parse_error"],
            "structured response is missing required keys",
        )

    def test_agent_passes_environment_metadata_to_graph(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = RecordingGraph()
        agent.graph = graph

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="netstat -ant",
                langfuse_trace_id="trace-test",
                environment_metadata=EnvironmentMetadata(
                    connection=ConnectionMetadata(
                        protocol="ssh",
                        source=ConnectionEndpoint(ip="203.0.113.5", port=51234),
                        destination=ConnectionEndpoint(ip="10.0.0.10", port=2222),
                    ),
                ),
            )
        )

        self.assertEqual(result.stdout, "traced output\n")
        self.assertEqual(
            graph.state["environment_metadata"]["connection"]["protocol"], "ssh"
        )
        self.assertEqual(
            graph.state["environment_metadata"]["connection"]["source"],
            {"ip": "203.0.113.5", "port": 51234},
        )
        self.assertEqual(
            graph.state["environment_metadata"]["connection"]["destination"],
            {"ip": "10.0.0.10", "port": 2222},
        )
        self.assertEqual(
            graph.config["metadata"]["dreamland.connection.source_port"],
            "51234",
        )

    def test_agent_stream_respond_yields_model_chunks_and_final_result(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = StreamingGraph()
        agent.graph = graph

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="uptime",
                    langfuse_trace_id="trace-test",
                )
            )
        )

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["line one\n", "line two"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "line one\nline two\n")
        self.assertEqual(graph.config["metadata"]["dreamland.command"], "uptime")

    def test_agent_stream_yields_structured_stdout_response(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = StructuredStreamingGraph()
        agent.graph = graph

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="whoami",
                    langfuse_trace_id="structured-stream",
                )
            )
        )

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["root\n"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "root\n")

    def test_agent_stream_yields_structured_stdout_line_by_line(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = StructuredLineStreamingGraph()
        agent.graph = graph

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="long output",
                    langfuse_trace_id="structured-line-stream",
                )
            )
        )

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["line one\n", "line two\n"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "line one\nline two\n")

    def test_agent_stream_suppresses_fenced_structured_envelope(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = FencedStructuredStreamingGraph()
        agent.graph = graph

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="whoami",
                    langfuse_trace_id="fenced-structured-stream",
                )
            )
        )

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["root\n"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "root\n")

    def test_agent_stream_suppresses_markdown_before_trailing_envelope(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = MarkdownTrailingStructuredStreamingGraph()
        agent.graph = graph

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="ls -la /opt",
                    langfuse_trace_id="markdown-trailing-structured-stream",
                )
            )
        )

        self.assertEqual(
            [event.text for event in events if isinstance(event, CommandOutputChunk)],
            ["total 12\n"],
        )
        self.assertIsInstance(events[-1], CommandStreamResult)
        self.assertEqual(events[-1].result.stdout, "total 12\n")

    def test_agent_interrupt_updates_graph_with_rendered_output_only(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = InterruptibleGraph()
        agent.graph = graph
        control = CommandExecutionControl()
        events = iter(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="slow command",
                    langfuse_trace_id="trace-test",
                ),
                control=control,
            )
        )

        first = next(events)
        self.assertEqual(first.text, "rendered line\n")
        control.add_rendered("stdout", first.text)
        second = next(events)
        self.assertEqual(second.text, "buffered partial")
        control.cancel()
        final = next(events)

        self.assertIsInstance(final, CommandStreamResult)
        self.assertEqual(final.result.stdout, "rendered line\n")
        self.assertEqual(graph.updated["result"]["stdout"], "rendered line\n")
        self.assertTrue(graph.updated["result"]["interrupted"])
        self.assertEqual(graph.updated["result"]["signal"], "SIGINT")
        host_history = graph.updated["world_state"]["hosts_by_name"]["ubuntu"][
            "command_history"
        ]
        self.assertEqual(host_history[-1]["stdout"], "rendered line\n")
        self.assertTrue(host_history[-1]["interrupted"])
        self.assertEqual(host_history[-1]["signal"], "SIGINT")
        self.assertNotIn("buffered partial", host_history[-1]["stdout"])

    def test_agent_interrupt_closes_and_records_on_stream_owner_thread(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = BlockingInterruptibleGraph()
        agent.graph = graph
        control = CommandExecutionControl()
        events = []
        failures = []
        worker_thread_ids = []

        def consume_stream():
            worker_thread_ids.append(threading.get_ident())
            try:
                events.extend(
                    agent.stream_respond(
                        CommandContext(
                            username="alice",
                            cwd="/home/alice",
                            hostname="ubuntu",
                            command="blocking command",
                            langfuse_trace_id="blocking-interrupt",
                        ),
                        control=control,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=consume_stream)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(graph.release.set)
        self.assertTrue(graph.entered.wait(5), "graph stream did not start")
        reads_before_cancel = len(graph.get_state_thread_ids)

        with patch("dreamland.models.LOGGER.debug") as cancellation_debug:
            control.cancel()

        cancellation_debug.assert_not_called()
        self.assertEqual(len(graph.get_state_thread_ids), reads_before_cancel)
        self.assertEqual(graph.update_state_thread_ids, [])
        graph.release.set()
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], CommandStreamResult)
        self.assertTrue(events[0].result.interrupted)
        self.assertEqual(events[0].result.signal, "SIGINT")
        self.assertEqual(graph.close_thread_id, worker_thread_ids[0])
        self.assertEqual(graph.update_state_thread_ids, worker_thread_ids)
        self.assertTrue(
            all(
                thread_id == worker_thread_ids[0]
                for thread_id in graph.get_state_thread_ids
            )
        )

    def test_agent_interrupt_records_once_when_provider_fails_after_cancel(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = BlockingInterruptibleGraph(provider_error=True)
        agent.graph = graph
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")
        control = CommandExecutionControl()
        events = []
        failures = []
        worker_thread_ids = []

        def consume_stream():
            worker_thread_ids.append(threading.get_ident())
            try:
                events.extend(
                    backend.execute_stream(
                        session,
                        "provider failure after cancellation",
                        control=control,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=consume_stream)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(graph.release.set)
        self.assertTrue(graph.entered.wait(5), "graph stream did not start")
        reads_before_cancel = len(graph.get_state_thread_ids)

        control.cancel()

        self.assertEqual(len(graph.get_state_thread_ids), reads_before_cancel)
        self.assertEqual(graph.update_state_thread_ids, [])
        graph.release.set()
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], CommandStreamResult)
        self.assertTrue(events[0].result.interrupted)
        self.assertEqual(events[0].result.signal, "SIGINT")
        self.assertEqual(len(graph.update_state_thread_ids), 1)
        self.assertEqual(graph.update_state_thread_ids, worker_thread_ids)
        self.assertEqual(graph.close_thread_id, worker_thread_ids[0])
        self.assertEqual(len(session.history), 1)
        self.assertTrue(session.history[0].interrupted)
        self.assertEqual(session.history[0].signal, "SIGINT")

    def test_agent_pre_cancelled_stream_owner_closes_and_records_once(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = BlockingInterruptibleGraph()
        graph.release.set()
        agent.graph = graph
        control = CommandExecutionControl()
        control.cancel()
        owner_thread_id = threading.get_ident()

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="already cancelled command",
                    langfuse_trace_id="pre-cancelled-stream",
                ),
                control=control,
            )
        )

        self.assertTrue(graph.entered.is_set())
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], CommandStreamResult)
        self.assertTrue(events[0].result.interrupted)
        self.assertEqual(events[0].result.signal, "SIGINT")
        self.assertEqual(graph.close_thread_id, owner_thread_id)
        self.assertEqual(graph.update_state_thread_ids, [owner_thread_id])
        self.assertTrue(
            all(
                thread_id == owner_thread_id for thread_id in graph.get_state_thread_ids
            )
        )

    def test_agent_completion_boundary_cancel_records_once(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        graph = EmptyCheckpointGraph()
        agent.graph = graph
        control = CompletionBoundaryCancellationControl()
        owner_thread_id = threading.get_ident()

        events = list(
            agent.stream_respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="completion boundary command",
                    langfuse_trace_id="completion-boundary-cancel",
                ),
                control=control,
            )
        )

        self.assertEqual(control.cancelled_checks, 3)
        self.assertEqual(graph.close_thread_id, owner_thread_id)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], CommandStreamResult)
        self.assertTrue(events[0].result.interrupted)
        self.assertEqual(events[0].result.signal, "SIGINT")
        self.assertEqual(graph.update_state_thread_ids, [owner_thread_id])
        self.assertTrue(
            all(
                thread_id == owner_thread_id for thread_id in graph.get_state_thread_ids
            )
        )

    def test_langfuse_disabled_builds_no_callbacks(self):
        self.assertEqual(
            _build_langfuse_callbacks(
                enabled=False,
                public_key=None,
                secret_key=None,
                host=None,
            ),
            (),
        )

    def test_langfuse_enabled_requires_public_and_secret_keys(self):
        with self.assertRaisesRegex(RuntimeError, "LANGFUSE_PUBLIC_KEY"):
            _build_langfuse_callbacks(
                enabled=True,
                public_key=None,
                secret_key="sk-test",
                host=None,
            )

    def test_langfuse_callback_initializes_sdk_client(self):
        with patch("langfuse.Langfuse") as langfuse:
            with patch(
                "langfuse.langchain.CallbackHandler", return_value=RecordingCallback()
            ) as handler:
                callbacks = _build_langfuse_callbacks(
                    enabled=True,
                    public_key="pk-test",
                    secret_key="sk-test",
                    host="https://langfuse.local",
                    trace_id="trace-test",
                )

        langfuse.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://langfuse.local",
        )
        handler.assert_called_once_with(
            public_key="pk-test",
            trace_context={"trace_id": "trace-test"},
        )
        self.assertEqual(len(callbacks), 1)

    def test_langgraph_command_callbacks_do_not_force_session_trace_id(self):
        callback = RecordingCallback()
        with (
            patch("dreamland.agent_core._initialize_langfuse_client"),
            patch(
                "dreamland.agent_core._build_langfuse_callbacks",
                return_value=(callback,),
            ) as build_callbacks,
        ):
            agent = LangGraphCommandAgent(
                model_name="test-model",
                system_prompt_name="system.md",
                model=RecordingModel("unused"),
                langfuse_enabled=True,
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
                langfuse_host="https://langfuse.local",
            )
            graph = RecordingGraph()
            agent.graph = graph

            agent.respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command="pwd",
                    langfuse_trace_id="session-test",
                )
            )

        build_callbacks.assert_called_once_with(
            enabled=True,
            public_key="pk-test",
            secret_key="sk-test",
            host="https://langfuse.local",
        )
        self.assertEqual(
            graph.config["metadata"]["langfuse_session_id"],
            "session-test",
        )
        self.assertEqual(graph.config["callbacks"][1:], [callback])

    def test_langfuse_session_start_records_event_and_flushes(self):
        session = SessionState(
            username="alice",
            login_username="alice",
            host_identity=HostIdentity(hostname="ubuntu"),
            cwd="/home/alice",
            langfuse_trace_id="trace-test",
        )
        context = RecordingContextManager()
        with patch("langfuse.Langfuse") as langfuse:
            client = langfuse.return_value
            with patch("langfuse.propagate_attributes", return_value=context) as prop:
                record_langfuse_session_start(
                    enabled=True,
                    public_key="pk-test",
                    secret_key="sk-test",
                    host="https://langfuse.local",
                    session=session,
                )

        metadata = session_start_trace_metadata(session)
        langfuse.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://langfuse.local",
        )
        prop.assert_called_once_with(
            user_id="alice",
            session_id="trace-test",
            trace_name="dreamland:alice@ubuntu",
            tags=["dreamland"],
            metadata=metadata,
        )
        client.create_event.assert_called_once_with(
            trace_context={"trace_id": "trace-test"},
            name=SESSION_START_OBSERVATION_NAME,
            metadata=metadata,
        )
        client.flush.assert_called_once_with()
        self.assertTrue(context.entered)
        self.assertTrue(context.exited)

    def test_langfuse_session_start_includes_session_tags(self):
        session = SessionState(
            username="alice",
            login_username="alice",
            host_identity=HostIdentity(hostname="ubuntu"),
            cwd="/home/alice",
            langfuse_trace_id="trace-test",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    SESSION_ORIGIN_METADATA_KEY: MCP_SESSION_ORIGIN,
                    SESSION_TAGS_METADATA_KEY: ["mcp", "scenario:login"],
                    SESSION_LABEL_METADATA_KEY: "agent-run-1",
                    SESSION_METADATA_METADATA_KEY: {"agent": "codex"},
                }
            ),
        )
        context = RecordingContextManager()
        with patch("langfuse.Langfuse"):
            with patch("langfuse.propagate_attributes", return_value=context) as prop:
                record_langfuse_session_start(
                    enabled=True,
                    public_key="pk-test",
                    secret_key="sk-test",
                    host="https://langfuse.local",
                    session=session,
                )

        metadata = session_start_trace_metadata(session)
        prop.assert_called_once_with(
            user_id="alice",
            session_id="trace-test",
            trace_name="dreamland:alice@ubuntu",
            tags=["dreamland", "mcp", "scenario:login"],
            metadata=metadata,
        )
        self.assertEqual(metadata[SESSION_ORIGIN_METADATA_KEY], MCP_SESSION_ORIGIN)
        self.assertEqual(metadata[SESSION_LABEL_METADATA_KEY], "agent-run-1")
        self.assertEqual(metadata[SESSION_TAGS_METADATA_KEY], "mcp,scenario:login")
        self.assertEqual(metadata[f"{SESSION_METADATA_METADATA_KEY}.agent"], "codex")

    def test_langfuse_command_config_includes_session_tags(self):
        context = CommandContext(
            username="alice",
            cwd="/home/alice",
            hostname="ubuntu",
            command="pwd",
            langfuse_trace_id="trace-test",
            environment_metadata=EnvironmentMetadata(
                metadata={
                    SESSION_ORIGIN_METADATA_KEY: MCP_SESSION_ORIGIN,
                    SESSION_TAGS_METADATA_KEY: ["mcp", "scenario:login"],
                    SESSION_LABEL_METADATA_KEY: "agent-run-1",
                    SESSION_METADATA_METADATA_KEY: {"agent": "codex"},
                }
            ),
        )

        config = graph_invoke_config((), context)

        self.assertEqual(
            config["tags"],
            ["dreamland", "mcp", "scenario:login"],
        )
        self.assertEqual(
            config["metadata"][SESSION_ORIGIN_METADATA_KEY],
            MCP_SESSION_ORIGIN,
        )
        self.assertEqual(config["metadata"][SESSION_LABEL_METADATA_KEY], "agent-run-1")
        self.assertEqual(
            config["metadata"][SESSION_TAGS_METADATA_KEY],
            "mcp,scenario:login",
        )
        self.assertEqual(
            config["metadata"][f"{SESSION_METADATA_METADATA_KEY}.agent"],
            "codex",
        )

    def test_langfuse_session_start_is_noop_when_disabled(self):
        session = SessionState(username="alice", langfuse_trace_id="trace-test")
        with patch("langfuse.Langfuse") as langfuse:
            record_langfuse_session_start(
                enabled=False,
                public_key=None,
                secret_key=None,
                host=None,
                session=session,
            )
        langfuse.assert_not_called()

    def test_dreamland_langfuse_callback_recategorizes_user_interrupts(self):
        callback = _DreamlandLangfuseCallbackHandler(
            base_handler=FakeLangfuseCallbackHandler,
            public_key="pk-test",
            trace_context={"trace_id": "trace-test"},
        )

        self.assertEqual(
            callback._get_error_level_and_status_message(GeneratorExit()),
            ("ERROR", "GeneratorExit"),
        )

        callback.mark_dreamland_interrupted()

        self.assertEqual(
            callback._get_error_level_and_status_message(GeneratorExit()),
            ("DEFAULT", "User interrupted command with Ctrl-C"),
        )
        self.assertEqual(
            callback._get_error_level_and_status_message(
                RuntimeError("provider failed")
            ),
            ("ERROR", "provider failed"),
        )

    def test_dreamland_langfuse_callback_records_transition_metadata(self):
        callback = _DreamlandLangfuseCallbackHandler(
            base_handler=FakeLangfuseCallbackHandler,
            public_key="pk-test",
            trace_context={"trace_id": "trace-test"},
        )
        span = RecordingSpan()
        callback._runs["run-id"] = span
        outputs = {
            "result": {
                "metadata": {
                    "dreamland.transition.requested_operation": "push",
                    "dreamland.transition.applied_operation": "push",
                    "dreamland.transition.resulting_stack_depth": 2,
                    "dreamland.reasoning_summary": "Checked shell state.",
                    "unrelated": "ignored",
                }
            }
        }

        callback.on_chain_end(outputs, run_id="run-id")

        self.assertEqual(
            span.metadata,
            {
                "dreamland.transition.requested_operation": "push",
                "dreamland.transition.applied_operation": "push",
                "dreamland.transition.resulting_stack_depth": 2,
                "dreamland.reasoning_summary": "Checked shell state.",
            },
        )
        self.assertEqual(callback.chain_end_calls[0][0], outputs)

    def test_langfuse_callback_does_not_mutate_environment(self):
        with patch("langfuse.Langfuse"):
            with patch(
                "langfuse.langchain.CallbackHandler", return_value=RecordingCallback()
            ):
                with patch.dict("os.environ", {}, clear=True):
                    _build_langfuse_callbacks(
                        enabled=True,
                        public_key="pk-test",
                        secret_key="sk-test",
                        host="https://langfuse.local",
                    )
                    self.assertNotIn("LANGFUSE_PUBLIC_KEY", os.environ)
                    self.assertNotIn("LANGFUSE_SECRET_KEY", os.environ)
                    self.assertNotIn("LANGFUSE_HOST", os.environ)

    def test_real_langfuse_callback_is_enabled_after_client_initialization(self):
        from langfuse._client.resource_manager import LangfuseResourceManager

        LangfuseResourceManager.reset()
        try:
            with patch.dict("os.environ", {}, clear=True):
                callbacks = _build_langfuse_callbacks(
                    enabled=True,
                    public_key="pk-test",
                    secret_key="sk-test",
                    host="https://langfuse.local",
                )

            self.assertTrue(callbacks[0]._langfuse_client._tracing_enabled)
        finally:
            LangfuseResourceManager.reset()

    def test_agent_uses_history_to_render_changed_directory_listing(self):
        profile = server_profile()
        model = StatefulDirectoryListingModel()
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(profile, agent=agent)
        session = backend.create_session("alice")

        first_listing = backend.execute(session, "ls -la")
        touch_result = backend.execute(session, "touch magicfile4124")
        second_listing = backend.execute(session, "ls -la")

        self.assertNotIn("magicfile4124", first_listing.stdout)
        self.assertEqual(touch_result.stdout, "")
        self.assertIn("magicfile4124", second_listing.stdout)
        self.assertEqual(
            [entry.command for entry in session.history],
            ["ls -la", "touch magicfile4124", "ls -la"],
        )
        self.assertIn('"command_history":', model.prompts[-1])
        self.assertIn('"command": "ls -la"', model.prompts[-1])
        self.assertIn('"command": "touch magicfile4124"', model.prompts[-1])
        self.assertNotIn("1. command:", model.prompts[-1])
        self.assertNotIn("2. command:", model.prompts[-1])
        self.assertIn("current command: ls -la", model.prompts[-1])

    def test_agent_uses_checkpointed_host_command_history(self):
        model = StatefulDirectoryListingModel()
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )

        base_context = {
            "username": "alice",
            "cwd": "/home/alice",
            "hostname": "ubuntu",
            "langfuse_trace_id": "checkpoint-thread",
        }

        first_listing = agent.respond(CommandContext(command="ls -la", **base_context))
        touch_result = agent.respond(
            CommandContext(command="touch magicfile4124", **base_context)
        )
        second_listing = agent.respond(CommandContext(command="ls -la", **base_context))

        self.assertNotIn("magicfile4124", first_listing.stdout)
        self.assertEqual(touch_result.stdout, "")
        self.assertIn("magicfile4124", second_listing.stdout)
        self.assertIn('"command_history":', model.prompts[-1])
        self.assertIn('"command": "ls -la"', model.prompts[-1])
        self.assertIn('"command": "touch magicfile4124"', model.prompts[-1])
        self.assertNotIn("1. command:", model.prompts[-1])
        self.assertNotIn("2. command:", model.prompts[-1])

    def test_agent_tracks_builtin_cwd_in_graph_state(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        base_context = {
            "username": "alice",
            "cwd": "/home/alice",
            "hostname": "ubuntu",
            "langfuse_trace_id": "cwd-thread",
        }

        cd_result = agent.respond(CommandContext(command="cd /var/log", **base_context))
        pwd_result = agent.respond(CommandContext(command="pwd", **base_context))

        self.assertEqual(cd_result.stdout, "")
        self.assertEqual(cd_result.state_update.cwd, "/var/log")
        self.assertEqual(pwd_result.stdout, "/var/log\n")

    def test_agent_tracks_shell_variables_in_graph_state(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel("unused"),
        )
        base_context = {
            "username": "alice",
            "cwd": "/home/alice",
            "hostname": "ubuntu",
            "langfuse_trace_id": "environment-thread",
        }

        assign_result = agent.respond(
            CommandContext(command="APP_ROOT=/opt/app", **base_context)
        )
        export_result = agent.respond(
            CommandContext(command="export APP_ROOT", **base_context)
        )
        printenv_result = agent.respond(
            CommandContext(command="printenv APP_ROOT", **base_context)
        )

        self.assertEqual(assign_result.stdout, "")
        self.assertEqual(export_result.stdout, "")
        self.assertEqual(printenv_result.stdout, "/opt/app\n")

    def test_backend_builtin_variables_update_langgraph_checkpoint(self):
        model = RecordingModel("model output")
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")

        backend.execute(session, "ls -la")
        backend.execute(session, "export APP_ROOT=/opt/app")
        backend.execute(session, "echo $APP_ROOT")

        self.assertIn("APP_ROOT exported: /opt/app", model.messages[-1][1].content)

    def test_external_state_update_forces_next_langgraph_context_refresh(self):
        model = RecordingModel(
            '{"stdout":"","stderr":"","exit_status":0,"state_update":{}}'
        )
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=model,
        )
        backend = AgentBackend(server_profile(), agent=agent)
        session = backend.create_session("alice")

        backend.execute(session, "id")
        apply_external_state_update(
            session,
            CommandStateUpdate(
                world_state={
                    "hosts_by_name": {
                        "ubuntu": {
                            "facts_by_id": {
                                "host-hardware": (
                                    "The host is a large HP rack-mounted system."
                                )
                            }
                        }
                    }
                }
            ),
        )
        backend.execute(session, "hostnamectl")

        self.assertFalse(session.force_session_state_next_command)
        self.assertIn("large HP rack-mounted system", model.messages[-1][1].content)

    def test_agent_captures_reasoning_summary_in_result_metadata(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(
                [
                    {
                        "type": "reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "Checked command availability.",
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": (
                            '{"stdout":"","stderr":"not found\\n",'
                            '"exit_status":127,"state_update":{}}'
                        ),
                    },
                ]
            ),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="python",
                langfuse_trace_id="reasoning-thread",
            )
        )

        self.assertEqual(
            result.metadata["dreamland.reasoning_summary"],
            "Checked command availability.",
        )

    def test_agent_captures_xai_reasoning_content_in_result_metadata(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(
                ModelResponse(
                    '{"stdout":"ok\\n","stderr":"","exit_status":0,"state_update":{}}',
                    additional_kwargs={
                        "reasoning_content": "Checked xAI command context."
                    },
                )
            ),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="xai-reasoning-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(
            result.metadata["dreamland.reasoning_summary"],
            "Checked xAI command context.",
        )

    def test_agent_captures_anthropic_thinking_in_result_metadata(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(
                [
                    {
                        "type": "thinking",
                        "thinking": "Checked Claude command context.",
                    },
                    {
                        "type": "text",
                        "text": (
                            '{"stdout":"ok\\n","stderr":"",'
                            '"exit_status":0,"state_update":{}}'
                        ),
                    },
                ]
            ),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="anthropic-reasoning-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(
            result.metadata["dreamland.reasoning_summary"],
            "Checked Claude command context.",
        )

    def test_agent_marks_anthropic_system_prompt_for_prompt_cache(self):
        agent = LangGraphCommandAgent(
            provider="anthropic",
            model_name="claude-test",
            system_prompt_name="system.md",
            model=RecordingModel(
                '{"stdout":"ok\\n","stderr":"","exit_status":0,"state_update":{}}'
            ),
            prompt_cache="automatic",
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="anthropic-cache-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        system_content = agent.model.messages[-1][0].content
        self.assertIsInstance(system_content, list)
        self.assertEqual(system_content[0]["type"], "text")
        self.assertEqual(system_content[0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("DREAMLAND State Contract", system_content[0]["text"])
        self.assertEqual(agent.model.invocation_kwargs[-1], {})

    def test_agent_can_disable_anthropic_prompt_cache_breakpoint(self):
        model = RecordingModel(
            '{"stdout":"ok\\n","stderr":"","exit_status":0,"state_update":{}}'
        )
        agent = LangGraphCommandAgent(
            provider="anthropic",
            model_name="claude-test",
            system_prompt_name="system.md",
            model=model,
            prompt_cache="off",
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="anthropic-cache-off-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertIsInstance(model.messages[-1][0].content, str)

    def test_agent_marks_openai_gpt_5_6_system_prompt_for_explicit_cache(self):
        model = RecordingModel(
            '{"stdout":"ok\\n","stderr":"","exit_status":0,"state_update":{}}'
        )
        agent = LangGraphCommandAgent(
            provider="openai",
            model_name="gpt-5.6-terra",
            system_prompt_name="system.md",
            model=model,
            prompt_cache="off",
        )

        first_world = {
            "active_host": "alpha",
            "hosts_by_name": {
                "alpha": {"hostname": "alpha", "facts_by_id": {"site": "A"}}
            },
        }
        second_world = {
            "active_host": "beta",
            "hosts_by_name": {
                "beta": {"hostname": "beta", "facts_by_id": {"site": "B"}}
            },
        }
        for command, trace_id, world_state in (
            ("echo one", "openai-cache-one", first_world),
            ("cat /etc/hostname", "openai-cache-two", second_world),
        ):
            result = agent.respond(
                CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="ubuntu",
                    command=command,
                    langfuse_trace_id=trace_id,
                    world_state=world_state,
                )
            )
            self.assertEqual(result.stdout, "ok\n")

        first_system = model.messages[0][0].content
        self.assertIsInstance(first_system, list)
        self.assertEqual(first_system[0]["type"], "text")
        self.assertEqual(
            first_system[0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertIn("DREAMLAND State Contract", first_system[0]["text"])
        self.assertIsInstance(model.messages[0][1].content, str)

        first_kwargs = model.invocation_kwargs[0]
        second_kwargs = model.invocation_kwargs[1]
        self.assertEqual(first_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(second_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(
            first_kwargs["prompt_cache_options"],
            {"mode": "explicit"},
        )
        self.assertRegex(
            first_kwargs["prompt_cache_key"],
            r"^dreamland-command-v1:[0-9a-f]{32}$",
        )
        self.assertEqual(
            first_kwargs["prompt_cache_key"],
            second_kwargs["prompt_cache_key"],
        )
        self.assertNotIn("ttl", first_kwargs["prompt_cache_options"])
        self.assertNotIn("prompt_cache_retention", first_kwargs)
        self.assertNotIn("extra_body", first_kwargs)

        python = InteractiveProgram(
            name="python",
            command="/usr/bin/python3",
            argv=("python3",),
        )
        agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="1 + 1",
                langfuse_trace_id="openai-cache-python",
                interactive_program=python,
            )
        )
        self.assertNotEqual(
            first_kwargs["prompt_cache_key"],
            model.invocation_kwargs[2]["prompt_cache_key"],
        )

    def test_openai_explicit_prompt_cache_gate_is_strict(self):
        cases = (
            ("openai", "gpt-5.6", True),
            ("openai", "gpt-5.6-sol", True),
            ("OPENAI", "GPT-5.6-2026-07-01", True),
            ("openai", "gpt-6-astra", True),
            ("openai", "gpt-6-astra-2026-09-03", True),
            ("openai", "gpt-6-sol", True),
            ("openai", "gpt-6.1-sol", True),
            ("OPENAI", "GPT-6.1-SOL-2026-10-01", True),
            ("openai", "gpt-6-luna", True),
            ("openai", "gpt-6-luna-2026-09-22", True),
            ("openai", "gpt-6-astra2", False),
            ("openai", "gpt-6-sol2", False),
            ("openai", "gpt-6.1-sol2", False),
            ("openai", "gpt-6-luna2", False),
            ("xai", "gpt-6.1-sol", False),
            ("anthropic", "gpt-6-luna", False),
            ("openai", "gpt-6-other", False),
            ("xai", "gpt-6-astra", False),
            ("anthropic", "gpt-6-astra", False),
            ("openai", "gpt-5.5", False),
            ("openai", "gpt-5.60", False),
            ("xai", "gpt-5.6", False),
            ("anthropic", "gpt-5.6", False),
        )

        for provider, model_name, expected in cases:
            with self.subTest(provider=provider, model=model_name):
                self.assertEqual(
                    _uses_openai_explicit_prompt_cache(
                        provider=provider,
                        model=model_name,
                    ),
                    expected,
                )

    def test_command_agent_forwards_evaluation_controls_to_provider(self):
        with patch("dreamland.agent_core.build_chat_model") as factory:
            LangGraphCommandAgent(
                model_name="gpt-6-astra",
                system_prompt_name="system.md",
                reasoning_effort="max",
                openai_api="responses",
                openai_service_tier="default",
                openai_reasoning_mode="pro",
            )
        config = factory.call_args.args[0]
        self.assertEqual(config.reasoning_effort, "max")
        self.assertEqual(config.openai_api, "responses")
        self.assertEqual(config.openai_service_tier, "default")
        self.assertEqual(config.openai_reasoning_mode, "pro")

    def test_openai_command_cache_key_tracks_model_and_system_prompt(self):
        base = _openai_command_prompt_cache_key(
            provider="openai",
            model="gpt-5.6",
            system_prompt="stable system",
        )

        self.assertEqual(
            base,
            "dreamland-command-v1:480f43fd658c2b953e4682f8696bebdd",
        )
        self.assertEqual(
            base,
            _openai_command_prompt_cache_key(
                provider="openai",
                model="gpt-5.6",
                system_prompt="stable system",
            ),
        )
        self.assertNotEqual(
            base,
            _openai_command_prompt_cache_key(
                provider="openai",
                model="gpt-5.6-sol",
                system_prompt="stable system",
            ),
        )
        self.assertNotEqual(
            base,
            _openai_command_prompt_cache_key(
                provider="openai",
                model="gpt-5.6",
                system_prompt="changed system",
            ),
        )

    def test_openai_models_before_gpt_5_6_receive_no_explicit_cache_fields(self):
        model = RecordingModel(
            '{"stdout":"ok\\n","stderr":"","exit_status":0,"state_update":{}}'
        )
        agent = LangGraphCommandAgent(
            provider="openai",
            model_name="gpt-5.5",
            system_prompt_name="system.md",
            model=model,
        )

        agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="openai-no-explicit-cache",
            )
        )

        self.assertIsInstance(model.messages[0][0].content, str)
        self.assertEqual(
            model.invocation_kwargs[0], {"response_format": {"type": "json_object"}}
        )

    def test_agent_parses_anthropic_thinking_with_fenced_structured_text(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(
                [
                    {
                        "type": "thinking",
                        "thinking": "Checked Claude command context.",
                    },
                    {
                        "type": "text",
                        "text": (
                            "```json\n"
                            '{"stdout":"ok\\n","stderr":"",'
                            '"exit_status":0,"state_update":{}}\n'
                            "```"
                        ),
                    },
                ]
            ),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="anthropic-fenced-reasoning-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertNotIn("structured_parse_error", result.metadata)
        self.assertEqual(
            result.metadata["dreamland.reasoning_summary"],
            "Checked Claude command context.",
        )

    def test_agent_parses_anthropic_thinking_with_trailing_structured_json(self):
        agent = LangGraphCommandAgent(
            model_name="test-model",
            system_prompt_name="system.md",
            model=RecordingModel(
                [
                    {
                        "type": "thinking",
                        "thinking": "Checked Claude command context.",
                    },
                    {
                        "type": "text",
                        "text": (
                            "```\n"
                            "ok\n"
                            "```\n\n"
                            "I'll provide the JSON response.\n\n"
                            '{"stdout":"ok\\n","stderr":"",'
                            '"exit_status":0,"state_update":{}}'
                        ),
                    },
                ]
            ),
        )

        result = agent.respond(
            CommandContext(
                username="alice",
                cwd="/home/alice",
                hostname="ubuntu",
                command="echo ok",
                langfuse_trace_id="anthropic-trailing-reasoning-thread",
            )
        )

        self.assertEqual(result.stdout, "ok\n")
        self.assertNotIn("structured_parse_error", result.metadata)
        self.assertEqual(
            result.metadata["dreamland.reasoning_summary"],
            "Checked Claude command context.",
        )


class RecordingModel:
    def __init__(self, response):
        self.response = response
        self.messages = []
        self.invocation_kwargs = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        self.invocation_kwargs.append(kwargs)
        if isinstance(self.response, ModelResponse):
            return self.response
        return ModelResponse(self.response)


class RecordingCallback:
    pass


class RecordingContextManager:
    def __init__(self):
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False


class FakeLangfuseCallbackHandler:
    def __init__(self, public_key=None, trace_context=None):
        self.public_key = public_key
        self.trace_context = trace_context
        self._runs = {}
        self.chain_end_calls = []

    def _get_error_level_and_status_message(self, error):
        return "ERROR", str(error) or type(error).__name__

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self.chain_end_calls.append((outputs, run_id, parent_run_id, kwargs))


class RecordingSpan:
    def __init__(self):
        self.metadata = {}

    def update(self, *, metadata):
        self.metadata.update(metadata)


class RecordingGraph:
    def __init__(self):
        self.config = None

    def invoke(self, state, config=None):
        self.state = state
        self.config = config
        return {"response": "traced output"}


class StreamingGraph:
    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        yield ("messages", (ModelResponse("line one\n"), {}))
        yield ("messages", (ModelResponse("line two"), {}))
        yield (
            "updates",
            {
                "call_model": {
                    "result": {
                        "stdout": "line one\nline two\n",
                        "stderr": "",
                        "exit_status": 0,
                        "cwd": None,
                        "interactive_program": None,
                        "shell_environment": None,
                    }
                }
            },
        )


class StructuredStreamingGraph:
    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        text = json.dumps(
            {
                "stdout": "root\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        yield ("messages", (ModelResponse(text[:8]), {}))
        yield ("messages", (ModelResponse(text[8:]), {}))
        yield (
            "updates",
            {
                "call_model": {
                    "result": {
                        "stdout": "root\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                        "cwd": None,
                        "interactive_program": None,
                        "shell_environment": None,
                        "shell_stack": None,
                    }
                }
            },
        )


class StructuredLineStreamingGraph:
    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        yield ("messages", (ModelResponse('{"stdout":"line one\\n'), {}))
        yield ("messages", (ModelResponse("line two\\n"), {}))
        yield (
            "messages",
            (
                ModelResponse('","stderr":"","exit_status":0,"state_update":{}}'),
                {},
            ),
        )
        yield (
            "updates",
            {
                "call_model": {
                    "result": {
                        "stdout": "line one\nline two\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                        "metadata": {},
                        "cwd": None,
                        "interactive_program": None,
                        "shell_environment": None,
                        "shell_stack": None,
                    }
                }
            },
        )


class FencedStructuredStreamingGraph:
    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        text = (
            "```json\n"
            + json.dumps(
                {
                    "stdout": "root\n",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": {},
                },
                indent=2,
            )
            + "\n```"
        )
        yield ("messages", (ModelResponse(text[:2]), {}))
        yield ("messages", (ModelResponse(text[2:8]), {}))
        yield ("messages", (ModelResponse(text[8:]), {}))


class MarkdownTrailingStructuredStreamingGraph:
    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        text = "```\ntotal 12\n```\n\nI'll provide the JSON response.\n\n" + json.dumps(
            {
                "stdout": "total 12\n",
                "stderr": "",
                "exit_status": 0,
                "state_update": {},
            }
        )
        yield ("messages", (ModelResponse(text[:2]), {}))
        yield ("messages", (ModelResponse(text[2:20]), {}))
        yield ("messages", (ModelResponse(text[20:]), {}))


class InterruptibleGraph:
    def __init__(self):
        self.updated = None

    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        yield ("messages", (ModelResponse("rendered line\n"), {}))
        yield ("messages", (ModelResponse("buffered partial"), {}))
        yield ("messages", (ModelResponse("unreachable"), {}))

    def get_state(self, config):
        return GraphSnapshot(self.state)

    def update_state(self, config, values):
        self.updated = values


class BlockingInterruptibleGraph:
    def __init__(self, *, provider_error=False):
        self.provider_error = provider_error
        self.entered = threading.Event()
        self.release = threading.Event()
        self.state = {}
        self.close_thread_id = None
        self.get_state_thread_ids = []
        self.update_state_thread_ids = []
        self.updated = None

    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        try:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("blocking graph was not released")
            if self.provider_error:
                raise RuntimeError("provider failed after cancellation")
            yield ("messages", (ModelResponse("discarded after cancellation"), {}))
        finally:
            self.close_thread_id = threading.get_ident()

    def get_state(self, config):
        self.get_state_thread_ids.append(threading.get_ident())
        return GraphSnapshot(self.state)

    def update_state(self, config, values):
        self.update_state_thread_ids.append(threading.get_ident())
        self.updated = values


class EmptyCheckpointGraph:
    """Complete without events so cancellation can win at the final boundary."""

    def __init__(self):
        self.state = {}
        self.close_thread_id = None
        self.get_state_thread_ids = []
        self.update_state_thread_ids = []
        self.updated = None

    def stream(self, state, config=None, stream_mode=None):
        self.state = state
        self.config = config
        self.stream_mode = stream_mode
        try:
            yield from ()
        finally:
            self.close_thread_id = threading.get_ident()

    def get_state(self, config):
        self.get_state_thread_ids.append(threading.get_ident())
        return GraphSnapshot(self.state)

    def update_state(self, config, values):
        self.update_state_thread_ids.append(threading.get_ident())
        self.updated = values


class CompletionBoundaryCancellationControl(CommandExecutionControl):
    """Cancel on the post-final check, after registration and finally checks."""

    def __init__(self):
        super().__init__()
        self.cancelled_checks = 0

    def cancelled(self):
        self.cancelled_checks += 1
        if self.cancelled_checks == 3:
            super().cancel()
        return super().cancelled()


class GraphSnapshot:
    def __init__(self, values):
        self.values = values


class StatefulDirectoryListingModel:
    def __init__(self):
        self.prompts = []

    def invoke(self, messages, **kwargs):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "current command: touch magicfile4124" in prompt:
            return ModelResponse("")
        if (
            "current command: ls -la" in prompt
            and '"command": "touch magicfile4124"' in prompt
        ):
            return ModelResponse(
                "\n".join(
                    [
                        "total 12",
                        "drwxr-xr-x 2 alice alice 4096 May 27 12:00 .",
                        "drwxr-xr-x 3 root  root  4096 May 27 12:00 ..",
                        "-rw-r--r-- 1 alice alice    0 May 27 12:01 magicfile4124",
                    ]
                )
            )
        if "current command: ls -la" in prompt:
            return ModelResponse(
                "\n".join(
                    [
                        "total 8",
                        "drwxr-xr-x 2 alice alice 4096 May 27 12:00 .",
                        "drwxr-xr-x 3 root  root  4096 May 27 12:00 ..",
                    ]
                )
            )
        return ModelResponse("unexpected command")


class ModelResponse:
    def __init__(self, content, *, additional_kwargs=None):
        self.content = content
        self.additional_kwargs = additional_kwargs or {}


def _trace_formatter():
    return logging.Formatter("%(message)s")


if __name__ == "__main__":
    unittest.main()
