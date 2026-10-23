"""Deterministic planning intents for the EnrichmentAgent."""

from __future__ import annotations

from enum import StrEnum


class EnrichmentMode(StrEnum):
    """Selects one inspectable Coordinator task without changing authority."""

    FOCUSED = "focused"
    AMBIENT_PRESENCE = "ambient_presence"


__all__ = ["EnrichmentMode"]
