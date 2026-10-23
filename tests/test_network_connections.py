from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace

from dreamland.agent_core import handle_builtin_command_context
from dreamland.models import (
    CommandContext,
    CommandStateUpdate,
    ConnectionEndpoint,
    ConnectionMetadata,
    HostIdentity,
    InteractiveProgram,
    SessionState,
    ShellStackTransition,
    default_shell_frame,
)
from dreamland.network_connections import (
    close_session_connections,
    initialize_session_connections,
    projected_environment_metadata,
)
from dreamland.session_state import apply_command_state_update, pop_active_shell_frame


def session_with_ingress() -> SessionState:
    frame = default_shell_frame(username="adam", hostname="hearth-c17")
    session = SessionState(
        username=frame.username,
        host_identity=frame.host_identity,
        shell_stack=[frame],
        shell_environment=frame.shell_environment,
        world_state={
            "entry_host": "hearth-c17",
            "active_host": "hearth-c17",
            "hosts_by_name": {
                "hearth-c17": {
                    "hostname": "hearth-c17",
                    "network": {"ipv4_address": "10.72.24.17"},
                },
                "anchor-welcome-01": {
                    "hostname": "anchor-welcome-01",
                    "aliases": ["welcome.anchor-mutual.bsides"],
                    "network": {"ipv4_address": "198.51.100.166"},
                },
                "third": {
                    "hostname": "third",
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


def remote_shell(
    session: SessionState, hostname: str, *, command: str, operation: str = "push"
) -> None:
    frame = default_shell_frame(username="reader", hostname=hostname)
    apply_command_state_update(
        session,
        CommandStateUpdate(
            host_identity=frame.host_identity,
            shell_environment=frame.shell_environment,
            interactive_program=frame.interactive_program,
            shell_stack_transition=ShellStackTransition(
                operation=operation, username="reader"
            ),
        ),
        command=command,
    )


def network_at(session: SessionState, host: str) -> dict[str, object]:
    copied = deepcopy(session)
    copied.host_identity = HostIdentity(host)
    return projected_environment_metadata(copied).metadata["active_network"]


class NetworkConnectionTests(unittest.TestCase):
    def test_initial_connection_uses_primary_address_and_port_22(self):
        session = session_with_ingress()
        projection = projected_environment_metadata(session)
        self.assertEqual(
            projection.connection.destination, ConnectionEndpoint("10.72.24.17", 22)
        )
        self.assertEqual(
            projection.connection.source, ConnectionEndpoint("198.51.100.23", 47486)
        )
        self.assertEqual(
            session.shell_environment.variables["SSH_CONNECTION"],
            "198.51.100.23 47486 10.72.24.17 22",
        )
        self.assertNotIn("2222", str(projection))
        self.assertNotIn("127.0.0.1", str(projection))
        self.assertEqual(
            projection.metadata["active_network"]["listeners"][0]["local"],
            {"ip": "10.72.24.17", "port": 22},
        )

    def test_hops_have_two_host_perspectives_and_stable_ports(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        anchor = projected_environment_metadata(session)
        incoming = anchor.metadata["active_network"]["connections"]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["local"], {"ip": "198.51.100.166", "port": 22})
        source = incoming[0]["peer"]
        self.assertEqual(source["ip"], "10.72.24.17")
        self.assertTrue(49152 <= source["port"] <= 65535)
        hearth = network_at(session, "hearth-c17")["connections"]
        self.assertEqual(len(hearth), 2)
        self.assertEqual(hearth[1]["local"], source)
        self.assertEqual(hearth[1]["id"], incoming[0]["id"])
        self.assertEqual(anchor.connection.source, ConnectionEndpoint(**source))
        self.assertIn(
            f"10.72.24.17 {source['port']} 198.51.100.166 22",
            session.shell_environment.variables.values(),
        )
        for _ in range(3):
            apply_command_state_update(session, CommandStateUpdate(), command="ss -nat")
            self.assertEqual(projected_environment_metadata(session), anchor)

    def test_three_hops_and_unwind_only_close_the_owning_connection(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        anchor_environment = session.shell_environment
        remote_shell(session, "third", command="ssh third")
        self.assertEqual(len(network_at(session, "hearth-c17")["connections"]), 2)
        self.assertEqual(
            len(network_at(session, "anchor-welcome-01")["connections"]), 2
        )
        self.assertEqual(len(network_at(session, "third")["connections"]), 1)
        self.assertEqual(
            projected_environment_metadata(session).connection.source.ip,
            "198.51.100.166",
        )
        self.assertTrue(
            pop_active_shell_frame(session, force_session_state_next_command=True)
        )
        self.assertEqual(session.shell_environment, anchor_environment)
        self.assertEqual(len(session.network_connections.connections), 2)
        self.assertEqual(network_at(session, "third")["connections"], [])
        apply_command_state_update(
            session,
            CommandStateUpdate(shell_stack_transition=ShellStackTransition("pop")),
            command="exit",
        )
        self.assertEqual(session.host_identity.hostname, "hearth-c17")
        self.assertEqual(len(session.network_connections.connections), 1)

    def test_local_shells_repls_and_sudo_do_not_own_the_inherited_connection(self):
        session = session_with_ingress()
        original = deepcopy(session.network_connections.connections)
        for program, command in [
            ("bash", "bash"),
            ("python", "python3"),
            ("bash", "sudo -i"),
        ]:
            apply_command_state_update(
                session,
                CommandStateUpdate(
                    interactive_program=InteractiveProgram(program, program),
                    shell_stack_transition=ShellStackTransition("push"),
                ),
                command=command,
            )
            self.assertIsNone(
                session.network_connections.frames[-1].owned_connection_id
            )
            self.assertEqual(session.network_connections.connections, original)
            pop_active_shell_frame(session)
            self.assertEqual(session.network_connections.connections, original)

    def test_authentication_reuses_connection_and_failure_closes_it(self):
        for success in (False, True):
            with self.subTest(success=success):
                session = session_with_ingress()
                self._client(session)
                edge = next(
                    edge
                    for edge in session.network_connections.connections.values()
                    if not edge.entry
                )
                self.assertEqual(edge.phase, "authenticating")
                self.assertEqual(session.host_identity.hostname, "hearth-c17")
                self.assertEqual(
                    len(network_at(session, "hearth-c17")["connections"]), 2
                )
                self._client(
                    session, operation="replace_active", command="wrong password"
                )
                self.assertEqual(len(session.network_connections.connections), 2)
                self.assertEqual(session.network_connections.connections[edge.id], edge)
                if success:
                    remote_shell(
                        session,
                        "anchor-welcome-01",
                        command="correct password",
                        operation="replace_active",
                    )
                    authenticated = session.network_connections.connections[edge.id]
                    self.assertEqual(authenticated.source, edge.source)
                    self.assertEqual(authenticated.phase, "authenticated")
                    self.assertEqual(len(session.shell_stack), 2)
                else:
                    apply_command_state_update(
                        session,
                        CommandStateUpdate(
                            shell_stack_transition=ShellStackTransition("pop")
                        ),
                        command="no",
                    )
                    self.assertEqual(len(session.network_connections.connections), 1)

    def test_local_passphrase_does_not_open_socket_until_peer_contact(self):
        session = session_with_ingress()
        self._client(session, phase="passphrase_auth")
        self.assertEqual(len(session.network_connections.connections), 1)
        self._client(
            session,
            phase="password_auth",
            operation="replace_active",
            command="key passphrase",
        )
        self.assertEqual(len(session.network_connections.connections), 2)

    def test_custom_port_forms(self):
        for command in (
            "ssh -p 2207 reader@welcome.anchor-mutual.bsides",
            "ssh -p2207 reader@welcome.anchor-mutual.bsides",
            "ssh -o Port=2207 welcome.anchor-mutual.bsides",
            "ssh ssh://reader@welcome.anchor-mutual.bsides:2207",
            "ssh -vp2207 welcome.anchor-mutual.bsides",
            "ssh -vp 2207 welcome.anchor-mutual.bsides",
        ):
            with self.subTest(command=command):
                session = session_with_ingress()
                remote_shell(session, "anchor-welcome-01", command=command)
                self.assertEqual(
                    projected_environment_metadata(session).connection.destination.port,
                    2207,
                )

    def test_passphrase_can_complete_directly_into_remote_shell(self):
        session = session_with_ingress()
        self._client(session, phase="passphrase_auth")
        remote_shell(
            session,
            "anchor-welcome-01",
            command="unlocked key",
            operation="replace_active",
        )
        self.assertEqual(len(session.network_connections.connections), 2)
        self.assertEqual(
            projected_environment_metadata(session).connection.source.ip, "10.72.24.17"
        )

    def test_exec_ssh_retains_upstream_ingress_until_replacement_exits(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        remote_shell(
            session, "third", command="exec ssh third", operation="replace_active"
        )
        self.assertEqual(len(session.network_connections.connections), 3)
        self.assertEqual(
            len(network_at(session, "anchor-welcome-01")["connections"]), 2
        )
        pop_active_shell_frame(session)
        self.assertEqual(session.host_identity.hostname, "hearth-c17")
        self.assertEqual(len(session.network_connections.connections), 1)

    def test_legacy_stack_replacement_keeps_connection_ownership(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        frame = default_shell_frame(username="reader", hostname="third")
        apply_command_state_update(
            session,
            CommandStateUpdate(shell_stack=(session.shell_stack[0], frame)),
            command="ssh third",
        )
        self.assertEqual(len(session.network_connections.connections), 3)
        self.assertEqual(
            projected_environment_metadata(session).connection.source.ip,
            "198.51.100.166",
        )

    def test_unknown_host_authentication_aligns_canonical_host_and_socket_view(self):
        session = session_with_ingress()
        apply_command_state_update(
            session,
            CommandStateUpdate(
                interactive_program=InteractiveProgram(
                    "ssh",
                    "ssh",
                    ("ssh", "new.example.bsides"),
                    metadata={
                        "phase": "password_auth",
                        "target_host": "new.example.bsides",
                    },
                ),
                shell_stack_transition=ShellStackTransition("push"),
            ),
            command="ssh new.example.bsides",
        )
        remote_shell(
            session, "new-server-01", command="password", operation="replace_active"
        )
        projection = projected_environment_metadata(session)
        self.assertEqual(
            projection.metadata["active_network"]["host"],
            session.world_state["active_host"],
        )
        self.assertEqual(len(projection.metadata["active_network"]["connections"]), 1)

    def test_entry_uses_compatible_fictional_source_family(self):
        session = session_with_ingress()
        session.world_state["hosts_by_name"]["hearth-c17"]["network"] = {
            "ipv6_address": "2001:db8:72::17"
        }
        initialize_session_connections(
            session,
            ingress=ConnectionMetadata(
                protocol="ssh", source=ConnectionEndpoint("198.51.100.23", 47486)
            ),
        )
        projection = projected_environment_metadata(session)
        self.assertEqual(projection.connection.source.ip, "2001:db8::23")
        self.assertEqual(projection.connection.destination.ip, "2001:db8:72::17")

    def test_same_host_ssh_has_two_distinct_socket_rows(self):
        session = session_with_ingress()
        remote_shell(session, "hearth-c17", command="ssh localhost")
        rows = network_at(session, "hearth-c17")["connections"]
        self.assertEqual(len(rows), 3)
        outbound, inbound = rows[1:]
        self.assertEqual(outbound["id"], inbound["id"])
        self.assertEqual(outbound["local"], inbound["peer"])
        self.assertEqual(outbound["peer"], inbound["local"])
        self.assertEqual(outbound["peer"]["ip"], "127.0.0.1")
        pop_active_shell_frame(session)
        self.assertEqual(len(session.network_connections.connections), 1)

    def test_return_hop_to_previous_host_is_a_new_connection(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        remote_shell(session, "hearth-c17", command="ssh hearth-c17")
        self.assertEqual(len(session.network_connections.connections), 3)
        self.assertEqual(len(network_at(session, "hearth-c17")["connections"]), 3)
        self.assertEqual(
            projected_environment_metadata(session).connection.source.ip,
            "198.51.100.166",
        )

    def test_hostname_rename_preserves_connection_identity(self):
        session = session_with_ingress()
        original = deepcopy(session.network_connections.connections)
        apply_command_state_update(
            session,
            CommandStateUpdate(host_identity=HostIdentity("renamed")),
            command="hostname renamed",
        )
        self.assertEqual(session.network_connections.connections, original)
        self.assertEqual(
            projected_environment_metadata(session).connection.destination.ip,
            "10.72.24.17",
        )

    def test_python_input_does_not_create_ssh_connection(self):
        session = session_with_ingress()
        frame = replace(
            session.shell_stack[-1],
            interactive_program=InteractiveProgram("python", "python3"),
        )
        session.shell_stack[-1] = frame
        remote_shell(session, "hearth-c17", command="ssh localhost")
        self.assertEqual(len(session.network_connections.connections), 1)

    def test_structured_ssh_hint_supports_alias_command(self):
        session = session_with_ingress()
        frame = default_shell_frame(username="reader", hostname="anchor-welcome-01")
        apply_command_state_update(
            session,
            CommandStateUpdate(
                host_identity=frame.host_identity,
                interactive_program=frame.interactive_program,
                shell_stack_transition=ShellStackTransition(
                    "push",
                    metadata={
                        "ssh": {"target": "welcome.anchor-mutual.bsides", "port": 2227}
                    },
                ),
            ),
            command="anchor",
        )
        self.assertEqual(
            projected_environment_metadata(session).connection.destination.port, 2227
        )

    def test_runtime_does_not_reset_user_environment_edits(self):
        session = session_with_ingress()
        environment = replace(
            session.shell_environment,
            variables={
                **session.shell_environment.variables,
                "SSH_CONNECTION": "operator override",
            },
        )
        apply_command_state_update(
            session,
            CommandStateUpdate(shell_environment=environment),
            command="export SSH_CONNECTION='operator override'",
        )
        apply_command_state_update(session, CommandStateUpdate(), command="ss -nat")
        self.assertEqual(
            session.shell_environment.variables["SSH_CONNECTION"], "operator override"
        )
        self.assertEqual(
            projected_environment_metadata(session).connection.destination.port, 22
        )

    def test_printenv_uses_runtime_login_values(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        context = CommandContext(
            username=session.username,
            cwd=session.cwd,
            hostname=session.host_identity.hostname,
            command="printenv SSH_CONNECTION",
            langfuse_trace_id="test",
            shell_stack=tuple(session.shell_stack),
            shell_environment=session.shell_environment,
        )
        result = handle_builtin_command_context(context)
        self.assertEqual(
            result.stdout, session.shell_environment.variables["SSH_CONNECTION"] + "\n"
        )

    def test_close_clears_all_session_edges(self):
        session = session_with_ingress()
        remote_shell(
            session, "anchor-welcome-01", command="ssh welcome.anchor-mutual.bsides"
        )
        close_session_connections(session)
        self.assertEqual(network_at(session, "hearth-c17")["connections"], [])
        self.assertEqual(network_at(session, "anchor-welcome-01")["connections"], [])

    def test_explicit_nat_uses_each_hosts_observed_endpoints(self):
        session = session_with_ingress()
        hosts = session.world_state["hosts_by_name"]
        hosts["anchor-welcome-01"]["network"] = {"ipv4_address": "10.184.0.10"}
        hosts["gateway"] = {
            "hostname": "gateway",
            "network": {"ipv4_address": "198.51.100.166"},
        }
        session.world_state["world_facts_by_id"] = {
            "network-targets": {
                "category": "network",
                "targets": {"welcome.anchor-mutual.bsides": "198.51.100.166"},
            },
            "network-translation": {
                "category": "network",
                "connection_translation": {
                    "source_host": "hearth-c17",
                    "target": "198.51.100.166",
                    "target_port": 2207,
                    "destination_host": "anchor-welcome-01",
                    "destination": {"ip": "10.184.0.10", "port": 22},
                    "source": {"ip": "203.0.113.67", "port": 53007},
                },
            },
        }
        remote_shell(
            session,
            "anchor-welcome-01",
            command="ssh -p2207 welcome.anchor-mutual.bsides",
        )
        outgoing = network_at(session, "hearth-c17")["connections"][1]
        incoming = network_at(session, "anchor-welcome-01")["connections"][0]
        self.assertEqual(outgoing["local"]["ip"], "10.72.24.17")
        self.assertEqual(outgoing["peer"], {"ip": "198.51.100.166", "port": 2207})
        self.assertEqual(incoming["local"], {"ip": "10.184.0.10", "port": 22})
        self.assertEqual(incoming["peer"], {"ip": "203.0.113.67", "port": 53007})
        self.assertEqual(
            session.shell_environment.variables["SSH_CONNECTION"],
            "203.0.113.67 53007 10.184.0.10 22",
        )
        self.assertEqual(
            session.world_state["hosts_by_name"]["gateway"]["network"]["ipv4_address"],
            "198.51.100.166",
        )

    def _client(
        self,
        session,
        *,
        phase="password_auth",
        operation="push",
        command="ssh welcome.anchor-mutual.bsides",
    ):
        apply_command_state_update(
            session,
            CommandStateUpdate(
                interactive_program=InteractiveProgram(
                    "ssh",
                    "/usr/bin/ssh",
                    ("ssh", "welcome.anchor-mutual.bsides"),
                    metadata={
                        "phase": phase,
                        "target_host": "welcome.anchor-mutual.bsides",
                    },
                ),
                shell_stack_transition=ShellStackTransition(operation),
            ),
            command=command,
        )


if __name__ == "__main__":
    unittest.main()
