"""Operator-facing control contract for the EnrichmentAgent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EnrichmentRuntimeStatus:
    """Small, stable snapshot of the enrichment runtime's health and activity."""

    configured: bool
    started: bool
    automatic_enabled: bool
    apply_mode: str
    inflight: int
    queued: int
    completed: int
    failed: int


@dataclass(frozen=True, slots=True)
class EnrichmentSubmission:
    """Result of asking the runtime to enqueue one manual enrichment job."""

    accepted: bool
    code: str
    reason: str
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class EnrichmentJobSnapshot:
    """Presentation-safe job state returned to operator tooling."""

    job_id: str
    session_id: str
    trigger: str
    target_host: str | None
    surface: str | None
    status: str
    reason: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    mode: str = "focused"


@runtime_checkable
class EnrichmentControl(Protocol):
    """Narrow control surface implemented by an EnrichmentAgent runtime."""

    def status(self) -> EnrichmentRuntimeStatus:
        """Return current runtime status without mutating it."""

    def submit_manual(
        self,
        session_id: str,
        target_host: str | None = None,
        surface: str | None = None,
    ) -> EnrichmentSubmission:
        """Submit one operator-requested job for a live session."""

    def list_jobs(self, limit: int) -> tuple[EnrichmentJobSnapshot, ...]:
        """Return up to ``limit`` recent jobs, newest first."""

    def graph_mermaid(self) -> str:
        """Return inspectable EnrichmentAgent topology as Mermaid or Markdown."""
