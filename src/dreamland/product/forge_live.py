"""Explicit single-attempt live World Forge composition primitives."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

from dreamland.product.forge_artifacts import ForgeInstruction
from dreamland.spiritguide import Spiritguide
from dreamland.spiritguide_profiles import create_spiritguide_profile
from dreamland.state_profiles import StateProfile, StateProfileStore

LOGGER = logging.getLogger("dreamland.lifecycle")
_DETACHED_LOGGER = logging.getLogger("dreamland.admin")


class _SuppressDetachedInstruction(logging.Filter):
    """Keep the compiled audience seed out of the live process log stream."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            isinstance(record.msg, str)
            and record.msg.startswith("Detached Spiritguide profile requested")
            and "instruction=%r" in record.msg
        )


class SpiritguideForgeProfileGenerator:
    """Generate one isolated candidate through the configured Spiritguide model."""

    def __init__(self, *, spiritguide: Spiritguide, candidate_directory: Path) -> None:
        self.spiritguide = spiritguide
        self.store = StateProfileStore(candidate_directory)
        candidate_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(candidate_directory, 0o700)

    def generate(self, instruction: ForgeInstruction) -> StateProfile:
        name = f"forge-a-{instruction.seed_hash.removeprefix('sha256:')[:16]}-{uuid4().hex[:12]}"
        LOGGER.info(
            "world_forge_candidate_started compiler_revision=%s seed_hash=%s",
            instruction.compiler_revision,
            instruction.seed_hash,
        )
        log_filter = _SuppressDetachedInstruction()
        _DETACHED_LOGGER.addFilter(log_filter)
        try:
            result = create_spiritguide_profile(
                store=self.store,
                spiritguide=self.spiritguide,
                name=name,
                instruction=instruction.text,
                level="world",
                comment=(
                    "World Forge single-attempt candidate; not selected authority until "
                    "independent validation and immutable storage complete."
                ),
            )
        finally:
            _DETACHED_LOGGER.removeFilter(log_filter)
        if not result.saved or result.structured_response_accepted is not True:
            LOGGER.warning(
                "world_forge_candidate_rejected structured_response_accepted=%s",
                result.structured_response_accepted is True,
            )
            raise ValueError(
                "World Forge provider returned no valid structured candidate"
            )
        profile = self.store.load(name)
        if result.path is not None:
            os.chmod(result.path, 0o600)
        LOGGER.info("world_forge_candidate_generated candidate_name=%s", name)
        return profile
