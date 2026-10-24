"""Deployment intent and provider-free checks for the SSH foundation."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator, Mapping
from configparser import ConfigParser
from configparser import Error as ConfigError
from contextlib import contextmanager
from pathlib import Path

from dreamland.config import DreamlandSettings, load_settings
from dreamland.deployment import storage
from dreamland.lobby_catalog import LobbyCatalog
from dreamland.model_defaults import ROLE_MODEL_DEFAULTS
from dreamland.state_profiles import StateProfileStore


class DeploymentError(ValueError):
    """Setup cannot proceed with the supplied configuration."""


# Profiles that collect Langfuse tracing data under the user agreement.
# Prerelease stays untraced; it predates this data-collection decision and is
# not expected to be used again. Widen deliberately, not by default.
LANGFUSE_ENABLED_PROFILES = ("beta", "conference")


def default_document(
    profile: str = "prerelease",
    *,
    ssh: bool = True,
    ssh_entry_mode: str = "lobby",
    ssh_bans: bool | None = None,
) -> dict:
    document = {
        "kind": "dreamland.deployment",
        "schema_version": 2,
        "profile": profile,
        "requested_services": {
            "ssh": ssh,
        },
        "ssh_entry_mode": ssh_entry_mode,
        "planned_access": {
            "ssh_bans": profile == "prerelease" if ssh_bans is None else ssh_bans,
            "forge_private_by_default": True,
        },
    }
    validate_document(document)
    return document


def validate_document(document: dict) -> None:
    if not isinstance(document, dict):
        raise DeploymentError("unsupported deployment document")
    if (
        document.get("kind") == "dreamland.deployment"
        and type(document.get("schema_version")) is int
        and document["schema_version"] == 1
    ):
        raise DeploymentError(
            "older deployment configuration requires migration to backend-only schema 2"
        )
    expected = {
        "kind",
        "schema_version",
        "profile",
        "requested_services",
        "ssh_entry_mode",
        "planned_access",
    }
    if (
        set(document) != expected
        or document["kind"] != "dreamland.deployment"
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 2
        or document["profile"] not in ("prerelease", "conference", "beta")
        or document["ssh_entry_mode"] not in ("lobby", "direct")
    ):
        raise DeploymentError("unsupported deployment document")
    services = document["requested_services"]
    access = document["planned_access"]
    if (
        not isinstance(services, dict)
        or set(services) != {"ssh"}
        or any(type(value) is not bool for value in services.values())
        or not isinstance(access, dict)
        or set(access)
        != {
            "ssh_bans",
            "forge_private_by_default",
        }
        or any(type(value) is not bool for value in access.values())
    ):
        raise DeploymentError("deployment switches must use the supported booleans")
    if not access["forge_private_by_default"]:
        raise DeploymentError("private Forge defaults are required")
    if document["profile"] == "conference" and access["ssh_bans"]:
        raise DeploymentError("conference SSH bans must be disabled")


def default_ini(document: dict) -> str:
    validate_document(document)
    base = f"""# Phase 1: container SSH foundation. Backend HTTP is not implemented here.
# Paths are relative to this persistent deployment directory.
[ssh]
host = 0.0.0.0
port = 2222
host_key_path = ssh_host_rsa_key
entry_mode = {document["ssh_entry_mode"]}

[state_profile]
profile = lobby-supermarket

[lobby]
catalog_path = lobby/catalog.json
public_registry_path = lobby/public_visits.sqlite3

[enrichment]
enabled = true
apply_mode = safe

[logging]
enabled = true
level = INFO
events = lifecycle,commands,tokens,failures,enrichment
reply_excerpt_chars = 0

[langfuse]
enabled = {"true" if document["profile"] in LANGFUSE_ENABLED_PROFILES else "false"}
public_key =
secret_key =
host = https://cloud.langfuse.com

[api]
enabled = false

[product_api]
enabled = false
"""
    roles = ConfigParser(interpolation=None)
    for section, defaults in ROLE_MODEL_DEFAULTS.items():
        roles[section] = defaults.as_ini()
    output = io.StringIO()
    roles.write(output)
    return base + "\n# Shared project agent and model defaults.\n" + output.getvalue()


def update_model_defaults(root: Path, *, apply: bool = False) -> dict:
    """Explicitly adopt current role controls; normal init never changes them."""
    validate(root)
    original = storage.read_managed(root, "dreamland.ini")
    parser = ConfigParser(interpolation=None)
    try:
        parser.read_string(original.decode("utf-8"))
    except (ConfigError, UnicodeError):
        raise DeploymentError(
            "runtime configuration changed or is invalid; review it again"
        ) from None
    changed = []
    for section, defaults in ROLE_MODEL_DEFAULTS.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in defaults.as_ini().items():
            if parser.get(section, key, fallback=None) != value:
                changed.append(f"{section}.{key}")
            parser.set(section, key, value)
    backup = None
    if apply and changed:
        output = io.StringIO()
        parser.write(output)
        backup = storage.replace_runtime_ini(
            root, expected=original, replacement=output.getvalue().encode("utf-8")
        )
    return {
        "operation": "model-defaults",
        "applied": bool(apply and changed),
        "changed_settings": changed,
        "backup": backup,
        "restart_required": bool(apply and changed),
        "defaults": {
            section: defaults.as_ini()
            for section, defaults in ROLE_MODEL_DEFAULTS.items()
        },
    }


LANGFUSE_CREDENTIAL_KEYS = ("public_key", "secret_key", "host")


def update_langfuse_settings(
    root: Path,
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> None:
    """Update Langfuse credentials in the deployment INI for traced profiles.

    A `None` value leaves that stored setting unchanged, so a caller filling one
    missing credential cannot blank the others.
    """
    document, _ = storage.read_configuration(root)
    validate_document(document)
    if document["profile"] not in LANGFUSE_ENABLED_PROFILES:
        raise DeploymentError(
            "Langfuse configuration only applies to beta or conference profile "
            "deployments"
        )

    original = storage.read_managed(root, "dreamland.ini")
    parser = ConfigParser(interpolation=None)
    try:
        parser.read_string(original.decode("utf-8"))
    except (ConfigError, UnicodeError):
        raise DeploymentError(
            "runtime configuration changed or is invalid; review it again"
        ) from None

    if not parser.has_section("langfuse"):
        parser.add_section("langfuse")

    parser.set("langfuse", "enabled", "true")
    for key, value in zip(
        LANGFUSE_CREDENTIAL_KEYS, (public_key, secret_key, host), strict=True
    ):
        if value is not None:
            parser.set("langfuse", key, value)

    output = io.StringIO()
    parser.write(output)
    storage.replace_runtime_ini(
        root, expected=original, replacement=output.getvalue().encode("utf-8")
    )


def missing_credentials(root: Path) -> list[str]:
    """Report the required credentials this deployment has not stored yet.

    The result drives gap-filling setup: it names what is absent without reading
    or disclosing any stored value. An empty list means every credential this
    profile requires is present, not that a provider has accepted it.
    """
    document, runtime_ini = storage.read_configuration(root)
    validate_document(document)
    missing = [
        name
        for name in ("ssh_password", "openai_api_key")
        if not storage.secret_present(root, name)
    ]
    if document["profile"] in LANGFUSE_ENABLED_PROFILES:
        parser = ConfigParser(interpolation=None)
        try:
            parser.read_string(runtime_ini)
        except ConfigError:
            raise DeploymentError(
                "runtime configuration changed or is invalid; review it again"
            ) from None
        missing.extend(
            f"langfuse.{key}"
            for key in LANGFUSE_CREDENTIAL_KEYS
            if not parser.get("langfuse", key, fallback="").strip()
        )
    return missing


def runtime_environment(
    inherited: Mapping[str, str],
    root: Path,
    document: dict,
    *,
    credentials: bool = False,
) -> dict[str, str]:
    # Start from a small OS allowlist so lab keys, proxy settings, provider
    # endpoints, tracing, and runtime overrides cannot silently change a release.
    environment = {
        key: value
        for key, value in inherited.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"}
    }
    environment.update(
        {
            "DREAMLAND_CONFIG": str(root.absolute() / "dreamland.ini"),
            # LangChain's own native tracing/callbacks stay off regardless of
            # profile; Dreamland's Langfuse integration is a separate switch.
            "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_TRACING": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if document.get("profile") not in LANGFUSE_ENABLED_PROFILES:
        # Force tracing off for profiles that must not collect data. When the
        # profile allows tracing, omit this key so the real dreamland.ini
        # `[langfuse] enabled` value is read instead of being shadowed here.
        environment["LANGFUSE_ENABLED"] = "false"
    if credentials:
        environment["OPENAI_API_KEY"] = storage.read_secret(root, "openai_api_key")
        environment["DREAMLAND_SSH_PASSWORD"] = storage.read_secret(
            root, "ssh_password"
        )
        if storage.secret_present(root, "ssh_fast_password"):
            environment["DREAMLAND_SSH_FAST_PASSWORD"] = storage.read_secret(
                root, "ssh_fast_password"
            )
    return environment


@contextmanager
def _configuration_environment(environment: Mapping[str, str]) -> Iterator[None]:
    """Use the existing settings parser in this single-threaded setup CLI only."""
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def validate(root: Path, *, require_langfuse_credentials: bool = True) -> dict:
    """Check saved intent, runtime configuration and actual world selection offline.

    `require_langfuse_credentials=False` skips the Langfuse completeness gate
    only. It exists so `configure-ssh` can validate everything else about a
    freshly initialized beta/conference deployment before the operator has had
    a chance to supply the Langfuse keys that command is responsible for
    collecting; normal validation (`show`, `serve-ssh`) always requires them.
    """
    document, runtime_ini = storage.read_configuration(root)
    validate_document(document)
    try:
        parser = ConfigParser()
        parser.read_string(runtime_ini)
        langfuse_enabled = parser.getboolean("langfuse", "enabled", fallback=False)
        if langfuse_enabled and document["profile"] not in LANGFUSE_ENABLED_PROFILES:
            raise DeploymentError("tracing is disabled for this checkpoint")
        if (
            langfuse_enabled
            and document["profile"] in LANGFUSE_ENABLED_PROFILES
            and require_langfuse_credentials
        ):
            # Validate that Langfuse credentials are present for traced profiles
            if not all(
                [
                    parser.get("langfuse", "public_key", fallback="").strip(),
                    parser.get("langfuse", "secret_key", fallback="").strip(),
                    parser.get("langfuse", "host", fallback="").strip(),
                ]
            ):
                raise DeploymentError(
                    "beta and conference profiles require complete Langfuse "
                    "configuration"
                )
        with _configuration_environment(
            runtime_environment(os.environ, root, document)
        ):
            settings = load_settings(root / "dreamland.ini")
        _validate_settings(root, document, settings)
        # Secure reads also ensure missing catalog files cannot be auto-created
        # by the existing catalog constructor during this read-only command.
        catalog_document = json.loads(storage.read_managed(root, "lobby/catalog.json"))
        for world in catalog_document["worlds"]:
            storage.read_profile(root, world["profile"])
        storage.read_profile(root, settings.state_profile.profile)
        store = StateProfileStore(root / "state_profiles")
        catalog = LobbyCatalog(store, root / "lobby/catalog.json")
        entries = catalog.snapshot().entries
        if settings.ssh.entry_mode == "lobby" and not entries:
            raise DeploymentError("the lobby needs at least one published world")
        store.load(settings.state_profile.profile)
    except (OSError, ValueError, KeyError, TypeError, ConfigError, RecursionError):
        raise DeploymentError(
            "deployment runtime or world configuration is invalid"
        ) from None
    stage = (
        f"{document['profile']}-ssh-langfuse"
        if document["profile"] in LANGFUSE_ENABLED_PROFILES
        else "phase1-ssh-foundation"
    )
    return {
        "validation": "passed",
        "stage": stage,
        "public_release_ready": False,
        "backend_http_implemented": False,
        "operator_console_implemented": True,
        "ssh_bans_implemented": False,
        "langfuse_tracing_enabled": langfuse_enabled,
        "credentials_checked": False,
        "world_count": len(entries),
        "configuration": document,
    }


def _validate_settings(root: Path, document: dict, settings: DreamlandSettings) -> None:
    root = root.absolute()
    if (
        settings.ssh.host != "0.0.0.0"
        or settings.ssh.port != 2222
        or settings.ssh.entry_mode != document["ssh_entry_mode"]
        or settings.ssh.host_key_path != root / "ssh_host_rsa_key"
        or settings.lobby.catalog_path != root / "lobby/catalog.json"
        or settings.lobby.public_registry_path != root / "lobby/public_visits.sqlite3"
        or settings.api.enabled
        or settings.product_api.enabled
        or (
            settings.langfuse.enabled
            and document["profile"] not in LANGFUSE_ENABLED_PROFILES
        )
    ):
        raise DeploymentError("unsupported foundation runtime settings")
    providers = [settings.agent.model_provider, settings.spiritguide.model_provider]
    if settings.enrichment.enabled:
        providers.extend(
            [
                settings.enrichment.coordinator.model_provider,
                settings.enrichment.ambient_coordinator.model_provider,
                settings.enrichment.expert.model_provider,
                settings.enrichment.integrator.model_provider,
            ]
        )
    if any(provider != "openai" for provider in providers):
        raise DeploymentError("this deployment supports OpenAI credentials only")
