import json
import unittest
from dataclasses import replace

from dreamland.agent_core import _format_command_context, _state_input_from_context
from dreamland.agent_tracing import graph_invoke_config, session_start_trace_metadata
from dreamland.models import (
    CommandContext,
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    HostIdentity,
    SessionState,
)


def _ingress():
    # Older SSH admission metadata can contain profile-projected outer endpoints;
    # the raw socket capture is the explicit deployment transport authority.
    return ConnectionMetadata(
        protocol="ssh",
        source=ConnectionEndpoint("198.51.100.23", 47486),
        destination=ConnectionEndpoint("10.72.24.17", 22),
        metadata={
            "transport": {
                "source": {"ip": "127.0.0.8", "port": 47486},
                "destination": {"ip": "127.0.0.9", "port": 2222},
            }
        },
    )


def _context():
    return CommandContext(
        username="reader",
        cwd="/home/reader",
        hostname="anchor-welcome-01",
        host_identity=HostIdentity("anchor-welcome-01"),
        command="ss -nat",
        langfuse_trace_id="connection-tracing-test",
        environment_metadata=EnvironmentMetadata(
            metadata={"state_profile.name": "debian-office"},
            connection=ConnectionMetadata(
                protocol="ssh",
                source=ConnectionEndpoint("10.72.24.17", 53124),
                destination=ConnectionEndpoint("198.51.100.166", 22),
            ),
        ),
        transport_connection=_ingress(),
    )


class ConnectionTracingTests(unittest.TestCase):
    def test_command_trace_distinguishes_simulated_login_from_raw_transport(self):
        config = graph_invoke_config((), _context())
        metadata = config["metadata"]
        self.assertEqual(metadata["dreamland.state_profile.name"], "debian-office")
        self.assertEqual(config["tags"], ["dreamland", "debian-office"])
        self.assertNotIn("dreamland.profile", metadata)
        self.assertEqual(metadata["dreamland.connection.scope"], "simulated_host")
        self.assertEqual(metadata["dreamland.connection.source_ip"], "10.72.24.17")
        self.assertEqual(metadata["dreamland.connection.source_port"], "53124")
        self.assertEqual(
            metadata["dreamland.connection.destination_ip"], "198.51.100.166"
        )
        self.assertEqual(metadata["dreamland.connection.destination_port"], "22")
        self.assertEqual(
            metadata["dreamland.transport.connection.source_ip"], "127.0.0.8"
        )
        self.assertEqual(
            metadata["dreamland.transport.connection.destination_ip"], "127.0.0.9"
        )
        self.assertEqual(
            metadata["dreamland.transport.connection.destination_port"], "2222"
        )

    def test_session_trace_labels_legacy_ingress_and_exposes_raw_transport(self):
        session = SessionState(
            username="adam",
            environment_metadata=EnvironmentMetadata(
                connection=_ingress(),
                metadata={"state_profile.name": "debian-office"},
            ),
        )
        metadata = session_start_trace_metadata(session)
        self.assertEqual(metadata["dreamland.state_profile.name"], "debian-office")
        self.assertNotIn("dreamland.profile", metadata)
        self.assertEqual(metadata["dreamland.connection.scope"], "transport_ingress")
        self.assertEqual(metadata["dreamland.connection.source_ip"], "198.51.100.23")
        self.assertEqual(metadata["dreamland.connection.destination_ip"], "10.72.24.17")
        self.assertEqual(
            metadata["dreamland.transport.connection.source_ip"], "127.0.0.8"
        )
        self.assertEqual(
            metadata["dreamland.transport.connection.destination_port"], "2222"
        )

    def test_transport_without_raw_capture_uses_supplied_ingress_endpoints(self):
        ingress = replace(_ingress(), metadata={})
        metadata = graph_invoke_config(
            (), replace(_context(), transport_connection=ingress)
        )["metadata"]
        self.assertEqual(
            metadata["dreamland.transport.connection.source_ip"], ingress.source.ip
        )
        self.assertEqual(
            metadata["dreamland.transport.connection.destination_ip"],
            ingress.destination.ip,
        )

    def test_absent_connections_do_not_invent_trace_endpoints(self):
        metadata = graph_invoke_config(
            (),
            replace(
                _context(),
                transport_connection=None,
                environment_metadata=EnvironmentMetadata(),
            ),
        )["metadata"]
        self.assertEqual(metadata["dreamland.connection.scope"], "simulated_host")
        self.assertNotIn("dreamland.connection.source_ip", metadata)
        self.assertNotIn("dreamland.transport.connection.source_ip", metadata)

    def test_transport_provenance_is_absent_from_model_prompt_and_graph_state(self):
        context = _context()
        rendered = [_format_command_context(context)]
        for include_session_state in (False, True):
            rendered.append(
                json.dumps(
                    _state_input_from_context(
                        context, include_session_state=include_session_state
                    )
                )
            )
        for text in rendered:
            self.assertNotIn("127.0.0.8", text)
            self.assertNotIn("127.0.0.9", text)
            self.assertNotIn("2222", text)
            self.assertNotIn("transport_connection", text)
            self.assertIn("198.51.100.166", text)


if __name__ == "__main__":
    unittest.main()
