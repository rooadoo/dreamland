"""Deterministic size limits for the EnrichmentAgent role contracts."""

from __future__ import annotations

from typing import Final

MAX_COORDINATOR_ANCHORS: Final[int] = 8
MAX_EXPERT_CANDIDATES: Final[int] = 24
MAX_EXPERT_AUXILIARY_ENTRIES: Final[int] = 8
MAX_EXPERT_RELATIONS: Final[int] = 24
MAX_PATCH_CONTRIBUTIONS: Final[int] = 24
MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION: Final[int] = 8

__all__ = [
    "MAX_COORDINATOR_ANCHORS",
    "MAX_EXPERT_AUXILIARY_ENTRIES",
    "MAX_EXPERT_CANDIDATES",
    "MAX_EXPERT_RELATIONS",
    "MAX_PATCH_CONTRIBUTIONS",
    "MAX_SOURCE_ITEM_IDS_PER_CONTRIBUTION",
]
