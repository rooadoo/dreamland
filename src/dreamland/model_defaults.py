"""Shared model preferences for new local and deployed configurations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class RoleModelDefaults:
    model: str
    reasoning_effort: str | None = "low"
    reasoning_summary: str | None = None
    verbosity: str | None = "low"
    speed: str | None = None
    prompt_cache: str | None = None
    max_output_tokens: int = 2048
    openai_api: str | None = None
    openai_service_tier: str | None = None
    openai_reasoning_mode: str | None = None

    def as_ini(self) -> dict[str, str]:
        """Return only model controls, with explicit blanks for provider defaults."""
        return {
            name: "" if value is None else str(value)
            for name, value in asdict(self).items()
        }


AGENT_MODEL_DEFAULTS = RoleModelDefaults(
    model="openai:gpt-6.1-sol",
    reasoning_summary="detailed",
    verbosity=None,
    max_output_tokens=12800,
    openai_service_tier="fast",
)
SPIRITGUIDE_MODEL_DEFAULTS = RoleModelDefaults(
    model="openai:gpt-6-astra",
    reasoning_effort="high",
    reasoning_summary="detailed",
    verbosity=None,
    max_output_tokens=16384,
    openai_service_tier="ultrafast",
)
ENRICHMENT_MODEL_DEFAULTS = RoleModelDefaults(
    model="openai:gpt-6-luna", openai_service_tier="fast"
)
ENRICHMENT_EXPERT_MODEL_DEFAULTS = RoleModelDefaults(
    model="openai:gpt-6.1-sol", openai_service_tier="fast"
)
ENRICHMENT_INTEGRATOR_MODEL_DEFAULTS = RoleModelDefaults(
    model="openai:gpt-6-luna", max_output_tokens=4096, openai_service_tier="fast"
)

ROLE_MODEL_DEFAULTS = MappingProxyType(
    {
        "agent": AGENT_MODEL_DEFAULTS,
        "spiritguide": SPIRITGUIDE_MODEL_DEFAULTS,
        "enrichment.coordinator": ENRICHMENT_MODEL_DEFAULTS,
        "enrichment.ambient_coordinator": ENRICHMENT_MODEL_DEFAULTS,
        "enrichment.expert": ENRICHMENT_EXPERT_MODEL_DEFAULTS,
        "enrichment.integrator": ENRICHMENT_INTEGRATOR_MODEL_DEFAULTS,
    }
)
