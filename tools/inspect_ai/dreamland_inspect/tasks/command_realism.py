"""Inspect tasks for scoring captured DREAMLAND command events."""

from __future__ import annotations

from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.solver import Generate, TaskState, solver

from dreamland_inspect.scorers.inspect_command_realism import (
    dreamland_command_realism,
)


@task
def command_realism(commands_path: str = "run.commands.jsonl") -> Task:
    """Score an existing DREAMLAND command-event JSONL artifact."""

    path = Path(commands_path)
    sample = Sample(
        id=path.name,
        input=(
            "Score the captured DREAMLAND command-event artifact for terminal "
            "realism and continuity."
        ),
        metadata={"commands_path": str(path)},
    )
    return Task(
        dataset=MemoryDataset([sample], name="dreamland-command-events"),
        solver=artifact_passthrough(),
        scorer=dreamland_command_realism(),
        model="none",
    )


@solver
def artifact_passthrough():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.completed = True
        return state

    return solve
