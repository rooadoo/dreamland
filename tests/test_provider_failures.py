from __future__ import annotations

import unittest

from dreamland.provider_failures import (
    ProviderFailureClassification,
    ProviderFailureDisposition,
    ProviderFailureKind,
    classify_provider_failure,
)


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: object = None,
        body: object = None,
        response: object = None,
        code: object = None,
        error_type: object = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.response = response
        self.code = code
        self.type = error_type


class RateLimitError(ProviderError):
    pass


class AuthenticationError(ProviderError):
    pass


class PermissionDeniedError(ProviderError):
    pass


class BadRequestError(ProviderError):
    pass


class APITimeoutError(ProviderError):
    pass


class APIConnectionError(ProviderError):
    pass


class FakeResponse:
    def __init__(self, status_code: object, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class BrokenJsonResponse(FakeResponse):
    def json(self) -> object:
        raise ValueError("not JSON")


class ProviderFailureClassificationTests(unittest.TestCase):
    def test_classification_contract_rejects_unbounded_operational_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider_code"):
            ProviderFailureClassification(
                kind=ProviderFailureKind.ERROR,
                disposition=ProviderFailureDisposition.UNKNOWN,
                provider_code="raw provider prose!",
            )
        with self.assertRaisesRegex(ValueError, "status_code"):
            ProviderFailureClassification(
                kind=ProviderFailureKind.ERROR,
                disposition=ProviderFailureDisposition.UNKNOWN,
                status_code=700,
            )

    def test_classification_contract_rejects_incoherent_disposition(self) -> None:
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            ProviderFailureClassification(
                kind=ProviderFailureKind.RATE_LIMIT,
                disposition=ProviderFailureDisposition.TERMINAL,
            )
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            ProviderFailureClassification(
                kind=ProviderFailureKind.CREDIT_EXHAUSTED,
                disposition=ProviderFailureDisposition.TRANSIENT,
            )

    def test_structured_credit_code_precedes_ordinary_429(self) -> None:
        failure = classify_provider_failure(
            RateLimitError(
                "account cannot continue",
                status_code=429,
                body={
                    "error": {
                        "message": "do not retain this provider-authored text",
                        "type": "billing_error",
                        "code": "credit_balance_exhausted",
                    }
                },
            )
        )

        self.assertEqual(failure.kind, ProviderFailureKind.CREDIT_EXHAUSTED)
        self.assertEqual(
            failure.disposition,
            ProviderFailureDisposition.TERMINAL,
        )
        self.assertTrue(failure.is_terminal)
        self.assertFalse(failure.is_transient)
        self.assertEqual(failure.provider_code, "credit_balance_exhausted")
        self.assertEqual(failure.status_code, 429)
        self.assertEqual(
            failure.to_log_fields(),
            {
                "failure_kind": "provider_credit_exhausted",
                "failure_disposition": "terminal",
                "provider_code": "credit_balance_exhausted",
                "provider_status_code": 429,
            },
        )
        self.assertNotIn("provider-authored", repr(failure))

    def test_structured_quota_type_precedes_ordinary_429(self) -> None:
        failure = classify_provider_failure(
            RateLimitError(
                "request rejected",
                status_code=429,
                body={"error": {"type": "insufficient_quota"}},
            )
        )

        self.assertEqual(failure.kind, ProviderFailureKind.QUOTA_EXHAUSTED)
        self.assertEqual(
            failure.disposition,
            ProviderFailureDisposition.TERMINAL,
        )
        self.assertEqual(failure.provider_code, "insufficient_quota")

    def test_terminal_http_statuses_are_distinct(self) -> None:
        cases = (
            (401, ProviderFailureKind.AUTHENTICATION),
            (403, ProviderFailureKind.AUTHORIZATION),
            (400, ProviderFailureKind.CONFIGURATION),
            (404, ProviderFailureKind.CONFIGURATION),
            (422, ProviderFailureKind.CONFIGURATION),
            (402, ProviderFailureKind.CREDIT_EXHAUSTED),
        )

        for status_code, expected in cases:
            with self.subTest(status_code=status_code):
                failure = classify_provider_failure(
                    ProviderError("request rejected", status_code=status_code)
                )
                self.assertEqual(failure.kind, expected)
                self.assertEqual(
                    failure.disposition,
                    ProviderFailureDisposition.TERMINAL,
                )

    def test_terminal_exception_names_are_used_without_status(self) -> None:
        cases = (
            (
                AuthenticationError("bad credentials"),
                ProviderFailureKind.AUTHENTICATION,
            ),
            (PermissionDeniedError("denied"), ProviderFailureKind.AUTHORIZATION),
            (BadRequestError("bad model setting"), ProviderFailureKind.CONFIGURATION),
        )

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                failure = classify_provider_failure(error)
                self.assertEqual(failure.kind, expected)
                self.assertTrue(failure.is_terminal)

    def test_ordinary_rate_limit_is_transient(self) -> None:
        failure = classify_provider_failure(
            RateLimitError("provider budget exhausted", status_code=429)
        )

        self.assertEqual(failure.kind, ProviderFailureKind.RATE_LIMIT)
        self.assertEqual(
            failure.disposition,
            ProviderFailureDisposition.TRANSIENT,
        )
        self.assertTrue(failure.is_transient)
        self.assertIsNone(failure.provider_code)

    def test_timeout_connection_and_server_statuses_are_transient(self) -> None:
        cases = (
            (APITimeoutError("timed out"), ProviderFailureKind.TIMEOUT),
            (ProviderError("timed out", status_code=408), ProviderFailureKind.TIMEOUT),
            (APIConnectionError("connection lost"), ProviderFailureKind.UNAVAILABLE),
            (
                ProviderError("server failed", status_code=500),
                ProviderFailureKind.UNAVAILABLE,
            ),
            (
                ProviderError("overloaded", status_code=529),
                ProviderFailureKind.UNAVAILABLE,
            ),
        )

        for error, expected in cases:
            with self.subTest(error=type(error).__name__, status=error.status_code):
                failure = classify_provider_failure(error)
                self.assertEqual(failure.kind, expected)
                self.assertEqual(
                    failure.disposition,
                    ProviderFailureDisposition.TRANSIENT,
                )

    def test_response_json_supplies_nested_provider_code_and_status(self) -> None:
        response = FakeResponse(
            429,
            {
                "type": "error",
                "error": {"type": "rate_limit_error"},
            },
        )

        failure = classify_provider_failure(
            ProviderError("request rejected", response=response)
        )

        self.assertEqual(failure.kind, ProviderFailureKind.RATE_LIMIT)
        self.assertEqual(failure.provider_code, "rate_limit_error")
        self.assertEqual(failure.status_code, 429)

    def test_known_message_marker_is_bounded_to_the_machine_code(self) -> None:
        failure = classify_provider_failure(
            ProviderError(
                "Long provider explanation: credit_balance_exhausted; secret=abc123",
                status_code=429,
            )
        )

        self.assertEqual(failure.kind, ProviderFailureKind.CREDIT_EXHAUSTED)
        self.assertEqual(failure.provider_code, "credit_balance_exhausted")
        self.assertNotIn("secret", repr(failure))
        self.assertNotIn("abc123", repr(failure))

    def test_unsafe_provider_code_and_raw_body_are_not_returned(self) -> None:
        failure = classify_provider_failure(
            RateLimitError(
                "ordinary throttling",
                status_code=429,
                body={
                    "error": {
                        "code": "secret key is abc123!!",
                        "message": "another raw secret",
                    }
                },
            )
        )

        self.assertEqual(failure.kind, ProviderFailureKind.RATE_LIMIT)
        self.assertIsNone(failure.provider_code)
        rendered = repr(failure.to_log_fields())
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("raw secret", rendered)

    def test_unknown_failure_keeps_only_safe_bounded_operational_fields(self) -> None:
        failure = classify_provider_failure(
            ProviderError(
                "unrecognized provider prose",
                status_code=True,
                body={"error": {"code": "VENDOR_ODDITY"}},
                response=BrokenJsonResponse(700, {}),
            )
        )

        self.assertEqual(failure.kind, ProviderFailureKind.ERROR)
        self.assertEqual(
            failure.disposition,
            ProviderFailureDisposition.UNKNOWN,
        )
        self.assertEqual(failure.provider_code, "vendor_oddity")
        self.assertIsNone(failure.status_code)
        self.assertEqual(
            failure.to_log_fields(),
            {
                "failure_kind": "provider_error",
                "failure_disposition": "unknown",
                "provider_code": "vendor_oddity",
            },
        )


if __name__ == "__main__":
    unittest.main()
