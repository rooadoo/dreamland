"""Deterministic creative latitude shared across enrichment role boundaries."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from dreamland.background_agents.enrichment.limits import MAX_EXPERT_RELATIONS

CreativeLatitudeLevel = Literal[
    "strict",
    "conservative",
    "moderate",
    "expansive",
    "abundant",
]
CandidateOrigin = Literal["derived", "selected"]

DEFAULT_CREATIVE_LATITUDE: Final[CreativeLatitudeLevel] = "moderate"
CREATIVE_LATITUDE_TEMPLATE_VERSION: Final[str] = "creative-latitude-v5"

_AFFIRMATIVE_REALIZATION_INSTRUCTION: Final[str] = (
    "State affirmative, concrete, mutually consistent details from one "
    "realization; do not return inspection advice, possibilities, or alternative "
    "menus. "
)


@dataclass(frozen=True, slots=True)
class CreativeLatitude:
    """One resolved breadth policy shared by Expert and Integrator routing."""

    level: CreativeLatitudeLevel
    instruction: str
    integration_instruction: str
    candidate_limit: int
    allowed_origins: frozenset[CandidateOrigin]
    configured_value: str | None
    defaulted: bool
    template_version: str = CREATIVE_LATITUDE_TEMPLATE_VERSION

    def public_payload(self) -> dict[str, object]:
        """Return the task-neutral subset which crosses the Expert boundary."""

        return {
            "instruction": self.instruction,
            "candidate_limit": self.candidate_limit,
            "relation_limit": self.relation_limit,
        }

    def integration_payload(self) -> dict[str, object]:
        """Return deterministic retention guidance for the Integrator."""

        return {"instruction": self.integration_instruction}

    @property
    def relation_limit(self) -> int:
        """Bound relation topology to the resolved artifact capacity."""

        return min(self.candidate_limit, MAX_EXPERT_RELATIONS)

    @property
    def instruction_sha256(self) -> str:
        """Identify the exact deterministic wording without logging its text."""

        return hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()

    @property
    def integration_instruction_sha256(self) -> str:
        """Identify exact Integrator guidance without logging its text."""

        return hashlib.sha256(self.integration_instruction.encode("utf-8")).hexdigest()


_LATITUDE_SPECS: Final[
    dict[
        CreativeLatitudeLevel,
        tuple[str, str, int, frozenset[CandidateOrigin]],
    ]
] = {
    "strict": (
        (
            _AFFIRMATIVE_REALIZATION_INSTRUCTION
            + "Return only net-new details that are directly supported or logically "
            "unavoidable from the evidence."
        ),
        (
            "Faithfully materialize every directly and unambiguously mappable "
            "eligible candidate; do not add interpretive substance."
        ),
        2,
        frozenset({"derived"}),
    ),
    "conservative": (
        (
            _AFFIRMATIVE_REALIZATION_INSTRUCTION
            + "Return a small number of net-new routine details that are strongly "
            "implied by the evidence."
        ),
        (
            "Faithfully materialize every clearly mappable eligible candidate "
            "while avoiding marginal or lossy encodings."
        ),
        4,
        frozenset({"derived"}),
    ),
    "moderate": (
        (
            _AFFIRMATIVE_REALIZATION_INSTRUCTION
            + "Construct one coherent representative realization. When several "
            "ordinary answers fit, select plausible concrete details instead of "
            "leaving every choice unresolved. Develop supporting observable "
            "context around the focal detail when it fits."
        ),
        (
            "Materialize a coherent representative cluster and preserve distinct "
            "safely mappable artifacts when each adds net-new observable substance."
        ),
        8,
        frozenset({"derived", "selected"}),
    ),
    "expansive": (
        (
            _AFFIRMATIVE_REALIZATION_INSTRUCTION
            + "Produce a substantial connected layer of mutually supporting "
            "operational details. Select plausible concrete specifics where the "
            "evidence leaves ordinary creative space. Use most available capacity "
            "when distinct coherent material exists, developing structural, "
            "lifecycle, causal, and residual depth instead of a flat inventory."
        ),
        (
            "Materialize most safely mappable eligible material. Preserve distinct "
            "observable kinds, nested paths, leaf artifacts, lifecycle distinctions, "
            "and relationships rather than compressing them into a minimal or flat "
            "summary."
        ),
        12,
        frozenset({"derived", "selected"}),
    ),
    "abundant": (
        (
            _AFFIRMATIVE_REALIZATION_INSTRUCTION
            + "Build a dense, coherent causal layer with candid operational texture "
            "and residue. Use the full available capacity when distinct coherent "
            "net-new material supports it. Develop applicable structural nesting, leaf "
            "material, lifecycle variation, causal relationships, and signs of "
            "present or past activity rather than a flat inventory. Prefer believable "
            "concrete choices over unspecified ordinary details unless a choice would "
            "conflict with the evidence."
        ),
        (
            "Materialize the fullest safely mappable version of the eligible cluster "
            "up to the patch limit. Preserve distinct nested paths, leaf artifacts, "
            "identities, lifecycle distinctions, candid cross-surface operational "
            "residue, and relationships rather than flattening hierarchy or treating "
            "time-varying activity as a permanent simultaneous inventory."
        ),
        24,
        frozenset({"derived", "selected"}),
    ),
}


def creative_latitude_from_snapshot(
    snapshot: Mapping[str, object],
) -> CreativeLatitude:
    """Resolve world generation liberty without exposing its internal schema."""

    configured_value: str | None = None
    world = snapshot.get("world")
    if isinstance(world, Mapping):
        policy = world.get("generation_policy")
        if isinstance(policy, Mapping):
            configured = policy.get("liberty")
            if isinstance(configured, str) and configured.strip():
                configured_value = configured.strip().lower()

    defaulted = configured_value not in _LATITUDE_SPECS
    level = cast(
        CreativeLatitudeLevel,
        DEFAULT_CREATIVE_LATITUDE if defaulted else configured_value,
    )
    (
        instruction,
        integration_instruction,
        candidate_limit,
        allowed_origins,
    ) = _LATITUDE_SPECS[level]
    return CreativeLatitude(
        level=level,
        instruction=instruction,
        integration_instruction=integration_instruction,
        candidate_limit=candidate_limit,
        allowed_origins=allowed_origins,
        configured_value=configured_value,
        defaulted=defaulted,
    )


__all__ = [
    "CREATIVE_LATITUDE_TEMPLATE_VERSION",
    "DEFAULT_CREATIVE_LATITUDE",
    "CandidateOrigin",
    "CreativeLatitude",
    "CreativeLatitudeLevel",
    "creative_latitude_from_snapshot",
]
