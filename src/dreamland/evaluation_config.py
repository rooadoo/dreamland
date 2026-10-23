"""Resolve non-secret evaluation overrides without changing process settings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import TypedDict, TypeVar, cast

from dreamland.config import (
    ENRICHMENT_APPLY_MODE_VALUES,
    ENRICHMENT_TRIGGER_VALUES,
    PROMPT_CACHE_VALUES,
    REASONING_EFFORT_VALUES,
    REASONING_SUMMARY_VALUES,
    SPEED_VALUES,
    VERBOSITY_VALUES,
    AgentSettings,
    DreamlandSettings,
    EnrichmentRoleSettings,
    SpiritguideSettings,
)
from dreamland.model_providers import (
    OPENAI_API_VALUES,
    OPENAI_REASONING_MODE_VALUES,
    OPENAI_SERVICE_TIER_VALUES,
    ChatModelProviderConfig,
    parse_model_reference,
    validate_model_controls,
)

EVALUATION_CONFIG_KEY = "dreamland.session.evaluation_config"
ENRICHMENT_ROLES = ("coordinator", "ambient_coordinator", "expert", "integrator")
_Role = TypeVar("_Role", AgentSettings, SpiritguideSettings, EnrichmentRoleSettings)
_CHOICES = {
    "reasoning_effort": REASONING_EFFORT_VALUES,
    "reasoning_summary": REASONING_SUMMARY_VALUES,
    "verbosity": VERBOSITY_VALUES,
    "speed": SPEED_VALUES,
    "prompt_cache": PROMPT_CACHE_VALUES,
    "openai_api": OPENAI_API_VALUES,
    "openai_service_tier": OPENAI_SERVICE_TIER_VALUES,
    "openai_reasoning_mode": OPENAI_REASONING_MODE_VALUES,
}
_ENRICHMENT_INTS = {
    "idle_delay_ms": 0,
    "cooldown_ms": 0,
    "max_jobs_per_minute": 1,
    "max_inflight_per_session": 1,
}


class _RoleChanges(TypedDict, total=False):
    model: str
    reasoning_effort: str | None
    reasoning_summary: str | None
    verbosity: str | None
    speed: str | None
    prompt_cache: str | None
    max_output_tokens: int | None
    openai_api: str | None
    openai_service_tier: str | None
    openai_reasoning_mode: str | None


class _EnrichmentChanges(TypedDict, total=False):
    enabled: bool
    apply_mode: str
    triggers: frozenset[str]
    idle_delay_ms: int
    cooldown_ms: int
    max_jobs_per_minute: int
    max_inflight_per_session: int
    coordinator: EnrichmentRoleSettings
    ambient_coordinator: EnrichmentRoleSettings
    expert: EnrichmentRoleSettings
    integrator: EnrichmentRoleSettings


def _object(value: object, name: str, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    if set(value) - allowed:
        # Do not echo arbitrary input: callers may accidentally supply credentials.
        raise ValueError(f"{name} contains unsupported fields")
    return value


def _role_override(base: _Role, value: object, name: str) -> _Role:
    changes = dict(_object(value, name, {*_CHOICES, "model", "max_output_tokens"}))
    if "model" in changes:
        model = changes["model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{name}.model must be a nonempty provider:model string")
        try:
            reference = parse_model_reference(model, name=f"{name}.model")
        except ValueError:
            raise ValueError(
                f"{name}.model requires a supported provider:model"
            ) from None
        changes["model"] = f"{reference.provider}:{reference.model}"
    for key, allowed in _CHOICES.items():
        if key not in changes:
            continue
        option = changes[key]
        if option is not None and (
            not isinstance(option, str) or option not in allowed
        ):
            raise ValueError(f"{name}.{key} must be null or one of {sorted(allowed)}")
    if "max_output_tokens" in changes:
        cap = changes["max_output_tokens"]
        if cap is not None and (type(cap) is not int or cap < 0):
            raise ValueError(
                f"{name}.max_output_tokens must be null or a nonnegative integer"
            )
    role = replace(base, **cast(_RoleChanges, changes))
    validate_model_controls(
        ChatModelProviderConfig(
            provider=role.model_provider,
            model=role.model_name,
            reasoning_effort=role.reasoning_effort,
            reasoning_summary=role.reasoning_summary,
            openai_api=role.openai_api,
            openai_service_tier=role.openai_service_tier,
            openai_reasoning_mode=role.openai_reasoning_mode,
        )
    )
    return role


def resolve_evaluation_config(
    base: DreamlandSettings,
    overrides: object,
    *,
    detached: bool = False,
) -> DreamlandSettings:
    allowed = {"spiritguide"} if detached else {"agent", "spiritguide", "enrichment"}
    values = _object(overrides, "config_overrides", allowed)
    agent = _role_override(base.agent, values.get("agent", {}), "agent")
    spiritguide = _role_override(
        base.spiritguide, values.get("spiritguide", {}), "spiritguide"
    )
    enrichment_values = _object(
        values.get("enrichment", {}),
        "enrichment",
        {*ENRICHMENT_ROLES, *_ENRICHMENT_INTS, "enabled", "apply_mode", "triggers"},
    )
    changes: dict[str, object] = {}
    for key, value in enrichment_values.items():
        if key in ENRICHMENT_ROLES:
            changes[key] = _role_override(
                getattr(base.enrichment, key), value, f"enrichment.{key}"
            )
        elif key == "enabled":
            if type(value) is not bool:
                raise ValueError("enrichment.enabled must be a boolean")
            changes[key] = value
        elif key == "apply_mode":
            if not isinstance(value, str) or value not in ENRICHMENT_APPLY_MODE_VALUES:
                raise ValueError("enrichment.apply_mode must be safe or shadow")
            changes[key] = value
        elif key == "triggers":
            if not isinstance(value, list) or any(
                not isinstance(trigger, str) or trigger not in ENRICHMENT_TRIGGER_VALUES
                for trigger in value
            ):
                raise ValueError(
                    "enrichment.triggers must be a list of supported triggers"
                )
            changes[key] = frozenset(value)
        else:
            if type(value) is not int or value < _ENRICHMENT_INTS[key]:
                raise ValueError(
                    f"enrichment.{key} must be an integer >= {_ENRICHMENT_INTS[key]}"
                )
            changes[key] = value
    return replace(
        base,
        agent=agent,
        spiritguide=spiritguide,
        enrichment=replace(base.enrichment, **cast(_EnrichmentChanges, changes)),
    )


def resolve_mcp_session_config(
    base: DreamlandSettings,
    overrides: object = None,
    *,
    model: str | None = None,
    openai_service_tier: str | None = None,
) -> DreamlandSettings:
    """Resolve command aliases and MCP's standard OpenAI serving default."""
    values = dict(
        _object(
            {} if overrides is None else overrides,
            "config_overrides",
            {"agent", "spiritguide", "enrichment"},
        )
    )
    agent = dict(
        _object(
            values.get("agent", {}), "agent", {*_CHOICES, "model", "max_output_tokens"}
        )
    )
    for key, value in (("model", model), ("openai_service_tier", openai_service_tier)):
        if value is None:
            continue
        if key in agent and agent[key] != value:
            raise ValueError(f"{key} conflicts with config_overrides.agent.{key}")
        agent[key] = value
    values["agent"] = agent
    settings = resolve_evaluation_config(base, values)
    if "openai_service_tier" not in agent and settings.agent.model_provider == "openai":
        settings = replace(
            settings, agent=replace(settings.agent, openai_service_tier="default")
        )
    return settings


def evaluation_snapshot(
    settings: DreamlandSettings, *, detached: bool = False
) -> dict[str, object]:
    roles = {"spiritguide": asdict(settings.spiritguide)}
    if not detached:
        roles["agent"] = asdict(settings.agent)
        enrichment = asdict(settings.enrichment)
        enrichment["triggers"] = sorted(settings.enrichment.triggers)
        roles["enrichment"] = enrichment
    encoded = json.dumps(roles, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "settings": roles,
    }
