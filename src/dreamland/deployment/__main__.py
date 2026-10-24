"""Single-process setup CLI; credentials never appear in arguments or output."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings
from pathlib import Path

from dreamland.deployment import configuration, storage


def _prompt_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    console_command = commands.add_parser("console")
    console_command.add_argument(
        "--socket", type=Path, default=Path("/run/dreamland/console.sock")
    )
    console_command.add_argument(
        "--command", dest="command_line", help="run one console command and disconnect"
    )
    for name in (
        "init",
        "validate",
        "show",
        "configure-ssh",
        "configure-ssh-fast",
        "serve-ssh",
        "model-defaults",
    ):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name == "serve-ssh":
            command.add_argument("--console-socket", type=Path)
        if name == "model-defaults":
            command.add_argument(
                "--apply",
                action="store_true",
                help="replace model controls, preserving an INI backup; stop SSH first",
            )
        if name == "configure-ssh":
            command.add_argument(
                "--only-missing",
                action="store_true",
                help="prompt only for credentials this deployment has not stored",
            )
        if name == "init":
            command.add_argument(
                "--profile",
                choices=("prerelease", "conference", "beta"),
                default="prerelease",
            )
            command.add_argument(
                "--ssh-entry-mode", choices=("lobby", "direct"), default="lobby"
            )
            command.add_argument(
                "--ssh", action=argparse.BooleanOptionalAction, default=True
            )
            command.add_argument(
                "--ssh-bans", action=argparse.BooleanOptionalAction, default=None
            )
    arguments = parser.parse_args(argv)
    if arguments.command == "console":
        from dreamland.console_client import main as console_main

        client_arguments = ["--socket", str(arguments.socket)]
        if arguments.command_line is not None:
            client_arguments.append("--command=" + arguments.command_line)
        return console_main(client_arguments)
    root = arguments.root.absolute()
    try:
        if arguments.command == "init":
            document = configuration.default_document(
                arguments.profile,
                ssh=arguments.ssh,
                ssh_entry_mode=arguments.ssh_entry_mode,
                ssh_bans=arguments.ssh_bans,
            )
            report = storage.initialize(
                root, document, configuration.default_ini(document)
            )
            if "deployment.json" not in report["absent"]:
                saved_document = json.loads(
                    storage.read_managed(root, "deployment.json")
                )
                configuration.validate_document(saved_document)
            print(json.dumps(report, indent=2))
        elif arguments.command in {"validate", "show"}:
            print(json.dumps(configuration.validate(root), indent=2, sort_keys=True))
        elif arguments.command == "model-defaults":
            print(
                json.dumps(
                    configuration.update_model_defaults(root, apply=arguments.apply),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif arguments.command == "configure-ssh-fast":
            configuration.validate(root, require_langfuse_credentials=False)
            document, _ = storage.read_configuration(root)
            if document["ssh_entry_mode"] != "lobby":
                raise configuration.DeploymentError(
                    "fast SSH logins require lobby entry mode"
                )
            if not sys.stdin.isatty():
                raise configuration.DeploymentError(
                    "configure-ssh-fast requires an interactive terminal"
                )
            normal_password = storage.read_secret(root, "ssh_password")
            password = _prompt_secret("New DREAMLAND fast SSH password: ")
            if password != _prompt_secret("Confirm fast SSH password: "):
                raise configuration.DeploymentError(
                    "fast SSH password confirmation did not match"
                )
            storage.validate_secret(password)
            if password == normal_password:
                raise configuration.DeploymentError(
                    "fast SSH password must differ from the normal SSH password"
                )
            storage.write_secret(root, "ssh_fast_password", password)
            print("Fast SSH password saved. Restart SSH to apply it.")
        elif arguments.command == "configure-ssh":
            # Langfuse completeness is intentionally not required here: for a
            # freshly initialized beta/conference deployment this command is
            # what supplies those keys in the first place.
            configuration.validate(root, require_langfuse_credentials=False)
            document, _ = storage.read_configuration(root)
            configuration.validate_document(document)
            traced = document["profile"] in configuration.LANGFUSE_ENABLED_PROFILES
            langfuse_names = tuple(
                f"langfuse.{key}" for key in configuration.LANGFUSE_CREDENTIAL_KEYS
            )
            if arguments.only_missing:
                needed = set(configuration.missing_credentials(root))
                if not needed:
                    # A no-op is a successful result for gap-filling setup, and
                    # needs no terminal.
                    print("Every required deployment credential is already stored.")
                    return 0
            else:
                needed = {"ssh_password", "openai_api_key"}
                if traced:
                    needed.update(langfuse_names)
            if not sys.stdin.isatty():
                raise configuration.DeploymentError(
                    "configure-ssh requires an interactive terminal"
                )

            # Collect and check every prompted value before replacing any stored
            # credential, so a rejected entry cannot leave a partial pair.
            password = None
            api_key = None
            if "ssh_password" in needed:
                password = _prompt_secret("New shared DREAMLAND SSH password: ")
                if password != _prompt_secret("Confirm shared SSH password: "):
                    raise configuration.DeploymentError(
                        "SSH password confirmation did not match"
                    )
                storage.validate_secret(password)
                if storage.secret_present(root, "ssh_fast_password") and (
                    password == storage.read_secret(root, "ssh_fast_password")
                ):
                    raise configuration.DeploymentError(
                        "normal SSH password must differ from the fast SSH password"
                    )
            if "openai_api_key" in needed:
                api_key = _prompt_secret("Fresh deployment OpenAI API key: ")
                storage.validate_secret(api_key)

            # Beta and conference deployments record sessions under the user
            # agreement; see decision 0111.
            langfuse_values: dict[str, str] = {}
            if traced and needed.intersection(langfuse_names):
                print("\nThis deployment profile records sessions in Langfuse.")
                if "langfuse.public_key" in needed:
                    value = _prompt_secret("Langfuse public key (pk-lf-...): ")
                    storage.validate_secret(value)
                    langfuse_values["public_key"] = value
                if "langfuse.secret_key" in needed:
                    value = _prompt_secret("Langfuse secret key (sk-lf-...): ")
                    storage.validate_secret(value)
                    langfuse_values["secret_key"] = value
                if "langfuse.host" in needed:
                    host = input("Langfuse host [https://cloud.langfuse.com]: ").strip()
                    langfuse_values["host"] = host or "https://cloud.langfuse.com"

            if password is not None:
                storage.write_secret(root, "ssh_password", password)
            if api_key is not None:
                storage.write_secret(root, "openai_api_key", api_key)
            if langfuse_values:
                configuration.update_langfuse_settings(root, **langfuse_values)
                print(
                    "Langfuse tracing configured for the "
                    f"{document['profile']} deployment."
                )
            print("Deployment credentials saved. Restart SSH to apply them.")
        elif arguments.command == "serve-ssh":
            report = configuration.validate(root)
            if not report["configuration"]["requested_services"]["ssh"]:
                raise configuration.DeploymentError(
                    "SSH is disabled in deployment.json"
                )
            environment = configuration.runtime_environment(
                os.environ,
                root,
                report["configuration"],
                credentials=True,
            )
            console_socket = (
                arguments.console_socket.absolute()
                if arguments.console_socket is not None
                else None
            )
            os.chdir(root)
            os.umask(0o077)
            daemon_arguments = [sys.executable, "-m", "dreamland", "--daemon"]
            if console_socket is not None:
                daemon_arguments.extend(["--console-socket", str(console_socket)])
            os.execve(
                sys.executable,
                daemon_arguments,
                environment,
            )
    except (configuration.DeploymentError, storage.StorageError) as error:
        print(f"Deployment setup failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, EOFError, getpass.GetPassWarning):
        print(
            "Deployment setup failed; check configuration and file access.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Deployment setup cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
