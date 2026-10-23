"""Finding and report models for DREAMLAND evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["info", "low", "medium", "high", "critical"]

SEVERITY_WEIGHTS: dict[Severity, float] = {
    "info": 0.0,
    "low": 0.05,
    "medium": 0.15,
    "high": 0.3,
    "critical": 0.5,
}


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    finding_type: str
    message: str
    command_id: str | None = None
    evidence: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {
            "severity": self.severity,
            "type": self.finding_type,
            "message": self.message,
        }
        if self.command_id is not None:
            data["command_id"] = self.command_id
        if self.evidence is not None:
            data["evidence"] = self.evidence
        return data


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    score: float
    findings: list[Finding] = field(default_factory=list)
    command_count: int = 0

    def findings_by_type(self, finding_type: str) -> list[Finding]:
        return [
            finding
            for finding in self.findings
            if finding.finding_type == finding_type
        ]


def score_from_findings(findings: list[Finding]) -> float:
    penalty = sum(SEVERITY_WEIGHTS[finding.severity] for finding in findings)
    return max(0.0, round(1.0 - penalty, 3))

