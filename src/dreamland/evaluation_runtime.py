"""Isolated agent and enrichment ownership for an MCP evaluation session."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, TypedDict

from dreamland.agent import LangGraphCommandAgent
from dreamland.agent_tracing import build_langfuse_callbacks
from dreamland.backend import AgentBackend
from dreamland.background_agents.enrichment.graph import EnrichmentAgent
from dreamland.background_agents.enrichment.service import EnrichmentRuntimeService
from dreamland.background_agents.event_bus import SessionEventBus
from dreamland.config import DreamlandSettings
from dreamland.evaluation_config import (
    EVALUATION_CONFIG_KEY,
    evaluation_snapshot,
    resolve_evaluation_config,
)
from dreamland.profile import ServerProfile
from dreamland.runtime_logging import trace_evaluation_config
from dreamland.spiritguide import (
    ModelSpiritguide,
    Spiritguide,
    SpiritguideContext,
    SpiritguideResult,
)
from dreamland.state_profiles import StateProfileRuntime
from dreamland.structured_failures import StructuredFailureStore

if TYPE_CHECKING:
    from dreamland.session_registry import LiveSessionRegistry

LOGGER = logging.getLogger("dreamland.lifecycle")


class _TracingArguments(TypedDict):
    langfuse_enabled: bool
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None


@dataclass
class EvaluationSessionRuntime:
    backend: AgentBackend
    spiritguide: Spiritguide
    enrichment: EnrichmentRuntimeService
    event_bus: SessionEventBus
    snapshot: dict[str, object]

    def close(self) -> None:
        # Stopping invalidates pending work before it can apply another patch.
        self.enrichment.stop(wait=False)


@dataclass
class RecordedSpiritguide:
    delegate: Spiritguide
    snapshot: dict[str, object]

    def respond(self, context: SpiritguideContext) -> SpiritguideResult:
        context.session.environment_metadata.metadata[EVALUATION_CONFIG_KEY] = deepcopy(
            self.snapshot
        )
        LOGGER.info(
            "Spiritguide evaluation session=%s config=%s",
            context.session_id,
            self.snapshot,
        )
        trace_evaluation_config(context.session_id, self.snapshot)
        result = self.delegate.respond(context)
        return replace(
            result,
            metadata={
                **result.metadata,
                "evaluation_config": deepcopy(self.snapshot),
                "session_id": context.session_id,
            },
        )


class EvaluationRuntimeFactory:
    def __init__(
        self,
        settings: DreamlandSettings,
        *,
        profile: ServerProfile,
        state_profile_runtime: StateProfileRuntime,
        session_registry: LiveSessionRegistry,
        structured_failure_store: StructuredFailureStore,
        api_key_resolver: Callable[[str], str | None],
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.state_profile_runtime = state_profile_runtime
        self.session_registry = session_registry
        self.structured_failure_store = structured_failure_store
        self.api_key_resolver = api_key_resolver

    def resolve(
        self, overrides: object, *, detached: bool = False
    ) -> DreamlandSettings:
        return resolve_evaluation_config(self.settings, overrides, detached=detached)

    def _tracing(self) -> _TracingArguments:
        settings = self.settings.langfuse
        return {
            "langfuse_enabled": settings.enabled,
            "langfuse_public_key": settings.public_key,
            "langfuse_secret_key": settings.secret_key,
            "langfuse_host": settings.host,
        }

    def spiritguide(
        self, settings: DreamlandSettings, *, detached: bool = False
    ) -> Spiritguide:
        guide = ModelSpiritguide(
            settings.spiritguide,
            api_key=self.api_key_resolver(settings.spiritguide.model_provider),
            **self._tracing(),
        )
        return RecordedSpiritguide(
            guide, evaluation_snapshot(settings, detached=detached)
        )

    def create(self, settings: DreamlandSettings) -> EvaluationSessionRuntime:
        role = settings.agent
        arguments = asdict(role)
        arguments.pop("model")
        agent = LangGraphCommandAgent(
            provider=role.model_provider,
            model_name=role.model_name,
            api_key=self.api_key_resolver(role.model_provider),
            **arguments,
            **self._tracing(),
        )
        guide = self.spiritguide(settings)
        event_bus = SessionEventBus()
        backend = AgentBackend(
            self.profile,
            agent=agent,
            state_profile_runtime=self.state_profile_runtime,
            structured_failure_store=self.structured_failure_store,
            event_bus=event_bus,
        )
        enrichment_agent = EnrichmentAgent.from_settings(
            settings.enrichment,
            api_key_resolver=self.api_key_resolver,
            lazy_models=True,
        )
        tracing = settings.langfuse
        enrichment = EnrichmentRuntimeService(
            settings.enrichment,
            session_registry=self.session_registry,
            event_bus=event_bus,
            agent=enrichment_agent,
            callbacks=build_langfuse_callbacks(
                enabled=tracing.enabled,
                public_key=tracing.public_key,
                secret_key=tracing.secret_key,
                host=tracing.host,
            ),
        )
        return EvaluationSessionRuntime(
            backend, guide, enrichment, event_bus, evaluation_snapshot(settings)
        )
