"""Contracts and deterministic state handling for the EnrichmentAgent."""

from dreamland.background_agents.enrichment.contracts import (
    EnrichmentApplyOutcome,
    EnrichmentApplyResult,
    EnrichmentAuthorityDependency,
    EnrichmentCluster,
    EnrichmentClusterOutcome,
    EnrichmentContribution,
    EnrichmentContributionOutcome,
    EnrichmentDependency,
    EnrichmentIntegratorOutput,
    EnrichmentNoOp,
    EnrichmentPatch,
    EnrichmentPatchValidationError,
    enrichment_integrator_output_from_json,
    enrichment_integrator_output_from_payload,
    enrichment_integrator_output_to_json,
    enrichment_patch_from_json,
    enrichment_patch_from_payload,
    enrichment_patch_to_json,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.patching import (
    ALLOWED_HOST_REGISTRY_PATHS,
    apply_enrichment_patch,
)

__all__ = [
    "ALLOWED_HOST_REGISTRY_PATHS",
    "EnrichmentApplyOutcome",
    "EnrichmentApplyResult",
    "EnrichmentAuthorityDependency",
    "EnrichmentCluster",
    "EnrichmentClusterOutcome",
    "EnrichmentContribution",
    "EnrichmentContributionOutcome",
    "EnrichmentDependency",
    "EnrichmentIntegratorOutput",
    "EnrichmentMode",
    "EnrichmentNoOp",
    "EnrichmentPatch",
    "EnrichmentPatchValidationError",
    "apply_enrichment_patch",
    "enrichment_integrator_output_from_json",
    "enrichment_integrator_output_from_payload",
    "enrichment_integrator_output_to_json",
    "enrichment_patch_from_json",
    "enrichment_patch_from_payload",
    "enrichment_patch_to_json",
]
