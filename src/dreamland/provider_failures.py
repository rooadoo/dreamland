"""Deterministic, provider-neutral model failure classification.

Provider SDKs expose broadly similar HTTP failure information through different
exception attributes.  This module deliberately uses a small duck-typed surface
so command emulation and background agents can share one taxonomy without
depending on a particular SDK's exception classes.

Only bounded operational identifiers leave this boundary.  Provider response
bodies and free-form error messages are inspected only for a fixed set of known
machine markers and are never retained on the returned record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_MAX_PROVIDER_CODE_CHARS = 96
_MAX_MESSAGE_INSPECTION_CHARS = 4096
_MAX_BODY_DEPTH = 4
_MAX_BODY_NODES = 32
_SAFE_PROVIDER_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_MESSAGE_TOKEN_SEPARATOR = re.compile(r"[^a-z0-9]+")


class ProviderFailureKind(StrEnum):
    """Stable categories suitable for logs, traces, and runtime policy."""

    CREDIT_EXHAUSTED = "provider_credit_exhausted"
    QUOTA_EXHAUSTED = "provider_quota_exhausted"
    AUTHENTICATION = "provider_authentication"
    AUTHORIZATION = "provider_authorization"
    CONFIGURATION = "provider_configuration"
    RATE_LIMIT = "provider_rate_limit"
    TIMEOUT = "provider_timeout"
    UNAVAILABLE = "provider_unavailable"
    ERROR = "provider_error"


class ProviderFailureDisposition(StrEnum):
    """Whether automatic work may reasonably retry without outside action."""

    TERMINAL = "terminal"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


_EXPECTED_DISPOSITION_BY_KIND = {
    ProviderFailureKind.CREDIT_EXHAUSTED: ProviderFailureDisposition.TERMINAL,
    ProviderFailureKind.QUOTA_EXHAUSTED: ProviderFailureDisposition.TERMINAL,
    ProviderFailureKind.AUTHENTICATION: ProviderFailureDisposition.TERMINAL,
    ProviderFailureKind.AUTHORIZATION: ProviderFailureDisposition.TERMINAL,
    ProviderFailureKind.CONFIGURATION: ProviderFailureDisposition.TERMINAL,
    ProviderFailureKind.RATE_LIMIT: ProviderFailureDisposition.TRANSIENT,
    ProviderFailureKind.TIMEOUT: ProviderFailureDisposition.TRANSIENT,
    ProviderFailureKind.UNAVAILABLE: ProviderFailureDisposition.TRANSIENT,
    ProviderFailureKind.ERROR: ProviderFailureDisposition.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class ProviderFailureClassification:
    """A bounded classification that contains no provider-authored prose."""

    kind: ProviderFailureKind
    disposition: ProviderFailureDisposition
    provider_code: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProviderFailureKind):
            raise TypeError("kind must be a ProviderFailureKind")
        if not isinstance(self.disposition, ProviderFailureDisposition):
            raise TypeError("disposition must be a ProviderFailureDisposition")
        if self.disposition is not _EXPECTED_DISPOSITION_BY_KIND[self.kind]:
            raise ValueError("failure kind and disposition are inconsistent")
        if self.provider_code is not None and (
            _sanitize_provider_code(self.provider_code) != self.provider_code
        ):
            raise ValueError("provider_code must be a bounded provider identifier")
        if self.status_code is not None and (
            _bounded_http_status(self.status_code) != self.status_code
        ):
            raise ValueError("status_code must be an HTTP status integer")

    @property
    def is_terminal(self) -> bool:
        return self.disposition is ProviderFailureDisposition.TERMINAL

    @property
    def is_transient(self) -> bool:
        return self.disposition is ProviderFailureDisposition.TRANSIENT

    def to_log_fields(self) -> dict[str, str | int]:
        """Return deterministic fields safe for structured operational logs."""

        fields: dict[str, str | int] = {
            "failure_kind": self.kind.value,
            "failure_disposition": self.disposition.value,
        }
        if self.provider_code is not None:
            fields["provider_code"] = self.provider_code
        if self.status_code is not None:
            fields["provider_status_code"] = self.status_code
        return fields


_CREDIT_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "credit_balance_exhausted",
        "credit_exhausted",
        "payment_required",
    }
)
_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "monthly_limit_reached",
        "quota_exceeded",
        "quota_exhausted",
        "usage_limit_exceeded",
    }
)
_AUTHENTICATION_CODES = frozenset(
    {
        "api_key_invalid",
        "authentication_error",
        "invalid_api_key",
        "invalid_authentication",
        "unauthorized",
    }
)
_AUTHORIZATION_CODES = frozenset(
    {
        "access_denied",
        "forbidden",
        "permission_denied",
        "permission_error",
    }
)
_CONFIGURATION_CODES = frozenset(
    {
        "bad_request",
        "configuration_error",
        "invalid_model",
        "invalid_request_error",
        "model_not_found",
        "not_found_error",
        "unprocessable_entity",
        "unsupported_model",
    }
)
_RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit",
        "rate_limit_error",
        "rate_limit_exceeded",
        "too_many_requests",
    }
)
_TIMEOUT_CODES = frozenset(
    {
        "api_timeout_error",
        "request_timeout",
        "timeout_error",
    }
)
_UNAVAILABLE_CODES = frozenset(
    {
        "api_connection_error",
        "connection_error",
        "internal_server_error",
        "overloaded_error",
        "server_error",
        "service_unavailable",
    }
)

_CODE_CLASSIFICATIONS = (
    (
        _CREDIT_CODES,
        ProviderFailureKind.CREDIT_EXHAUSTED,
        ProviderFailureDisposition.TERMINAL,
    ),
    (
        _QUOTA_CODES,
        ProviderFailureKind.QUOTA_EXHAUSTED,
        ProviderFailureDisposition.TERMINAL,
    ),
    (
        _AUTHENTICATION_CODES,
        ProviderFailureKind.AUTHENTICATION,
        ProviderFailureDisposition.TERMINAL,
    ),
    (
        _AUTHORIZATION_CODES,
        ProviderFailureKind.AUTHORIZATION,
        ProviderFailureDisposition.TERMINAL,
    ),
    (
        _CONFIGURATION_CODES,
        ProviderFailureKind.CONFIGURATION,
        ProviderFailureDisposition.TERMINAL,
    ),
    (
        _RATE_LIMIT_CODES,
        ProviderFailureKind.RATE_LIMIT,
        ProviderFailureDisposition.TRANSIENT,
    ),
    (
        _TIMEOUT_CODES,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureDisposition.TRANSIENT,
    ),
    (
        _UNAVAILABLE_CODES,
        ProviderFailureKind.UNAVAILABLE,
        ProviderFailureDisposition.TRANSIENT,
    ),
)


def classify_provider_failure(error: Exception) -> ProviderFailureClassification:
    """Classify a provider exception without retaining its response or message.

    Structured provider codes take precedence over HTTP status and exception
    class.  Some providers report exhausted credit as HTTP 429, so reversing
    that order would incorrectly treat a terminal account condition as ordinary
    transient throttling.
    """

    status_code = _status_code(error)
    provider_codes = _provider_codes(error)
    classified = _classification_from_codes(provider_codes, status_code=status_code)
    if classified is not None:
        return classified

    message_code = _known_message_code(error)
    if message_code is not None:
        classified = _classification_from_codes(
            (message_code,),
            status_code=status_code,
        )
        if classified is not None:
            return classified

    provider_code = provider_codes[0] if provider_codes else None
    error_name = type(error).__name__.lower()

    if status_code == 402:
        return _classification(
            ProviderFailureKind.CREDIT_EXHAUSTED,
            ProviderFailureDisposition.TERMINAL,
            provider_code,
            status_code,
        )
    if status_code == 401 or "authentication" in error_name:
        return _classification(
            ProviderFailureKind.AUTHENTICATION,
            ProviderFailureDisposition.TERMINAL,
            provider_code,
            status_code,
        )
    if status_code == 403 or any(
        marker in error_name for marker in ("permissiondenied", "forbidden")
    ):
        return _classification(
            ProviderFailureKind.AUTHORIZATION,
            ProviderFailureDisposition.TERMINAL,
            provider_code,
            status_code,
        )
    if status_code in {400, 404, 422} or any(
        marker in error_name
        for marker in ("badrequest", "configuration", "notfound", "unprocessable")
    ):
        return _classification(
            ProviderFailureKind.CONFIGURATION,
            ProviderFailureDisposition.TERMINAL,
            provider_code,
            status_code,
        )
    if status_code == 429 or any(
        marker in error_name for marker in ("ratelimit", "rate_limit")
    ):
        return _classification(
            ProviderFailureKind.RATE_LIMIT,
            ProviderFailureDisposition.TRANSIENT,
            provider_code,
            status_code,
        )
    if status_code == 408 or "timeout" in error_name:
        return _classification(
            ProviderFailureKind.TIMEOUT,
            ProviderFailureDisposition.TRANSIENT,
            provider_code,
            status_code,
        )
    if (status_code is not None and 500 <= status_code <= 599) or any(
        marker in error_name for marker in ("connection", "unavailable")
    ):
        return _classification(
            ProviderFailureKind.UNAVAILABLE,
            ProviderFailureDisposition.TRANSIENT,
            provider_code,
            status_code,
        )
    return _classification(
        ProviderFailureKind.ERROR,
        ProviderFailureDisposition.UNKNOWN,
        provider_code,
        status_code,
    )


def _classification_from_codes(
    provider_codes: tuple[str, ...],
    *,
    status_code: int | None,
) -> ProviderFailureClassification | None:
    for codes, kind, disposition in _CODE_CLASSIFICATIONS:
        matching = next((code for code in provider_codes if code in codes), None)
        if matching is not None:
            return _classification(kind, disposition, matching, status_code)
    return None


def _classification(
    kind: ProviderFailureKind,
    disposition: ProviderFailureDisposition,
    provider_code: str | None,
    status_code: int | None,
) -> ProviderFailureClassification:
    return ProviderFailureClassification(
        kind=kind,
        disposition=disposition,
        provider_code=provider_code,
        status_code=status_code,
    )


def _status_code(error: Exception) -> int | None:
    direct = _bounded_http_status(getattr(error, "status_code", None))
    if direct is not None:
        return direct
    response = getattr(error, "response", None)
    return _bounded_http_status(getattr(response, "status_code", None))


def _bounded_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 100 <= value <= 599:
        return value
    return None


def _provider_codes(error: Exception) -> tuple[str, ...]:
    candidates: list[object] = [
        getattr(error, "code", None),
        getattr(error, "type", None),
    ]
    body = getattr(error, "body", None)
    candidates.extend(_body_code_candidates(body))

    response = getattr(error, "response", None)
    response_body = _response_json(response)
    if response_body is not body:
        candidates.extend(_body_code_candidates(response_body))

    result: list[str] = []
    for candidate in candidates:
        sanitized = _sanitize_provider_code(candidate)
        if sanitized is None or sanitized in result:
            continue
        result.append(sanitized)
    return tuple(result)


def _body_code_candidates(body: object) -> tuple[object, ...]:
    if not isinstance(body, Mapping):
        return ()
    code_values: list[object] = []
    type_values: list[object] = []
    pending: list[tuple[Mapping[object, object], int]] = [(body, 0)]
    visited = 0
    while pending and visited < _MAX_BODY_NODES:
        current, depth = pending.pop(0)
        for key, value in current.items():
            visited += 1
            if visited > _MAX_BODY_NODES:
                break
            if key == "code":
                code_values.append(value)
            elif key == "type":
                type_values.append(value)
            elif isinstance(value, Mapping) and depth < _MAX_BODY_DEPTH:
                pending.append((value, depth + 1))
    return tuple((*code_values, *type_values))


def _response_json(response: object) -> object:
    if response is None:
        return None
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        return None
    try:
        return json_method()
    except Exception:
        return None


def _sanitize_provider_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    if (
        not code
        or len(code) > _MAX_PROVIDER_CODE_CHARS
        or _SAFE_PROVIDER_CODE.fullmatch(code) is None
    ):
        return None
    return code


def _known_message_code(error: Exception) -> str | None:
    try:
        message = str(error)[:_MAX_MESSAGE_INSPECTION_CHARS].lower()
    except Exception:
        return None
    normalized = _MESSAGE_TOKEN_SEPARATOR.sub("_", message).strip("_")
    bounded = f"_{normalized}_"
    for codes, _kind, _disposition in _CODE_CLASSIFICATIONS:
        matching = next(
            (
                code
                for code in sorted(codes, key=lambda value: (-len(value), value))
                if f"_{code}_" in bounded
            ),
            None,
        )
        if matching is not None:
            return matching
    return None


__all__ = [
    "ProviderFailureClassification",
    "ProviderFailureDisposition",
    "ProviderFailureKind",
    "classify_provider_failure",
]
