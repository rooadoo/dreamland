"""First-pass command realism and continuity checks."""

from __future__ import annotations

from collections import Counter

from dreamland_inspect.events import CommandEvent
from dreamland_inspect.findings import EvaluationReport, Finding, score_from_findings
from dreamland_inspect.parsers.observations import CommandObservation, observe_event


def evaluate_command_events(
    events: list[CommandEvent],
    *,
    expected_state_profile: str | None = None,
) -> EvaluationReport:
    observations = [observe_event(event) for event in events]
    findings: list[Finding] = []
    findings.extend(_artifact_integrity_findings(events, expected_state_profile))
    findings.extend(_envelope_findings(events, observations))
    findings.extend(_timeout_findings(events))
    findings.extend(_network_continuity_findings(observations))
    return EvaluationReport(
        score=score_from_findings(findings),
        findings=findings,
        command_count=len(events),
    )


def _artifact_integrity_findings(
    events: list[CommandEvent],
    expected_state_profile: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    command_id_counts = Counter(event.command_id for event in events)
    duplicate_command_ids = [
        command_id
        for command_id, count in command_id_counts.items()
        if command_id and count > 1
    ]
    if duplicate_command_ids:
        findings.append(
            Finding(
                severity="high",
                finding_type="artifact.duplicate_command_id",
                command_id=duplicate_command_ids[0],
                message=(
                    "Command artifact contains duplicate harness command ids; "
                    "the run may include stale or mixed command events."
                ),
                evidence=", ".join(duplicate_command_ids[:5]),
            )
        )

    session_ids = {
        event.session_id for event in events if event.session_id is not None
    }
    if len(session_ids) > 1:
        findings.append(
            Finding(
                severity="high",
                finding_type="artifact.multiple_sessions",
                message=(
                    "Command artifact contains events from multiple DREAMLAND "
                    "sessions; score interpretation is not run-clean."
                ),
                evidence=", ".join(sorted(session_ids)[:5]),
            )
        )

    run_ids = {
        str(event.metadata.get("run_id"))
        for event in events
        if event.metadata.get("run_id") is not None
    }
    if len(run_ids) > 1:
        findings.append(
            Finding(
                severity="medium",
                finding_type="artifact.multiple_run_ids",
                message=(
                    "Command artifact contains multiple harness run ids; "
                    "review run-directory isolation."
                ),
                evidence=", ".join(sorted(run_ids)[:5]),
            )
        )

    state_profiles = {
        str(event.metadata.get("state_profile"))
        for event in events
        if event.metadata.get("state_profile") is not None
    }
    if len(state_profiles) > 1:
        findings.append(
            Finding(
                severity="high",
                finding_type="artifact.multiple_state_profiles",
                message=(
                    "Command artifact contains commands from multiple "
                    "DREAMLAND state profiles."
                ),
                evidence=", ".join(sorted(state_profiles)[:5]),
            )
        )
    if expected_state_profile:
        mismatches = sorted(
            profile for profile in state_profiles if profile != expected_state_profile
        )
        if mismatches:
            findings.append(
                Finding(
                    severity="critical",
                    finding_type="scenario.state_profile_mismatch",
                    message=(
                        "Command artifact state profile does not match the "
                        "trusted run spec."
                    ),
                    evidence=(
                        f"expected={expected_state_profile}; "
                        f"observed={', '.join(mismatches[:5])}"
                    ),
                )
            )
        if events and not state_profiles:
            findings.append(
                Finding(
                    severity="medium",
                    finding_type="scenario.missing_state_profile",
                    message=(
                        "Command artifact did not record a DREAMLAND state "
                        "profile, so scenario consistency cannot be verified."
                    ),
                    evidence=f"expected={expected_state_profile}",
                )
            )
    return findings


def _envelope_findings(
    events: list[CommandEvent], observations: list[CommandObservation]
) -> list[Finding]:
    by_id = {event.command_id: event for event in events}
    findings: list[Finding] = []
    for observation in observations:
        if not observation.envelope_leak:
            continue
        event = by_id[observation.command_id]
        findings.append(
            Finding(
                severity="critical",
                finding_type="format_realism.envelope_leak",
                command_id=event.command_id,
                message=(
                    "Command output appears to expose the structured DREAMLAND "
                    "JSON result envelope instead of terminal stdout."
                ),
                evidence=event.stdout[:240],
            )
        )
    return findings


def _timeout_findings(events: list[CommandEvent]) -> list[Finding]:
    findings: list[Finding] = []
    for event in events:
        if not event.metadata.get("timed_out"):
            continue
        findings.append(
            Finding(
                severity="high",
                finding_type="command.timeout",
                command_id=event.command_id,
                message=(
                    "Command exceeded the evaluation harness timeout and "
                    "was recorded as a failed command so exploration can continue."
                ),
                evidence=event.stderr[:240],
            )
        )
    return findings


def _network_continuity_findings(
    observations: list[CommandObservation],
) -> list[Finding]:
    findings: list[Finding] = []
    for observation in observations:
        private_count = sum(
            1 for ip in observation.ipv4_addresses if _is_private_ipv4(ip)
        )
        public_count = len(observation.ipv4_addresses) - private_count
        if public_count > 5 and private_count > 0:
            findings.append(
                Finding(
                    severity="low",
                    finding_type="topology.public_private_mix",
                    command_id=observation.command_id,
                    message=(
                        "Output mixes many public-looking IP addresses with "
                        "private topology evidence; review whether those are "
                        "real network observations or documentation noise."
                    ),
                )
            )
    return findings


def _is_private_ipv4(value: str) -> bool:
    first, second, *_ = [int(part) for part in value.split(".")]
    return (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or first == 127
    )
