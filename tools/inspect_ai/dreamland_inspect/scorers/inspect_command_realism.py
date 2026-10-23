"""Inspect AI scorer adapters for DREAMLAND command-event artifacts."""

from __future__ import annotations

from pathlib import Path

from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState

from dreamland_inspect.events import load_events_jsonl
from dreamland_inspect.findings import Finding
from dreamland_inspect.scorers.command_realism import evaluate_command_events


@scorer(metrics=[mean()])
def dreamland_command_realism():
    async def score(state: TaskState, target: Target) -> Score:
        commands_path = state.metadata.get("commands_path")
        if not commands_path:
            return Score.unscored(
                explanation="No command-event artifact path was provided.",
                metadata={"error": "missing commands_path"},
            )

        events = load_events_jsonl(Path(str(commands_path)))
        expected_state_profile = state.metadata.get("expected_state_profile")
        report = evaluate_command_events(
            events,
            expected_state_profile=(
                str(expected_state_profile) if expected_state_profile else None
            ),
        )
        return Score(
            value=report.score,
            explanation=_explanation(report.findings),
            metadata={
                "command_count": report.command_count,
                "findings": [
                    finding.to_dict() for finding in report.findings
                ],
            },
        )

    return score


def _explanation(findings: list[Finding]) -> str:
    if not findings:
        return "No command realism findings."
    return f"{len(findings)} command realism finding(s)."
