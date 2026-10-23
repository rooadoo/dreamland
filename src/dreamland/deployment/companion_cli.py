"""Private deployment-control setup; separate from SSH and domain runtimes."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dreamland.deployment import companion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "describe", "export-token", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        if name in {"init", "serve"}:
            command.add_argument(
                "--role", choices=("attendee", "operator"), required=True
            )
        if name == "init":
            command.add_argument("--project", required=True)
            command.add_argument("--network", required=True)
            command.add_argument("--image", required=True)
            command.add_argument("--revision", required=True)
        elif name == "export-token":
            command.add_argument("--output", required=True, type=Path)
        elif name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8080)
            command.add_argument("--container-network", action="store_true")
            command.add_argument(
                "--log-level",
                choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                default="INFO",
            )
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            result = companion.initialize(
                arguments.root,
                role=arguments.role,
                project=arguments.project,
                network=arguments.network,
                image=arguments.image,
                revision=arguments.revision,
            )
        elif arguments.command == "describe":
            result = companion.describe(arguments.root)
        elif arguments.command == "export-token":
            companion.export_token(arguments.root, arguments.output)
            result = {"credential_exported": True}
        else:
            # Setup and export commands never construct a listener.
            from dreamland.deployment.companion_http import build_server, serve

            logging.basicConfig(
                level=arguments.log_level,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
            )
            server = build_server(
                arguments.root,
                role=arguments.role,
                host=arguments.host,
                port=arguments.port,
                container_network=arguments.container_network,
            )
            print(
                json.dumps(
                    {
                        "event": "deployment_control_started",
                        "role": arguments.role,
                        "port": server.server_address[1],
                        "domain_services_available": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return serve(server)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError):
        print(
            "Deployment-control operation failed; check configuration and private file access.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Deployment-control operation cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
