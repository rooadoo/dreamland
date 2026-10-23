from __future__ import annotations

import unittest
from copy import deepcopy

from dreamland.background_agents.focus import (
    classify_presence_probe,
    infer_command_focus,
)
from dreamland.background_agents.surfaces import infer_command_surface
from dreamland.models import (
    CommandStateEffect,
    CommandStateUpdate,
    ConnectionEndpoint,
    ConnectionMetadata,
    HostIdentity,
    SessionState,
    ShellStackTransition,
    default_shell_frame,
)
from dreamland.network_connections import (
    initialize_session_connections,
    projected_environment_metadata,
)
from dreamland.session_state import apply_command_state_update, pop_active_shell_frame


def connected_session() -> SessionState:
    frame = default_shell_frame(username="guest", hostname="entry")
    session = SessionState(
        username=frame.username,
        cwd=frame.cwd,
        host_identity=frame.host_identity,
        shell_stack=[frame],
        shell_environment=frame.shell_environment,
        world_state={
            "entry_host": "entry",
            "active_host": "entry",
            "hosts_by_name": {
                "entry": {
                    "hostname": "entry",
                    "network": {"ipv4_address": "10.72.24.17"},
                },
                "remote": {
                    "hostname": "remote",
                    "network": {"ipv4_address": "198.51.100.166"},
                },
                "skipped": {
                    "hostname": "skipped",
                    "network": {"ipv4_address": "198.51.100.170"},
                },
            },
        },
    )
    initialize_session_connections(
        session,
        ingress=ConnectionMetadata(
            protocol="ssh",
            source=ConnectionEndpoint("198.51.100.23", 47486),
            destination=ConnectionEndpoint("127.0.0.1", 2222),
        ),
    )
    return session


class CompoundCommandConsumerTests(unittest.TestCase):
    def test_compound_focus_retains_directory_context_without_inventing_paths(self):
        for command in (
            "cat first;cat second",
            "cat first && cd /elsewhere",
            "cd /tmp; pwd ; touch testfile",
            "cat first | cat second",
            "cat $(pwd)/first",
            "systemctl status first; systemctl status second",
        ):
            with self.subTest(command=command):
                focus = infer_command_focus(
                    command,
                    starting_cwd="/home/guest",
                    result_cwd="/tmp",
                )
                self.assertEqual(
                    focus,
                    {
                        "cwd": "/home/guest",
                        "result_cwd": "/tmp",
                        "primary": {"kind": "working_directory", "id": "/tmp"},
                        "key": "working_directory:/tmp",
                    },
                )
                self.assertEqual(infer_command_surface(command), "shell")

    def test_aggregate_status_does_not_classify_a_successful_presence_probe(self):
        for command in (
            "ps aux; false",
            "w || whoami",
            "ss -nt && false",
            "journalctl; echo done",
            "ps aux | cat",
        ):
            with self.subTest(command=command):
                self.assertIsNone(classify_presence_probe(command))
                self.assertEqual(infer_command_surface(command), "shell")

    def test_literal_control_characters_remain_single_command_operands(self):
        for command in ("cat 'first;second'", r"cat first\;second", "cat ';'"):
            with self.subTest(command=command):
                filename = ";" if command == "cat ';'" else "first;second"
                focus = infer_command_focus(
                    command, starting_cwd="/tmp", result_cwd="/tmp"
                )
                self.assertEqual(focus["paths"], (f"/tmp/{filename}",))
                self.assertEqual(infer_command_surface(command), "filesystem")

    def test_aggregate_ssh_transition_uses_its_metadata(self):
        session = connected_session()
        remote = default_shell_frame(username="reader", hostname="remote")
        apply_command_state_update(
            session,
            CommandStateUpdate(
                host_identity=remote.host_identity,
                shell_environment=remote.shell_environment,
                interactive_program=remote.interactive_program,
                shell_stack_transition=ShellStackTransition(
                    "push",
                    metadata={"ssh": {"target": "remote", "port": 2207}},
                ),
            ),
            command="ssh -p 2299 skipped || ssh -p 2207 remote",
        )
        connection = projected_environment_metadata(session).connection
        self.assertEqual(
            connection.destination, ConnectionEndpoint("198.51.100.166", 2207)
        )
        self.assertEqual(session.host_identity.hostname, "remote")
        self.assertEqual(len(session.network_connections.connections), 2)

    def test_aggregate_ssh_line_does_not_infer_a_connection_from_first_command(self):
        session = connected_session()
        original = deepcopy(session.network_connections.connections)
        remote = default_shell_frame(username="reader", hostname="remote")
        apply_command_state_update(
            session,
            CommandStateUpdate(
                host_identity=remote.host_identity,
                shell_stack_transition=ShellStackTransition("push"),
            ),
            command="ssh skipped; remote-alias",
        )
        self.assertEqual(session.network_connections.connections, original)

    def test_aggregate_hostname_line_does_not_rename_the_first_host(self):
        session = connected_session()
        apply_command_state_update(
            session,
            CommandStateUpdate(host_identity=HostIdentity("remote")),
            command="hostname renamed; remote-alias",
        )
        self.assertEqual(
            session.world_state["hosts_by_name"]["entry"]["hostname"], "entry"
        )

    def test_ordered_rename_then_ssh_preserves_connection_identity(self):
        session = connected_session()
        original = deepcopy(session.network_connections.connections)
        remote = default_shell_frame(username="reader", hostname="remote")
        apply_command_state_update(
            session,
            CommandStateUpdate(
                effects=(
                    CommandStateEffect(
                        command="hostname renamed",
                        state_update=CommandStateUpdate(
                            host_identity=HostIdentity("renamed")
                        ),
                    ),
                    CommandStateEffect(
                        command="ssh -p 2207 remote",
                        state_update=CommandStateUpdate(
                            host_identity=remote.host_identity,
                            shell_environment=remote.shell_environment,
                            interactive_program=remote.interactive_program,
                            shell_stack_transition=ShellStackTransition("push"),
                        ),
                    ),
                )
            ),
            command="hostname renamed; ssh -p 2207 remote",
        )
        self.assertEqual(len(session.network_connections.connections), 2)
        connection = projected_environment_metadata(session).connection
        self.assertEqual(connection.source.ip, "10.72.24.17")
        self.assertEqual(connection.destination.port, 2207)
        self.assertTrue(
            pop_active_shell_frame(session, force_session_state_next_command=True)
        )
        self.assertEqual(session.host_identity.hostname, "renamed")
        self.assertEqual(session.network_connections.connections, original)
        self.assertEqual(
            session.world_state["hosts_by_name"]["entry"]["hostname"], "renamed"
        )
        self.assertEqual(
            projected_environment_metadata(session).connection.destination.ip,
            "10.72.24.17",
        )


if __name__ == "__main__":
    unittest.main()
