"""Serve the integration-only Phase 2 product-session core on loopback.

This executable is a BFF compatibility harness, not DREAMLAND runtime
composition.  It is imported by no normal startup path, calls no provider, and
never reports a live product capability as enabled.  Its deterministic
executor exists only to exercise the protocol-neutral product core through an
HTTP boundary.
"""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from http import HTTPStatus
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from dreamland.product.core import ProductCorePolicy, ProductSessionCoreService
from dreamland.product.execution import (
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputStream,
    ProductStateEffect,
    ProductStateEffectKind,
)
from dreamland.product.journal import EventCursor, TranscriptCursor
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)


def _load_lifecycle_preview_transport() -> ModuleType:
    """Load the hardened Phase 1 HTTP boundary without making tools a package."""

    try:
        import product_lifecycle_preview as module

        return module
    except ModuleNotFoundError:
        path = Path(__file__).with_name("product_lifecycle_preview.py")
        specification = importlib.util.spec_from_file_location(
            "dreamland_product_lifecycle_preview_transport",
            path,
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("lifecycle preview transport is unavailable") from None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module


_LIFECYCLE_TRANSPORT = _load_lifecycle_preview_transport()
_LifecyclePreviewServerBase: Any = _LIFECYCLE_TRANSPORT.LifecyclePreviewServer
_LifecyclePreviewHandlerBase: Any = _LIFECYCLE_TRANSPORT.LifecyclePreviewHandler

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.3.0-rc.1"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
PREVIEW_EXECUTOR = "deterministic-no-provider-v1"
PREVIEW_PROVIDER_CALLS = False
PREVIEW_TEST_ONLY = True

CAPABILITIES_PATH = "/product/v1/capabilities"
CURRENT_PATH = "/product/v1/sessions/current"
SESSIONS_PATH = "/product/v1/sessions"
SESSION_PATH_PREFIX = "/product/v1/sessions/"

EXPECTED_EPOCH_HEADER = _LIFECYCLE_TRANSPORT.EXPECTED_EPOCH_HEADER
ACTOR_CONTEXT_HEADER = _LIFECYCLE_TRANSPORT.ACTOR_CONTEXT_HEADER
SESSION_CAPABILITY_HEADER = _LIFECYCLE_TRANSPORT.SESSION_CAPABILITY_HEADER
IDEMPOTENCY_HEADER = _LIFECYCLE_TRANSPORT.IDEMPOTENCY_HEADER
LAST_EVENT_ID_HEADER = "Last-Event-ID"

MAX_REQUEST_BODY_BYTES = _LIFECYCLE_TRANSPORT.MAX_REQUEST_BODY_BYTES
MAX_HEADER_VALUE_CHARACTERS = _LIFECYCLE_TRANSPORT.MAX_HEADER_VALUE_CHARACTERS
MAX_CONTROL_REVISION = _LIFECYCLE_TRANSPORT.MAX_CONTROL_REVISION
MAX_CONCURRENT_CONNECTIONS = _LIFECYCLE_TRANSPORT.MAX_CONCURRENT_CONNECTIONS
MILLISECONDS_PER_MINUTE = _LIFECYCLE_TRANSPORT.MILLISECONDS_PER_MINUTE

SERVICE_EPOCH_PATTERN = _LIFECYCLE_TRANSPORT.SERVICE_EPOCH_PATTERN
ACTOR_CONTEXT_PATTERN = _LIFECYCLE_TRANSPORT.ACTOR_CONTEXT_PATTERN
SESSION_CAPABILITY_PATTERN = _LIFECYCLE_TRANSPORT.SESSION_CAPABILITY_PATTERN
SESSION_REFERENCE_PATTERN = _LIFECYCLE_TRANSPORT.SESSION_REFERENCE_PATTERN
LAUNCH_GRANT_PATTERN = _LIFECYCLE_TRANSPORT.LAUNCH_GRANT_PATTERN
IDEMPOTENCY_KEY_PATTERN = _LIFECYCLE_TRANSPORT.IDEMPOTENCY_KEY_PATTERN
COMMAND_ID_PATTERN = re.compile(r"^command:[A-Za-z0-9_-]{16,128}$")
EVENT_ID_PATTERN = re.compile(
    r"^event:(?P<stream>[A-Za-z0-9_-]{8,96}):(?P<sequence>[1-9][0-9]*)$"
)
ENTRY_ID_PATTERN = re.compile(
    r"^entry:(?P<transcript>[A-Za-z0-9_-]{8,96}):(?P<position>[1-9][0-9]*)$"
)
MEDIA_RANGE_PATTERN = re.compile(
    r"^(?P<type>\*|[!#$%&'*+.^_`|~0-9A-Za-z-]+)/"
    r"(?P<subtype>\*|[!#$%&'*+.^_`|~0-9A-Za-z-]+)$"
)
QUALITY_PATTERN = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")

CAPABILITY_NAMES = _LIFECYCLE_TRANSPORT.CAPABILITY_NAMES
SCENARIOS = (
    "core-happy",
    "command-accept-response-lost",
    "promised-terminal-event-withheld",
)

PREVIEW_ACTOR_A_CONTEXT = "actor:preview_alpha_0001"
PREVIEW_ACTOR_B_CONTEXT = "actor:preview_bravo_0001"
PREVIEW_ACTOR_A_ID = "preview-actor-alpha"
PREVIEW_ACTOR_B_ID = "preview-actor-bravo"
PREVIEW_CREATE_GRANT_A = "launch-grant:preview_core_alpha_create_000001"
PREVIEW_CREATE_GRANT_B = "launch-grant:preview_core_bravo_create_000001"
PREVIEW_SWITCH_GRANT_A = "launch-grant:preview_core_alpha_switch_000001"
PREVIEW_REVISION_A = "world-revision:preview_core_alpha_0001"
PREVIEW_REVISION_B = "world-revision:preview_core_bravo_0001"
PREVIEW_SELECTED_RECORD_A = "selected-record:preview_core_alpha_0001"
PREVIEW_SELECTED_RECORD_B = "selected-record:preview_core_bravo_0001"
PREVIEW_POLICY_ID = "policy:core-integration-preview-v1"
PREVIEW_NOTICE_VERSION = "notice:core-integration-preview-v1"

EXPECTED_CONTRACT_PATHS = frozenset(
    {
        CAPABILITIES_PATH,
        SESSIONS_PATH,
        CURRENT_PATH,
        f"{SESSION_PATH_PREFIX}{{reference}}",
        f"{SESSION_PATH_PREFIX}{{reference}}/transcript",
        f"{SESSION_PATH_PREFIX}{{reference}}/events",
        f"{SESSION_PATH_PREFIX}{{reference}}/commands",
        f"{SESSION_PATH_PREFIX}{{reference}}/commands/{{command_id}}",
        f"{SESSION_PATH_PREFIX}{{reference}}/commands/{{command_id}}/cancel",
        f"{SESSION_PATH_PREFIX}{{reference}}/reset",
        f"{SESSION_PATH_PREFIX}{{reference}}/switch",
        f"{SESSION_PATH_PREFIX}{{reference}}/close",
    }
)

SAFE_ERROR_DETAILS: dict[str, tuple[int, str, str, int | None]] = {
    "unauthorized": (
        401,
        "Missing or invalid private product authority.",
        "none",
        None,
    ),
    "not_found": (
        404,
        "The requested private product resource is unavailable.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        400,
        "Expected product service epoch evidence is required.",
        "none",
        None,
    ),
    "session_lost": (
        410,
        "The product session was lost after service restart.",
        "none",
        None,
    ),
    "outcome_unknown": (
        410,
        "The prior product mutation outcome is unknown after service restart.",
        "none",
        None,
    ),
    "authority_denied": (
        403,
        "The requested product resource is unavailable.",
        "none",
        None,
    ),
    "invalid_request": (400, "The private product request is invalid.", "none", None),
    "invalid_json": (400, "The request body is not valid JSON.", "none", None),
    "unsupported_media_type": (
        415,
        "The request content type is unsupported.",
        "none",
        None,
    ),
    "content_length_required": (
        411,
        "A Content-Length header is required.",
        "none",
        None,
    ),
    "request_too_large": (
        413,
        "The request body exceeds the product preview limit.",
        "none",
        None,
    ),
    "idempotency_conflict": (
        409,
        "The idempotency key was already used for another request.",
        "none",
        None,
    ),
    "current_session_exists": (
        409,
        "A current product session already exists.",
        "resume_snapshot",
        None,
    ),
    "control_revision_conflict": (
        409,
        "The product session control revision changed.",
        "resume_snapshot",
        None,
    ),
    "command_in_progress": (
        409,
        "A product command is already in progress.",
        "resume_snapshot",
        None,
    ),
    "command_slot_conflict": (
        409,
        "The product command slot generation changed.",
        "resume_snapshot",
        None,
    ),
    "command_not_found": (
        404,
        "The product command is unavailable.",
        "resume_snapshot",
        None,
    ),
    "command_limit_reached": (
        409,
        "The product session command limit was reached.",
        "none",
        None,
    ),
    "command_too_large": (
        413,
        "The product command exceeds this session's limit.",
        "none",
        None,
    ),
    "invalid_command_encoding": (
        422,
        "Command input must be well-formed UTF-8.",
        "none",
        None,
    ),
    "invalid_command_text": (
        422,
        "Command text contains unsupported characters.",
        "none",
        None,
    ),
    "invalid_command_syntax": (
        422,
        "Command syntax is incomplete or unsupported.",
        "none",
        None,
    ),
    "compound_syntax_unsupported": (
        422,
        "Shell-control syntax is unsupported in the browser terminal; use SSH for compound shell workflows.",
        "none",
        None,
    ),
    "snapshot_required": (
        409,
        "The product cursor requires an authoritative snapshot.",
        "resume_snapshot",
        None,
    ),
    "session_closed": (
        409,
        "The product session is no longer active.",
        "resume_snapshot",
        None,
    ),
    "replacement_unavailable": (
        503,
        "The replacement product session could not be provisioned.",
        "same_request",
        None,
    ),
    "capacity_exhausted": (
        503,
        "Product command capacity is temporarily exhausted.",
        "after_delay",
        1_000,
    ),
    "hosted_budget_exhausted": (
        503,
        "Product hosted-work budget is temporarily unavailable.",
        "after_delay",
        1_000,
    ),
    "resource_unavailable": (
        409,
        "Product command resources are unavailable.",
        "resume_snapshot",
        None,
    ),
    "internal_error": (
        500,
        "The product preview request could not be completed.",
        "none",
        None,
    ),
}


def _preview_context() -> dict[str, object]:
    return {
        "test_only": PREVIEW_TEST_ONLY,
        "integration_only": True,
        "executor": PREVIEW_EXECUTOR,
        "provider_calls": PREVIEW_PROVIDER_CALLS,
        "live_capability_evidence": False,
    }


def _contract_context() -> dict[str, str]:
    return {
        "name": CONTRACT_NAME,
        "revision": CONTRACT_REVISION,
        "compatibility": CONTRACT_COMPATIBILITY,
    }


def _decorate_success(
    domain: Mapping[str, object],
    *,
    request_id: str,
) -> dict[str, object]:
    value = deepcopy(dict(domain))
    if "contract" in value or "preview" in value or "transport_request_id" in value:
        raise ValueError("domain result collides with preview envelope")
    value["contract"] = _contract_context()
    value["preview"] = _preview_context()
    value["transport_request_id"] = request_id
    return value


def _problem(
    *,
    code: str,
    message: str,
    retry: str,
    retry_after_ms: int | None,
    request_id: str,
) -> dict[str, object]:
    return {
        "contract": _contract_context(),
        "preview": _preview_context(),
        "error": {
            "code": code,
            "message": message,
            "retry": retry,
            "retry_after_ms": retry_after_ms,
            "request_id": request_id,
        },
    }


def _transport_problem(code: str, request_id: str) -> dict[str, object]:
    if code not in SAFE_ERROR_DETAILS:
        code = "internal_error"
    _status, message, retry, retry_after_ms = SAFE_ERROR_DETAILS[code]
    return _problem(
        code=code,
        message=message,
        retry=retry,
        retry_after_ms=retry_after_ms,
        request_id=request_id,
    )


def _domain_problem(error: ProductLifecycleError, request_id: str) -> dict[str, object]:
    return _transport_problem(error.code, request_id)


def _domain_status(error: ProductLifecycleError) -> HTTPStatus:
    details = SAFE_ERROR_DETAILS.get(error.code, SAFE_ERROR_DETAILS["internal_error"])
    return HTTPStatus(details[0])


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_LIFECYCLE_TRANSPORT._reject_duplicate_keys,
            parse_constant=_LIFECYCLE_TRANSPORT._reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid product-core contract artifact") from error
    if not isinstance(value, dict):
        raise ValueError("product-core contract artifact must be an object")
    return value


def validate_contract_root(contract_root: Path) -> Path:
    """Accept only the unfrozen, disabled Phase 2 integration-preview RC."""

    root = contract_root.resolve()
    validator_path = Path(__file__).with_name("product_session_contract.py")
    try:
        specification = importlib.util.spec_from_file_location(
            "dreamland_product_session_contract_validator",
            validator_path,
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("product-session contract validator could not be loaded")
        validator = importlib.util.module_from_spec(specification)
        module_name = specification.name
        prior_module = sys.modules.get(module_name)
        sys.modules[module_name] = validator
        try:
            specification.loader.exec_module(validator)
        finally:
            if prior_module is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = prior_module
        validate = getattr(validator, "validate_contract", None)
        if not callable(validate):
            raise RuntimeError("product-session contract validator API is unavailable")
        if (
            getattr(validator, "CONTRACT_NAME", None) != CONTRACT_NAME
            or getattr(validator, "CONTRACT_REVISION", None) != CONTRACT_REVISION
            or getattr(validator, "CONTRACT_COMPATIBILITY", None)
            != CONTRACT_COMPATIBILITY
            or tuple(getattr(validator, "CAPABILITY_NAMES", ()))
            != tuple(CAPABILITY_NAMES)
            or frozenset(getattr(validator, "EXPECTED_OPERATIONS", ()))
            != EXPECTED_CONTRACT_PATHS
            or getattr(validator, "SAFE_ERROR_CASES", None) != SAFE_ERROR_DETAILS
        ):
            raise RuntimeError(
                "product-session contract policy differs from the preview"
            )
        validation = validate(root)
    except Exception as error:
        raise ValueError(
            "product-core preview contract failed complete artifact validation"
        ) from error
    if not isinstance(validation, Mapping) or (
        validation.get("contract_revision") != CONTRACT_REVISION
        or validation.get("compatibility") != CONTRACT_COMPATIBILITY
        or validation.get("implementation_status") != "integration_preview_only"
        or validation.get("routes") != len(EXPECTED_CONTRACT_PATHS)
        or validation.get("recovery_scenarios") != 7
        or validation.get("capabilities_enabled") != 0
    ):
        raise ValueError("product-core contract validation result is incompatible")

    manifest = _load_json_object(root / "manifest.json")
    openapi = _load_json_object(root / "openapi.json")
    info = openapi.get("info")
    if (
        manifest.get("contract_revision") != CONTRACT_REVISION
        or manifest.get("compatibility") != CONTRACT_COMPATIBILITY
        or manifest.get("implementation_status") != "integration_preview_only"
        or openapi.get("openapi") != "3.1.0"
        or not isinstance(info, Mapping)
        or info.get("version") != CONTRACT_REVISION
        or openapi.get("servers") != []
    ):
        raise ValueError("product-core preview requires the exact unfrozen Phase 2 RC")
    paths = openapi.get("paths")
    if not isinstance(paths, Mapping) or frozenset(paths) != EXPECTED_CONTRACT_PATHS:
        raise ValueError("product-core preview contract routes are incompatible")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, Mapping) or frozenset(capabilities) != frozenset(
        CAPABILITY_NAMES
    ):
        raise ValueError("product-core manifest capability set is incompatible")
    if any(
        not isinstance(value, Mapping)
        or value.get("enabled") is not False
        or value.get("enabled_revision") is not None
        for value in capabilities.values()
    ):
        raise ValueError("product-core preview capabilities must remain disabled")
    for path_item in paths.values():
        if not isinstance(path_item, Mapping):
            raise ValueError("product-core preview path item is invalid")
        for operation in path_item.values():
            if (
                not isinstance(operation, Mapping)
                or operation.get("x-dreamland-availability")
                != "integration-preview-only"
            ):
                raise ValueError("product-core operation is not integration-only")
    return root


class DeterministicPreviewExecutor:
    """A bounded simulator that performs no provider, network, or host command work."""

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: Any,
    ) -> ProductExecutionResult:
        if request.canonical_command == "preview-hold":
            stop_at = time.monotonic() + 2.0
            while not request.cancelled() and time.monotonic() < stop_at:
                time.sleep(0.005)
            if request.cancelled():
                return ProductExecutionResult.interrupted("cancelled")
            return ProductExecutionResult.failed("preview_hold_elapsed")

        if request.cancelled():
            return ProductExecutionResult.interrupted("cancelled")
        emit(
            ProductOutputChunk(
                ProductOutputStream.STDOUT,
                f"integration preview accepted: {request.canonical_command}\n",
            )
        )
        usage_tokens = min(request.max_tokens, 1)
        usage_spend_units = min(request.max_spend_units, 1)
        if request.canonical_command == "preview-signaled":
            return ProductExecutionResult.completed_signaled(
                signal="SIGTERM",
                usage_tokens=usage_tokens,
                usage_spend_units=usage_spend_units,
            )
        if request.canonical_command == "preview-state-rejected":
            return ProductExecutionResult.completed_exited(
                exit_status=0,
                state_effect=ProductStateEffect(
                    ProductStateEffectKind.REJECTED,
                    safe_reason="preview_rejected",
                ),
                usage_tokens=usage_tokens,
                usage_spend_units=usage_spend_units,
            )
        provisional = deepcopy(dict(request.provisional_document))
        preview_state = provisional.get("integration_preview")
        count = 0
        if isinstance(preview_state, Mapping):
            prior_count = preview_state.get("command_count")
            if isinstance(prior_count, int) and not isinstance(prior_count, bool):
                count = prior_count
        provisional["integration_preview"] = {
            "command_count": count + 1,
            "last_command": request.canonical_command,
        }
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            state_effect=ProductStateEffect(
                ProductStateEffectKind.COMMITTED,
                provisional_document=provisional,
            ),
            usage_tokens=usage_tokens,
            usage_spend_units=usage_spend_units,
        )


class CorePreviewHarness:
    """Own the explicit preview service and its bounded deterministic workers."""

    def __init__(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown product-core preview scenario")
        self.scenario = scenario
        self.clock = _LIFECYCLE_TRANSPORT.MutablePreviewClock()
        self._workers = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="dreamland-core-preview",
        )
        self._future_lock = threading.Lock()
        self._futures: set[Future[object]] = set()
        self._closed = False
        self._fault_lock = threading.Lock()
        self._command_accept_response_lost = False
        self._terminal_event_response_withheld = False
        try:
            self.service = self._build_service()
        except Exception:
            self._workers.shutdown(wait=True, cancel_futures=True)
            raise

    def _build_service(self) -> ProductSessionCoreService:
        now_ms = self.clock.now_ms()
        seed_expiry = now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE
        launch_limits = {
            "absolute_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
            "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
            "max_commands": 8,
            "max_command_bytes": 4_096,
            "command_reserved_tokens": 32,
            "command_reserved_spend_units": 32,
            "command_max_attempts": 1,
            "command_deadline_ms": 10_000,
        }
        return ProductSessionCoreService(
            revisions=(
                ImmutableRevisionSeed(
                    revision_id=PREVIEW_REVISION_A,
                    document={"host": "preview-alpha", "nested": {"seed": "alpha"}},
                ),
                ImmutableRevisionSeed(
                    revision_id=PREVIEW_REVISION_B,
                    document={"host": "preview-bravo", "nested": {"seed": "bravo"}},
                ),
            ),
            actors=(
                ActorAuthoritySeed(
                    token=PREVIEW_ACTOR_A_CONTEXT,
                    actor_id=PREVIEW_ACTOR_A_ID,
                    role=ProductRole.ATTENDEE,
                    expires_at_ms=seed_expiry,
                ),
                ActorAuthoritySeed(
                    token=PREVIEW_ACTOR_B_CONTEXT,
                    actor_id=PREVIEW_ACTOR_B_ID,
                    role=ProductRole.PRESENTER,
                    expires_at_ms=seed_expiry,
                ),
            ),
            launch_grants=(
                LaunchGrantSeed(
                    token=PREVIEW_CREATE_GRANT_A,
                    selected_record_id=PREVIEW_SELECTED_RECORD_A,
                    actor_id=PREVIEW_ACTOR_A_ID,
                    role=ProductRole.ATTENDEE,
                    revision_id=PREVIEW_REVISION_A,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset({ProductOperation.CREATE}),
                    limits=launch_limits,
                    policy_id=PREVIEW_POLICY_ID,
                    notice_version=PREVIEW_NOTICE_VERSION,
                ),
                LaunchGrantSeed(
                    token=PREVIEW_SWITCH_GRANT_A,
                    selected_record_id=PREVIEW_SELECTED_RECORD_B,
                    actor_id=PREVIEW_ACTOR_A_ID,
                    role=ProductRole.ATTENDEE,
                    revision_id=PREVIEW_REVISION_B,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset({ProductOperation.SWITCH}),
                    limits=launch_limits,
                    policy_id=PREVIEW_POLICY_ID,
                    notice_version=PREVIEW_NOTICE_VERSION,
                ),
                LaunchGrantSeed(
                    token=PREVIEW_CREATE_GRANT_B,
                    selected_record_id=PREVIEW_SELECTED_RECORD_B,
                    actor_id=PREVIEW_ACTOR_B_ID,
                    role=ProductRole.PRESENTER,
                    revision_id=PREVIEW_REVISION_B,
                    expires_at_ms=seed_expiry,
                    allowed_operations=frozenset({ProductOperation.CREATE}),
                    limits=launch_limits,
                    policy_id=PREVIEW_POLICY_ID,
                    notice_version=PREVIEW_NOTICE_VERSION,
                ),
            ),
            executor=DeterministicPreviewExecutor(),
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=8,
                max_tombstones=16,
                max_idempotency_records=64,
                max_launch_grants=8,
            ),
            core_policy=ProductCorePolicy(
                max_commands_per_session=8,
                command_deadline_ms=10_000,
                cancellation_deadline_ms=2_000,
                max_output_bytes_per_command=16 * 1_024,
                max_output_chunks_per_command=16,
                reserved_tokens_per_command=32,
                reserved_spend_units_per_command=32,
                max_attempts_per_command=1,
            ),
            clock_ms=self.clock.now_ms,
            monotonic_ms=self.clock.now_ms,
            token_factory=_LIFECYCLE_TRANSPORT.PreviewTokens(1),
        )

    def submit_task(self, task: Any) -> None:
        if not callable(task):
            raise TypeError("preview task must be callable")
        with self._future_lock:
            if self._closed:
                raise RuntimeError("product-core preview is closed")
            future = self._workers.submit(task)
            self._futures.add(future)
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future: Future[object]) -> None:
        with self._future_lock:
            self._futures.discard(future)

    def should_drop_command_accept_response(self) -> bool:
        """Claim the process-selected one-shot post-acceptance transport fault."""

        with self._fault_lock:
            if (
                self.scenario == "command-accept-response-lost"
                and not self._command_accept_response_lost
            ):
                self._command_accept_response_lost = True
                return True
        return False

    def should_withhold_terminal_event_response(
        self,
        page: Mapping[str, object],
    ) -> bool:
        """Claim one response containing terminal truth without altering the journal."""

        if self.scenario != "promised-terminal-event-withheld":
            return False
        events = page.get("events")
        if not isinstance(events, list) or not any(
            isinstance(event, Mapping)
            and event.get("type")
            in {"command.completed", "command.interrupted", "command.failed"}
            for event in events
        ):
            return False
        with self._fault_lock:
            if self._terminal_event_response_withheld:
                return False
            self._terminal_event_response_withheld = True
            return True

    def close(self) -> None:
        with self._future_lock:
            if self._closed:
                return
            self._closed = True
        self._workers.shutdown(wait=True, cancel_futures=True)


def _query_pairs(raw_query: str) -> dict[str, str]:
    if not raw_query:
        return {}
    try:
        pairs = parse_qsl(
            raw_query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except ValueError as error:
        raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "The product preview query is invalid.",
        ) from error
    if len({name for name, _value in pairs}) != len(pairs):
        raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "The product preview query is ambiguous.",
        )
    return dict(pairs)


def _bounded_counter(value: str, name: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} is invalid")
    parsed = int(value)
    if not 1 <= parsed <= MAX_CONTROL_REVISION:
        raise ValueError(f"{name} is invalid")
    return parsed


def _quality_weight(value: str) -> int:
    if not QUALITY_PATTERN.fullmatch(value):
        raise ValueError("Accept quality is invalid")
    if value.startswith("1"):
        return 1_000
    _whole, _separator, fraction = value.partition(".")
    return int((fraction + "000")[:3])


def _event_response_media_type(values: Sequence[str]) -> str:
    """Negotiate the finite event representation without treating q=0 as accepted."""

    if not values:
        return "application/json"
    ranges: list[tuple[str, str, int, int]] = []
    order = 0
    for header_value in values:
        for raw_item in header_value.split(","):
            item = raw_item.strip()
            if not item:
                raise ValueError("Accept contains an empty media range")
            pieces = [piece.strip() for piece in item.split(";")]
            match = MEDIA_RANGE_PATTERN.fullmatch(pieces[0])
            if match is None:
                raise ValueError("Accept media range is invalid")
            if match.group("type") == "*" and match.group("subtype") != "*":
                raise ValueError("Accept wildcard media range is invalid")
            quality = 1_000
            quality_seen = False
            for parameter in pieces[1:]:
                name, separator, parameter_value = parameter.partition("=")
                if not separator or not name.strip() or not parameter_value.strip():
                    raise ValueError("Accept parameter is invalid")
                if name.strip().lower() == "q":
                    if quality_seen:
                        raise ValueError("Accept quality is ambiguous")
                    quality = _quality_weight(parameter_value.strip())
                    quality_seen = True
            ranges.append(
                (
                    match.group("type").lower(),
                    match.group("subtype").lower(),
                    quality,
                    order,
                )
            )
            order += 1

    def effective_quality(media_type: str) -> tuple[int, int]:
        offered_type, offered_subtype = media_type.split("/", 1)
        matches: list[tuple[int, int, int]] = []
        for accepted_type, accepted_subtype, quality, accepted_order in ranges:
            if accepted_type not in {"*", offered_type} or accepted_subtype not in {
                "*",
                offered_subtype,
            }:
                continue
            specificity = int(accepted_type != "*") + int(accepted_subtype != "*")
            matches.append((specificity, quality, accepted_order))
        if not matches:
            return 0, order
        specificity = max(item[0] for item in matches)
        controlling = [item for item in matches if item[0] == specificity]
        quality = max(item[1] for item in controlling)
        accepted_order = min(item[2] for item in controlling if item[1] == quality)
        return quality, accepted_order

    candidates: list[tuple[int, int, int, str]] = []
    for default_preference, media_type in enumerate(
        ("application/json", "text/event-stream")
    ):
        quality, accepted_order = effective_quality(media_type)
        if quality > 0:
            candidates.append(
                (quality, -accepted_order, -default_preference, media_type)
            )
    if not candidates:
        raise ValueError("Accept excludes every supported event representation")
    return max(candidates)[3]


def _event_cursor(query: Mapping[str, str]) -> EventCursor | None:
    expected = {"after_stream_id", "after_sequence", "after_event_id"}
    if not query:
        return None
    if set(query) != expected:
        raise ValueError("event cursor query must be complete")
    stream_id = query["after_stream_id"]
    sequence = _bounded_counter(query["after_sequence"], "event sequence")
    event_id = query["after_event_id"]
    match = EVENT_ID_PATTERN.fullmatch(event_id)
    if (
        match is None
        or f"stream:{match.group('stream')}" != stream_id
        or int(match.group("sequence")) != sequence
    ):
        raise ValueError("event cursor tuple is inconsistent")
    return EventCursor(stream_id, sequence, event_id)


def _transcript_cursor(query: Mapping[str, str]) -> TranscriptCursor | None:
    expected = {"after_transcript_id", "after_position", "after_entry_id"}
    if not query:
        return None
    if set(query) != expected:
        raise ValueError("transcript cursor query must be complete")
    transcript_id = query["after_transcript_id"]
    position = _bounded_counter(query["after_position"], "transcript position")
    entry_id = query["after_entry_id"]
    match = ENTRY_ID_PATTERN.fullmatch(entry_id)
    if (
        match is None
        or f"transcript:{match.group('transcript')}" != transcript_id
        or int(match.group("position")) != position
    ):
        raise ValueError("transcript cursor tuple is inconsistent")
    return TranscriptCursor(transcript_id, position, entry_id)


def _last_event_cursor(value: str) -> EventCursor:
    match = EVENT_ID_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("Last-Event-ID is invalid")
    sequence = int(match.group("sequence"))
    if sequence > MAX_CONTROL_REVISION:
        raise ValueError("Last-Event-ID is invalid")
    return EventCursor(
        f"stream:{match.group('stream')}",
        sequence,
        value,
    )


def _required_generation(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= MAX_CONTROL_REVISION
    ):
        raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "The product preview precondition is invalid.",
        )
    return value


class ProductCorePreviewServer(_LifecyclePreviewServerBase):
    """The Phase 1 hardened transport with a Phase 2-only explicit harness."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        scenario: str,
        contract_root: Path,
    ) -> None:
        self.preview_bearer = bearer
        self.contract_root = contract_root
        self.preview = CorePreviewHarness(scenario)
        self._request_sequence = 0
        self._request_lock = threading.Lock()
        self._connection_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        self._connection_count_lock = threading.Lock()
        self._active_connections = 0
        self._maximum_observed_connections = 0
        self._synchronous_request = False
        try:
            ThreadingHTTPServer.__init__(self, address, ProductCorePreviewHandler)
        except Exception:
            self.preview.close()
            raise

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
        return f"request:core-preview-{sequence:08d}"

    def server_close(self) -> None:
        try:
            HTTPServer.server_close(self)
        finally:
            self.preview.close()


class ProductCorePreviewHandler(_LifecyclePreviewHandlerBase):
    server: ProductCorePreviewServer
    server_version = "DREAMLANDProductCorePreview/0.3"

    def _additional_get_operations(self) -> frozenset[str]:
        """Allow an explicit derived preview to add read-only private routes."""
        return frozenset()

    def _additional_post_operations(self) -> frozenset[str]:
        """Allow an explicit derived preview to add private mutations."""
        return frozenset()

    def _send_json(
        self,
        status: HTTPStatus,
        value: Mapping[str, Any],
        *,
        content_type: str = "application/json",
    ) -> None:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Dreamland-Contract-Revision", CONTRACT_REVISION)
        self.send_header("X-Dreamland-Integration-Preview", "true")
        if self.close_connection:
            self.send_header("Connection", "close")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except OSError:
            return

    def _send_problem(
        self,
        status: HTTPStatus,
        code: str,
        request_id: str,
    ) -> None:
        self._send_json(
            status,
            _transport_problem(code, request_id),
            content_type="application/problem+json",
        )

    def _send_sse(self, page: Mapping[str, object]) -> None:
        events = page.get("events")
        if not isinstance(events, list):
            raise ValueError("domain event page is invalid")
        frames: list[str] = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError("domain event is invalid")
            event_id = event.get("event_id")
            event_type = event.get("type")
            if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(
                event_id
            ):
                raise ValueError("domain event ID is invalid")
            if not isinstance(event_type, str):
                raise ValueError("domain event type is invalid")
            data = json.dumps(
                dict(event),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            frames.append(f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n")
        body = "".join(frames).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Dreamland-Contract-Revision", CONTRACT_REVISION)
        self.send_header("X-Dreamland-Integration-Preview", "true")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            return

    def _route(
        self,
    ) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        query = parsed.query
        if parsed.path == CAPABILITIES_PATH:
            return "capabilities", None, None, query
        if parsed.path == SESSIONS_PATH:
            return "create", None, None, query
        if parsed.path == CURRENT_PATH:
            return "current", None, None, query
        if not parsed.path.startswith(SESSION_PATH_PREFIX):
            return "not-found", None, None, query
        parts = parsed.path.removeprefix(SESSION_PATH_PREFIX).split("/")
        if not parts or len(parts[0]) > 420:
            return "not-found", None, None, query
        reference = unquote(parts[0])
        if not SESSION_REFERENCE_PATTERN.fullmatch(reference):
            return "not-found", None, None, query
        if len(parts) == 1:
            return "snapshot", reference, None, query
        if len(parts) == 2 and parts[1] in {
            "transcript",
            "events",
            "commands",
            "reset",
            "switch",
            "close",
        }:
            return parts[1], reference, None, query
        if len(parts) in {3, 4} and parts[1] == "commands":
            command_id = unquote(parts[2])
            if not COMMAND_ID_PATTERN.fullmatch(command_id):
                return "not-found", None, None, query
            if len(parts) == 3:
                return "command-result", reference, command_id, query
            if parts[3] == "cancel":
                return "cancel", reference, command_id, query
        return "not-found", None, None, query

    def _capabilities(self, request_id: str) -> dict[str, object]:
        return _decorate_success(
            {
                "kind": "product_capabilities",
                "service_epoch": self.server.preview.service.service_epoch,
                "request_id": request_id,
                "admission": {
                    "state": "closed",
                    "reason": "integration_preview_only",
                },
                "capabilities": {
                    name: {
                        "declared_revision": CONTRACT_REVISION,
                        "enabled": False,
                        "enabled_revision": None,
                        "reason": "integration_preview_only",
                    }
                    for name in CAPABILITY_NAMES
                },
            },
            request_id=request_id,
        )

    def _read_context(self) -> tuple[str, str]:
        return (
            self._required_header(EXPECTED_EPOCH_HEADER, SERVICE_EPOCH_PATTERN),
            self._required_header(ACTOR_CONTEXT_HEADER, ACTOR_CONTEXT_PATTERN),
        )

    def _session_context(self) -> tuple[str, str, str]:
        expected_epoch, actor_authority = self._read_context()
        session_authority = self._required_header(
            SESSION_CAPABILITY_HEADER,
            SESSION_CAPABILITY_PATTERN,
        )
        return expected_epoch, actor_authority, session_authority

    def _gate_expected_epoch(self, *, mutation: bool) -> None:
        """Apply the canonical Phase 2 epoch-loss HTTP mapping."""

        values = self.headers.get_all(EXPECTED_EPOCH_HEADER, [])
        value = values[0] if len(values) == 1 else None
        if value is None or not value.strip():
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "expected_epoch_required",
                "Expected product service epoch evidence is required.",
            )
        if not SERVICE_EPOCH_PATTERN.fullmatch(value):
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The expected service epoch is malformed.",
            )
        if not hmac.compare_digest(
            value,
            self.server.preview.service.service_epoch,
        ):
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.GONE,
                "outcome_unknown" if mutation else "session_lost",
                "The supplied service epoch is no longer current.",
            )

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        query = _query_pairs(raw_query)
        if operation == "capabilities":
            if query:
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
                )
            return self._capabilities(request_id)
        if operation == "current":
            if query:
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
                )
            expected_epoch, actor_authority = self._read_context()
            domain = self.server.preview.service.current(
                actor_authority=actor_authority,
                expected_epoch=expected_epoch,
            )
            return _decorate_success(domain, request_id=request_id)
        if reference is None:
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        expected_epoch, actor_authority, session_authority = self._session_context()
        service = self.server.preview.service
        if operation == "snapshot" and not query:
            domain = service.snapshot(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
        elif operation == "command-result" and command_id is not None and not query:
            domain = service.command_result(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                command_id=command_id,
            )
        elif operation == "transcript":
            try:
                transcript_cursor = _transcript_cursor(query)
            except ValueError as error:
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The transcript cursor is invalid.",
                ) from error
            domain = service.transcript(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                after=transcript_cursor,
            )
        elif operation == "events":
            try:
                response_media_type = _event_response_media_type(
                    self.headers.get_all("Accept", [])
                )
                event_cursor = _event_cursor(query)
                last_event_values = self.headers.get_all(LAST_EVENT_ID_HEADER, [])
                if last_event_values:
                    if (
                        response_media_type != "text/event-stream"
                        or len(last_event_values) != 1
                        or event_cursor is not None
                    ):
                        raise ValueError("event cursors are ambiguous")
                    value = last_event_values[0]
                    if len(value) > MAX_HEADER_VALUE_CHARACTERS:
                        raise ValueError("Last-Event-ID is too large")
                    event_cursor = _last_event_cursor(value)
            except ValueError as error:
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The event cursor is invalid.",
                ) from error
            domain = {
                **service.events(
                    actor_authority=actor_authority,
                    session_authority=session_authority,
                    reference=reference,
                    expected_epoch=expected_epoch,
                    after=event_cursor,
                ),
                "service_epoch": service.service_epoch,
            }
            if self.server.preview.should_withhold_terminal_event_response(domain):
                # Model a promised-feed delivery loss at the HTTP boundary.  The
                # canonical journal is untouched, so retry/query/snapshot
                # recovery never observes an artificial cursor hole.
                self._drop_connection()
                return None
            if response_media_type == "text/event-stream":
                self._send_sse(domain)
                return None
        else:
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        if operation == "transcript":
            domain = {**domain, "service_epoch": service.service_epoch}
        return _decorate_success(domain, request_id=request_id)

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object]:
        query = _query_pairs(raw_query)
        if query:
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        expected_epoch, actor_authority = self._read_context()
        idempotency_key = self._required_header(
            IDEMPOTENCY_HEADER,
            IDEMPOTENCY_KEY_PATTERN,
        )
        service = self.server.preview.service
        if operation == "create":
            body = self._read_json_object(frozenset({"launch_grant"}))
            launch_grant = body["launch_grant"]
            if not isinstance(launch_grant, str) or not LAUNCH_GRANT_PATTERN.fullmatch(
                launch_grant
            ):
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The launch grant is invalid.",
                )
            domain = service.create(
                actor_authority=actor_authority,
                launch_grant=launch_grant,
                expected_epoch=expected_epoch,
                idempotency_key=idempotency_key,
            )
            return _decorate_success(domain, request_id=request_id)
        if reference is None:
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        session_authority = self._required_header(
            SESSION_CAPABILITY_HEADER,
            SESSION_CAPABILITY_PATTERN,
        )
        common = {
            "actor_authority": actor_authority,
            "session_authority": session_authority,
            "reference": reference,
            "expected_epoch": expected_epoch,
            "idempotency_key": idempotency_key,
        }
        if operation == "commands":
            body = self._read_json_object(
                frozenset({"command", "expected_slot_generation"})
            )
            command = body["command"]
            if not isinstance(command, str):
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The product command must be a string.",
                )
            generation = _required_generation(
                body["expected_slot_generation"], allow_zero=True
            )
            domain = service.submit_command(
                **common,
                expected_slot_generation=generation,
                command=command,
            )
        elif operation == "cancel" and command_id is not None:
            body = self._read_json_object(frozenset({"slot_generation"}))
            generation = _required_generation(body["slot_generation"], allow_zero=False)
            domain = service.cancel_command(
                **common,
                command_id=command_id,
                slot_generation=generation,
            )
        elif operation in {"reset", "close"}:
            body = self._read_json_object(frozenset({"expected_control_revision"}))
            revision = _required_generation(
                body["expected_control_revision"], allow_zero=False
            )
            domain = getattr(service, operation)(
                **common,
                expected_control_revision=revision,
            )
        elif operation == "switch":
            body = self._read_json_object(
                frozenset({"expected_control_revision", "launch_grant"})
            )
            revision = _required_generation(
                body["expected_control_revision"], allow_zero=False
            )
            launch_grant = body["launch_grant"]
            if not isinstance(launch_grant, str) or not LAUNCH_GRANT_PATTERN.fullmatch(
                launch_grant
            ):
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "The launch grant is invalid.",
                )
            domain = service.switch(
                **common,
                expected_control_revision=revision,
                launch_grant=launch_grant,
            )
        else:
            raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
            )
        return _decorate_success(domain, request_id=request_id)

    def _handle(self, method: str) -> None:
        request_id = self.server.next_request_id()
        try:
            self._require_service_bearer(request_id)
            operation, reference, command_id, raw_query = self._route()
            if operation == "capabilities" and method == "GET":
                value = self._dispatch_get(
                    operation, reference, command_id, raw_query, request_id
                )
            elif method == "GET" and operation in {
                "current",
                "snapshot",
                "command-result",
                "transcript",
                "events",
                *self._additional_get_operations(),
            }:
                self._gate_expected_epoch(mutation=False)
                value = self._dispatch_get(
                    operation, reference, command_id, raw_query, request_id
                )
            elif method == "POST" and operation in {
                "create",
                "commands",
                "cancel",
                "reset",
                "switch",
                "close",
                *self._additional_post_operations(),
            }:
                self._gate_expected_epoch(mutation=True)
                value = self._dispatch_post(
                    operation, reference, command_id, raw_query, request_id
                )
            else:
                raise _LIFECYCLE_TRANSPORT.PreviewTransportError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "The requested product preview operation is unavailable.",
                )
            if value is not None:
                if (
                    method == "POST"
                    and operation == "commands"
                    and self.server.preview.should_drop_command_accept_response()
                ):
                    # The service has committed acceptance and idempotency.  A
                    # transport abort here proves identical retry recovery.
                    self._drop_connection()
                    return
                if method == "POST" and operation == "create":
                    status = HTTPStatus.CREATED
                elif method == "POST" and operation == "commands":
                    status = HTTPStatus.ACCEPTED
                else:
                    status = HTTPStatus.OK
                self._send_json(status, value)
        except _LIFECYCLE_TRANSPORT.PreviewTransportError as error:
            self._send_problem(error.status, error.code, request_id)
        except ProductLifecycleError as error:
            self._send_json(
                _domain_status(error),
                _domain_problem(error, request_id),
                content_type="application/problem+json",
            )
        except Exception:
            self._send_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                request_id,
            )


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
) -> ProductCorePreviewServer:
    """Build, but do not start, the explicit loopback product-core preview."""

    if not _LIFECYCLE_TRANSPORT._loopback_host(host):
        raise ValueError(
            "the product-core preview supports only loopback hosts 127.0.0.1 or localhost"
        )
    if port < 0 or port > 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _LIFECYCLE_TRANSPORT._validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown product-core preview scenario")
    validated_contract_root = validate_contract_root(contract_root)
    return ProductCorePreviewServer(
        (host, port),
        bearer=bearer,
        scenario=scenario,
        contract_root=validated_contract_root,
    )


def serve_preview(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
    once: bool,
) -> None:
    server = build_preview_server(
        host=host,
        port=port,
        bearer=bearer,
        scenario=scenario,
        contract_root=contract_root,
    )
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "contract_revision": CONTRACT_REVISION,
                "executor": PREVIEW_EXECUTOR,
                "host": address,
                "integration_only": True,
                "live_capability_evidence": False,
                "port": bound_port,
                "scenario": scenario,
                "test_only": True,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        if once:
            server.handle_request_synchronously()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the integration-only DREAMLAND product-core preview."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    bearer_source = parser.add_mutually_exclusive_group(required=True)
    bearer_source.add_argument("--token")
    bearer_source.add_argument("--token-file", type=Path)
    parser.add_argument("--contract-root", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bearer = (
            arguments.token
            if arguments.token is not None
            else _LIFECYCLE_TRANSPORT.load_preview_bearer_file(arguments.token_file)
        )
        serve_preview(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            scenario=arguments.scenario,
            contract_root=arguments.contract_root,
            once=arguments.once,
        )
    except (OSError, ValueError) as error:
        print(f"product-core preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
