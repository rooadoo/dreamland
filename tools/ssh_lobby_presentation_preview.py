"""Run a disposable, provider-free SSH lobby for human terminal checkpoints.

This tool composes the real lobby, SSH transport, console, and visit lifecycle
with the existing deterministic command agent. It does not load dreamland.ini,
initialize provider/tracing clients, or reuse a deployed catalog or host key.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dreamland.agent import DeterministicCommandAgent
from dreamland.backend import AgentBackend
from dreamland.config import ConsoleSettings, LobbySettings
from dreamland.console import ConsoleContext, DreamlandConsole
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.profile import server_profile
from dreamland.runtime_logging import LogRecordBuffer
from dreamland.ssh_auth import SharedPasswordAuthenticator
from dreamland.ssh_lobby import SSHLobby
from dreamland.ssh_transport import DreamlandSSHServer, SSHServerConfig
from dreamland.state_profiles import (
    StateProfile,
    StateProfileRuntime,
    StateProfileStore,
)
from dreamland.structured_failures import StructuredFailureStore


@dataclass(frozen=True, slots=True)
class PreviewInstance:
    server: DreamlandSSHServer
    console: DreamlandConsole
    backend: AgentBackend
    catalog: LobbyCatalog
    content: LobbyContent
    settings: LobbySettings
    password: str


def build_preview(
    root: Path,
    *,
    port: int = 0,
    catalog_case: str = "normal",
    welcome_path: Path | None = None,
    guidelines_path: Path | None = None,
    contact_path: Path | None = None,
    color: str = "auto",
    clear_screen: str = "auto",
    banner: str = "auto",
    intro_animation: str = "auto",
    langfuse_notice: bool = False,
) -> PreviewInstance:
    """Compose one test instance under an empty disposable directory."""
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if catalog_case not in {"normal", "empty", "unavailable"}:
        raise ValueError("catalog case must be normal, empty, or unavailable")
    if any(root.iterdir()):
        raise ValueError("preview directory must be empty")
    content = LobbyContent(
        welcome_path,
        guidelines_path,
        contact_path,
        langfuse_enabled=langfuse_notice,
    )
    # Fail before opening the listener if operator copy cannot be displayed.
    content.snapshot()
    settings = LobbySettings(
        catalog_path=root / "catalog.json",
        public_registry_path=root / "visits.sqlite3",
        welcome_path=welcome_path,
        guidelines_path=guidelines_path,
        contact_path=contact_path,
        color=color,
        clear_screen=clear_screen,
        banner=banner,
        intro_animation=intro_animation,
    )
    store = StateProfileStore(root / "profiles")
    for name in ("alpha", "beta"):
        store.save(
            StateProfile(
                name,
                "world",
                {
                    "world_state": {
                        "entry_host": "ubuntu",
                        "active_host": "ubuntu",
                        "hosts_by_name": {
                            "ubuntu": {
                                "hostname": "ubuntu",
                                "login_banner": f"Preview {name.title()} login console.\n",
                            }
                        },
                    }
                },
                datetime.now(UTC).isoformat(),
            )
        )
    catalog = LobbyCatalog(store, settings.catalog_path)
    if catalog_case != "empty":
        catalog.add(
            "alpha",
            title="Preview Alpha",
            description="First disposable test world.",
            entry_username="operator",
        )
        catalog.add(
            "beta",
            title="Preview Beta",
            description="Second disposable test world.",
            entry_username="reader",
        )
    if catalog_case == "unavailable":
        # Deliberately invalidate only this newly created fixture after publishing.
        store.path_for("beta").unlink()
    identities = PublicSimulationRegistry(settings.public_registry_path)
    runtime = StateProfileRuntime(store=store)
    failures = StructuredFailureStore(root / "failures")
    backend = AgentBackend(
        server_profile(),
        agent=DeterministicCommandAgent(),
        state_profile_runtime=runtime,
        structured_failure_store=failures,
        structured_failure_capture_enabled=False,
    )
    console = DreamlandConsole(
        ConsoleSettings(),
        LogRecordBuffer(100),
        lambda: server.request_stop(),
        state_profile_runtime=runtime,
        structured_failure_store=failures,
        lobby_catalog=catalog,
        public_simulations=identities,
    )
    password = secrets.token_urlsafe(12)
    server = DreamlandSSHServer(
        SSHServerConfig(
            host="127.0.0.1",
            port=port,
            host_key_path=root / "host_key",
            entry_mode="lobby",
        ),
        backend,
        console,
        authenticator=SharedPasswordAuthenticator(password),
        lobby=SSHLobby(catalog, identities, content, settings),
    )
    return PreviewInstance(
        server, console, backend, catalog, content, settings, password
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--port", type=int, default=0, help="loopback port; 0 chooses one"
    )
    result.add_argument(
        "--catalog", choices=("normal", "empty", "unavailable"), default="normal"
    )
    result.add_argument("--welcome-path", type=Path)
    result.add_argument("--guidelines-path", type=Path)
    result.add_argument("--contact-path", type=Path)
    result.add_argument("--color", choices=("auto", "off"), default="auto")
    result.add_argument("--clear-screen", choices=("auto", "off"), default="auto")
    result.add_argument("--banner", choices=("auto", "simple"), default="auto")
    result.add_argument(
        "--intro-animation",
        choices=("auto", "off"),
        default="auto",
        help="show the introduction on supported terminals, or disable it",
    )
    result.add_argument(
        "--langfuse-notice",
        action="store_true",
        help="display the optional disclosure as a copy fixture; tracing stays disabled",
    )
    result.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="WARNING"
    )
    return result


def run_console(instance: PreviewInstance) -> None:
    context = ConsoleContext()
    while not instance.console.shutdown_requested:
        try:
            command = input("preview> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        result = instance.console.execute(command, allow_exit=True, context=context)
        print(result.output, end="", flush=True)
        if result.refresh is not None or result.follow:
            print("This preview console supports one-shot commands; omit --follow.")
        if result.leave or result.shutdown:
            return


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    logging.basicConfig(
        level=arguments.log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    with tempfile.TemporaryDirectory(
        prefix="dreamland-lobby-presentation-"
    ) as directory:
        instance = build_preview(
            Path(directory),
            port=arguments.port,
            catalog_case=arguments.catalog,
            welcome_path=arguments.welcome_path,
            guidelines_path=arguments.guidelines_path,
            contact_path=arguments.contact_path,
            color=arguments.color,
            clear_screen=arguments.clear_screen,
            banner=arguments.banner,
            intro_animation=arguments.intro_animation,
            langfuse_notice=arguments.langfuse_notice,
        )
        server = instance.server
        try:
            server.start_in_thread()
            if not server.wait_until_ready():
                raise RuntimeError("preview SSH listener did not become ready")
            print(
                "Provider-free SSH presentation preview; disposable test worlds only."
            )
            print(f"Temporary state: {directory}")
            print(
                f"Host key: {server.host_key_algorithm} {server.host_key_fingerprint}"
            )
            print(f"Password: {instance.password}")
            print(
                "Connect from a second local terminal:\n"
                f"ssh -tt -p {server.bound_port} -o UserKnownHostsFile=/dev/null "
                "-o StrictHostKeyChecking=ask visitor@127.0.0.1"
            )
            print(
                f"Presentation: color={arguments.color}, "
                f"clear_screen={arguments.clear_screen}, banner={arguments.banner}, "
                f"intro_animation={instance.settings.intro_animation}; "
                f"catalog={arguments.catalog}"
            )
            print(f"Startup notice SHA-256: {instance.content.snapshot().version}")
            print("Operator commands: lobby worlds list; sessions --live; shutdown")
            print(
                "Ctrl-C or EOF here also stops the preview and removes temporary state."
            )
            run_console(instance)
        finally:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
