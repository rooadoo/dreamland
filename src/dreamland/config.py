from __future__ import annotations

import os
import secrets
from configparser import ConfigParser
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path

from dreamland.model_defaults import (
    AGENT_MODEL_DEFAULTS,
    ENRICHMENT_EXPERT_MODEL_DEFAULTS,
    ENRICHMENT_INTEGRATOR_MODEL_DEFAULTS,
    ENRICHMENT_MODEL_DEFAULTS,
    ROLE_MODEL_DEFAULTS,
    SPIRITGUIDE_MODEL_DEFAULTS,
    RoleModelDefaults,
)
from dreamland.model_providers import (
    OPENAI_API_VALUES,
    OPENAI_REASONING_MODE_VALUES,
    OPENAI_SERVICE_TIER_VALUES,
    ChatModelProviderConfig,
    ModelReference,
    parse_model_reference,
    validate_model_controls,
)
from dreamland.ssh_host_keys import default_ssh_host_key_path
from dreamland.state_profiles import DEFAULT_STATE_PROFILE_NAME

DEFAULT_CONFIG_PATH = Path("dreamland.ini")
DEFAULT_AGENT_MODEL = AGENT_MODEL_DEFAULTS.model
DEFAULT_AGENT_REASONING_EFFORT = AGENT_MODEL_DEFAULTS.reasoning_effort
DEFAULT_SPIRITGUIDE_MODEL = SPIRITGUIDE_MODEL_DEFAULTS.model
DEFAULT_ENRICHMENT_MODEL = ENRICHMENT_MODEL_DEFAULTS.model
DEFAULT_REASONING_SUMMARY = AGENT_MODEL_DEFAULTS.reasoning_summary
DEFAULT_SYSTEM_PROMPT_NAME = "system.md"
DEFAULT_MAX_OUTPUT_TOKENS = AGENT_MODEL_DEFAULTS.max_output_tokens
DEFAULT_SPIRITGUIDE_MAX_OUTPUT_TOKENS = SPIRITGUIDE_MODEL_DEFAULTS.max_output_tokens
DEFAULT_ENRICHMENT_COORDINATOR_MAX_OUTPUT_TOKENS = (
    ENRICHMENT_MODEL_DEFAULTS.max_output_tokens
)
DEFAULT_ENRICHMENT_AMBIENT_COORDINATOR_MAX_OUTPUT_TOKENS = (
    ENRICHMENT_MODEL_DEFAULTS.max_output_tokens
)
DEFAULT_ENRICHMENT_EXPERT_MAX_OUTPUT_TOKENS = (
    ENRICHMENT_EXPERT_MODEL_DEFAULTS.max_output_tokens
)
DEFAULT_ENRICHMENT_INTEGRATOR_MAX_OUTPUT_TOKENS = (
    ENRICHMENT_INTEGRATOR_MODEL_DEFAULTS.max_output_tokens
)
DEFAULT_ENRICHMENT_IDLE_DELAY_MS = 5000
DEFAULT_ENRICHMENT_MAX_INFLIGHT_PER_SESSION = 3
DEFAULT_ENRICHMENT_EVENT_LOG_CAPACITY = 10_000
REASONING_EFFORT_VALUES = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
REASONING_SUMMARY_VALUES = frozenset({"auto", "concise", "detailed"})
VERBOSITY_VALUES = frozenset({"low", "medium", "high"})
SPEED_VALUES = frozenset({"fast"})
PROMPT_CACHE_VALUES = frozenset({"automatic", "off"})
LOG_LEVEL_VALUES = frozenset({"debug", "info", "warning", "error", "critical"})
LOG_EVENT_VALUES = frozenset(
    {
        "lifecycle",
        "commands",
        "replies",
        "tokens",
        "console",
        "failures",
        "mcp",
        "enrichment",
    }
)
ENRICHMENT_APPLY_MODE_VALUES = frozenset({"shadow", "safe"})
ENRICHMENT_TRIGGER_VALUES = frozenset(
    {
        "host_discovered",
        "active_host_changed",
        "host_presence_bootstrap",
        "presence_probe",
        "subject_first_touch",
        "state_materialized",
        "materialization_followthrough",
        "surface_focus",
        "interaction_pulse",
        "session_idle",
    }
)
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765


@dataclass(frozen=True, slots=True)
class OpenAISettings:
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class XAISettings:
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class AnthropicSettings:
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class LangfuseSettings:
    enabled: bool = False
    public_key: str | None = None
    secret_key: str | None = None
    host: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSettings:
    model: str = DEFAULT_AGENT_MODEL
    system_prompt_name: str = DEFAULT_SYSTEM_PROMPT_NAME
    reasoning_effort: str | None = DEFAULT_AGENT_REASONING_EFFORT
    reasoning_summary: str | None = DEFAULT_REASONING_SUMMARY
    verbosity: str | None = AGENT_MODEL_DEFAULTS.verbosity
    speed: str | None = AGENT_MODEL_DEFAULTS.speed
    prompt_cache: str | None = AGENT_MODEL_DEFAULTS.prompt_cache
    max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS
    openai_api: str | None = AGENT_MODEL_DEFAULTS.openai_api
    openai_service_tier: str | None = AGENT_MODEL_DEFAULTS.openai_service_tier
    openai_reasoning_mode: str | None = AGENT_MODEL_DEFAULTS.openai_reasoning_mode

    @property
    def model_reference(self) -> ModelReference:
        return parse_model_reference(self.model, name="agent model")

    @property
    def model_name(self) -> str:
        return self.model_reference.model

    @property
    def model_provider(self) -> str:
        return self.model_reference.provider


@dataclass(frozen=True, slots=True)
class SpiritguideSettings:
    model: str = DEFAULT_SPIRITGUIDE_MODEL
    system_prompt_name: str = "spiritguide.md"
    reasoning_effort: str | None = SPIRITGUIDE_MODEL_DEFAULTS.reasoning_effort
    reasoning_summary: str | None = SPIRITGUIDE_MODEL_DEFAULTS.reasoning_summary
    verbosity: str | None = SPIRITGUIDE_MODEL_DEFAULTS.verbosity
    speed: str | None = SPIRITGUIDE_MODEL_DEFAULTS.speed
    prompt_cache: str | None = SPIRITGUIDE_MODEL_DEFAULTS.prompt_cache
    max_output_tokens: int | None = DEFAULT_SPIRITGUIDE_MAX_OUTPUT_TOKENS
    openai_api: str | None = SPIRITGUIDE_MODEL_DEFAULTS.openai_api
    openai_service_tier: str | None = SPIRITGUIDE_MODEL_DEFAULTS.openai_service_tier
    openai_reasoning_mode: str | None = SPIRITGUIDE_MODEL_DEFAULTS.openai_reasoning_mode

    @property
    def model_reference(self) -> ModelReference:
        return parse_model_reference(self.model, name="spiritguide model")

    @property
    def model_name(self) -> str:
        return self.model_reference.model

    @property
    def model_provider(self) -> str:
        return self.model_reference.provider


@dataclass(frozen=True, slots=True)
class EnrichmentRoleSettings:
    model: str = DEFAULT_ENRICHMENT_MODEL
    system_prompt_name: str = "enrichment.md"
    reasoning_effort: str | None = ENRICHMENT_MODEL_DEFAULTS.reasoning_effort
    reasoning_summary: str | None = ENRICHMENT_MODEL_DEFAULTS.reasoning_summary
    verbosity: str | None = ENRICHMENT_MODEL_DEFAULTS.verbosity
    speed: str | None = ENRICHMENT_MODEL_DEFAULTS.speed
    prompt_cache: str | None = ENRICHMENT_MODEL_DEFAULTS.prompt_cache
    max_output_tokens: int | None = ENRICHMENT_MODEL_DEFAULTS.max_output_tokens
    openai_api: str | None = ENRICHMENT_MODEL_DEFAULTS.openai_api
    openai_service_tier: str | None = ENRICHMENT_MODEL_DEFAULTS.openai_service_tier
    openai_reasoning_mode: str | None = ENRICHMENT_MODEL_DEFAULTS.openai_reasoning_mode

    @property
    def model_reference(self) -> ModelReference:
        return parse_model_reference(self.model, name="enrichment model")

    @property
    def model_name(self) -> str:
        return self.model_reference.model

    @property
    def model_provider(self) -> str:
        return self.model_reference.provider


def _default_enrichment_role(role: str) -> EnrichmentRoleSettings:
    defaults = ROLE_MODEL_DEFAULTS[f"enrichment.{role}"]
    return EnrichmentRoleSettings(
        model=defaults.model,
        system_prompt_name=f"enrichment_{role}.md",
        reasoning_effort=defaults.reasoning_effort,
        reasoning_summary=defaults.reasoning_summary,
        verbosity=defaults.verbosity,
        speed=defaults.speed,
        prompt_cache=defaults.prompt_cache,
        max_output_tokens=defaults.max_output_tokens,
        openai_api=defaults.openai_api,
        openai_service_tier=defaults.openai_service_tier,
        openai_reasoning_mode=defaults.openai_reasoning_mode,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentSettings:
    enabled: bool = True
    apply_mode: str = "safe"
    triggers: frozenset[str] = ENRICHMENT_TRIGGER_VALUES
    idle_delay_ms: int = DEFAULT_ENRICHMENT_IDLE_DELAY_MS
    cooldown_ms: int = 30000
    max_jobs_per_minute: int = 6
    max_inflight_per_session: int = DEFAULT_ENRICHMENT_MAX_INFLIGHT_PER_SESSION
    event_log_capacity: int = DEFAULT_ENRICHMENT_EVENT_LOG_CAPACITY
    coordinator: EnrichmentRoleSettings = field(
        default_factory=lambda: _default_enrichment_role("coordinator")
    )
    ambient_coordinator: EnrichmentRoleSettings = field(
        default_factory=lambda: _default_enrichment_role("ambient_coordinator")
    )
    expert: EnrichmentRoleSettings = field(
        default_factory=lambda: _default_enrichment_role("expert")
    )
    integrator: EnrichmentRoleSettings = field(
        default_factory=lambda: _default_enrichment_role("integrator")
    )


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    enabled: bool = True
    level: str = "INFO"
    events: frozenset[str] = LOG_EVENT_VALUES
    reply_excerpt_chars: int = 160


@dataclass(frozen=True, slots=True)
class SSHSettings:
    host: str = "127.0.0.1"
    port: int = 2222
    host_key_path: Path = field(default_factory=default_ssh_host_key_path)
    password: str | None = field(default=None, repr=False)
    fast_password: str | None = field(default=None, repr=False)
    entry_mode: str = "direct"


@dataclass(frozen=True, slots=True)
class LobbySettings:
    catalog_path: Path = Path("lobby/catalog.json")
    public_registry_path: Path = Path("lobby/public_visits.sqlite3")
    welcome_path: Path | None = None
    guidelines_path: Path | None = None
    contact_path: Path | None = None
    idle_timeout_seconds: int = 300
    max_input_bytes: int = 256
    max_invalid_attempts: int = 20
    retention_days: int = 30
    color: str = "auto"
    clear_screen: str = "auto"
    banner: str = "auto"
    intro_animation: str = "auto"


@dataclass(frozen=True, slots=True)
class ConsoleSettings:
    log_capacity: int = 1000


@dataclass(frozen=True, slots=True)
class ApiSettings:
    enabled: bool = False
    host: str = DEFAULT_API_HOST
    port: int = DEFAULT_API_PORT
    token: str | None = None
    temporary_token: bool = False
    mcp_write_tools: bool = False

    @property
    def mcp_enabled(self) -> bool:
        return self.enabled


@dataclass(frozen=True, slots=True)
class ProductApiSettings:
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class StateProfileSettings:
    profile: str = DEFAULT_STATE_PROFILE_NAME


@dataclass(frozen=True, slots=True)
class DreamlandSettings:
    config_path: Path
    openai: OpenAISettings
    xai: XAISettings
    anthropic: AnthropicSettings
    langfuse: LangfuseSettings
    agent: AgentSettings
    spiritguide: SpiritguideSettings
    enrichment: EnrichmentSettings
    logging: LoggingSettings
    ssh: SSHSettings
    console: ConsoleSettings
    api: ApiSettings
    state_profile: StateProfileSettings
    product_api: ProductApiSettings = field(default_factory=ProductApiSettings)
    lobby: LobbySettings = field(default_factory=LobbySettings)


def load_settings(path: Path | str | None = None) -> DreamlandSettings:
    configured_path = path or os.environ.get("DREAMLAND_CONFIG") or DEFAULT_CONFIG_PATH
    config_path = Path(configured_path)
    parser = ConfigParser()
    parser.read(config_path)
    ssh_port = os.environ.get("DREAMLAND_SSH_PORT")
    if ssh_port is None:
        ssh_port = parser.get("ssh", "port", fallback="2222") or "2222"

    return DreamlandSettings(
        config_path=config_path,
        openai=OpenAISettings(
            api_key=_optional_value(os.environ.get("OPENAI_API_KEY"))
        ),
        xai=XAISettings(api_key=_optional_value(os.environ.get("XAI_API_KEY"))),
        anthropic=AnthropicSettings(
            api_key=_optional_value(os.environ.get("ANTHROPIC_API_KEY"))
        ),
        langfuse=LangfuseSettings(
            enabled=_bool_value(
                os.environ.get("LANGFUSE_ENABLED")
                or parser.get("langfuse", "enabled", fallback=None),
                default=False,
            ),
            public_key=_optional_value(
                os.environ.get("LANGFUSE_PUBLIC_KEY")
                or parser.get("langfuse", "public_key", fallback=None)
            ),
            secret_key=_optional_value(
                os.environ.get("LANGFUSE_SECRET_KEY")
                or parser.get("langfuse", "secret_key", fallback=None)
            ),
            host=_optional_value(
                os.environ.get("LANGFUSE_HOST")
                or parser.get("langfuse", "host", fallback=None)
            ),
        ),
        agent=_agent_settings(
            parser,
            section="agent",
            env_prefix="DREAMLAND",
            default_prompt=DEFAULT_SYSTEM_PROMPT_NAME,
        ),
        spiritguide=_spiritguide_settings(parser),
        enrichment=_enrichment_settings(parser),
        logging=LoggingSettings(
            enabled=_bool_value(
                os.environ.get("DREAMLAND_LOG_ENABLED")
                or parser.get("logging", "enabled", fallback=None),
                default=True,
            ),
            level=(
                _choice_value(
                    os.environ.get("DREAMLAND_LOG_LEVEL")
                    or parser.get("logging", "level", fallback="INFO"),
                    name="logging level",
                    allowed=LOG_LEVEL_VALUES,
                )
                or "INFO"
            ).upper(),
            events=_event_values(
                os.environ.get("DREAMLAND_LOG_EVENTS")
                or parser.get(
                    "logging",
                    "events",
                    fallback=",".join(sorted(LOG_EVENT_VALUES)),
                )
            ),
            reply_excerpt_chars=_nonnegative_int(
                (
                    os.environ.get("DREAMLAND_REPLY_EXCERPT_CHARS")
                    or parser.get("logging", "reply_excerpt_chars", fallback="160")
                    or "160"
                ),
                name="reply_excerpt_chars",
            ),
        ),
        ssh=SSHSettings(
            host=str(
                os.environ.get("DREAMLAND_SSH_HOST")
                or parser.get("ssh", "host", fallback="127.0.0.1")
            ),
            port=int(ssh_port),
            host_key_path=_ssh_host_key_path(parser, config_path),
            password=os.environ.get("DREAMLAND_SSH_PASSWORD") or None,
            fast_password=os.environ.get("DREAMLAND_SSH_FAST_PASSWORD") or None,
            entry_mode=_ssh_entry_mode(parser),
        ),
        lobby=_lobby_settings(parser, config_path),
        console=_console_settings(parser),
        api=_api_settings(parser),
        product_api=_product_api_settings(parser),
        state_profile=StateProfileSettings(
            profile=(
                _optional_value(
                    os.environ.get("DREAMLAND_STATE_PROFILE")
                    or parser.get("state_profile", "profile", fallback=None)
                )
                or DEFAULT_STATE_PROFILE_NAME
            )
        ),
    )


def _ssh_entry_mode(parser: ConfigParser) -> str:
    value = (
        os.environ.get(
            "DREAMLAND_SSH_ENTRY_MODE",
            parser.get("ssh", "entry_mode", fallback="direct"),
        )
        .strip()
        .lower()
    )
    if value not in {"direct", "lobby"}:
        raise ValueError("ssh entry_mode must be direct or lobby")
    return value


def _lobby_settings(parser: ConfigParser, config_path: Path) -> LobbySettings:
    def path_value(name: str, default: str | None = None) -> Path | None:
        environment_name = f"DREAMLAND_LOBBY_{name.upper()}"
        if environment_name in os.environ:
            value = os.environ[environment_name].strip()
            if not value:
                raise ValueError(f"{environment_name} must not be blank")
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{environment_name} must be an absolute path")
            return path
        configured = (
            parser.get("lobby", name) if parser.has_option("lobby", name) else default
        )
        if configured is None:
            return None
        value = configured
        if not value.strip():
            raise ValueError(f"lobby {name} must not be blank")
        path = Path(value.strip()).expanduser()
        return (
            path
            if path.is_absolute()
            else config_path.expanduser().absolute().parent / path
        )

    def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
        value = os.environ.get(
            f"DREAMLAND_LOBBY_{name.upper()}",
            parser.get("lobby", name, fallback=str(default)),
        )
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"lobby {name} must be an integer") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"lobby {name} must be between {minimum} and {maximum}")
        return number

    def presentation_mode(
        name: str,
        choices: tuple[str, ...] = ("auto", "off"),
        *,
        default: str = "auto",
    ) -> str:
        value = (
            os.environ.get(
                f"DREAMLAND_LOBBY_{name.upper()}",
                parser.get("lobby", name, fallback=default),
            )
            .strip()
            .lower()
        )
        if value not in choices:
            raise ValueError(f"lobby {name} must be {' or '.join(choices)}")
        return value

    catalog = path_value("catalog_path", "lobby/catalog.json")
    registry = path_value("public_registry_path", "lobby/public_visits.sqlite3")
    assert catalog is not None and registry is not None
    return LobbySettings(
        catalog_path=catalog,
        public_registry_path=registry,
        welcome_path=path_value("welcome_path"),
        guidelines_path=path_value("guidelines_path"),
        contact_path=path_value("contact_path"),
        idle_timeout_seconds=bounded("idle_timeout_seconds", 300, 1, 86400),
        max_input_bytes=bounded("max_input_bytes", 256, 16, 4096),
        max_invalid_attempts=bounded("max_invalid_attempts", 20, 1, 1000),
        retention_days=bounded("retention_days", 30, 1, 3650),
        color=presentation_mode("color"),
        clear_screen=presentation_mode("clear_screen"),
        banner=presentation_mode("banner", ("auto", "simple")),
        intro_animation=presentation_mode("intro_animation"),
    )


def _ssh_host_key_path(parser: ConfigParser, config_path: Path) -> Path:
    environment_name = "DREAMLAND_SSH_HOST_KEY_PATH"
    if environment_name in os.environ:
        environment_value = os.environ[environment_name].strip()
        if not environment_value:
            raise ValueError(f"{environment_name} must not be blank")
        path = Path(environment_value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{environment_name} must be an absolute path")
        return path

    configured_value = (
        parser.get("ssh", "host_key_path")
        if parser.has_option("ssh", "host_key_path")
        else None
    )
    if configured_value is None:
        return default_ssh_host_key_path()
    configured_value = configured_value.strip()
    if not configured_value:
        raise ValueError("ssh host_key_path must not be blank")
    path = Path(configured_value).expanduser()
    if path.is_absolute():
        return path
    config_directory = config_path.expanduser().absolute().parent
    return config_directory / path


def persist_state_profile_setting(
    path: Path | str,
    profile_name: str | None,
) -> None:
    config_path = Path(path)
    parser = ConfigParser()
    parser.read(config_path)
    if not parser.has_section("state_profile"):
        parser.add_section("state_profile")
    parser.set("state_profile", "profile", profile_name or "")
    with config_path.open("w", encoding="utf-8") as file:
        parser.write(file)


def _agent_settings(
    parser: ConfigParser,
    *,
    section: str,
    env_prefix: str,
    default_prompt: str,
    defaults: RoleModelDefaults = AGENT_MODEL_DEFAULTS,
) -> AgentSettings:
    model = str(
        os.environ.get(f"{env_prefix}_MODEL")
        or parser.get(section, "model", fallback=defaults.model)
    )
    reference = parse_model_reference(model, name=f"{section} model")

    def control_value(name: str, fallback: str | None) -> str | None:
        # An explicit blank resets a control to the provider default, including
        # environment overrides of an INI value or a newly introduced default.
        return os.environ.get(
            f"{env_prefix}_{name.upper()}",
            parser.get(section, name, fallback=fallback),
        )

    default_reasoning_effort = defaults.reasoning_effort
    if section == "spiritguide" and reference.provider != "openai":
        # Preserve the provider-default effort for explicitly selected xAI or
        # Anthropic Spiritguide models. The tested high effort is for OpenAI.
        default_reasoning_effort = None
    openai_controls = {
        control: _choice_value(
            control_value(
                control,
                getattr(defaults, control) if reference.provider == "openai" else None,
            ),
            name=f"{section} {control}",
            allowed=allowed,
        )
        for control, allowed in (
            ("openai_api", OPENAI_API_VALUES),
            ("openai_service_tier", OPENAI_SERVICE_TIER_VALUES),
            ("openai_reasoning_mode", OPENAI_REASONING_MODE_VALUES),
        )
    }
    settings = AgentSettings(
        model=model,
        system_prompt_name=str(
            os.environ.get(f"{env_prefix}_SYSTEM_PROMPT")
            or parser.get(section, "system_prompt", fallback=default_prompt)
        ),
        reasoning_effort=_choice_value(
            control_value("reasoning_effort", default_reasoning_effort),
            name=f"{section} reasoning_effort",
            allowed=REASONING_EFFORT_VALUES,
        ),
        reasoning_summary=_choice_value(
            control_value("reasoning_summary", defaults.reasoning_summary),
            name=f"{section} reasoning_summary",
            allowed=REASONING_SUMMARY_VALUES,
        ),
        verbosity=_choice_value(
            control_value("verbosity", defaults.verbosity),
            name=f"{section} verbosity",
            allowed=VERBOSITY_VALUES,
        ),
        speed=_choice_value(
            control_value("speed", defaults.speed),
            name=f"{section} speed",
            allowed=SPEED_VALUES,
        ),
        prompt_cache=_choice_value(
            control_value("prompt_cache", defaults.prompt_cache),
            name=f"{section} prompt_cache",
            allowed=PROMPT_CACHE_VALUES,
        ),
        max_output_tokens=_optional_nonnegative_int(
            control_value("max_output_tokens", str(defaults.max_output_tokens)),
            name=f"{section} max_output_tokens",
        ),
        openai_api=openai_controls["openai_api"],
        openai_service_tier=openai_controls["openai_service_tier"],
        openai_reasoning_mode=openai_controls["openai_reasoning_mode"],
    )
    validate_model_controls(
        ChatModelProviderConfig(
            provider=reference.provider,
            model=reference.model,
            reasoning_effort=settings.reasoning_effort,
            reasoning_summary=settings.reasoning_summary,
            openai_api=openai_controls["openai_api"],
            openai_service_tier=openai_controls["openai_service_tier"],
            openai_reasoning_mode=openai_controls["openai_reasoning_mode"],
        )
    )
    return settings


def _spiritguide_settings(parser: ConfigParser) -> SpiritguideSettings:
    settings = _agent_settings(
        parser,
        section="spiritguide",
        env_prefix="DREAMLAND_SPIRITGUIDE",
        default_prompt="spiritguide.md",
        defaults=SPIRITGUIDE_MODEL_DEFAULTS,
    )
    return SpiritguideSettings(
        model=settings.model,
        system_prompt_name=settings.system_prompt_name,
        reasoning_effort=settings.reasoning_effort,
        reasoning_summary=settings.reasoning_summary,
        verbosity=settings.verbosity,
        speed=settings.speed,
        prompt_cache=settings.prompt_cache,
        max_output_tokens=settings.max_output_tokens,
        openai_api=settings.openai_api,
        openai_service_tier=settings.openai_service_tier,
        openai_reasoning_mode=settings.openai_reasoning_mode,
    )


def _enrichment_settings(parser: ConfigParser) -> EnrichmentSettings:
    return EnrichmentSettings(
        enabled=_bool_value(
            os.environ.get("DREAMLAND_ENRICHMENT_ENABLED")
            or parser.get("enrichment", "enabled", fallback=None),
            default=True,
        ),
        apply_mode=(
            _choice_value(
                os.environ.get("DREAMLAND_ENRICHMENT_APPLY_MODE")
                or parser.get("enrichment", "apply_mode", fallback="safe"),
                name="enrichment apply_mode",
                allowed=ENRICHMENT_APPLY_MODE_VALUES,
            )
            or "safe"
        ),
        triggers=_enrichment_trigger_values(
            os.environ.get("DREAMLAND_ENRICHMENT_TRIGGERS")
            or parser.get(
                "enrichment",
                "triggers",
                fallback=",".join(sorted(ENRICHMENT_TRIGGER_VALUES)),
            )
        ),
        idle_delay_ms=_nonnegative_int(
            os.environ.get("DREAMLAND_ENRICHMENT_IDLE_DELAY_MS")
            or parser.get(
                "enrichment",
                "idle_delay_ms",
                fallback=str(DEFAULT_ENRICHMENT_IDLE_DELAY_MS),
            )
            or str(DEFAULT_ENRICHMENT_IDLE_DELAY_MS),
            name="enrichment idle_delay_ms",
        ),
        cooldown_ms=_nonnegative_int(
            os.environ.get("DREAMLAND_ENRICHMENT_COOLDOWN_MS")
            or parser.get("enrichment", "cooldown_ms", fallback="30000")
            or "30000",
            name="enrichment cooldown_ms",
        ),
        max_jobs_per_minute=_positive_int(
            os.environ.get("DREAMLAND_ENRICHMENT_MAX_JOBS_PER_MINUTE")
            or parser.get(
                "enrichment",
                "max_jobs_per_minute",
                fallback="6",
            )
            or "6",
            name="enrichment max_jobs_per_minute",
        ),
        max_inflight_per_session=_positive_int(
            os.environ.get("DREAMLAND_ENRICHMENT_MAX_INFLIGHT_PER_SESSION")
            or parser.get(
                "enrichment",
                "max_inflight_per_session",
                fallback=str(DEFAULT_ENRICHMENT_MAX_INFLIGHT_PER_SESSION),
            )
            or str(DEFAULT_ENRICHMENT_MAX_INFLIGHT_PER_SESSION),
            name="enrichment max_inflight_per_session",
        ),
        event_log_capacity=_positive_int(
            os.environ.get("DREAMLAND_ENRICHMENT_EVENT_LOG_CAPACITY")
            or parser.get(
                "enrichment",
                "event_log_capacity",
                fallback=str(DEFAULT_ENRICHMENT_EVENT_LOG_CAPACITY),
            )
            or str(DEFAULT_ENRICHMENT_EVENT_LOG_CAPACITY),
            name="enrichment event_log_capacity",
        ),
        coordinator=_enrichment_role_settings(
            parser,
            role="coordinator",
        ),
        ambient_coordinator=_enrichment_role_settings(
            parser,
            role="ambient_coordinator",
        ),
        expert=_enrichment_role_settings(
            parser,
            role="expert",
        ),
        integrator=_enrichment_role_settings(
            parser,
            role="integrator",
        ),
    )


def _enrichment_role_settings(
    parser: ConfigParser,
    *,
    role: str,
) -> EnrichmentRoleSettings:
    section = f"enrichment.{role}"
    settings = _agent_settings(
        parser,
        section=section,
        env_prefix=f"DREAMLAND_ENRICHMENT_{role.upper()}",
        default_prompt=f"enrichment_{role}.md",
        defaults=ROLE_MODEL_DEFAULTS[section],
    )
    return EnrichmentRoleSettings(
        model=settings.model,
        system_prompt_name=settings.system_prompt_name,
        reasoning_effort=settings.reasoning_effort,
        reasoning_summary=settings.reasoning_summary,
        verbosity=settings.verbosity,
        speed=settings.speed,
        prompt_cache=settings.prompt_cache,
        max_output_tokens=settings.max_output_tokens,
        openai_api=settings.openai_api,
        openai_service_tier=settings.openai_service_tier,
        openai_reasoning_mode=settings.openai_reasoning_mode,
    )


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned == "replace-me":
        return None
    return cleaned


def _bool_value(value: str | None, *, default: bool) -> bool:
    cleaned = _optional_value(value)
    if cleaned is None:
        return default
    if cleaned.lower() in {"1", "true", "yes", "on"}:
        return True
    if cleaned.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got {value!r}.")


def _choice_value(
    value: str | None, *, name: str, allowed: frozenset[str]
) -> str | None:
    cleaned = _optional_value(value)
    if cleaned is None:
        return None
    normalized = cleaned.lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Expected {name} to be one of {choices}, got {value!r}.")
    return normalized


def _optional_nonnegative_int(value: str | None, *, name: str) -> int | None:
    cleaned = _optional_value(value)
    if cleaned is None:
        return None
    parsed = _nonnegative_int(cleaned, name=name)
    return parsed or None


def _nonnegative_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Expected {name} to be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError(f"Expected {name} to be a non-negative integer.")
    return parsed


def _positive_int(value: str, *, name: str) -> int:
    parsed = _nonnegative_int(value, name=name)
    if parsed == 0:
        raise ValueError(f"Expected {name} to be greater than zero.")
    return parsed


def _console_settings(parser: ConfigParser) -> ConsoleSettings:
    return ConsoleSettings(
        log_capacity=_positive_int(
            os.environ.get("DREAMLAND_CONSOLE_LOG_CAPACITY")
            or parser.get("console", "log_capacity", fallback="1000")
            or "1000",
            name="console log_capacity",
        ),
    )


def _api_settings(parser: ConfigParser) -> ApiSettings:
    enabled = _bool_value(
        os.environ.get("DREAMLAND_API_ENABLED")
        or parser.get("api", "enabled", fallback=None),
        default=False,
    )
    host = str(
        os.environ.get("DREAMLAND_API_HOST")
        or parser.get("api", "host", fallback=DEFAULT_API_HOST)
        or DEFAULT_API_HOST
    )
    port = _positive_int(
        os.environ.get("DREAMLAND_API_PORT")
        or parser.get("api", "port", fallback=str(DEFAULT_API_PORT))
        or str(DEFAULT_API_PORT),
        name="api port",
    )
    token = _optional_value(
        os.environ.get("DREAMLAND_API_TOKEN")
        or parser.get("api", "token", fallback=None)
    )
    mcp_write_tools = _bool_value(
        os.environ.get("DREAMLAND_API_MCP_WRITE_TOOLS")
        or parser.get("api", "mcp_write_tools", fallback=None),
        default=False,
    )
    temporary_token = False
    if enabled and token is None:
        token = secrets.token_urlsafe(32)
        temporary_token = True
    if enabled and not api_host_is_loopback(host):
        raise ValueError("API host must be a loopback address for local access.")
    if mcp_write_tools and not enabled:
        raise ValueError("MCP write tools require API access to be enabled.")
    return ApiSettings(
        enabled=enabled,
        host=host,
        port=port,
        token=token,
        temporary_token=temporary_token,
        mcp_write_tools=mcp_write_tools,
    )


def _product_api_settings(parser: ConfigParser) -> ProductApiSettings:
    return ProductApiSettings(
        enabled=_bool_value(
            os.environ.get("DREAMLAND_PRODUCT_API_ENABLED")
            or parser.get("product_api", "enabled", fallback=None),
            default=False,
        )
    )


def api_host_is_loopback(host: str) -> bool:
    cleaned = host.strip().lower()
    if cleaned == "localhost":
        return True
    try:
        return ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _event_values(value: str | None) -> frozenset[str]:
    cleaned = _optional_value(value)
    if cleaned is None:
        return frozenset()
    events = frozenset(
        event.strip().lower() for event in cleaned.split(",") if event.strip()
    )
    invalid = events - LOG_EVENT_VALUES
    if invalid:
        choices = ", ".join(sorted(LOG_EVENT_VALUES))
        invalid_values = ", ".join(sorted(invalid))
        raise ValueError(
            f"Expected logging events to use {choices}; got {invalid_values}."
        )
    return events


def _enrichment_trigger_values(value: str | None) -> frozenset[str]:
    cleaned = _optional_value(value)
    if cleaned is None:
        return frozenset()
    triggers = frozenset(
        trigger.strip().lower() for trigger in cleaned.split(",") if trigger.strip()
    )
    invalid = triggers - ENRICHMENT_TRIGGER_VALUES
    if invalid:
        choices = ", ".join(sorted(ENRICHMENT_TRIGGER_VALUES))
        invalid_values = ", ".join(sorted(invalid))
        raise ValueError(
            f"Expected enrichment triggers to use {choices}; got {invalid_values}."
        )
    return triggers
