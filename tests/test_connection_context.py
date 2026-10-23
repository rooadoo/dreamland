import importlib.util
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from dreamland.agent_core import (
    LangGraphCommandAgent,
    _format_environment_metadata,
)
from dreamland.models import (
    CommandContext,
    ConnectionEndpoint,
    ConnectionMetadata,
    EnvironmentMetadata,
    HostIdentity,
)


def _network_metadata(host, source_ip, source_port, destination_ip):
    return EnvironmentMetadata(
        connection=ConnectionMetadata(
            protocol="ssh",
            source=ConnectionEndpoint(ip=source_ip, port=source_port),
            destination=ConnectionEndpoint(ip=destination_ip, port=22),
        ),
        metadata={
            "active_network": {
                "host": host,
                "authoritative_session_connections": True,
                "connections": [
                    {
                        "id": f"login-{host}",
                        "protocol": "tcp",
                        "service": "ssh",
                        "state": "ESTAB",
                        "direction": "inbound",
                        "local": {"ip": destination_ip, "port": 22},
                        "peer": {"ip": source_ip, "port": source_port},
                        "phase": "authenticated",
                    }
                ],
            },
            "dreamland.session.evaluation_config": {"internal": "hidden"},
        },
    )


class _PivotModel:
    def __init__(self):
        self.prompts = []

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages[-1].content)
        update = (
            {
                "host_identity": {"hostname": "host-b"},
                "shell_stack_transition": {"operation": "push"},
            }
            if len(self.prompts) == 1
            else {}
        )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "stdout": "",
                    "stderr": "",
                    "exit_status": 0,
                    "state_update": update,
                }
            ),
            additional_kwargs={},
        )


class ConnectionContextTests(unittest.TestCase):
    def test_active_network_is_json_and_internal_evaluation_metadata_stays_hidden(self):
        metadata = _network_metadata("host-a", "203.0.113.8", 47123, "10.0.0.1")
        rendered = _format_environment_metadata(metadata)
        row = next(
            line
            for line in rendered.splitlines()
            if line.startswith("  active_network:")
        )
        self.assertEqual(
            json.loads(row.partition(": ")[2]), metadata.metadata["active_network"]
        )
        self.assertNotIn("evaluation_config", rendered)
        self.assertNotIn("hidden", rendered)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None, "langgraph not installed"
    )
    def test_checkpointed_commands_use_latest_host_connection_in_both_execution_modes(
        self,
    ):
        for stream in (False, True):
            with self.subTest(stream=stream):
                model = _PivotModel()
                agent = LangGraphCommandAgent(
                    model_name="test-model",
                    system_prompt_name="ubuntu",
                    system_prompt="Test connection context.",
                    model=model,
                )
                context = CommandContext(
                    username="alice",
                    cwd="/home/alice",
                    hostname="host-a",
                    host_identity=HostIdentity(hostname="host-a"),
                    command="ssh host-b",
                    langfuse_trace_id=f"connection-context-{stream}",
                    environment_metadata=_network_metadata(
                        "host-a", "203.0.113.8", 47123, "10.0.0.1"
                    ),
                )

                def run(command_context, *, stream=stream, agent=agent):
                    if stream:
                        list(agent.stream_respond(command_context))
                    else:
                        agent.respond(command_context)

                run(context)
                # The graph has the pushed host-b frame. The backend now supplies
                # that host's connection projection without a full state refresh.
                context = replace(
                    context,
                    hostname="host-b",
                    host_identity=HostIdentity(hostname="host-b"),
                    command="ss -nat",
                    environment_metadata=_network_metadata(
                        "host-b", "10.0.0.1", 53124, "198.51.100.166"
                    ),
                )
                run(context)
                prompt = model.prompts[-1]
                self.assertIn('"id": "login-host-b"', prompt)
                self.assertIn('"port": 53124', prompt)
                self.assertNotIn('"id": "login-host-a"', prompt)
                self.assertNotIn("203.0.113.8", prompt)
                self.assertNotIn("47123", prompt)
                # Explicit absence also replaces prior checkpoint metadata.
                run(replace(context, environment_metadata=EnvironmentMetadata()))
                self.assertNotIn("active_network", model.prompts[-1])
                self.assertNotIn("53124", model.prompts[-1])


if __name__ == "__main__":
    unittest.main()
