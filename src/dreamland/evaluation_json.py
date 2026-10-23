"""Offline, evidence-based classification of emulator JSON interventions.

This module deliberately does not import the runtime parser. Reconstruction is
valid only after the caller verifies all recorded runtime sources against
``SUPPORTED_RUNTIME_SOURCE_SHA256``. It does not repair responses or change
execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

POLICY_VERSION = "emulator-json-v1"
SCORE_NAME = "dreamland.json_status"
PARSER_SOURCE_PATH = "src/dreamland/agent_structured.py"
SUPPORTED_PARSER_SHA256 = frozenset(
    {"9d8144f0a48bd7b002ae4fa41c0428029b44b448f623726ae2f5e3e0e5fb7b69"}
)
SUPPORTED_RUNTIME_SOURCE_SHA256 = {
    PARSER_SOURCE_PATH: SUPPORTED_PARSER_SHA256,
    "src/dreamland/agent_core.py": frozenset(
        {
            "84298e21d62f24557bdf9a3aa81d442b9664bafdfc357ae9fb42e8ef6a60112c",
            # Retirement-only addition; audit: docs/EMULATION_JSON_ANNOTATIONS.md.
            "643d32a12c0561be1c0086407f9e8aabd6e011374cb0e1108315893639276847",
            # Network projection and JSON mode; same extraction/coercion rules.
            "ab3ad3a6e839ee25cee83854b7bf50822dd495bef11271b819daefb891e68a07",
            # Whole-line routing and ordered effects; documented compatibility audit.
            "9c7462c03a4a695330409b5366a42d45538fe2c0b471a973be8e80d9d0759903",
            # Active-host prompt deduplication; unchanged response processing.
            "41373ebb277bc7e9cb47808d1d9504985f0abd53305f8920e9e44878610b58d4",
            # Interactive input lifecycle; same JSON extraction/output coercions.
            "09adb254658450c96011a0e14e3be49e9050785e59d5602a4c0f50c7d0270d76",
            # World-owned entry banner/profile removal; unchanged response processing.
            "2196009a70f62e38458d8806eac069bb2b8bffd94abd6e63b692f5660acae526",
        }
    ),
    "src/dreamland/agent_state.py": frozenset(
        {"b429531d133f4cc232bd4e7d09ffff411d20f29ef40d7d7e79023ba1ab949998"}
    ),
}

JsonStatus = Literal["repaired", "rejected", "none_observed", "unknown", "out_of_scope"]


@dataclass(frozen=True)
class JsonAssessment:
    status: JsonStatus
    reason: str
    events: tuple[dict[str, object], ...] = ()


# Extension boundary: add a stable rule ID and its evidence check together, and
# bump POLICY_VERSION when changing classification semantics. A future syntax
# repair must have its own verified parser baseline or explicit runtime marker;
# never infer that a repair occurred merely because malformed text is reparable.
STRUCTURAL_RULE_IDS = (
    "invalid_json",
    "markdown_fence_removed",
    "leading_text_removed",
)

_FAILURE_CLASS = "dreamland.structured.failure_class"
_RAW_RESPONSE = "dreamland.structured.raw_response"
_REQUIRED_KEYS = frozenset({"stdout", "stderr", "exit_status", "state_update"})
_OUT_OF_SCOPE_FAILURES = frozenset(
    {
        "wrong_top_level_type",
        "missing_required_keys",
        "invalid_state_update",
        "validation_error",
    }
)
_OUT_OF_SCOPE_ERRORS = frozenset(
    {
        "structured response must be a JSON object",
        "structured response is missing required keys",
        "state_update must be an object",
    }
)
_FENCE = re.compile(
    r"\A```[ \t]*(?P<language>[A-Za-z0-9_-]*)[ \t]*\r?\n"
    r"(?P<body>.*)\r?\n```[ \t]*\Z",
    re.DOTALL,
)


def assess_json_response(
    raw: str | None,
    result: Mapping[str, object],
    *,
    parser_verified: bool,
) -> JsonAssessment:
    """Assess one raw model response and its corresponding command result.

    ``result`` has top-level stdout/stderr/exit_status/interrupted fields and a
    nested metadata mapping. Explicit rejection markers are runtime evidence;
    repair and no-intervention conclusions require verified reconstruction and
    matching processed output. ``parser_verified`` means that every source in
    SUPPORTED_RUNTIME_SOURCE_SHA256 was verified for the recorded runtime.
    Reasons and events never contain response text.
    """
    metadata = result.get("metadata", {})
    if not isinstance(metadata, Mapping):
        return JsonAssessment("unknown", "invalid_result_metadata")
    if result.get("interrupted", False):
        return JsonAssessment("unknown", "interrupted_response")
    if raw is not None and not isinstance(raw, str):
        return JsonAssessment("unknown", "invalid_raw_response")
    recorded_raw = metadata.get(_RAW_RESPONSE)
    if _RAW_RESPONSE in metadata:
        if not isinstance(recorded_raw, str):
            return JsonAssessment("unknown", "invalid_recorded_raw_response")
        if raw is not None and raw != recorded_raw:
            return JsonAssessment("unknown", "contradictory_raw_response")
        if raw is None:
            raw = recorded_raw

    accepted = metadata.get("structured_response_accepted")
    failure_class = metadata.get(_FAILURE_CLASS)
    parse_error = metadata.get("structured_parse_error")
    if failure_class == "invalid_json":
        if accepted is not False or (
            parse_error is not None
            and (
                not isinstance(parse_error, str)
                or not parse_error.startswith("invalid JSON:")
            )
        ):
            return JsonAssessment("unknown", "contradictory_rejection_marker")
        return _assess_marked_rejection(raw, result, parser_verified=parser_verified)
    if accepted is False:
        if (
            isinstance(failure_class, str) and failure_class in _OUT_OF_SCOPE_FAILURES
        ) or (isinstance(parse_error, str) and parse_error in _OUT_OF_SCOPE_ERRORS):
            return JsonAssessment("out_of_scope", "non_json_structured_rejection")
        return JsonAssessment("unknown", "unclassified_rejection_marker")
    if failure_class is not None or parse_error is not None:
        return JsonAssessment("unknown", "contradictory_rejection_marker")
    if accepted is not None and accepted is not True:
        return JsonAssessment("unknown", "invalid_acceptance_marker")
    if not parser_verified:
        return JsonAssessment("unknown", "runtime_parser_unverified")
    if raw is None:
        return JsonAssessment("unknown", "missing_raw_response")

    try:
        candidate, repair_rule = _extract_candidate(raw)
    except (ValueError, RecursionError):
        return JsonAssessment("unknown", "unreconstructable_raw_response")
    if not candidate.startswith("{"):
        return JsonAssessment("unknown", "unrecognized_structured_response")
    try:
        payload = json.loads(candidate)
    except (ValueError, RecursionError):
        return JsonAssessment("unknown", "missing_invalid_json_rejection_marker")
    if not isinstance(payload, dict):
        return JsonAssessment("unknown", "unrecognized_structured_response")
    if not _REQUIRED_KEYS <= payload.keys() or not isinstance(
        payload["state_update"], dict
    ):
        return JsonAssessment("out_of_scope", "non_json_structured_validation")
    if not _processed_output_matches(payload, result):
        return JsonAssessment("unknown", "processed_result_mismatch")
    if repair_rule is not None:
        return JsonAssessment(
            "repaired",
            "verified_json_envelope_recovery",
            (_event("repaired", repair_rule, "reconstructed"),),
        )
    if metadata.get("dreamland.transition.accepted") is False or metadata.get(
        "dreamland.transition.normalized"
    ):
        return JsonAssessment("out_of_scope", "non_json_transition_intervention")
    return JsonAssessment("none_observed", "verified_unmodified_json_envelope")


def _assess_marked_rejection(
    raw: str | None,
    result: Mapping[str, object],
    *,
    parser_verified: bool,
) -> JsonAssessment:
    if raw is not None:
        try:
            candidate = _extract_candidate(raw)[0] if parser_verified else raw.strip()
        except (ValueError, RecursionError):
            return JsonAssessment("unknown", "unreconstructable_raw_response")
        if parser_verified and not candidate.startswith("{"):
            return JsonAssessment("unknown", "contradictory_rejection_raw_response")
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            pass
        except (ValueError, RecursionError):
            return JsonAssessment("unknown", "unreconstructable_raw_response")
        else:
            return JsonAssessment("unknown", "contradictory_rejection_raw_response")
        if parser_verified:
            expected_stdout = raw if not raw or raw.endswith("\n") else raw + "\n"
            expected = {"stdout": expected_stdout, "stderr": "", "exit_status": 0}
            if any(
                key in result and result[key] != value
                for key, value in expected.items()
            ):
                return JsonAssessment("unknown", "processed_result_mismatch")
    return JsonAssessment(
        "rejected",
        "invalid_json_runtime_marker",
        (_event("rejected", "invalid_json", "runtime_marker"),),
    )


def _extract_candidate(raw: str) -> tuple[str, str | None]:
    """Reconstruct only the extraction steps in the supported parser source."""
    stripped = raw.strip()
    fence = _FENCE.match(stripped)
    if fence is not None and fence.group("language").lower() in {"", "json"}:
        body = fence.group("body").strip()
        if body.startswith("{"):
            return body, "markdown_fence_removed"
    if stripped.endswith("}"):
        decoder = json.JSONDecoder()
        for match in re.finditer(r"{", stripped):
            candidate = stripped[match.start() :]
            try:
                payload, end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if (
                end == len(candidate)
                and isinstance(payload, dict)
                and _REQUIRED_KEYS <= payload.keys()
            ):
                return candidate, "leading_text_removed" if match.start() else None
    return stripped, None


def _processed_output_matches(
    payload: Mapping[str, object], result: Mapping[str, object]
) -> bool:
    exit_status = payload["exit_status"]
    if isinstance(exit_status, (str, bytes, bytearray, int, float)):
        try:
            exit_status = int(exit_status)
        except (TypeError, ValueError):
            exit_status = 0
        except OverflowError:
            # The runtime raises for infinity instead of returning a result.
            return False
    else:
        exit_status = 0
    expected = {
        "stdout": "" if payload["stdout"] is None else str(payload["stdout"]),
        "stderr": "" if payload["stderr"] is None else str(payload["stderr"]),
        "exit_status": exit_status,
    }
    return all(
        key in result and result[key] == value for key, value in expected.items()
    )


def _event(status: str, rule: str, evidence: str) -> dict[str, object]:
    return {"status": status, "rule": rule, "evidence": evidence}
