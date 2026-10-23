"""Inspectable LangGraph workflow for session-local world enrichment.

The graph proposes an :class:`EnrichmentPatch`; it never applies one.  State
capture and patch application remain runtime responsibilities so their locks,
drift checks, and outcomes stay explicit at the owning layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, TypeAlias, TypedDict, cast

from langchain_core.runnables import RunnableConfig

from dreamland.agent_tracing import TokenUsageLoggingCallback
from dreamland.background_agents.enrichment.context import (
    EnrichmentContextProjection,
    canonical_json,
)
from dreamland.background_agents.enrichment.contracts import (
    EnrichmentAuthorityDependency,
    EnrichmentContribution,
    EnrichmentDependency,
    EnrichmentIntegratorOutput,
    EnrichmentNoOp,
    EnrichmentPatch,
    EnrichmentPatchValidationError,
    StatePath,
    enrichment_integrator_output_from_json,
)
from dreamland.background_agents.enrichment.foreground_evidence import (
    FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN,
    FOREGROUND_EVIDENCE_POLICY_VERSION,
)
from dreamland.background_agents.enrichment.latitude import CreativeLatitude
from dreamland.background_agents.enrichment.limits import (
    MAX_COORDINATOR_ANCHORS,
    MAX_PATCH_CONTRIBUTIONS,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.normalization import (
    IntegratorNormalizationResult,
    normalize_integrator_patch,
)
from dreamland.background_agents.enrichment.patching import (
    ALLOWED_HOST_REGISTRY_PATHS,
    apply_enrichment_patch,
)
from dreamland.background_agents.enrichment.role_inputs import (
    PRESENCE_GAP_POLICY_VERSION,
    CoordinatorPromptInput,
    EstablishedReference,
    RoleInputValidationError,
    build_coordinator_coverage_summary,
    build_coordinator_prompt_input,
    build_expert_brief,
    expert_task_policy_log_fields,
    resolve_current_reference_bindings,
)
from dreamland.background_agents.enrichment.role_outputs import (
    ApiKeyResolver,
    CoordinatorDecision,
    CoordinatorStatus,
    ExpertCondition,
    ExpertGate,
    ExpertiseScope,
    ExpertItem,
    ExpertOrigin,
    ExpertRelation,
    ExpertReport,
    ExpertStatus,
    ModelFactory,
    RoleOutputValidationError,
    basis_host_from_snapshot,
    configured_role_runtimes,
    enrichment_invoke_config,
    injected_role_runtimes,
    message_content_to_text,
    parse_coordinator_decision,
    parse_expert_report,
    parse_expert_report_with_relation_salvage,
    require_nonempty_text,
    snapshot_document,
    snapshot_sha256,
    validated_text_tuple,
)
from dreamland.background_agents.enrichment.role_outputs import (
    RoleRuntime as _RoleRuntime,
)
from dreamland.background_agents.enrichment.telemetry import (
    EnrichmentLogContext,
    log_enrichment_event,
)
from dreamland.config import EnrichmentSettings
from dreamland.model_providers import build_chat_model
from dreamland.models import RuntimeProvenance
from dreamland.prompt_loader import load_prompt
from dreamland.provider_failures import (
    ProviderFailureClassification,
    classify_provider_failure,
)

ENRICHMENT_GRAPH_VERSION = "enrichment-v15"
EXPERT_RELATION_POLICY_VERSION = "expert-relations-v2"
AMBIENT_HUMAN_INTEGRATION_POLICY_VERSION = "ambient-human-integration-v3"
_AMBIENT_INTEGRATOR_PROMPT_SUFFIX = load_prompt(
    "enrichment_ambient_integrator_suffix.md"
)
_AMBIENT_INTEGRATOR_PROMPT_SUFFIX_SHA256 = hashlib.sha256(
    _AMBIENT_INTEGRATOR_PROMPT_SUFFIX.encode("utf-8")
).hexdigest()
_MAX_INTERACTION_FOCUS_CHARS = 4096
_FOREGROUND_AUTHORITY_POLICY_VERSION = "foreground-authority-v1"
_ACTIVE_SESSION_AUTHORITY_DOMAIN = "active_session"
_ACTIVE_SESSION_SURFACES = frozenset({"identity", "service", "shell"})
_ACTIVE_SESSION_MARKERS = frozenset(
    {
        "access",
        "authentication",
        "interactive",
        "login",
        "pam",
        "prompt",
        "session",
        "shell",
        "ssh",
        "sudo",
        "terminal",
    }
)
_ACTIVE_SESSION_MARKER_PREFIXES = (
    "auth",
    "login",
    "pam",
    "prompt",
    "session",
    "shell",
    "ssh",
    "sudo",
    "terminal",
)
_AUTHORITY_TOKEN = re.compile(r"[a-z0-9]+")

SnapshotInput: TypeAlias = EnrichmentContextProjection | Mapping[str, object]
CurrentSnapshotCapture = Callable[["EnrichmentJobContext"], SnapshotInput]


class EnrichmentGraphStatus(StrEnum):
    PATCH_PROPOSED = "patch_proposed"
    NO_OP = "no_op"
    REJECTED = "rejected"


class EnrichmentGraphReason(StrEnum):
    PATCH_PROPOSED = "patch_proposed"
    COORDINATOR_NO_OP = "coordinator_no_op"
    COORDINATOR_MODEL_FAILED = "coordinator_model_failed"
    COORDINATOR_OUTPUT_INVALID = "coordinator_output_invalid"
    COORDINATOR_CONTEXT_INVALID = "coordinator_context_invalid"
    EXPERT_BRIEF_INVALID = "expert_brief_invalid"
    EXPERT_MODEL_FAILED = "expert_model_failed"
    EXPERT_OUTPUT_INVALID = "expert_output_invalid"
    EXPERT_INSUFFICIENT = "expert_insufficient"
    EXPERT_NO_ELIGIBLE_ITEMS = "expert_no_eligible_items"
    CURRENT_SNAPSHOT_CAPTURE_FAILED = "current_snapshot_capture_failed"
    CURRENT_SNAPSHOT_INVALID = "current_snapshot_invalid"
    CURRENT_REFERENCE_INVALID = "current_reference_invalid"
    INTEGRATOR_MODEL_FAILED = "integrator_model_failed"
    INTEGRATOR_OUTPUT_INVALID = "integrator_output_invalid"
    INTEGRATOR_NO_OP = "integrator_no_op"
    INTEGRATOR_PROVENANCE_INVALID = "integrator_provenance_invalid"
    INTEGRATOR_REFERENCE_DEPENDENCY_INVALID = "integrator_reference_dependency_invalid"
    INTEGRATOR_TARGET_MISMATCH = "integrator_target_mismatch"
    INTEGRATOR_CLUSTER_COUNT_INVALID = "integrator_cluster_count_invalid"
    INTEGRATOR_FORBIDDEN_REGISTRY = "integrator_forbidden_registry"
    INTEGRATOR_LIMIT_EXCEEDED = "integrator_limit_exceeded"


@dataclass(frozen=True, slots=True)
class EnrichmentJobContext:
    """Correlation and scope for one temporal enrichment attempt."""

    session_id: str
    target_host: str
    trigger_id: str
    event_id: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    caused_by_event_id: str | None = None
    command_id: str | None = None
    surface_hint: str | None = None
    anchors: tuple[str, ...] = ()
    interaction_focus: Mapping[str, object] = field(default_factory=dict)
    langfuse_session_id: str | None = None
    username: str | None = None
    profile_name: str | None = None
    mode: EnrichmentMode = EnrichmentMode.FOCUSED
    runtime_provenance: RuntimeProvenance | None = None

    def __post_init__(self) -> None:
        try:
            mode = EnrichmentMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be a supported enrichment mode") from exc
        object.__setattr__(self, "mode", mode)
        for name in ("session_id", "target_host", "trigger_id", "event_id", "job_id"):
            require_nonempty_text(getattr(self, name), name)
        for name in (
            "correlation_id",
            "caused_by_event_id",
            "command_id",
            "surface_hint",
            "langfuse_session_id",
            "username",
            "profile_name",
        ):
            value = getattr(self, name)
            if value is not None:
                require_nonempty_text(value, name)
        anchors = validated_text_tuple(
            self.anchors,
            location="anchors",
            maximum=MAX_COORDINATOR_ANCHORS,
        )
        object.__setattr__(self, "anchors", anchors)
        if not isinstance(self.interaction_focus, Mapping):
            raise TypeError("interaction_focus must be an object")
        try:
            serialized_focus = canonical_json(dict(self.interaction_focus))
            plain_focus = json.loads(serialized_focus)
        except (TypeError, ValueError) as exc:
            raise ValueError("interaction_focus must contain JSON values") from exc
        if len(serialized_focus) > _MAX_INTERACTION_FOCUS_CHARS:
            raise ValueError("interaction_focus exceeds its bounded size")
        object.__setattr__(self, "interaction_focus", plain_focus)

    @property
    def thread_id(self) -> str:
        """A checkpoint namespace unique to this job, not the whole session."""

        return f"{self.session_id}:enrichment:{self.job_id}"


@dataclass(frozen=True, slots=True)
class EnrichmentModelFailure:
    """Bounded provider failure propagated to the owning runtime."""

    role: str
    provider: str
    model: str
    classification: ProviderFailureClassification

    def to_log_fields(self) -> dict[str, str | int]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            **self.classification.to_log_fields(),
        }


@dataclass(frozen=True, slots=True)
class EnrichmentIntegratorRefreshBasis:
    """Model-neutral state required to rerun only the Integrator once.

    This basis is assembled from already validated Coordinator and Expert
    results.  It deliberately excludes raw role output so a runtime refresh
    cannot accidentally reparse or expose model prose.
    """

    selected_references: tuple[EstablishedReference, ...]
    creative_latitude: CreativeLatitude
    eligible_expert_item_ids: tuple[str, ...]
    blocked_expert_item_ids: tuple[str, ...]
    unresolved_condition_ids: tuple[str, ...]
    presence_gap: dict[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentGraphResult:
    context: EnrichmentJobContext
    status: EnrichmentGraphStatus
    reason_code: EnrichmentGraphReason
    coordinator: CoordinatorDecision | None = None
    expert: ExpertReport | None = None
    patch: EnrichmentPatch | None = None
    initial_snapshot_sha256: str = ""
    current_snapshot_sha256: str | None = None
    rejection_detail: str | None = None
    model_failure: EnrichmentModelFailure | None = None
    integrator_refresh_basis: EnrichmentIntegratorRefreshBasis | None = field(
        default=None,
        repr=False,
        compare=False,
    )


class _EnrichmentGraphState(TypedDict, total=False):
    context: EnrichmentJobContext
    initial_snapshot: dict[str, object]
    initial_snapshot_sha256: str
    coordinator_coverage_summary: dict[str, int]
    coordinator_input: dict[str, object]
    presence_gap: dict[str, object]
    reference_catalog: tuple[EstablishedReference, ...]
    current_snapshot: dict[str, object]
    current_basis_host: dict[str, object]
    current_snapshot_sha256: str
    reference_bindings: list[dict[str, object]]
    coordinator_raw: str
    coordinator: CoordinatorDecision
    expert_brief: dict[str, object]
    selected_references: tuple[EstablishedReference, ...]
    creative_latitude: CreativeLatitude
    expert_raw: str
    expert: ExpertReport
    eligible_expert_item_ids: tuple[str, ...]
    blocked_expert_item_ids: tuple[str, ...]
    refresh_eligible_expert_item_ids: tuple[str, ...]
    refresh_blocked_expert_item_ids: tuple[str, ...]
    unresolved_condition_ids: tuple[str, ...]
    integrator_expert: dict[str, object]
    integrator_reference_bindings: list[dict[str, object]]
    integrator_raw: str
    integrator_output: EnrichmentIntegratorOutput
    patch: EnrichmentPatch
    status: EnrichmentGraphStatus
    reason_code: EnrichmentGraphReason
    rejection_detail: str
    model_failure: EnrichmentModelFailure
    integrator_refresh_attempt: int


class EnrichmentAgent:
    """Coordinator -> neutral Expert brief -> Integrator proposal graph.

    The injected ``capture_current_snapshot`` callback runs in the visible
    ``snapshot_current`` node after Expert completes.  A runtime can therefore
    take its second short lock at the correct point without hiding lock policy
    inside model code.
    """

    def __init__(
        self,
        *,
        coordinator_model: Any,
        expert_model: Any,
        integrator_model: Any,
        ambient_coordinator_model: Any | None = None,
        coordinator_model_name: str = "injected-coordinator",
        ambient_coordinator_model_name: str | None = None,
        expert_model_name: str = "injected-expert",
        integrator_model_name: str = "injected-integrator",
        coordinator_provider: str = "injected",
        ambient_coordinator_provider: str | None = None,
        expert_provider: str = "injected",
        integrator_provider: str = "injected",
        coordinator_prompt: str | None = None,
        ambient_coordinator_prompt: str | None = None,
        expert_prompt: str | None = None,
        integrator_prompt: str | None = None,
        coordinator_reasoning_effort: str | None = None,
        ambient_coordinator_reasoning_effort: str | None = None,
        expert_reasoning_effort: str | None = None,
        integrator_reasoning_effort: str | None = None,
        coordinator_verbosity: str | None = None,
        ambient_coordinator_verbosity: str | None = None,
        expert_verbosity: str | None = None,
        integrator_verbosity: str | None = None,
        coordinator_max_output_tokens: int | None = None,
        ambient_coordinator_max_output_tokens: int | None = None,
        expert_max_output_tokens: int | None = None,
        integrator_max_output_tokens: int | None = None,
        callbacks: Sequence[Any] = (),
    ) -> None:
        self._initialize(
            injected_role_runtimes(
                models=(
                    coordinator_model,
                    (
                        ambient_coordinator_model
                        if ambient_coordinator_model is not None
                        else coordinator_model
                    ),
                    expert_model,
                    integrator_model,
                ),
                model_names=(
                    coordinator_model_name,
                    ambient_coordinator_model_name or coordinator_model_name,
                    expert_model_name,
                    integrator_model_name,
                ),
                providers=(
                    coordinator_provider,
                    ambient_coordinator_provider or coordinator_provider,
                    expert_provider,
                    integrator_provider,
                ),
                prompts=(
                    coordinator_prompt,
                    ambient_coordinator_prompt,
                    expert_prompt,
                    integrator_prompt,
                ),
                reasoning_efforts=(
                    coordinator_reasoning_effort,
                    (
                        ambient_coordinator_reasoning_effort
                        if ambient_coordinator_reasoning_effort is not None
                        else coordinator_reasoning_effort
                    ),
                    expert_reasoning_effort,
                    integrator_reasoning_effort,
                ),
                verbosities=(
                    coordinator_verbosity,
                    (
                        ambient_coordinator_verbosity
                        if ambient_coordinator_verbosity is not None
                        else coordinator_verbosity
                    ),
                    expert_verbosity,
                    integrator_verbosity,
                ),
                max_output_tokens=(
                    coordinator_max_output_tokens,
                    (
                        ambient_coordinator_max_output_tokens
                        if ambient_coordinator_max_output_tokens is not None
                        else coordinator_max_output_tokens
                    ),
                    expert_max_output_tokens,
                    integrator_max_output_tokens,
                ),
            ),
            callbacks,
        )

    def _initialize(
        self,
        roles: Mapping[str, _RoleRuntime],
        callbacks: Sequence[Any],
    ) -> None:
        self._roles = dict(roles)
        self._callbacks = tuple(callbacks)
        self._snapshot_capture_lock = threading.Lock()
        self._snapshot_captures: dict[str, CurrentSnapshotCapture] = {}
        self._trusted_current_basis_hosts: dict[str, dict[str, object]] = {}
        self._trusted_current_authority_sha256: dict[str, str] = {}
        self._trusted_current_evidence_sha256: dict[str, str] = {}
        self.graph = self._build_graph()
        self.integrator_refresh_graph = self._build_integrator_refresh_graph()

    @property
    def provider_names(self) -> frozenset[str]:
        """Providers required by this graph, for process-local circuit policy."""

        return frozenset(
            role.provider.casefold()
            for role in self._roles.values()
            if role.provider.strip()
        )

    @classmethod
    def from_settings(
        cls,
        settings: EnrichmentSettings,
        *,
        api_key_resolver: ApiKeyResolver | None = None,
        callbacks: Sequence[Any] = (),
        model_factory: ModelFactory = build_chat_model,
        lazy_models: bool = False,
    ) -> EnrichmentAgent:
        """Build role models without coupling graph construction to ``__main__``."""

        roles = configured_role_runtimes(
            settings,
            api_key_resolver=api_key_resolver or (lambda _provider: None),
            model_factory=model_factory,
            lazy_models=lazy_models,
        )
        # Preserve complete configured descriptors, including provider controls.
        # __init__ serves callers that inject models without provider settings.
        agent = cls.__new__(cls)
        agent._initialize(roles, callbacks)
        return agent

    def invoke(
        self,
        context: EnrichmentJobContext,
        *,
        initial_snapshot: SnapshotInput,
        capture_current_snapshot: CurrentSnapshotCapture,
        callbacks: Any = (),
    ) -> EnrichmentGraphResult:
        """Run one proposal job against an initial and freshly captured snapshot."""

        if not callable(capture_current_snapshot):
            raise TypeError("capture_current_snapshot must be callable")
        initial_document = snapshot_document(initial_snapshot)
        initial_hash = snapshot_sha256(initial_snapshot, initial_document)
        try:
            coverage_summary = build_coordinator_coverage_summary(
                initial_document,
                target_host=context.target_host,
            )
        except RoleInputValidationError:
            # The visible context-preparation node owns structured rejection.
            # Keep the outer trace alive without bypassing that boundary.
            coverage_summary = {}
        invoke_callbacks: Any
        if callbacks and self._callbacks:
            from langchain_core.callbacks.manager import CallbackManager

            invoke_callbacks = CallbackManager.configure(
                inheritable_callbacks=callbacks,
                local_callbacks=list(self._callbacks),
            )
        elif callbacks:
            invoke_callbacks = callbacks
        else:
            invoke_callbacks = list(self._callbacks)
        config = enrichment_invoke_config(
            context,
            graph_version=ENRICHMENT_GRAPH_VERSION,
            callbacks=invoke_callbacks,
            coverage_summary=coverage_summary,
        )
        log_enrichment_event(
            "graph.started",
            context=_graph_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            enrichment_mode=context.mode.value,
            thread_id=context.thread_id,
            initial_snapshot_sha256=initial_hash,
            coverage_summary=coverage_summary,
        )
        with self._snapshot_capture_lock:
            if context.thread_id in self._snapshot_captures:
                raise RuntimeError(
                    f"enrichment job thread is already active: {context.thread_id}"
                )
            self._snapshot_captures[context.thread_id] = capture_current_snapshot
        current_authority_sha256: str | None = None
        current_evidence_sha256: str | None = None
        try:
            state = self.graph.invoke(
                {
                    "context": context,
                    "initial_snapshot": initial_document,
                    "initial_snapshot_sha256": initial_hash,
                    "coordinator_coverage_summary": coverage_summary,
                },
                config=config,
            )
        finally:
            with self._snapshot_capture_lock:
                self._snapshot_captures.pop(context.thread_id, None)
                self._trusted_current_basis_hosts.pop(context.thread_id, None)
                current_authority_sha256 = self._trusted_current_authority_sha256.pop(
                    context.thread_id,
                    None,
                )
                current_evidence_sha256 = self._trusted_current_evidence_sha256.pop(
                    context.thread_id,
                    None,
                )
        result = _result_from_state(state)
        result = self._bind_current_authority_dependency(
            context,
            result,
            current_authority_sha256=current_authority_sha256,
            current_evidence_sha256=current_evidence_sha256,
        )
        failure_fields = (
            result.model_failure.to_log_fields()
            if result.model_failure is not None
            else {}
        )
        log_enrichment_event(
            "graph.completed",
            context=_graph_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            enrichment_mode=context.mode.value,
            status=result.status.value,
            reason_code=result.reason_code.value,
            patch_clusters=(
                len(result.patch.clusters) if result.patch is not None else 0
            ),
            **failure_fields,
        )
        return result

    def refresh_integrator(
        self,
        context: EnrichmentJobContext,
        *,
        previous_result: EnrichmentGraphResult,
        initial_snapshot: SnapshotInput,
        capture_current_snapshot: CurrentSnapshotCapture,
        callbacks: Any = (),
        attempt: int = 1,
    ) -> EnrichmentGraphResult:
        """Rerun only the Integrator against one newly captured snapshot.

        The runtime may call this only after final application detects a
        foreground-evidence authority conflict.  The validated Coordinator
        decision and Expert report are reused; no planning or generation role
        is called again.  Bounding the number of attempts remains the runtime's
        responsibility and ``attempt`` is retained only for trace identity.
        """

        if attempt != 1:
            raise ValueError("Integrator refresh supports exactly attempt 1")
        if not callable(capture_current_snapshot):
            raise TypeError("capture_current_snapshot must be callable")
        if previous_result.context != context:
            raise ValueError("previous result belongs to another enrichment job")
        if previous_result.status is not EnrichmentGraphStatus.PATCH_PROPOSED:
            raise ValueError("Integrator refresh requires a proposed patch")
        coordinator = previous_result.coordinator
        expert = previous_result.expert
        basis = previous_result.integrator_refresh_basis
        if coordinator is None or expert is None or basis is None:
            raise ValueError("Integrator refresh basis is unavailable")

        initial_document = snapshot_document(initial_snapshot)
        initial_hash = snapshot_sha256(initial_snapshot, initial_document)
        if initial_hash != previous_result.initial_snapshot_sha256:
            raise ValueError("Integrator refresh initial snapshot changed")

        invoke_callbacks: Any
        if callbacks and self._callbacks:
            from langchain_core.callbacks.manager import CallbackManager

            invoke_callbacks = CallbackManager.configure(
                inheritable_callbacks=callbacks,
                local_callbacks=list(self._callbacks),
            )
        elif callbacks:
            invoke_callbacks = callbacks
        else:
            invoke_callbacks = list(self._callbacks)
        config = enrichment_invoke_config(
            context,
            graph_version=ENRICHMENT_GRAPH_VERSION,
            callbacks=invoke_callbacks,
        )
        refresh_thread_id = f"{context.thread_id}:integrator-refresh:{attempt}"
        config["configurable"]["thread_id"] = refresh_thread_id
        config["run_name"] = "dreamland-enrichment-integrator-refresh-graph"
        config["metadata"]["dreamland.enrichment.integrator_refresh_attempt"] = attempt
        config["metadata"]["dreamland.enrichment.integrator_refresh_thread_id"] = (
            refresh_thread_id
        )
        config["tags"].append("integrator-refresh")

        log_context = _graph_log_context(
            context,
            selected_surface=coordinator.surface,
        )
        log_enrichment_event(
            "integrator.refresh.graph_started",
            context=log_context,
            graph_version=ENRICHMENT_GRAPH_VERSION,
            integrator_refresh_attempt=attempt,
            thread_id=refresh_thread_id,
            initial_snapshot_sha256=initial_hash,
        )
        with self._snapshot_capture_lock:
            if refresh_thread_id in self._snapshot_captures:
                raise RuntimeError(
                    f"Integrator refresh thread is already active: {refresh_thread_id}"
                )
            self._snapshot_captures[refresh_thread_id] = capture_current_snapshot
        current_authority_sha256: str | None = None
        current_evidence_sha256: str | None = None
        try:
            state = self.integrator_refresh_graph.invoke(
                {
                    "context": context,
                    "initial_snapshot": initial_document,
                    "initial_snapshot_sha256": initial_hash,
                    "presence_gap": deepcopy(basis.presence_gap),
                    "coordinator": coordinator,
                    "selected_references": basis.selected_references,
                    "creative_latitude": basis.creative_latitude,
                    "expert": expert,
                    "eligible_expert_item_ids": (basis.eligible_expert_item_ids),
                    "blocked_expert_item_ids": basis.blocked_expert_item_ids,
                    "refresh_eligible_expert_item_ids": (
                        basis.eligible_expert_item_ids
                    ),
                    "refresh_blocked_expert_item_ids": (basis.blocked_expert_item_ids),
                    "unresolved_condition_ids": basis.unresolved_condition_ids,
                    "integrator_expert": _eligible_expert_payload(
                        expert,
                        basis.eligible_expert_item_ids,
                    ),
                    "integrator_reference_bindings": [],
                    "integrator_refresh_attempt": attempt,
                },
                config=config,
            )
        finally:
            with self._snapshot_capture_lock:
                self._snapshot_captures.pop(refresh_thread_id, None)
                self._trusted_current_basis_hosts.pop(refresh_thread_id, None)
                current_authority_sha256 = self._trusted_current_authority_sha256.pop(
                    refresh_thread_id,
                    None,
                )
                current_evidence_sha256 = self._trusted_current_evidence_sha256.pop(
                    refresh_thread_id,
                    None,
                )
        result = _result_from_state(state)
        result = self._bind_current_authority_dependency(
            context,
            result,
            current_authority_sha256=current_authority_sha256,
            current_evidence_sha256=current_evidence_sha256,
        )
        failure_fields = (
            result.model_failure.to_log_fields()
            if result.model_failure is not None
            else {}
        )
        log_enrichment_event(
            "integrator.refresh.graph_completed",
            context=log_context,
            graph_version=ENRICHMENT_GRAPH_VERSION,
            integrator_refresh_attempt=attempt,
            status=result.status.value,
            reason_code=result.reason_code.value,
            patch_clusters=(
                len(result.patch.clusters) if result.patch is not None else 0
            ),
            **failure_fields,
        )
        return result

    def _bind_current_authority_dependency(
        self,
        context: EnrichmentJobContext,
        result: EnrichmentGraphResult,
        *,
        current_authority_sha256: str | None,
        current_evidence_sha256: str | None,
    ) -> EnrichmentGraphResult:
        """Attach hidden race authority only after traced graph execution."""

        patch = result.patch
        coordinator = result.coordinator
        if patch is None or coordinator is None:
            return result
        cluster = patch.clusters[0]
        authority_reason = _active_session_authority_reason(
            selected_surface=coordinator.surface,
            contributions=cluster.contributions,
        )
        dependencies = list(cluster.authority_dependencies)
        if authority_reason is not None and current_authority_sha256 is None:
            log_enrichment_event(
                "authority.dependency_unavailable",
                level=logging.DEBUG,
                context=_graph_log_context(
                    context,
                    surface=coordinator.surface,
                ),
                policy_version=_FOREGROUND_AUTHORITY_POLICY_VERSION,
                authority_domain=_ACTIVE_SESSION_AUTHORITY_DOMAIN,
                binding_reason=authority_reason,
                reason="target_not_foreground",
            )
        elif authority_reason is not None:
            assert current_authority_sha256 is not None
            dependencies.append(
                EnrichmentAuthorityDependency(
                    domain="active_session",
                    expected_sha256=current_authority_sha256,
                )
            )
            log_enrichment_event(
                "authority.dependency_bound",
                context=_graph_log_context(
                    context,
                    surface=coordinator.surface,
                ),
                policy_version=_FOREGROUND_AUTHORITY_POLICY_VERSION,
                authority_domain=_ACTIVE_SESSION_AUTHORITY_DOMAIN,
                binding_reason=authority_reason,
                authority_dependency_count=1,
            )
        if current_evidence_sha256 is not None:
            dependencies.append(
                EnrichmentAuthorityDependency(
                    domain=FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN,
                    expected_sha256=current_evidence_sha256,
                )
            )
            log_enrichment_event(
                "authority.dependency_bound",
                context=_graph_log_context(
                    context,
                    surface=coordinator.surface,
                ),
                policy_version=FOREGROUND_EVIDENCE_POLICY_VERSION,
                authority_domain=FOREGROUND_EVIDENCE_AUTHORITY_DOMAIN,
                binding_reason="foreground_evidence_view",
                authority_dependency_count=1,
            )
        if tuple(dependencies) == cluster.authority_dependencies:
            return result
        guarded_cluster = replace(
            cluster,
            authority_dependencies=tuple(dependencies),
        )
        guarded_patch = replace(patch, clusters=(guarded_cluster,))
        return replace(result, patch=guarded_patch)

    def topology_mermaid(self, *, expanded: bool = True) -> str:
        """Return presentation-ready Mermaid for the explicit logical flow."""

        return self.graph.get_graph(xray=expanded).draw_mermaid()

    def integrator_refresh_topology_mermaid(
        self,
        *,
        expanded: bool = True,
    ) -> str:
        """Return Mermaid for the bounded Integrator-only recovery flow."""

        return self.integrator_refresh_graph.get_graph(xray=expanded).draw_mermaid()

    def _build_graph(self) -> Any:
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:  # pragma: no cover - required runtime dependency.
            raise RuntimeError(
                "EnrichmentAgent requires langgraph. Install project dependencies."
            ) from exc

        focus_coordinator_graph = StateGraph(_EnrichmentGraphState)
        focus_coordinator_graph.add_node(
            "call_focus_coordinator_model", self._focus_coordinator_model_node
        )
        focus_coordinator_graph.add_node(
            "parse_coordinator_output", self._coordinator_parse_node
        )
        focus_coordinator_graph.add_edge(START, "call_focus_coordinator_model")
        focus_coordinator_graph.add_edge(
            "call_focus_coordinator_model",
            "parse_coordinator_output",
        )
        focus_coordinator_graph.add_edge("parse_coordinator_output", END)

        ambient_coordinator_graph = StateGraph(_EnrichmentGraphState)
        ambient_coordinator_graph.add_node(
            "call_ambient_presence_coordinator_model",
            self._ambient_presence_coordinator_model_node,
        )
        ambient_coordinator_graph.add_node(
            "parse_coordinator_output",
            self._coordinator_parse_node,
        )
        ambient_coordinator_graph.add_edge(
            START,
            "call_ambient_presence_coordinator_model",
        )
        ambient_coordinator_graph.add_edge(
            "call_ambient_presence_coordinator_model",
            "parse_coordinator_output",
        )
        ambient_coordinator_graph.add_edge("parse_coordinator_output", END)

        expert_graph = StateGraph(_EnrichmentGraphState)
        expert_graph.add_node("call_expert_model", self._expert_model_node)
        expert_graph.add_node("parse_expert_output", self._expert_parse_node)
        expert_graph.add_edge(START, "call_expert_model")
        expert_graph.add_edge("call_expert_model", "parse_expert_output")
        expert_graph.add_edge("parse_expert_output", END)

        integrator_graph = StateGraph(_EnrichmentGraphState)
        integrator_graph.add_node("call_integrator_model", self._integrator_model_node)
        integrator_graph.add_node(
            "parse_integrator_output",
            self._integrator_parse_node,
        )
        integrator_graph.add_edge(START, "call_integrator_model")
        integrator_graph.add_edge("call_integrator_model", "parse_integrator_output")
        integrator_graph.add_edge("parse_integrator_output", END)

        graph = StateGraph(_EnrichmentGraphState)
        graph.add_node("snapshot_initial", self._snapshot_initial_node)
        graph.add_node(
            "prepare_coordinator_context",
            self._prepare_coordinator_context_node,
        )
        graph.add_node(
            "focus_coordinator_agent",
            focus_coordinator_graph.compile(),
        )
        graph.add_node(
            "ambient_presence_coordinator_agent",
            ambient_coordinator_graph.compile(),
        )
        graph.add_node("prepare_expert_brief", self._prepare_expert_brief_node)
        graph.add_node("expert_agent", expert_graph.compile())
        graph.add_node("snapshot_current", self._snapshot_current_node)
        graph.add_node(
            "resolve_current_references",
            self._resolve_current_references_node,
        )
        graph.add_node(
            "classify_expert_items",
            self._classify_expert_items_node,
        )
        graph.add_node("integrator_agent", integrator_graph.compile())
        graph.add_node(
            "normalize_integrator_output",
            self._normalize_integrator_output_node,
        )
        graph.add_node(
            "validate_integrator_output",
            self._validate_integrator_output_node,
        )
        graph.add_edge(START, "snapshot_initial")
        graph.add_edge("snapshot_initial", "prepare_coordinator_context")
        graph.add_conditional_edges(
            "prepare_coordinator_context",
            _route_after_coordinator_context,
            {
                "focus_coordinator_agent": "focus_coordinator_agent",
                "ambient_presence_coordinator_agent": (
                    "ambient_presence_coordinator_agent"
                ),
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "focus_coordinator_agent",
            _route_after_coordinator,
            {"prepare_expert_brief": "prepare_expert_brief", "end": END},
        )
        graph.add_conditional_edges(
            "ambient_presence_coordinator_agent",
            _route_after_coordinator,
            {"prepare_expert_brief": "prepare_expert_brief", "end": END},
        )
        graph.add_conditional_edges(
            "prepare_expert_brief",
            _route_after_expert_brief,
            {"expert_agent": "expert_agent", "end": END},
        )
        graph.add_conditional_edges(
            "expert_agent",
            _route_after_expert,
            {"classify_expert_items": "classify_expert_items", "end": END},
        )
        graph.add_conditional_edges(
            "classify_expert_items",
            _route_after_expert_item_classification,
            {
                "snapshot_current": "snapshot_current",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "snapshot_current",
            _route_after_current_snapshot,
            {
                "resolve_current_references": "resolve_current_references",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "resolve_current_references",
            _route_after_current_references,
            {"integrator_agent": "integrator_agent", "end": END},
        )
        graph.add_conditional_edges(
            "integrator_agent",
            _route_after_integrator,
            {
                "normalize_integrator_output": "normalize_integrator_output",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "normalize_integrator_output",
            _route_after_integrator_normalization,
            {
                "validate_integrator_output": "validate_integrator_output",
                "end": END,
            },
        )
        graph.add_edge("validate_integrator_output", END)
        return graph.compile()

    def _build_integrator_refresh_graph(self) -> Any:
        """Build the explicit one-shot Integrator recovery workflow."""

        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:  # pragma: no cover - required dependency.
            raise RuntimeError(
                "EnrichmentAgent requires langgraph. Install project dependencies."
            ) from exc

        integrator_graph = StateGraph(_EnrichmentGraphState)
        integrator_graph.add_node(
            "call_integrator_refresh_model",
            self._integrator_model_node,
        )
        integrator_graph.add_node(
            "parse_integrator_refresh_output",
            self._integrator_parse_node,
        )
        integrator_graph.add_edge(START, "call_integrator_refresh_model")
        integrator_graph.add_edge(
            "call_integrator_refresh_model",
            "parse_integrator_refresh_output",
        )
        integrator_graph.add_edge("parse_integrator_refresh_output", END)

        graph = StateGraph(_EnrichmentGraphState)
        graph.add_node(
            "snapshot_integrator_refresh",
            self._snapshot_current_node,
        )
        graph.add_node(
            "resolve_integrator_refresh_references",
            self._resolve_current_references_node,
        )
        graph.add_node(
            "integrator_refresh_agent",
            integrator_graph.compile(),
        )
        graph.add_node(
            "normalize_integrator_refresh_output",
            self._normalize_integrator_output_node,
        )
        graph.add_node(
            "validate_integrator_refresh_output",
            self._validate_integrator_output_node,
        )
        graph.add_edge(START, "snapshot_integrator_refresh")
        graph.add_conditional_edges(
            "snapshot_integrator_refresh",
            _route_after_current_snapshot,
            {
                "resolve_current_references": ("resolve_integrator_refresh_references"),
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "resolve_integrator_refresh_references",
            _route_after_current_references,
            {
                "integrator_agent": "integrator_refresh_agent",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "integrator_refresh_agent",
            _route_after_integrator,
            {
                "normalize_integrator_output": ("normalize_integrator_refresh_output"),
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "normalize_integrator_refresh_output",
            _route_after_integrator_normalization,
            {
                "validate_integrator_output": ("validate_integrator_refresh_output"),
                "end": END,
            },
        )
        graph.add_edge("validate_integrator_refresh_output", END)
        return graph.compile()

    def _snapshot_initial_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        context = state["context"]
        log_enrichment_event(
            "snapshot.accepted",
            context=_graph_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            snapshot="initial",
            snapshot_sha256=state["initial_snapshot_sha256"],
            snapshot_chars=len(canonical_json(state["initial_snapshot"])),
            **_foreground_evidence_log_fields(state["initial_snapshot"]),
        )
        return {}

    def _prepare_coordinator_context_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        context = state["context"]
        try:
            prepared: CoordinatorPromptInput = build_coordinator_prompt_input(
                state["initial_snapshot"],
                target_host=context.target_host,
                trigger=context.trigger_id,
                surface_hint=context.surface_hint,
                focus_hints=context.anchors,
                interaction_focus=context.interaction_focus,
                mode=context.mode,
                coverage_summary=(state.get("coordinator_coverage_summary") or None),
            )
        except RoleInputValidationError as exc:
            return _input_rejection_update(
                context,
                boundary="coordinator_context",
                reason=EnrichmentGraphReason.COORDINATOR_CONTEXT_INVALID,
                error=exc,
            )
        log_enrichment_event(
            "reference.catalog_prepared",
            context=_graph_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            available_count=len(prepared.references),
            omitted_count=prepared.omitted_references,
        )
        log_enrichment_event(
            "coordinator.context_prepared",
            context=_graph_log_context(context),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            enrichment_mode=context.mode.value,
            coverage_summary=prepared.payload["coverage_summary"],
            coverage_summary_sha256=hashlib.sha256(
                canonical_json(prepared.payload["coverage_summary"]).encode("utf-8")
            ).hexdigest(),
            prompt_chars=len(canonical_json(prepared.payload)),
            **_foreground_evidence_log_fields(prepared.payload),
            **_presence_gap_log_fields(prepared.payload.get("presence_gap")),
        )
        update: dict[str, object] = {
            "coordinator_input": prepared.payload,
            "reference_catalog": prepared.references,
        }
        presence_gap = prepared.payload.get("presence_gap")
        if isinstance(presence_gap, Mapping):
            update["presence_gap"] = deepcopy(dict(presence_gap))
        return update

    def _focus_coordinator_model_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        return self._call_role(
            self._roles["coordinator"],
            context,
            state["coordinator_input"],
            config,
        )

    def _ambient_presence_coordinator_model_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        result = self._call_role(
            self._roles["ambient_coordinator"],
            context,
            state["coordinator_input"],
            config,
        )
        raw = result.pop("ambient_coordinator_raw", None)
        if raw is not None:
            result["coordinator_raw"] = raw
        return result

    def _coordinator_parse_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        if state.get("reason_code") is EnrichmentGraphReason.COORDINATOR_MODEL_FAILED:
            return {}
        context = state["context"]
        coordinator_role = _coordinator_role(context)
        try:
            decision = parse_coordinator_decision(state.get("coordinator_raw", ""))
        except RoleOutputValidationError as exc:
            return _rejection_update(
                context,
                role=coordinator_role,
                reason=EnrichmentGraphReason.COORDINATOR_OUTPUT_INVALID,
                error=exc,
            )
        _log_role_parsed(
            context,
            coordinator_role,
            decision.status,
            selected_surface=decision.surface,
        )
        log_enrichment_event(
            "coordinator.decision",
            context=_graph_log_context(context, surface=decision.surface),
            enrichment_mode=context.mode.value,
            coordinator_role=coordinator_role,
            status=decision.status,
            reason_preview=_log_preview(decision.reason),
            reason_chars=len(decision.reason),
            expertise_domain=(
                decision.expertise.domain if decision.expertise is not None else None
            ),
            expertise_perspective=(
                decision.expertise.perspective
                if decision.expertise is not None
                else None
            ),
            anchor_count=len(decision.anchors),
        )
        log_enrichment_event(
            "coordinator.decision_detail",
            level=logging.DEBUG,
            context=_graph_log_context(context, surface=decision.surface),
            enrichment_mode=context.mode.value,
            coordinator_role=coordinator_role,
            anchors=decision.anchors,
        )
        if decision.status == "no_op":
            log_enrichment_event(
                "workflow.no_op",
                context=_graph_log_context(context),
                stage="coordinator",
                reason_code=EnrichmentGraphReason.COORDINATOR_NO_OP.value,
            )
            return {
                "coordinator": decision,
                "status": EnrichmentGraphStatus.NO_OP,
                "reason_code": EnrichmentGraphReason.COORDINATOR_NO_OP,
            }
        return {"coordinator": decision}

    def _prepare_expert_brief_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        context = state["context"]
        decision = state["coordinator"]
        if (
            decision.surface is None
            or decision.expertise is None
            or decision.expert_task is None
        ):
            raise RuntimeError("work decision is missing its Expert scope")
        try:
            brief, selected, latitude = build_expert_brief(
                state["initial_snapshot"],
                target_host=context.target_host,
                surface=decision.surface,
                expertise=decision.expertise.to_payload(),
                question=decision.expert_task,
                selected_reference_ids=decision.anchors,
                reference_catalog=state["reference_catalog"],
                mode=context.mode,
            )
        except RoleInputValidationError as exc:
            return _input_rejection_update(
                context,
                boundary="expert_brief",
                reason=EnrichmentGraphReason.EXPERT_BRIEF_INVALID,
                error=exc,
            )
        observations = cast(list[object], brief["observations"])
        (
            command_evidence_count,
            same_surface_command_evidence_count,
            command_evidence_chars,
            command_evidence_truncated_count,
        ) = _expert_command_evidence_metrics(
            observations,
            selected_surface=decision.surface,
        )
        log_enrichment_event(
            "expert_brief.prepared",
            context=_graph_log_context(context, surface=decision.surface),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            reference_count=len(selected),
            observation_count=len(observations),
            command_evidence_count=command_evidence_count,
            same_surface_command_evidence_count=(same_surface_command_evidence_count),
            command_evidence_chars=command_evidence_chars,
            command_evidence_truncated_count=command_evidence_truncated_count,
            command_evidence_selection="surface_then_recency",
            prompt_chars=len(canonical_json(brief)),
            latitude_configured=(
                latitude.configured_value
                if not latitude.defaulted
                else ("missing" if latitude.configured_value is None else "invalid")
            ),
            latitude_effective=latitude.level,
            latitude_defaulted=latitude.defaulted,
            latitude_template_version=latitude.template_version,
            latitude_instruction_sha256=latitude.instruction_sha256,
            candidate_limit=latitude.candidate_limit,
            relation_limit=latitude.relation_limit,
            relation_policy_version=EXPERT_RELATION_POLICY_VERSION,
            latitude_instruction_chars=len(latitude.instruction),
            **expert_task_policy_log_fields(mode=context.mode),
        )
        selected_kind_counts: dict[str, int] = {}
        for reference in selected:
            selected_kind_counts[reference.kind] = (
                selected_kind_counts.get(reference.kind, 0) + 1
            )
        log_enrichment_event(
            "reference.selection_evaluated",
            context=_graph_log_context(context, surface=decision.surface),
            available_count=len(state["reference_catalog"]),
            selected_count=len(selected),
            selected_kind_counts=selected_kind_counts,
        )
        log_enrichment_event(
            "reference.selection_detail",
            level=logging.DEBUG,
            context=_graph_log_context(context, surface=decision.surface),
            selected_ids=[item.id for item in selected],
        )
        return {
            "expert_brief": brief,
            "selected_references": selected,
            "creative_latitude": latitude,
        }

    def _expert_model_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        return self._call_role(
            self._roles["expert"],
            context,
            state["expert_brief"],
            config,
            selected_surface=state["coordinator"].surface,
        )

    def _expert_parse_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        if state.get("reason_code") is EnrichmentGraphReason.EXPERT_MODEL_FAILED:
            return {}
        context = state["context"]
        try:
            parsed = parse_expert_report_with_relation_salvage(
                state.get("expert_raw", ""),
                reference_ids=tuple(item.id for item in state["selected_references"]),
                max_items=state["creative_latitude"].candidate_limit,
                max_relations=state["creative_latitude"].relation_limit,
            )
        except RoleOutputValidationError as exc:
            return _rejection_update(
                context,
                role="expert",
                reason=EnrichmentGraphReason.EXPERT_OUTPUT_INVALID,
                error=exc,
            )
        report = parsed.report
        latitude = state["creative_latitude"]
        rejected_relation_counts: dict[str, int] = {}
        for rejection in parsed.rejected_relations:
            rejected_relation_counts[rejection.rule] = (
                rejected_relation_counts.get(rejection.rule, 0) + 1
            )
        relation_received_count = len(report.relations) + len(parsed.rejected_relations)
        relation_duplicate_count = rejected_relation_counts.get(
            "duplicate_relation",
            0,
        )
        relation_overflow_count = rejected_relation_counts.get(
            "relation_limit_exceeded",
            0,
        )
        relation_invalid_count = max(
            0,
            len(parsed.rejected_relations)
            - relation_duplicate_count
            - relation_overflow_count,
        )
        log_enrichment_event(
            "expert.relations_evaluated",
            level=(logging.WARNING if parsed.rejected_relations else logging.DEBUG),
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            relation_policy_version=EXPERT_RELATION_POLICY_VERSION,
            relation_limit=latitude.relation_limit,
            relation_received_count=relation_received_count,
            relation_accepted_count=len(report.relations),
            relation_invalid_count=relation_invalid_count,
            relation_duplicate_count=relation_duplicate_count,
            relation_overflow_count=relation_overflow_count,
            relation_discarded_count=len(parsed.rejected_relations),
            reason_counts=rejected_relation_counts,
        )
        for rejection in parsed.rejected_relations:
            log_enrichment_event(
                "expert.relation_discarded",
                level=logging.DEBUG,
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                relation_index=rejection.index,
                relation_policy_version=EXPERT_RELATION_POLICY_VERSION,
                relation_limit=latitude.relation_limit,
                rule=rejection.rule,
                validation_code=rejection.reason_code,
                location=rejection.location,
            )
        _log_role_parsed(
            context,
            "expert",
            report.status,
            selected_surface=state["coordinator"].surface,
        )
        log_enrichment_event(
            "expert.report",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            status=report.status,
            condition_count=len(report.conditions),
            item_count=len(report.items),
            candidate_limit=latitude.candidate_limit,
            unused_candidate_capacity=max(
                0,
                latitude.candidate_limit - len(report.items),
            ),
            candidate_limit_reached=(len(report.items) == latitude.candidate_limit),
            gate_count=len(report.gates),
            relation_count=len(report.relations),
            relation_policy_version=EXPERT_RELATION_POLICY_VERSION,
            relation_limit=latitude.relation_limit,
            relation_received_count=relation_received_count,
            relation_accepted_count=len(report.relations),
            relation_invalid_count=relation_invalid_count,
            relation_duplicate_count=relation_duplicate_count,
            relation_overflow_count=relation_overflow_count,
            relation_discarded_count=len(parsed.rejected_relations),
            origin_counts=_candidate_origin_counts(report.items),
            basis_reference_count=len(
                {reference_id for item in report.items for reference_id in item.basis}
            ),
        )
        selected_ids = tuple(item.id for item in state["selected_references"])
        used_id_set = {
            reference_id for item in report.items for reference_id in item.basis
        }
        used_ids = [item for item in selected_ids if item in used_id_set]
        unused_ids = [item for item in selected_ids if item not in used_id_set]
        log_enrichment_event(
            "reference.usage_evaluated",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            usage_basis="candidate_basis",
            selected_count=len(selected_ids),
            used_count=len(used_ids),
            unused_count=len(unused_ids),
        )
        log_enrichment_event(
            "reference.usage_detail",
            level=logging.DEBUG,
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            used_ids=used_ids,
            unused_ids=unused_ids,
        )
        if report.status == "insufficient":
            log_enrichment_event(
                "workflow.no_op",
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                stage="expert",
                reason_code=EnrichmentGraphReason.EXPERT_INSUFFICIENT.value,
            )
            return {
                "expert": report,
                "status": EnrichmentGraphStatus.NO_OP,
                "reason_code": EnrichmentGraphReason.EXPERT_INSUFFICIENT,
            }
        return {"expert": report}

    def _snapshot_current_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        selected_surface = state["coordinator"].surface
        started = time.perf_counter()
        log_enrichment_event(
            "snapshot.capture_started",
            context=_graph_log_context(
                context,
                selected_surface=selected_surface,
            ),
            snapshot="current",
        )
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        try:
            with self._snapshot_capture_lock:
                capture = self._snapshot_captures.get(thread_id)
            if capture is None:
                raise RuntimeError("current snapshot capture is unavailable")
            snapshot = capture(context)
        except Exception as exc:
            elapsed_ms = _elapsed_ms(started)
            log_enrichment_event(
                "snapshot.capture_failed",
                level=logging.WARNING,
                context=_graph_log_context(
                    context,
                    selected_surface=selected_surface,
                ),
                snapshot="current",
                reason_code=(
                    EnrichmentGraphReason.CURRENT_SNAPSHOT_CAPTURE_FAILED.value
                ),
                error_type=type(exc).__name__,
                latency_ms=elapsed_ms,
            )
            return {
                "status": EnrichmentGraphStatus.REJECTED,
                "reason_code": EnrichmentGraphReason.CURRENT_SNAPSHOT_CAPTURE_FAILED,
                "rejection_detail": type(exc).__name__,
            }
        try:
            document = snapshot_document(snapshot)
            basis_host = basis_host_from_snapshot(document, context.target_host)
            snapshot_hash = snapshot_sha256(snapshot, document)
            trusted_basis_host = (
                deepcopy(snapshot.trusted_target_host)
                if isinstance(snapshot, EnrichmentContextProjection)
                else deepcopy(basis_host)
            )
        except Exception as exc:
            elapsed_ms = _elapsed_ms(started)
            log_enrichment_event(
                "snapshot.rejected",
                level=logging.WARNING,
                context=_graph_log_context(
                    context,
                    selected_surface=selected_surface,
                ),
                snapshot="current",
                reason_code=EnrichmentGraphReason.CURRENT_SNAPSHOT_INVALID.value,
                error_type=type(exc).__name__,
                latency_ms=elapsed_ms,
            )
            return {
                "status": EnrichmentGraphStatus.REJECTED,
                "reason_code": EnrichmentGraphReason.CURRENT_SNAPSHOT_INVALID,
                "rejection_detail": type(exc).__name__,
            }
        with self._snapshot_capture_lock:
            self._trusted_current_basis_hosts[thread_id] = trusted_basis_host
            if (
                isinstance(snapshot, EnrichmentContextProjection)
                and snapshot.foreground_authority_sha256 is not None
            ):
                self._trusted_current_authority_sha256[thread_id] = (
                    snapshot.foreground_authority_sha256
                )
            else:
                self._trusted_current_authority_sha256.pop(thread_id, None)
            if isinstance(snapshot, EnrichmentContextProjection):
                self._trusted_current_evidence_sha256[thread_id] = (
                    snapshot.foreground_evidence_sha256
                )
            else:
                self._trusted_current_evidence_sha256.pop(thread_id, None)
        log_enrichment_event(
            "snapshot.accepted",
            context=_graph_log_context(
                context,
                selected_surface=selected_surface,
            ),
            snapshot="current",
            snapshot_sha256=snapshot_hash,
            snapshot_chars=len(canonical_json(document)),
            latency_ms=_elapsed_ms(started),
            foreground_authority_present=(
                isinstance(snapshot, EnrichmentContextProjection)
                and snapshot.foreground_authority_sha256 is not None
            ),
            **_foreground_evidence_log_fields(document),
        )
        return {
            "current_snapshot": document,
            "current_basis_host": basis_host,
            "current_snapshot_sha256": snapshot_hash,
        }

    def _resolve_current_references_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        context = state["context"]
        eligible_item_ids = state["eligible_expert_item_ids"]
        eligible_ids = set(eligible_item_ids)
        eligible_items = tuple(
            item for item in state["expert"].items if item.id in eligible_ids
        )
        required_reference_ids = {
            reference_id for item in eligible_items for reference_id in item.basis
        }
        required_references = tuple(
            reference
            for reference in state["selected_references"]
            if reference.id in required_reference_ids
        )
        try:
            initial_basis_host = basis_host_from_snapshot(
                state["initial_snapshot"],
                context.target_host,
            )
        except ValueError as exc:
            return {
                "status": EnrichmentGraphStatus.REJECTED,
                "reason_code": EnrichmentGraphReason.CURRENT_REFERENCE_INVALID,
                "rejection_detail": type(exc).__name__,
            }

        bindings_by_id: dict[str, dict[str, object]] = {}
        unavailable_reasons: dict[str, str] = {}
        for reference in required_references:
            initial_binding = resolve_current_reference_bindings(
                initial_basis_host,
                (reference,),
            )[0]
            try:
                current_binding = resolve_current_reference_bindings(
                    state["current_basis_host"],
                    (reference,),
                )[0]
            except RoleInputValidationError as exc:
                if exc.reason_code != "reference_not_current":
                    return _input_rejection_update(
                        context,
                        boundary="current_references",
                        reason=EnrichmentGraphReason.CURRENT_REFERENCE_INVALID,
                        error=exc,
                    )
                unavailable_reasons[reference.id] = "basis_reference_missing"
                continue
            if canonical_json(initial_binding["expected"]) != canonical_json(
                current_binding["expected"]
            ):
                unavailable_reasons[reference.id] = "basis_reference_changed"
                continue
            bindings_by_id[reference.id] = current_binding

        retained_item_ids: list[str] = []
        newly_blocked_item_ids: list[str] = []
        item_decisions: list[dict[str, object]] = []
        currentness_reason_counts: dict[str, int] = {}
        for item in eligible_items:
            unavailable = [
                reference_id
                for reference_id in item.basis
                if reference_id in unavailable_reasons
            ]
            if unavailable:
                newly_blocked_item_ids.append(item.id)
                reasons = {
                    unavailable_reasons[reference_id] for reference_id in unavailable
                }
                reason_code = (
                    "basis_reference_missing"
                    if "basis_reference_missing" in reasons
                    else "basis_reference_changed"
                )
                outcome = "blocked"
            else:
                retained_item_ids.append(item.id)
                reason_code = "basis_current"
                outcome = "accepted"
            currentness_reason_counts[reason_code] = (
                currentness_reason_counts.get(reason_code, 0) + 1
            )
            item_decisions.append(
                {
                    "item_id": item.id,
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "unavailable_reference_ids": unavailable,
                }
            )

        retained_ids = tuple(retained_item_ids)
        blocked_ids = (
            *state["blocked_expert_item_ids"],
            *newly_blocked_item_ids,
        )
        retained_reference_ids = {
            reference_id
            for item in eligible_items
            if item.id in retained_ids
            for reference_id in item.basis
        }
        bindings = [
            bindings_by_id[reference.id]
            for reference in required_references
            if reference.id in retained_reference_ids
        ]
        log_context = _graph_log_context(
            context,
            surface=state["coordinator"].surface,
        )
        log_enrichment_event(
            "reference.currentness_evaluated",
            context=log_context,
            candidate_count=len(eligible_items),
            retained_count=len(retained_ids),
            blocked_count=len(newly_blocked_item_ids),
            reference_count=len(required_references),
            missing_count=sum(
                reason == "basis_reference_missing"
                for reason in unavailable_reasons.values()
            ),
            changed_count=sum(
                reason == "basis_reference_changed"
                for reason in unavailable_reasons.values()
            ),
            reason_counts=currentness_reason_counts,
        )
        log_enrichment_event(
            "reference.currentness_detail",
            level=logging.DEBUG,
            context=log_context,
            decisions=item_decisions,
            unavailable_references=unavailable_reasons,
        )
        log_enrichment_event(
            "reference.bindings_resolved",
            context=log_context,
            graph_version=ENRICHMENT_GRAPH_VERSION,
            reference_count=len(bindings),
            candidate_basis_count=len(retained_reference_ids),
        )
        update: dict[str, object] = {
            "reference_bindings": bindings,
            "integrator_reference_bindings": bindings,
            "eligible_expert_item_ids": retained_ids,
            "blocked_expert_item_ids": blocked_ids,
            "integrator_expert": _eligible_expert_payload(
                state["expert"],
                retained_ids,
            ),
        }
        if not retained_ids:
            log_enrichment_event(
                "workflow.no_op",
                context=log_context,
                stage="current_reference_filter",
                reason_code=EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS.value,
                blocked_item_count=len(newly_blocked_item_ids),
            )
            update.update(
                {
                    "status": EnrichmentGraphStatus.NO_OP,
                    "reason_code": EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS,
                }
            )
        return update

    def _classify_expert_items_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        """Apply deterministic latitude, prerequisite, and exact-novelty rules."""

        context = state["context"]
        report = state["expert"]
        latitude = state["creative_latitude"]
        blocked_set = {gate.item_id for gate in report.gates}
        eligible_ids_list: list[str] = []
        blocked_ids_list: list[str] = []
        item_decisions: list[dict[str, object]] = []
        item_reason_counts: dict[str, int] = {}
        for item in report.items:
            reason_code: str | None = None
            if item.id in blocked_set:
                reason_code = "condition_unresolved"
            elif item.origin not in latitude.allowed_origins:
                reason_code = "origin_not_allowed"
            else:
                reason_code = _candidate_restatement_reason(
                    item,
                    state["selected_references"],
                )
            if reason_code is None:
                eligible_ids_list.append(item.id)
                outcome = "accepted"
                reason_code = "candidate_allowed"
            else:
                blocked_ids_list.append(item.id)
                outcome = "blocked"
            item_reason_counts[reason_code] = item_reason_counts.get(reason_code, 0) + 1
            item_decisions.append(
                {
                    "item_id": item.id,
                    "origin": item.origin,
                    "basis_count": len(item.basis),
                    "outcome": outcome,
                    "reason_code": reason_code,
                }
            )
        eligible_ids = tuple(eligible_ids_list)
        blocked_ids = tuple(blocked_ids_list)
        unresolved_condition_ids = tuple(
            condition.id for condition in report.conditions
        )
        condition_reason_counts = (
            {"not_machine_verifiable": len(unresolved_condition_ids)}
            if unresolved_condition_ids
            else {}
        )

        log_context = _graph_log_context(
            context,
            surface=state["coordinator"].surface,
        )
        log_enrichment_event(
            "condition.resolution_evaluated",
            context=log_context,
            condition_count=len(report.conditions),
            established_count=0,
            unresolved_count=len(unresolved_condition_ids),
            reason_counts=condition_reason_counts,
        )
        log_enrichment_event(
            "condition.resolution_detail",
            level=logging.DEBUG,
            context=log_context,
            decisions=[
                {
                    "condition_id": condition_id,
                    "status": "unresolved",
                    "reason_code": "not_machine_verifiable",
                }
                for condition_id in unresolved_condition_ids
            ],
        )
        log_enrichment_event(
            "expert.items_evaluated",
            context=log_context,
            item_count=len(report.items),
            accepted_count=len(eligible_ids),
            discarded_count=len(blocked_ids),
            latitude_effective=latitude.level,
            candidate_limit=latitude.candidate_limit,
            origin_counts=_candidate_origin_counts(report.items),
            reason_counts=item_reason_counts,
        )
        log_enrichment_event(
            "expert.item_decisions",
            level=logging.DEBUG,
            context=log_context,
            eligible_item_ids=eligible_ids,
            blocked_item_ids=blocked_ids,
            decisions=item_decisions,
        )
        update: dict[str, object] = {
            "eligible_expert_item_ids": eligible_ids,
            "blocked_expert_item_ids": blocked_ids,
            "refresh_eligible_expert_item_ids": eligible_ids,
            "refresh_blocked_expert_item_ids": blocked_ids,
            "unresolved_condition_ids": unresolved_condition_ids,
            "integrator_expert": _eligible_expert_payload(report, eligible_ids),
        }
        if not eligible_ids:
            log_enrichment_event(
                "workflow.no_op",
                context=log_context,
                stage="candidate_filter",
                reason_code=EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS.value,
                blocked_item_count=len(blocked_ids),
            )
            update.update(
                {
                    "status": EnrichmentGraphStatus.NO_OP,
                    "reason_code": EnrichmentGraphReason.EXPERT_NO_ELIGIBLE_ITEMS,
                }
            )
        return update

    def _integrator_model_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        latitude = state["creative_latitude"]
        integration_guidance = latitude.integration_payload()
        ambient_suffix = (
            _AMBIENT_INTEGRATOR_PROMPT_SUFFIX
            if context.mode is EnrichmentMode.AMBIENT_PRESENCE
            else ""
        )
        log_enrichment_event(
            "integrator.guidance_prepared",
            context=_graph_log_context(
                context,
                selected_surface=state["coordinator"].surface,
            ),
            latitude_effective=latitude.level,
            latitude_template_version=latitude.template_version,
            patch_contribution_limit=MAX_PATCH_CONTRIBUTIONS,
            integration_instruction_sha256=(latitude.integration_instruction_sha256),
            integration_instruction_chars=len(latitude.integration_instruction),
            **(
                {
                    "ambient_integration_policy_version": (
                        AMBIENT_HUMAN_INTEGRATION_POLICY_VERSION
                    ),
                    "ambient_integration_policy_sha256": (
                        _AMBIENT_INTEGRATOR_PROMPT_SUFFIX_SHA256
                    ),
                    "ambient_integration_policy_chars": len(ambient_suffix),
                }
                if ambient_suffix
                else {}
            ),
        )
        payload: dict[str, object] = {
            "integration_scope": {
                "target_host": context.target_host,
                "surface": state["coordinator"].surface,
            },
            "integration_guidance": integration_guidance,
            "expert": state["integrator_expert"],
            "eligibility": {
                "eligible_item_ids": list(state["eligible_expert_item_ids"]),
                "blocked_item_count": len(state["blocked_expert_item_ids"]),
                "unresolved_condition_count": len(state["unresolved_condition_ids"]),
            },
            "reference_bindings": state["integrator_reference_bindings"],
            "foreground_authority": state["current_snapshot"].get(
                "foreground_authority"
            ),
            "current_snapshot": state["current_snapshot"],
        }
        if context.mode is EnrichmentMode.AMBIENT_PRESENCE:
            presence_gap = state.get("presence_gap")
            if isinstance(presence_gap, Mapping):
                payload["presence_gap"] = deepcopy(dict(presence_gap))
        return self._call_role(
            self._roles["integrator"],
            context,
            payload,
            config,
            selected_surface=state["coordinator"].surface,
            system_prompt_suffix=ambient_suffix,
        )

    def _integrator_parse_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        context = state["context"]
        if state.get("reason_code") is EnrichmentGraphReason.INTEGRATOR_MODEL_FAILED:
            return {}
        try:
            output = enrichment_integrator_output_from_json(
                state.get("integrator_raw", ""),
                basis_host=state["current_basis_host"],
            )
        except EnrichmentPatchValidationError as exc:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_OUTPUT_INVALID,
                detail=f"{exc.reason_code}@{exc.location}",
            )
        log_enrichment_event(
            "integrator.decision",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            status="no_op" if isinstance(output, EnrichmentNoOp) else "patch",
            reason_code=(
                output.reason_code if isinstance(output, EnrichmentNoOp) else None
            ),
        )
        return {"integrator_output": output}

    def _normalize_integrator_output_node(
        self,
        state: _EnrichmentGraphState,
    ) -> dict[str, object]:
        output = state["integrator_output"]
        if isinstance(output, EnrichmentNoOp):
            return {}
        context = state["context"]
        result: IntegratorNormalizationResult = normalize_integrator_patch(output)
        stats = result.stats
        changed = (
            stats.pruned_array_field_count > 0 or stats.dropped_contribution_count > 0
        )
        log_enrichment_event(
            "integrator.output_normalized",
            level=logging.INFO if changed else logging.DEBUG,
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            policy_version=result.policy_version,
            changed=changed,
            received_contribution_count=stats.received_contribution_count,
            retained_contribution_count=stats.retained_contribution_count,
            dropped_contribution_count=stats.dropped_contribution_count,
            array_affected_contribution_count=(stats.array_affected_contribution_count),
            pruned_array_field_count=stats.pruned_array_field_count,
            pruned_empty_object_count=stats.pruned_empty_object_count,
            all_contributions_pruned=result.all_contributions_pruned,
        )
        for decision in result.contributions:
            if decision.status == "unchanged":
                continue
            log_enrichment_event(
                (
                    "integrator.contribution_discarded"
                    if decision.status == "dropped"
                    else "integrator.contribution_normalized"
                ),
                level=(logging.INFO if decision.status == "dropped" else logging.DEBUG),
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                policy_version=result.policy_version,
                cluster_index=decision.cluster_index,
                contribution_index=decision.contribution_index,
                contribution_id=decision.contribution_id,
                reason="forbidden_array",
                pruned_array_field_count=decision.pruned_array_field_count,
                pruned_empty_object_count=decision.pruned_empty_object_count,
                pruned_array_paths=[list(path) for path in decision.pruned_array_paths],
                pruned_empty_object_paths=[
                    list(path) for path in decision.pruned_empty_object_paths
                ],
                structural_paths_truncated=decision.structural_paths_truncated,
            )
        normalized_output: EnrichmentIntegratorOutput
        if result.patch is None:
            normalized_output = EnrichmentNoOp("no_supported_contribution")
        else:
            normalized_output = result.patch
        return {"integrator_output": normalized_output}

    def _validate_integrator_output_node(
        self,
        state: _EnrichmentGraphState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        context = state["context"]
        output = state["integrator_output"]
        if isinstance(output, EnrichmentNoOp):
            log_enrichment_event(
                "workflow.no_op",
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                stage="integrator",
                reason_code=output.reason_code,
            )
            return {
                "status": EnrichmentGraphStatus.NO_OP,
                "reason_code": EnrichmentGraphReason.INTEGRATOR_NO_OP,
            }
        patch = output
        if patch.target_host != context.target_host:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_TARGET_MISMATCH,
                detail="target_host",
            )
        if len(patch.clusters) != 1:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_CLUSTER_COUNT_INVALID,
                detail="clusters",
            )
        cluster = patch.clusters[0]
        if len(cluster.contributions) > MAX_PATCH_CONTRIBUTIONS:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_LIMIT_EXCEEDED,
                detail="contributions",
            )
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        with self._snapshot_capture_lock:
            trusted_basis_host = self._trusted_current_basis_hosts.get(thread_id)
        if trusted_basis_host is None:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_OUTPUT_INVALID,
                detail="trusted_basis_unavailable",
            )
        known_item_ids = {item.id for item in state["expert"].items}
        eligible_item_ids = set(state["eligible_expert_item_ids"])
        contribution_sources: dict[str, list[str]] = {}
        for contribution_index, contribution in enumerate(cluster.contributions):
            contribution_sources[contribution.id] = list(contribution.source_item_ids)
            for source_index, source_item_id in enumerate(contribution.source_item_ids):
                location = (
                    f"$.clusters[0].contributions[{contribution_index}]"
                    f".source_item_ids[{source_index}]"
                )
                if source_item_id not in known_item_ids:
                    return _provenance_rejection_update(
                        context,
                        contribution_id=contribution.id,
                        source_item_id=source_item_id,
                        reason_code="source_item_unknown",
                        location=location,
                    )
                if source_item_id not in eligible_item_ids:
                    return _provenance_rejection_update(
                        context,
                        contribution_id=contribution.id,
                        source_item_id=source_item_id,
                        reason_code="source_item_blocked",
                        location=location,
                    )
        used_source_item_ids = {
            source_id
            for source_ids in contribution_sources.values()
            for source_id in source_ids
        }
        item_by_id = {item.id: item for item in state["expert"].items}
        required_reference_ids = {
            reference_id
            for source_item_id in used_source_item_ids
            for reference_id in item_by_id[source_item_id].basis
        }
        required_bindings = [
            binding
            for binding in state["integrator_reference_bindings"]
            if binding.get("id") in required_reference_ids
        ]
        for binding in required_bindings:
            reference_id = cast(str, binding["id"])
            required_path = tuple(cast(list[str], binding["path"]))
            required_expected = binding["expected"]
            matching_path_dependencies = [
                dependency
                for dependency in cluster.dependencies
                if dependency.path == required_path
            ]
            if not any(
                canonical_json(dependency.expected) == canonical_json(required_expected)
                for dependency in matching_path_dependencies
            ):
                return _reference_dependency_rejection_update(
                    context,
                    reference_id=reference_id,
                    path=required_path,
                    reason_code=(
                        "required_reference_dependency_mismatch"
                        if matching_path_dependencies
                        else "required_reference_dependency_missing"
                    ),
                )
        try:
            trusted_dependency_hashes = _trusted_dependency_hashes(
                trusted_basis_host,
                cluster.dependencies,
            )
        except ValueError:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_OUTPUT_INVALID,
                detail="trusted_dependency_path_invalid",
            )
        patch = replace(
            patch,
            trusted_dependency_hashes=trusted_dependency_hashes,
        )
        log_enrichment_event(
            "patch.reference_dependencies_validated",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            required_count=len(required_bindings),
            declared_count=len(cluster.dependencies),
            trusted_fingerprint_count=len(trusted_dependency_hashes),
            sourced_candidate_count=len(used_source_item_ids),
        )
        log_enrichment_event(
            "patch.reference_dependency_detail",
            level=logging.DEBUG,
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            required_reference_ids=[
                cast(str, binding["id"]) for binding in required_bindings
            ],
            required_paths=[
                cast(list[str], binding["path"]) for binding in required_bindings
            ],
        )
        forbidden = next(
            (
                contribution.registry_path
                for contribution in cluster.contributions
                if contribution.registry_path not in ALLOWED_HOST_REGISTRY_PATHS
            ),
            None,
        )
        if forbidden is not None:
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_FORBIDDEN_REGISTRY,
                detail="/".join(forbidden),
            )
        patch_shape = _patch_shape_metrics(cluster.contributions)
        log_enrichment_event(
            "patch.provenance_validated",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            contribution_count=len(cluster.contributions),
            contribution_limit=MAX_PATCH_CONTRIBUTIONS,
            unused_contribution_capacity=(
                MAX_PATCH_CONTRIBUTIONS - len(cluster.contributions)
            ),
            contribution_limit_reached=(
                len(cluster.contributions) == MAX_PATCH_CONTRIBUTIONS
            ),
            linked_contribution_count=len(contribution_sources),
            eligible_item_count=len(eligible_item_ids),
            source_item_count=len(used_source_item_ids),
            unsourced_eligible_item_count=len(eligible_item_ids - used_source_item_ids),
            **patch_shape,
        )
        log_enrichment_event(
            "patch.provenance_detail",
            level=logging.DEBUG,
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            contribution_sources=contribution_sources,
        )
        dry_run = apply_enrichment_patch(
            {
                "hosts_by_name": {
                    patch.target_host: trusted_basis_host,
                }
            },
            patch,
        )
        if dry_run.outcome.status == "skipped":
            return _patch_rejection_update(
                context,
                EnrichmentGraphReason.INTEGRATOR_OUTPUT_INVALID,
                detail=dry_run.outcome.reason_code,
            )
        if dry_run.outcome.status == "already_satisfied":
            log_enrichment_event(
                "patch.validated",
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                reason_code="already_materialized",
                cluster_id=cluster.id,
                contribution_count=len(cluster.contributions),
                dependency_count=len(cluster.dependencies),
                validation_copy_outcome=dry_run.outcome.status,
            )
            log_enrichment_event(
                "workflow.no_op",
                context=_graph_log_context(
                    context,
                    surface=state["coordinator"].surface,
                ),
                stage="patch_validation",
                reason_code="already_materialized",
                validation_copy_outcome=dry_run.outcome.status,
            )
            return {
                "status": EnrichmentGraphStatus.NO_OP,
                "reason_code": EnrichmentGraphReason.INTEGRATOR_NO_OP,
            }
        log_enrichment_event(
            "patch.validated",
            context=_graph_log_context(
                context,
                surface=state["coordinator"].surface,
            ),
            reason_code=EnrichmentGraphReason.PATCH_PROPOSED.value,
            cluster_id=cluster.id,
            contribution_count=len(cluster.contributions),
            dependency_count=len(cluster.dependencies),
            validation_copy_outcome=dry_run.outcome.status,
        )
        return {
            "patch": patch,
            "status": EnrichmentGraphStatus.PATCH_PROPOSED,
            "reason_code": EnrichmentGraphReason.PATCH_PROPOSED,
        }

    def _call_role(
        self,
        role: _RoleRuntime,
        context: EnrichmentJobContext,
        prompt_payload: Mapping[str, object],
        config: RunnableConfig,
        *,
        selected_surface: str | None = None,
        system_prompt_suffix: str = "",
    ) -> dict[str, object]:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_core.runnables.config import merge_configs
        except ImportError as exc:  # pragma: no cover - required runtime dependency.
            raise RuntimeError(
                "EnrichmentAgent requires langchain-core. Install dependencies."
            ) from exc

        started = time.perf_counter()
        system_prompt = role.system_prompt
        if system_prompt_suffix:
            system_prompt = f"{system_prompt}\n\n{system_prompt_suffix}"
        log_enrichment_event(
            "role.started",
            context=_graph_log_context(
                context,
                selected_surface=selected_surface,
            ),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            enrichment_mode=context.mode.value,
            role=role.role,
            provider=role.provider,
            model=role.model_name,
            reasoning_effort=role.reasoning_effort or "default",
            verbosity=role.verbosity or "default",
            openai_api=role.openai_api or "automatic",
            openai_service_tier=role.openai_service_tier or "project_default",
            openai_reasoning_mode=role.openai_reasoning_mode or "default",
            max_output_tokens=(
                role.max_output_tokens
                if role.max_output_tokens is not None
                else "default"
            ),
        )
        token_callback = TokenUsageLoggingCallback(
            role.model_name,
            command=f"enrichment job_id={context.job_id} role={role.role}",
            max_output_tokens=role.max_output_tokens,
        )
        role_config = merge_configs(
            config,
            {
                "callbacks": [token_callback],
                "run_name": f"dreamland-enrichment-{role.role}-model",
                "metadata": {
                    "dreamland.enrichment.role": role.role,
                    "dreamland.enrichment.mode": context.mode.value,
                    "dreamland.enrichment.role_provider": role.provider,
                    "dreamland.enrichment.role_model": role.model_name,
                    "dreamland.enrichment.role_reasoning_effort": (
                        role.reasoning_effort or "default"
                    ),
                    "dreamland.enrichment.role_verbosity": (
                        role.verbosity or "default"
                    ),
                    "dreamland.enrichment.role_openai_api": role.openai_api
                    or "automatic",
                    "dreamland.enrichment.role_openai_service_tier": (
                        role.openai_service_tier or "project_default"
                    ),
                    "dreamland.enrichment.role_openai_reasoning_mode": (
                        role.openai_reasoning_mode or "default"
                    ),
                    "dreamland.enrichment.role_max_output_tokens": (
                        str(role.max_output_tokens)
                        if role.max_output_tokens is not None
                        else "default"
                    ),
                },
            },
        )
        try:
            response = role.model.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=canonical_json(prompt_payload)),
                ],
                config=role_config,
            )
            text = message_content_to_text(response)
        except Exception as exc:
            reason = _model_failure_reason(role.role)
            classification = classify_provider_failure(exc)
            failure = EnrichmentModelFailure(
                role=role.role,
                provider=role.provider,
                model=role.model_name,
                classification=classification,
            )
            log_enrichment_event(
                "role.failed",
                level=logging.WARNING,
                context=_graph_log_context(
                    context,
                    selected_surface=selected_surface,
                ),
                graph_version=ENRICHMENT_GRAPH_VERSION,
                enrichment_mode=context.mode.value,
                role=role.role,
                provider=role.provider,
                model=role.model_name,
                reason_code=reason.value,
                error_type=type(exc).__name__,
                latency_ms=_elapsed_ms(started),
                **classification.to_log_fields(),
            )
            return {
                "status": EnrichmentGraphStatus.REJECTED,
                "reason_code": reason,
                "rejection_detail": type(exc).__name__,
                "model_failure": failure,
            }
        log_enrichment_event(
            "role.completed",
            context=_graph_log_context(
                context,
                selected_surface=selected_surface,
            ),
            graph_version=ENRICHMENT_GRAPH_VERSION,
            enrichment_mode=context.mode.value,
            role=role.role,
            provider=role.provider,
            model=role.model_name,
            response_chars=len(text),
            latency_ms=_elapsed_ms(started),
        )
        return {f"{role.role}_raw": text}


def _eligible_expert_payload(
    report: ExpertReport,
    eligible_item_ids: Sequence[str],
) -> dict[str, object]:
    """Expose only deterministically accepted candidate material to Integrator."""

    eligible = set(eligible_item_ids)
    relations = [
        relation.to_payload()
        for relation in report.relations
        if _relation_is_eligible(relation, eligible)
    ]
    return {
        "v": report.schema_version,
        "status": report.status,
        "reason": None,
        "conditions": [],
        "items": [item.to_payload() for item in report.items if item.id in eligible],
        "gates": [],
        "relations": relations,
    }


def _candidate_origin_counts(items: Sequence[ExpertItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.origin] = counts.get(item.origin, 0) + 1
    return counts


def _patch_shape_metrics(
    contributions: Sequence[EnrichmentContribution],
) -> dict[str, object]:
    """Summarize patch density without retaining model-authored paths or prose."""

    registry_counts: dict[str, int] = {}
    target_counts: dict[tuple[str, ...], int] = {}
    filesystem_paths: list[tuple[str, ...]] = []
    filesystem_depth_counts: dict[int, int] = {}
    for contribution in contributions:
        registry = "/".join(contribution.registry_path)
        registry_counts[registry] = registry_counts.get(registry, 0) + 1
        target_counts[contribution.target_path] = (
            target_counts.get(contribution.target_path, 0) + 1
        )
        if contribution.registry_path != ("filesystem", "artifacts_by_path"):
            continue
        segments = _filesystem_path_segments(contribution.key)
        filesystem_paths.append(segments)
        depth = len(segments)
        filesystem_depth_counts[depth] = filesystem_depth_counts.get(depth, 0) + 1

    filesystem_depths = tuple(sorted(filesystem_depth_counts))
    ancestor_counts = [
        sum(
            1
            for possible_ancestor in filesystem_paths
            if len(possible_ancestor) < len(path)
            and path[: len(possible_ancestor)] == possible_ancestor
        )
        for path in filesystem_paths
    ]
    return {
        "registry_contribution_counts": registry_counts,
        "repeated_target_group_count": sum(
            count > 1 for count in target_counts.values()
        ),
        "filesystem_contribution_count": sum(filesystem_depth_counts.values()),
        "filesystem_path_segment_depth_min": (
            filesystem_depths[0] if filesystem_depths else None
        ),
        "filesystem_path_segment_depth_max": (
            filesystem_depths[-1] if filesystem_depths else None
        ),
        "filesystem_path_segment_depth_counts": {
            str(depth): filesystem_depth_counts[depth] for depth in filesystem_depths
        },
        "filesystem_ancestor_linked_count": sum(count > 0 for count in ancestor_counts),
        "filesystem_max_proposed_ancestor_chain": (
            max(ancestor_counts, default=-1) + 1
        ),
    }


def _active_session_authority_reason(
    *,
    selected_surface: str | None,
    contributions: Sequence[EnrichmentContribution],
) -> str | None:
    """Select a race guard without interpreting arbitrary prose as truth."""

    if selected_surface in _ACTIVE_SESSION_SURFACES:
        return "selected_surface"
    if any(
        contribution.registry_path in {("users_by_username",), ("groups_by_name",)}
        for contribution in contributions
    ):
        return "identity_registry"
    for contribution in contributions:
        tokens = _authority_tokens(
            {
                "key": contribution.key,
                "value": contribution.value,
            }
        )
        if tokens & _ACTIVE_SESSION_MARKERS or any(
            token.startswith(prefix)
            for token in tokens
            for prefix in _ACTIVE_SESSION_MARKER_PREFIXES
        ):
            return "semantic_marker"
    return None


def _authority_tokens(value: object) -> set[str]:
    if isinstance(value, str):
        return set(_AUTHORITY_TOKEN.findall(value.casefold()))
    if isinstance(value, Mapping):
        tokens: set[str] = set()
        for key, child in value.items():
            tokens.update(_authority_tokens(str(key)))
            tokens.update(_authority_tokens(child))
        return tokens
    if isinstance(value, (list, tuple)):
        tokens: set[str] = set()
        for child in value:
            tokens.update(_authority_tokens(child))
        return tokens
    return set()


def _filesystem_path_segments(path: str) -> tuple[str, ...]:
    """Return common path segments for aggregate metrics, never raw log output."""

    return tuple(segment for segment in path.replace("\\", "/").split("/") if segment)


def _candidate_restatement_reason(
    item: ExpertItem,
    references: Sequence[EstablishedReference],
) -> str | None:
    """Reject only obvious same-kind identity copies, never fuzzy similarity."""

    candidate_name = _normalized_candidate_text(item.name)
    candidate_detail = _normalized_candidate_text(item.detail)
    for reference in references:
        if reference.kind.casefold() != item.kind.casefold():
            continue
        reference_label = _normalized_candidate_text(reference.label)
        exact_reference_texts = {reference_label}
        if isinstance(reference.observation, str):
            exact_reference_texts.add(_normalized_candidate_text(reference.observation))
        if (
            candidate_name in exact_reference_texts
            and candidate_detail in exact_reference_texts
        ):
            return "anchor_restatement"
    return None


def _normalized_candidate_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _trusted_dependency_hashes(
    trusted_basis_host: Mapping[str, object],
    dependencies: Sequence[EnrichmentDependency],
) -> tuple[tuple[StatePath, str], ...]:
    """Fingerprint complete dependency values without retaining their contents."""

    fingerprints: list[tuple[StatePath, str]] = []
    for dependency in dependencies:
        value: object = trusted_basis_host
        for segment in dependency.path:
            if not isinstance(value, Mapping) or segment not in value:
                raise ValueError("trusted dependency path is absent")
            value = value[segment]
        fingerprints.append(
            (
                dependency.path,
                hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest(),
            )
        )
    return tuple(fingerprints)


def _relation_is_eligible(
    relation: ExpertRelation,
    eligible_item_ids: set[str],
) -> bool:
    endpoints = (relation.subject, relation.object)
    return any(endpoint in eligible_item_ids for endpoint in endpoints) and all(
        endpoint.startswith("@") or endpoint in eligible_item_ids
        for endpoint in endpoints
    )


def _presence_gap_log_fields(value: object) -> dict[str, object]:
    """Summarize deterministic gap routing without logging state-derived names."""

    if not isinstance(value, Mapping):
        return {}
    anti_repetition = value.get("anti_repetition")
    subjects: object = None
    systems: object = None
    if isinstance(anti_repetition, Mapping):
        subjects = anti_repetition.get("subjects")
        systems = anti_repetition.get("systems")
    return {
        "presence_gap_policy_version": PRESENCE_GAP_POLICY_VERSION,
        "presence_gap_sha256": hashlib.sha256(
            canonical_json(value).encode("utf-8")
        ).hexdigest(),
        "presence_gap_ambient_objective": value.get("ambient_objective"),
        "presence_gap_preferred_surface": value.get("preferred_surface"),
        "presence_gap_anti_repetition_subject_count": _bounded_sequence_count(subjects),
        "presence_gap_anti_repetition_system_count": _bounded_sequence_count(systems),
    }


def _foreground_evidence_log_fields(
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Summarize typed foreground evidence without retaining observed output."""

    host_time = snapshot.get("host_time")
    exhaustive = snapshot.get("recent_exhaustive_evidence")
    observed = isinstance(host_time, Mapping) and host_time.get("status") == "observed"
    evidence_items = (
        [item for item in exhaustive if isinstance(item, Mapping)]
        if isinstance(exhaustive, (list, tuple))
        else []
    )
    public_contract = {
        "host_time": host_time,
        "recent_exhaustive_evidence": evidence_items,
    }
    return {
        "foreground_evidence_policy_version": FOREGROUND_EVIDENCE_POLICY_VERSION,
        "foreground_evidence_sha256": hashlib.sha256(
            canonical_json(public_contract).encode("utf-8")
        ).hexdigest(),
        "host_time_observed": observed,
        "recent_exhaustive_evidence_count": len(evidence_items),
        "recent_exhaustive_evidence_kinds": sorted(
            {
                str(item.get("kind"))
                for item in evidence_items
                if isinstance(item.get("kind"), str) and item.get("kind")
            }
        ),
    }


def _bounded_sequence_count(value: object) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def _route_after_coordinator_context(state: _EnrichmentGraphState) -> str:
    if "status" in state:
        return "end"
    context = state["context"]
    if context.mode is EnrichmentMode.AMBIENT_PRESENCE:
        return "ambient_presence_coordinator_agent"
    return "focus_coordinator_agent"


def _route_after_coordinator(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "prepare_expert_brief"


def _route_after_expert_brief(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "expert_agent"


def _route_after_expert(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "classify_expert_items"


def _route_after_current_snapshot(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "resolve_current_references"


def _route_after_current_references(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "integrator_agent"


def _route_after_expert_item_classification(
    state: _EnrichmentGraphState,
) -> str:
    return "end" if "status" in state else "snapshot_current"


def _route_after_integrator(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "normalize_integrator_output"


def _route_after_integrator_normalization(state: _EnrichmentGraphState) -> str:
    return "end" if "status" in state else "validate_integrator_output"


def _result_from_state(state: Mapping[str, object]) -> EnrichmentGraphResult:
    context = cast(EnrichmentJobContext, state["context"])
    status = state.get("status")
    reason = state.get("reason_code")
    if not isinstance(status, EnrichmentGraphStatus) or not isinstance(
        reason, EnrichmentGraphReason
    ):
        raise RuntimeError("enrichment graph ended without a structured outcome")
    refresh_basis: EnrichmentIntegratorRefreshBasis | None = None
    if (
        status is EnrichmentGraphStatus.PATCH_PROPOSED
        and isinstance(state.get("creative_latitude"), CreativeLatitude)
        and isinstance(state.get("selected_references"), tuple)
        and isinstance(state.get("refresh_eligible_expert_item_ids"), tuple)
        and isinstance(state.get("refresh_blocked_expert_item_ids"), tuple)
        and isinstance(state.get("unresolved_condition_ids"), tuple)
    ):
        presence_gap = state.get("presence_gap")
        refresh_basis = EnrichmentIntegratorRefreshBasis(
            selected_references=cast(
                tuple[EstablishedReference, ...],
                state["selected_references"],
            ),
            creative_latitude=cast(
                CreativeLatitude,
                state["creative_latitude"],
            ),
            eligible_expert_item_ids=cast(
                tuple[str, ...],
                state["refresh_eligible_expert_item_ids"],
            ),
            blocked_expert_item_ids=cast(
                tuple[str, ...],
                state["refresh_blocked_expert_item_ids"],
            ),
            unresolved_condition_ids=cast(
                tuple[str, ...],
                state["unresolved_condition_ids"],
            ),
            presence_gap=(
                deepcopy(dict(presence_gap))
                if isinstance(presence_gap, Mapping)
                else {}
            ),
        )
    return EnrichmentGraphResult(
        context=context,
        status=status,
        reason_code=reason,
        coordinator=cast(CoordinatorDecision | None, state.get("coordinator")),
        expert=cast(ExpertReport | None, state.get("expert")),
        patch=cast(EnrichmentPatch | None, state.get("patch")),
        initial_snapshot_sha256=str(state.get("initial_snapshot_sha256", "")),
        current_snapshot_sha256=(
            str(state["current_snapshot_sha256"])
            if state.get("current_snapshot_sha256") is not None
            else None
        ),
        rejection_detail=(
            str(state["rejection_detail"])
            if state.get("rejection_detail") is not None
            else None
        ),
        model_failure=cast(
            EnrichmentModelFailure | None,
            state.get("model_failure"),
        ),
        integrator_refresh_basis=refresh_basis,
    )


def _model_failure_reason(role: str) -> EnrichmentGraphReason:
    return {
        "coordinator": EnrichmentGraphReason.COORDINATOR_MODEL_FAILED,
        "ambient_coordinator": EnrichmentGraphReason.COORDINATOR_MODEL_FAILED,
        "expert": EnrichmentGraphReason.EXPERT_MODEL_FAILED,
        "integrator": EnrichmentGraphReason.INTEGRATOR_MODEL_FAILED,
    }[role]


def _coordinator_role(context: EnrichmentJobContext) -> str:
    if context.mode is EnrichmentMode.AMBIENT_PRESENCE:
        return "ambient_coordinator"
    return "coordinator"


def _rejection_update(
    context: EnrichmentJobContext,
    *,
    role: str,
    reason: EnrichmentGraphReason,
    error: RoleOutputValidationError,
) -> dict[str, object]:
    detail = f"{error.reason_code}@{error.location}"
    log_enrichment_event(
        "role.rejected",
        level=logging.WARNING,
        context=_graph_log_context(context),
        role=role,
        reason_code=reason.value,
        validation_code=error.reason_code,
        location=error.location,
    )
    return {
        "status": EnrichmentGraphStatus.REJECTED,
        "reason_code": reason,
        "rejection_detail": detail,
    }


def _input_rejection_update(
    context: EnrichmentJobContext,
    *,
    boundary: str,
    reason: EnrichmentGraphReason,
    error: RoleInputValidationError,
) -> dict[str, object]:
    detail = f"{error.reason_code}@{error.location}"
    log_enrichment_event(
        "handoff.rejected",
        level=logging.WARNING,
        context=_graph_log_context(context),
        graph_version=ENRICHMENT_GRAPH_VERSION,
        boundary=boundary,
        reason_code=reason.value,
        validation_code=error.reason_code,
        location=error.location,
    )
    return {
        "status": EnrichmentGraphStatus.REJECTED,
        "reason_code": reason,
        "rejection_detail": detail,
    }


def _patch_rejection_update(
    context: EnrichmentJobContext,
    reason: EnrichmentGraphReason,
    *,
    detail: str,
) -> dict[str, object]:
    log_enrichment_event(
        "patch.rejected",
        level=logging.WARNING,
        context=_graph_log_context(context),
        reason_code=reason.value,
        detail=detail,
    )
    return {
        "status": EnrichmentGraphStatus.REJECTED,
        "reason_code": reason,
        "rejection_detail": detail,
    }


def _provenance_rejection_update(
    context: EnrichmentJobContext,
    *,
    contribution_id: str,
    source_item_id: str,
    reason_code: str,
    location: str,
) -> dict[str, object]:
    log_enrichment_event(
        "patch.provenance_rejected",
        level=logging.WARNING,
        context=_graph_log_context(context),
        reason_code=reason_code,
        location=location,
        contribution_id=contribution_id,
        source_item_ids=[source_item_id],
    )
    return {
        "status": EnrichmentGraphStatus.REJECTED,
        "reason_code": EnrichmentGraphReason.INTEGRATOR_PROVENANCE_INVALID,
        "rejection_detail": f"{reason_code}@{location}",
    }


def _reference_dependency_rejection_update(
    context: EnrichmentJobContext,
    *,
    reference_id: str,
    path: tuple[str, ...],
    reason_code: str,
) -> dict[str, object]:
    location = "$.clusters[0].dependencies"
    log_enrichment_event(
        "patch.reference_dependency_rejected",
        level=logging.WARNING,
        context=_graph_log_context(context),
        reason_code=reason_code,
        location=location,
        reference_id=reference_id,
        path=list(path),
    )
    return {
        "status": EnrichmentGraphStatus.REJECTED,
        "reason_code": (EnrichmentGraphReason.INTEGRATOR_REFERENCE_DEPENDENCY_INVALID),
        "rejection_detail": f"{reason_code}@{location}",
    }


def _log_role_parsed(
    context: EnrichmentJobContext,
    role: str,
    output_status: str,
    *,
    selected_surface: str | None = None,
) -> None:
    log_enrichment_event(
        "role.accepted",
        context=_graph_log_context(
            context,
            selected_surface=selected_surface,
        ),
        role=role,
        output_status=output_status,
    )


def _graph_log_context(
    context: EnrichmentJobContext,
    *,
    surface: str | None = None,
    selected_surface: str | None = None,
) -> EnrichmentLogContext:
    resolved_selected_surface = selected_surface or surface
    return EnrichmentLogContext(
        session_id=context.session_id,
        job_id=context.job_id,
        event_id=context.event_id,
        caused_by_event_id=context.caused_by_event_id,
        correlation_id=context.correlation_id,
        trigger_id=context.trigger_id,
        enrichment_mode=context.mode,
        command_id=context.command_id,
        target_host=context.target_host,
        requested_surface=context.surface_hint,
        selected_surface=resolved_selected_surface,
        surface=resolved_selected_surface or context.surface_hint,
    )


def _expert_command_evidence_metrics(
    observations: Sequence[object],
    *,
    selected_surface: str,
) -> tuple[int, int, int, int]:
    count = 0
    same_surface_count = 0
    chars = 0
    truncated_count = 0
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        if observation.get("kind") != "recent_command_result":
            continue
        details = observation.get("details")
        if not isinstance(details, Mapping):
            continue
        count += 1
        if details.get("surface") == selected_surface:
            same_surface_count += 1
        chars += sum(
            len(value)
            for key, value in details.items()
            if key in {"stdout_excerpt", "stderr_excerpt"} and isinstance(value, str)
        )
        if any(
            key in {"stdout_omitted_chars", "stderr_omitted_chars"}
            and isinstance(value, int)
            and value > 0
            for key, value in details.items()
        ):
            truncated_count += 1
    return count, same_surface_count, chars, truncated_count


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _log_preview(value: str, *, limit: int = 160) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


__all__ = [
    "CurrentSnapshotCapture",
    "CoordinatorDecision",
    "CoordinatorStatus",
    "ENRICHMENT_GRAPH_VERSION",
    "EnrichmentAgent",
    "EnrichmentGraphReason",
    "EnrichmentGraphResult",
    "EnrichmentGraphStatus",
    "EnrichmentJobContext",
    "EnrichmentModelFailure",
    "EnrichmentMode",
    "EXPERT_RELATION_POLICY_VERSION",
    "ExpertCondition",
    "ExpertGate",
    "ExpertItem",
    "ExpertOrigin",
    "ExpertRelation",
    "ExpertReport",
    "ExpertStatus",
    "ExpertiseScope",
    "RoleOutputValidationError",
    "parse_coordinator_decision",
    "parse_expert_report",
]
