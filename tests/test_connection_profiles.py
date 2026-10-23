import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace

from dreamland.connection_codec import (
    network_connections_from_state,
    network_connections_to_state,
)
from dreamland.models import (
    ConnectionEndpoint,
    ConnectionMetadata,
    FrameConnectionBinding,
    SessionConnections,
    SimulatedConnection,
    default_shell_frame,
)


class ConnectionCodecTests(unittest.TestCase):
    def test_exact_round_trip_preserves_ownership_and_endpoint_identity(self):
        graph = _graph()

        decoded = network_connections_from_state(
            json.loads(json.dumps(network_connections_to_state(graph))), frame_count=3
        )

        self.assertEqual(decoded, graph)
        self.assertIsNot(decoded.connections, graph.connections)
        self.assertEqual(decoded.frames[2].login_connection_id, "hop")
        self.assertIsNone(decoded.frames[2].owned_connection_id)

    def test_malformed_connection_fields_are_ignored_without_coercion(self):
        mutations = (
            {"id": "wrong-key"},
            {"source_host": []},
            {"destination_host": " "},
            {"phase": []},
            {"phase": "closed"},
            {"entry": "true"},
            {"source": {"ip": "hearth", "port": 42001}},
            {"source": {"ip": "10.72.24.17", "port": True}},
            {"source": {"ip": "10.72.24.17", "port": 65536}},
            {"destination": {"ip": 42, "port": 22}},
            {"destination": {"ip": "198.51.100.166", "port": "22"}},
            {"destination": None},
            {"source_peer": {"ip": "not-an-address", "port": 22}},
            {"destination_peer": {"ip": "203.0.113.17", "port": False}},
        )
        for patch in mutations:
            with self.subTest(patch=patch):
                state = network_connections_to_state(_graph())
                state["connections"]["hop"].update(patch)

                decoded = network_connections_from_state(state, frame_count=3)

                self.assertEqual(set(decoded.connections), {"entry"})
                self.assertEqual(decoded.frames[1], FrameConnectionBinding())
                self.assertEqual(decoded.frames[2], FrameConnectionBinding())

    def test_bindings_drop_duplicates_dangling_references_and_orphan_edges(self):
        state = network_connections_to_state(_graph())
        state["frames"] = [
            {"login_connection_id": "missing", "owned_connection_id": "entry"},
            {"login_connection_id": "hop", "owned_connection_id": "entry"},
        ]

        decoded = network_connections_from_state(state, frame_count=3)

        self.assertEqual(set(decoded.connections), {"entry"})
        self.assertEqual(decoded.frames[0], FrameConnectionBinding(None, "entry"))
        self.assertEqual(decoded.frames[1:], [FrameConnectionBinding()] * 2)

    def test_removed_frame_does_not_restore_its_connection(self):
        decoded = network_connections_from_state(
            network_connections_to_state(_graph()), frame_count=1
        )

        self.assertEqual(set(decoded.connections), {"entry"})
        self.assertEqual(decoded.frames, [FrameConnectionBinding("entry", "entry")])

    def test_retained_owner_survives_exact_round_trip(self):
        graph = _graph()
        graph.frames = [FrameConnectionBinding("hop", "hop", ("entry",))]

        decoded = network_connections_from_state(
            json.loads(json.dumps(network_connections_to_state(graph))), frame_count=1
        )

        self.assertEqual(decoded, graph)
        self.assertEqual(set(decoded.connections), {"entry", "hop"})

    def test_nat_peer_overrides_preserve_local_endpoints_across_round_trip(self):
        graph = _graph()
        graph.connections["hop"] = replace(
            graph.connections["hop"],
            destination=ConnectionEndpoint("10.31.1.6", 22),
            source_peer=ConnectionEndpoint("203.0.113.66", 2222),
            destination_peer=ConnectionEndpoint("203.0.113.17", 42002),
        )

        state = json.loads(json.dumps(network_connections_to_state(graph)))
        decoded = network_connections_from_state(state, frame_count=3)

        self.assertEqual(decoded, graph)
        self.assertEqual(decoded.connections["hop"].source.ip, "10.72.24.17")
        self.assertEqual(decoded.connections["hop"].destination.ip, "10.31.1.6")
        self.assertNotIn("source_peer", state["connections"]["entry"])
        self.assertNotIn("destination_peer", state["connections"]["entry"])

    def test_retained_owners_drop_duplicate_missing_and_malformed_references(self):
        graph = _graph()
        graph.frames = [
            FrameConnectionBinding("hop", "hop", ("entry",)),
            FrameConnectionBinding("entry", "entry", ("hop",)),
        ]
        state = network_connections_to_state(graph)
        state["frames"][0]["retained_connection_ids"] += [
            "entry",
            "hop",
            "missing",
            [],
        ]

        decoded = network_connections_from_state(state, frame_count=2)

        self.assertEqual(set(decoded.connections), {"entry", "hop"})
        self.assertEqual(
            decoded.frames,
            [
                FrameConnectionBinding("hop", "hop", ("entry",)),
                FrameConnectionBinding("entry"),
            ],
        )

    def test_authenticating_connection_can_be_owned_but_cannot_be_login(self):
        state = network_connections_to_state(_graph())
        state["connections"]["hop"]["phase"] = "authenticating"

        decoded = network_connections_from_state(state, frame_count=3)

        self.assertEqual(decoded.frames[1], FrameConnectionBinding(None, "hop"))
        self.assertEqual(decoded.frames[2], FrameConnectionBinding())
        self.assertEqual(decoded.connections["hop"].phase, "authenticating")

    def test_missing_or_invalid_graph_preserves_frame_alignment(self):
        for state in (None, [], "legacy", {}, {"connections": [], "frames": {}}):
            with self.subTest(state=state):
                self.assertEqual(
                    network_connections_from_state(state, frame_count=2),
                    SessionConnections(frames=[FrameConnectionBinding()] * 2),
                )


class ConnectionProfileTests(unittest.TestCase):
    def test_product_exact_checkpoints_keep_connections_between_commands(self):
        from dreamland.product.runtime_executor import DreamlandStateProfileCodec
        from dreamland.state_profiles import create_state_profile

        original = _connected_session()
        document = create_state_profile(
            original, name="connected", level="exact"
        ).to_document()
        codec = DreamlandStateProfileCodec()

        first = codec.decode(document, checkpoint_namespace="product:visit:1:command1")
        second = codec.decode(
            codec.encode(first), checkpoint_namespace="product:visit:2:command2"
        )

        self.assertNotEqual(
            first.session.langfuse_trace_id, second.session.langfuse_trace_id
        )
        self.assertEqual(
            first.session.network_connections, original.network_connections
        )
        self.assertEqual(
            second.session.network_connections, original.network_connections
        )

    def test_fresh_profile_levels_never_restore_saved_session_connections(self):
        from dreamland.state_profiles import state_profile_to_session

        for level in ("world", "system"):
            for ingress in (None, _ingress()):
                with self.subTest(level=level, ingress=ingress):
                    profile = _profile(level)
                    profile.state["network_connections"] = network_connections_to_state(
                        _graph()
                    )
                    profile.state["environment_metadata"] = {
                        "connection": {
                            "protocol": "ssh",
                            "source": {"ip": "198.51.100.99", "port": 49999},
                            "destination": {"ip": "127.0.0.1", "port": 2222},
                        }
                    }

                    session = state_profile_to_session(
                        profile, current_session_id="fresh", connection_metadata=ingress
                    )

                    connections = session.network_connections.connections
                    self.assertNotIn("entry", connections)
                    self.assertNotIn("hop", connections)
                    self.assertEqual(len(connections), 0 if ingress is None else 1)
                    if ingress is not None:
                        entry = next(iter(connections.values()))
                        self.assertEqual(entry.destination_host, "hearth")
                        self.assertEqual(
                            entry.destination, ConnectionEndpoint("10.72.24.17", 22)
                        )
                        self.assertEqual(entry.source, ingress.source)

    def test_only_exact_profile_exports_session_connections(self):
        from dreamland.state_profiles import session_state_for_level

        session = _connected_session()
        for level in ("world", "system", "exact"):
            with self.subTest(level=level):
                state = session_state_for_level(session, level)
                self.assertEqual("network_connections" in state, level == "exact")
                self.assertNotIn("network_connections", state["world_state"])
                self.assertEqual(
                    state["world_state"]["hosts_by_name"]["hearth"]["network"][
                        "listening_ports"
                    ],
                    [{"port": 631, "protocol": "tcp", "service": "ipp"}],
                )

    def test_applying_profile_offline_does_not_revive_saved_transport_metadata(self):
        from dreamland.state_profiles import (
            apply_state_profile_to_session,
            state_profile_to_session,
        )

        profile = _profile("system")
        profile.state["environment_metadata"] = {
            "connection": {
                "protocol": "ssh",
                "source": {"ip": "198.51.100.23", "port": 47486},
                "destination": {"ip": "127.0.0.1", "port": 2222},
            }
        }
        session = state_profile_to_session(profile, current_session_id="offline")
        self.assertEqual(session.network_connections.connections, {})
        apply_state_profile_to_session(session, _profile("world"))
        self.assertEqual(session.network_connections.connections, {})

    def test_live_exact_restore_rebinds_entry_and_preserves_nested_connections(self):
        from dreamland.state_profiles import (
            apply_state_profile_to_session,
            create_state_profile,
            state_profile_to_session,
        )

        saved = create_state_profile(_connected_session(), name="saved", level="exact")
        ingress = _ingress(source_ip="198.51.100.44", source_port=48888)
        target = state_profile_to_session(
            _profile("world"), current_session_id="current", connection_metadata=ingress
        )

        apply_state_profile_to_session(target, saved)

        self.assertEqual(target.langfuse_trace_id, "current")
        self.assertEqual(target.environment_metadata.connection, ingress)
        self.assertEqual(
            target.network_connections.connections["hop"], _graph().connections["hop"]
        )
        entries = [
            c for c in target.network_connections.connections.values() if c.entry
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].source, ingress.source)
        self.assertEqual(entries[0].destination, ConnectionEndpoint("10.72.24.17", 22))
        self.assertEqual(
            target.network_connections.frames[2].login_connection_id, "hop"
        )

    def test_legacy_exact_stack_recovers_hops_once_when_graph_is_absent(self):
        from dreamland.state_profiles import (
            create_state_profile,
            state_profile_to_session,
        )

        profile = create_state_profile(
            _connected_session(), name="legacy", level="exact"
        )
        del profile.state["network_connections"]

        restored = state_profile_to_session(
            profile, current_session_id="restored", connection_metadata=_ingress()
        )
        checkpoint = create_state_profile(restored, name="checkpoint", level="exact")
        resumed = state_profile_to_session(checkpoint, current_session_id="resumed")

        self.assertEqual(len(restored.network_connections.connections), 2)
        hop = next(
            edge
            for edge in restored.network_connections.connections.values()
            if not edge.entry
        )
        self.assertEqual((hop.source_host, hop.destination_host), ("hearth", "visitor"))
        self.assertEqual(hop.destination, ConnectionEndpoint("198.51.100.166", 22))
        self.assertEqual(
            restored.network_connections.frames[2], FrameConnectionBinding(hop.id)
        )
        self.assertEqual(resumed.network_connections, restored.network_connections)

    def test_exact_explicit_empty_graph_does_not_reconstruct_legacy_hops(self):
        from dreamland.state_profiles import (
            create_state_profile,
            state_profile_to_session,
        )

        profile = create_state_profile(
            _connected_session(), name="explicit-empty", level="exact"
        )
        profile.state["network_connections"] = network_connections_to_state(
            SessionConnections(frames=[FrameConnectionBinding()] * 3)
        )

        restored = state_profile_to_session(profile, current_session_id="restored")

        self.assertEqual(restored.network_connections.connections, {})
        self.assertEqual(
            restored.network_connections.frames, [FrameConnectionBinding()] * 3
        )

    def test_exact_restore_keeps_entry_on_original_host_after_exec_ssh(self):
        from dreamland.state_profiles import (
            create_state_profile,
            state_profile_to_session,
        )

        original = _connected_session()
        original.shell_stack = original.shell_stack[1:2]
        original.network_connections.frames = [
            FrameConnectionBinding("hop", "hop", ("entry",))
        ]
        profile = create_state_profile(original, name="exec-login", level="exact")
        ingress = _ingress(source_ip="198.51.100.44", source_port=48888)

        restored = state_profile_to_session(
            profile, current_session_id="restored", connection_metadata=ingress
        )

        self.assertEqual(
            set(restored.network_connections.connections), {"entry", "hop"}
        )
        entry = restored.network_connections.connections["entry"]
        self.assertEqual(entry.destination_host, "hearth")
        self.assertEqual(entry.destination, ConnectionEndpoint("10.72.24.17", 22))
        self.assertEqual(entry.source, ingress.source)
        self.assertEqual(
            restored.network_connections.frames,
            [FrameConnectionBinding("hop", "hop", ("entry",))],
        )

    def test_two_lobby_visits_bind_the_selected_host_over_shared_transport(self):
        from dreamland.backend import AgentBackend
        from dreamland.profile import server_profile
        from dreamland.state_profiles import StateProfileRuntime, StateProfileStore

        first_profile = _profile("world")
        second_profile = _profile("world", host="visitor", address="198.51.100.166")
        ingress = _ingress()
        with tempfile.TemporaryDirectory() as directory:
            backend = AgentBackend(
                server_profile(),
                state_profile_runtime=StateProfileRuntime(
                    store=StateProfileStore(directory), current_profile=first_profile
                ),
            )
            first = backend.create_session("adam", ingress, state_profile=first_profile)
            second = backend.create_session(
                "adam", ingress, state_profile=second_profile
            )

        first_entry = next(iter(first.network_connections.connections.values()))
        second_entry = next(iter(second.network_connections.connections.values()))
        self.assertNotEqual(first_entry.id, second_entry.id)
        self.assertEqual(first_entry.destination_host, "hearth")
        self.assertEqual(second_entry.destination_host, "visitor")
        self.assertEqual(
            second_entry.destination, ConnectionEndpoint("198.51.100.166", 22)
        )
        self.assertEqual(first_entry.source, second_entry.source)

    def test_legacy_exact_and_offline_profiles_do_not_invent_outer_ingress(self):
        from dreamland.state_profiles import state_profile_to_session

        for level in ("world", "system", "exact"):
            with self.subTest(level=level):
                session = state_profile_to_session(
                    _profile(level), current_session_id="offline"
                )
                self.assertEqual(session.network_connections.connections, {})
                self.assertEqual(
                    session.network_connections.frames, [FrameConnectionBinding()]
                )


def _ingress(*, source_ip="198.51.100.23", source_port=47486):
    return ConnectionMetadata(
        protocol="ssh",
        source=ConnectionEndpoint(source_ip, source_port),
        destination=ConnectionEndpoint("127.0.0.1", 2222),
    )


def _graph():
    return SessionConnections(
        connections={
            "entry": SimulatedConnection(
                id="entry",
                source_host=None,
                destination_host="hearth",
                source=ConnectionEndpoint("198.51.100.23", 47486),
                destination=ConnectionEndpoint("10.72.24.17", 22),
                entry=True,
            ),
            "hop": SimulatedConnection(
                id="hop",
                source_host="hearth",
                destination_host="visitor",
                source=ConnectionEndpoint("10.72.24.17", 42001),
                destination=ConnectionEndpoint("198.51.100.166", 22),
            ),
        },
        frames=[
            FrameConnectionBinding("entry", "entry"),
            FrameConnectionBinding("hop", "hop"),
            FrameConnectionBinding("hop", None),
        ],
    )


def _profile(level, *, host="hearth", address="10.72.24.17"):
    from dreamland.state_profiles import StateProfile

    return StateProfile(
        name=f"{host}-{level}",
        level=level,
        created_at="2026-09-11T00:00:00+00:00",
        state={
            "username": "adam",
            "world_state": {
                "entry_host": host,
                "active_host": host,
                "hosts_by_name": {
                    host: {
                        "login_banner": "Test console\n",
                        "hostname": host,
                        "network": {
                            "interfaces": [
                                {"name": "lo", "address": "127.0.0.1/8"},
                                {"name": "eth0", "address": address},
                            ],
                            "listening_ports": [
                                {"port": 631, "protocol": "tcp", "service": "ipp"}
                            ],
                        },
                    }
                },
            },
        },
    )


def _connected_session():
    from dreamland.state_profiles import state_profile_to_session

    session = state_profile_to_session(
        _profile("world"), current_session_id="original", connection_metadata=_ingress()
    )
    remote = default_shell_frame(username="reader", hostname="visitor")
    session.shell_stack.extend([remote, deepcopy(remote)])
    session.username = remote.username
    session.host_identity = remote.host_identity
    session.cwd = remote.cwd
    session.interactive_program = remote.interactive_program
    session.shell_environment = remote.shell_environment
    session.world_state["active_host"] = "visitor"
    session.world_state["hosts_by_name"]["visitor"] = deepcopy(
        _profile("world", host="visitor", address="198.51.100.166").state[
            "world_state"
        ]["hosts_by_name"]["visitor"]
    )
    session.network_connections = _graph()
    return session


if __name__ == "__main__":
    unittest.main()
