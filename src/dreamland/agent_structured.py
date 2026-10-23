from __future__ import annotations

import json
import re
from dataclasses import replace

from dreamland.models import CommandResult, DreamlandCommandState

STRUCTURED_FAILURE_CLASS_METADATA = "dreamland.structured.failure_class"
STRUCTURED_RAW_RESPONSE_METADATA = "dreamland.structured.raw_response"
_STRUCTURED_RESPONSE_KEYS = frozenset(
    {"stdout", "stderr", "exit_status", "state_update"}
)
_WHOLE_CODE_FENCE_RE = re.compile(
    r"\A```[ \t]*(?P<language>[A-Za-z0-9_-]*)[ \t]*\r?\n"
    r"(?P<body>.*)\r?\n```[ \t]*\Z",
    re.DOTALL,
)


def command_result_from_model_response(
    text: str,
    state: DreamlandCommandState | None = None,
) -> CommandResult:
    from dreamland import agent_core

    stripped = _structured_response_json_text(text)
    if not stripped.startswith("{"):
        return CommandResult(stdout=agent_core._ensure_trailing_newline(text))
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return agent_core._unparsed_structured_model_response(
            text,
            f"invalid JSON: {exc.msg}",
        )
    if not isinstance(payload, dict):
        return agent_core._unparsed_structured_model_response(
            text,
            "structured response must be a JSON object",
        )
    result = agent_core._result_from_structured_model_payload(payload, state)
    if result is None:
        return agent_core._unparsed_structured_model_response(
            text,
            "structured response is missing required keys",
        )
    if "structured_parse_error" in result.metadata:
        return _result_with_structured_failure_metadata(
            result,
            text,
            str(result.metadata["structured_parse_error"]),
        )
    return result


def _structured_response_json_text(text: str) -> str:
    stripped = text.strip()
    fence_match = _WHOLE_CODE_FENCE_RE.match(stripped)
    if fence_match is not None:
        language = fence_match.group("language").lower()
        if language in {"", "json"}:
            body = fence_match.group("body").strip()
            if body.startswith("{"):
                return body
    trailing_json = _trailing_structured_json_text(stripped)
    return trailing_json or stripped


def _trailing_structured_json_text(text: str) -> str | None:
    if not text.endswith("}"):
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"{", text):
        candidate = text[match.start() :]
        try:
            payload, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if end != len(candidate):
            continue
        if isinstance(payload, dict) and _STRUCTURED_RESPONSE_KEYS <= payload.keys():
            return candidate
    return None


def _result_with_structured_failure_metadata(
    result: CommandResult,
    text: str,
    error: str,
) -> CommandResult:
    return replace(
        result,
        metadata={
            **result.metadata,
            "structured_response_accepted": False,
            STRUCTURED_FAILURE_CLASS_METADATA: classify_structured_failure(error),
            STRUCTURED_RAW_RESPONSE_METADATA: text,
        },
    )


def classify_structured_failure(error: str) -> str:
    if error.startswith("invalid JSON:"):
        return "invalid_json"
    if error == "structured response must be a JSON object":
        return "wrong_top_level_type"
    if error == "structured response is missing required keys":
        return "missing_required_keys"
    if error == "state_update must be an object":
        return "invalid_state_update"
    return "validation_error"


def structured_stdout_prefix(text: str) -> str | None:
    match = re.search(r'"stdout"\s*:\s*"', text)
    if match is None:
        return None
    return json_string_decoded_prefix(text, match.end())


def json_string_decoded_prefix(text: str, start: int) -> str:
    decoded: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            break
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            break
        escape = text[index + 1]
        if escape == "u":
            digits = text[index + 2 : index + 6]
            if len(digits) < 4 or re.fullmatch(r"[0-9A-Fa-f]{4}", digits) is None:
                break
            decoded.append(chr(int(digits, 16)))
            index += 6
            continue
        replacements = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape not in replacements:
            break
        decoded.append(replacements[escape])
        index += 2
    return "".join(decoded)
