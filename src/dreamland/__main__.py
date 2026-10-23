import argparse
import sys
from dataclasses import asdict, replace
from pathlib import Path

from dreamland.admin_api import DreamlandAdminAPIServer
from dreamland.admin_interface import DreamlandAdminService
from dreamland.agent import LangGraphCommandAgent
from dreamland.agent_tracing import (
    build_langfuse_callbacks,
    record_langfuse_session_start,
)
from dreamland.backend import AgentBackend
from dreamland.background_agents.enrichment.graph import EnrichmentAgent
from dreamland.background_agents.enrichment.service import EnrichmentRuntimeService
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.config import DreamlandSettings, load_settings
from dreamland.console import DreamlandConsole
from dreamland.console_runtime import LocalConsole
from dreamland.console_socket import ConsoleSocketServer
from dreamland.evaluation_runtime import EvaluationRuntimeFactory
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.profile import ServerProfile, server_profile
from dreamland.runtime_logging import (
    configure_console_logging,
    configure_enrichment_event_logging,
    configure_session_trace_logging,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.spiritguide import ModelSpiritguide
from dreamland.ssh_lobby import SSHLobby
from dreamland.ssh_server import (
    DreamlandSSHServer,
    SharedPasswordAuthenticator,
    SSHServerConfig,
)
from dreamland.state_profiles import StateProfileRuntime, StateProfileStore
from dreamland.structured_failures import StructuredFailureStore

# Lobby mode never uses the SSH transport username as an in-world identity;
# see decisions/0113. Fixing it closes the field as a wildcard for guessing
# and generic scanners. Direct mode keeps an operator-chosen username because
# it becomes the session persona there.
LOBBY_SSH_USERNAME = "dreamland"


def main() -> None:
    arguments = _parse_arguments()
    settings = load_settings()
    _compose_product_runtime_if_enabled(settings)
    ssh_authenticator = SharedPasswordAuthenticator(
        settings.ssh.password or "",
        required_username=(
            LOBBY_SSH_USERNAME if settings.ssh.entry_mode == "lobby" else None
        ),
        allow_simple_login=settings.ssh.entry_mode == "lobby",
        fast_password=(
            settings.ssh.fast_password if settings.ssh.entry_mode == "lobby" else None
        ),
    )
    _print_temporary_api_token(settings)
    logs = configure_console_logging(
        settings.logging,
        stream=arguments.daemon,
        log_capacity=settings.console.log_capacity,
    )
    trace_logs = configure_session_trace_logging(settings.console.log_capacity)
    enrichment_event_logs = configure_enrichment_event_logging(
        settings.logging,
        settings.enrichment.event_log_capacity,
    )
    state_profile_store = StateProfileStore()
    initial_state_profile = state_profile_store.load(settings.state_profile.profile)
    world_state = initial_state_profile.state.get("world_state")
    profile = server_profile(world_state if isinstance(world_state, dict) else None)
    config = _ssh_server_config(settings, profile)
    state_profile_runtime = StateProfileRuntime(
        store=state_profile_store,
        current_profile=initial_state_profile,
    )
    structured_failure_store = StructuredFailureStore()
    event_bus = SessionEventBus()
    session_registry = LiveSessionRegistry(event_bus=event_bus)
    spiritguide_model = settings.spiritguide.model_reference
    agent = _command_agent(settings)
    backend = AgentBackend(
        profile,
        agent=agent,
        state_profile_runtime=state_profile_runtime,
        structured_failure_store=structured_failure_store,
        event_bus=event_bus,
    )
    lobby = _compose_ssh_lobby(
        settings,
        state_profile_store,
        fast_backend=_compose_fast_lobby_backend(settings, backend),
    )
    spiritguide = ModelSpiritguide(
        settings.spiritguide,
        api_key=_api_key_for_provider(settings, spiritguide_model.provider),
        langfuse_enabled=settings.langfuse.enabled,
        langfuse_public_key=settings.langfuse.public_key,
        langfuse_secret_key=settings.langfuse.secret_key,
        langfuse_host=settings.langfuse.host,
    )
    enrichment_callbacks = build_langfuse_callbacks(
        enabled=settings.langfuse.enabled,
        public_key=settings.langfuse.public_key,
        secret_key=settings.langfuse.secret_key,
        host=settings.langfuse.host,
    )
    enrichment_agent = EnrichmentAgent.from_settings(
        settings.enrichment,
        api_key_resolver=lambda provider: _api_key_for_provider(settings, provider),
        lazy_models=True,
    )
    enrichment_service = EnrichmentRuntimeService(
        settings.enrichment,
        session_registry=session_registry,
        event_bus=event_bus,
        agent=enrichment_agent,
        callbacks=enrichment_callbacks,
    )
    server: DreamlandSSHServer
    api_server: DreamlandAdminAPIServer | None = None
    console_server: ConsoleSocketServer | None = None

    def request_shutdown() -> None:
        server.request_stop()
        if api_server is not None:
            api_server.request_stop()

    console = DreamlandConsole(
        settings.console,
        logs,
        request_shutdown,
        trace_logs=trace_logs,
        langfuse_settings=settings.langfuse,
        spiritguide=spiritguide,
        state_profile_runtime=state_profile_runtime,
        structured_failure_store=structured_failure_store,
        session_registry=session_registry,
        enrichment_control=enrichment_service,
        config_path=settings.config_path,
        lobby_catalog=lobby.catalog if lobby is not None else None,
        public_simulations=lobby.identities if lobby is not None else None,
    )
    server = DreamlandSSHServer(
        config=config,
        backend=backend,
        console=console,
        authenticator=ssh_authenticator,
        lobby=lobby,
    )
    if settings.api.enabled:

        def record_admin_session_start(session):
            record_langfuse_session_start(
                enabled=settings.langfuse.enabled,
                public_key=settings.langfuse.public_key,
                secret_key=settings.langfuse.secret_key,
                host=settings.langfuse.host,
                session=session,
            )

        api_server = DreamlandAdminAPIServer(
            settings=settings.api,
            service=DreamlandAdminService(
                logs=logs,
                trace_logs=trace_logs,
                enrichment_event_logs=enrichment_event_logs,
                session_registry=console.session_registry,
                state_profile_runtime=state_profile_runtime,
                introspection=console.introspection,
                spiritguide=spiritguide,
                request_shutdown=console.request_shutdown,
                shutdown_requested=lambda: console.shutdown_requested,
                spiritguide_available=True,
                shutdown_available=True,
                mcp_write_tools=settings.api.mcp_write_tools,
                effective_config=_redacted_effective_config(settings),
                command_backend=backend,
                record_session_start=record_admin_session_start,
                structured_failure_store=structured_failure_store,
                evaluation_factory=EvaluationRuntimeFactory(
                    settings,
                    profile=profile,
                    state_profile_runtime=state_profile_runtime,
                    session_registry=session_registry,
                    structured_failure_store=structured_failure_store,
                    api_key_resolver=lambda provider: _api_key_for_provider(
                        settings, provider
                    ),
                ),
            ),
        )
    enrichment_service.start()
    try:
        console_path = getattr(arguments, "console_socket", None)
        if console_path is not None:
            console_server = ConsoleSocketServer(console, console_path)
            console_server.start()
        if api_server is not None:
            api_server.start_in_thread()
            if not api_server.wait_until_ready():
                raise RuntimeError("Dreamland Admin API server did not start.")
        if arguments.daemon:
            server.serve_forever()
            return
        server.start_in_thread()
        try:
            if not server.wait_until_ready():
                raise RuntimeError("Dreamland SSH server did not start.")
            LocalConsole(console).run()
        finally:
            server.stop()
    finally:
        if console_server is not None:
            console_server.stop()
        if arguments.daemon:
            server.stop()
        if api_server is not None:
            api_server.stop()
        for registration in session_registry.list_sessions():
            if registration.runtime is not None:
                session_registry.unregister(
                    registration.session_id, session=registration.session
                )
        enrichment_service.stop()


def _command_agent(settings: DreamlandSettings) -> LangGraphCommandAgent:
    role = settings.agent
    arguments = asdict(role)
    arguments.pop("model")
    return LangGraphCommandAgent(
        provider=role.model_provider,
        model_name=role.model_name,
        api_key=_api_key_for_provider(settings, role.model_provider),
        **arguments,
        langfuse_enabled=settings.langfuse.enabled,
        langfuse_public_key=settings.langfuse.public_key,
        langfuse_secret_key=settings.langfuse.secret_key,
        langfuse_host=settings.langfuse.host,
    )


def _compose_fast_lobby_backend(
    settings: DreamlandSettings, backend: AgentBackend
) -> AgentBackend | None:
    if settings.ssh.entry_mode != "lobby" or not settings.ssh.fast_password:
        return None
    fast_settings = replace(
        settings, agent=replace(settings.agent, openai_service_tier="ultrafast")
    )
    return AgentBackend(
        backend.profile,
        agent=_command_agent(fast_settings),
        state_profile_runtime=backend.state_profile_runtime,
        structured_failure_store=backend.structured_failure_store,
        structured_failure_capture_enabled=backend.structured_failure_capture_enabled,
        event_bus=backend.event_bus,
    )


def _compose_ssh_lobby(
    settings: DreamlandSettings,
    store: StateProfileStore,
    *,
    fast_backend: AgentBackend | None = None,
) -> SSHLobby | None:
    if settings.ssh.entry_mode == "direct":
        return None
    roles = [settings.agent.model_reference, settings.spiritguide.model_reference]
    if settings.enrichment.enabled:
        roles.extend(
            [
                settings.enrichment.coordinator.model_reference,
                settings.enrichment.ambient_coordinator.model_reference,
                settings.enrichment.expert.model_reference,
                settings.enrichment.integrator.model_reference,
            ]
        )
    if any(role.provider != "openai" for role in roles):
        raise ValueError(
            "The SSH lobby notice covers OpenAI processing. Configure the command, "
            "Spiritguide, and enabled enrichment roles for OpenAI before enabling lobby mode."
        )
    catalog = LobbyCatalog(store, settings.lobby.catalog_path)
    content = LobbyContent(
        welcome_path=settings.lobby.welcome_path,
        guidelines_path=settings.lobby.guidelines_path,
        contact_path=settings.lobby.contact_path,
        langfuse_enabled=settings.langfuse.enabled,
    )
    content.snapshot()
    identities = PublicSimulationRegistry(
        settings.lobby.public_registry_path,
        retention_days=settings.lobby.retention_days,
    )
    return SSHLobby(
        catalog, identities, content, settings.lobby, fast_backend=fast_backend
    )


def _compose_product_runtime_if_enabled(settings: DreamlandSettings) -> None:
    if not settings.product_api.enabled:
        return

    from dreamland.product.composition import compose_product_runtime

    compose_product_runtime()


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dreamland",
        description="Run the Dreamland SSH server.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="run without the Dreamland console and stream logs",
    )
    parser.add_argument(
        "--console-socket",
        type=Path,
        help="serve the private operator console on this Unix socket (daemon only)",
    )
    arguments = parser.parse_args()
    if arguments.console_socket is not None and not arguments.daemon:
        parser.error("--console-socket requires --daemon")
    return arguments


def _print_temporary_api_token(settings: DreamlandSettings) -> None:
    if not settings.api.enabled or not settings.api.temporary_token:
        return
    print(
        "Dreamland API temporary bearer token generated for this process:",
        file=sys.stderr,
    )
    print(settings.api.token, file=sys.stderr)
    print(
        "Set [api] token or DREAMLAND_API_TOKEN to use a stable token.",
        file=sys.stderr,
    )


def _ssh_server_config(
    settings: DreamlandSettings,
    profile: ServerProfile,
) -> SSHServerConfig:
    return SSHServerConfig(
        host=settings.ssh.host,
        port=settings.ssh.port,
        profile=profile,
        host_key_path=settings.ssh.host_key_path,
        entry_mode=settings.ssh.entry_mode,
    )


def _redacted_effective_config(settings: DreamlandSettings) -> dict[str, object]:
    value = _json_safe_settings(asdict(settings))
    if not isinstance(value, dict):
        return {}
    openai = value.get("openai")
    if isinstance(openai, dict):
        openai["api_key_configured"] = bool(openai.pop("api_key", None))
    xai = value.get("xai")
    if isinstance(xai, dict):
        xai["api_key_configured"] = bool(xai.pop("api_key", None))
    anthropic = value.get("anthropic")
    if isinstance(anthropic, dict):
        anthropic["api_key_configured"] = bool(anthropic.pop("api_key", None))
    langfuse = value.get("langfuse")
    if isinstance(langfuse, dict):
        langfuse["public_key_configured"] = bool(langfuse.pop("public_key", None))
        langfuse["secret_key_configured"] = bool(langfuse.pop("secret_key", None))
    ssh = value.get("ssh")
    if isinstance(ssh, dict):
        ssh["password_configured"] = bool(ssh.pop("password", None))
        ssh["fast_password_configured"] = bool(ssh.pop("fast_password", None))
    api = value.get("api")
    if isinstance(api, dict):
        api["token_configured"] = bool(api.pop("token", None))
    return value


def _api_key_for_provider(settings: DreamlandSettings, provider: str) -> str | None:
    if provider == "anthropic":
        return settings.anthropic.api_key
    if provider == "openai":
        return settings.openai.api_key
    if provider == "xai":
        return settings.xai.api_key
    return None


def _json_safe_settings(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): _json_safe_settings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_settings(item) for item in value]
    return value


if __name__ == "__main__":
    main()
