"""Serve the integration-only product lifecycle preview on loopback.

This module is development infrastructure.  DREAMLAND runtime composition never
imports it, and every product capability remains disabled while this preview is
available to an explicitly started BFF integration test.
"""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit

from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LaunchGrantSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductLifecycleService,
    ProductOperation,
    ProductRole,
)

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.2.0-rc.2"
CONTRACT_COMPATIBILITY = "exact-release-candidate"

CAPABILITIES_PATH = "/product/v1/capabilities"
CURRENT_PATH = "/product/v1/sessions/current"
SESSIONS_PATH = "/product/v1/sessions"
SESSION_PATH_PREFIX = "/product/v1/sessions/"
SESSION_CLOSE_SUFFIX = "/close"

EXPECTED_EPOCH_HEADER = "X-Dreamland-Expected-Epoch"
ACTOR_CONTEXT_HEADER = "X-Dreamland-Actor-Context"
SESSION_CAPABILITY_HEADER = "X-Dreamland-Session-Capability"
IDEMPOTENCY_HEADER = "Idempotency-Key"

MAX_REQUEST_BODY_BYTES = 16_384
MAX_HEADER_VALUE_CHARACTERS = 1_024
MAX_CONTROL_REVISION = 9_007_199_254_740_991
MILLISECONDS_PER_MINUTE = 60_000
MAX_CONCURRENT_CONNECTIONS = 8
CONNECTION_READ_TIMEOUT_SECONDS = 1.0
CONNECTION_DEADLINE_SECONDS = 3.0

SERVICE_EPOCH_PATTERN = re.compile(r"^epoch:[A-Za-z0-9_-]{16,128}$")
ACTOR_CONTEXT_PATTERN = re.compile(r"^actor:[A-Za-z0-9_-]{16,128}$")
SESSION_CAPABILITY_PATTERN = re.compile(r"^session-cap:[A-Za-z0-9_-]{24,256}$")
SESSION_REFERENCE_PATTERN = re.compile(r"^session-ref:[A-Za-z0-9_-]{16,128}$")
LAUNCH_GRANT_PATTERN = re.compile(r"^launch-grant:[A-Za-z0-9_-]{24,480}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")

CAPABILITY_NAMES = (
    "product_session_core",
    "presenter_stage_terminal",
    "public_attendee_sessions",
    "forge_to_stage_handoff",
    "stage_xray",
)

SAFE_ERROR_DETAILS: dict[str, tuple[str, str, int | None]] = {
    "unauthorized": (
        "Missing or invalid private service authority.",
        "none",
        None,
    ),
    "not_found": (
        "The requested lifecycle preview resource is unavailable.",
        "none",
        None,
    ),
    "grant_unavailable": ("The launch grant is unavailable.", "none", None),
    "current_session_exists": (
        "A current lifecycle preview session already exists.",
        "resume_snapshot",
        None,
    ),
    "idempotency_conflict": (
        "The idempotency key was already used for another request.",
        "none",
        None,
    ),
    "control_revision_conflict": (
        "The lifecycle control revision has changed.",
        "resume_snapshot",
        None,
    ),
    "session_lost": (
        "The lifecycle preview session was lost after service restart.",
        "none",
        None,
    ),
    "outcome_unknown": (
        "The prior lifecycle mutation outcome is unknown after service restart.",
        "none",
        None,
    ),
    "invalid_request": ("The private lifecycle request is invalid.", "none", None),
    "invalid_json": ("The request body is not valid JSON.", "none", None),
    "request_too_large": (
        "The request body exceeds the lifecycle preview limit.",
        "none",
        None,
    ),
    "unsupported_media_type": (
        "The request content type is unsupported.",
        "none",
        None,
    ),
    "expected_epoch_required": (
        "Expected product service epoch evidence is required.",
        "none",
        None,
    ),
    "authority_denied": (
        "The requested lifecycle preview resource is unavailable.",
        "none",
        None,
    ),
    "content_length_required": (
        "A Content-Length header is required.",
        "none",
        None,
    ),
    "capacity_exhausted": (
        "Lifecycle preview capacity is unavailable.",
        "after_delay",
        1_000,
    ),
    "internal_error": (
        "The lifecycle preview request could not be completed.",
        "none",
        None,
    ),
}

SCENARIOS = (
    "lifecycle-happy",
    "create-response-dropped-after-commit",
    "close-response-dropped-after-commit",
    "expires-on-next-read",
    "epoch-loss",
)

# These values authorize only the locally constructed simulator service.  They
# are stable so an external BFF test can use them without parsing a secret from
# process output.  The separately supplied service bearer is still required.
PREVIEW_ACTOR_A_CONTEXT = "actor:preview_alpha_0001"
PREVIEW_ACTOR_B_CONTEXT = "actor:preview_bravo_0001"
PREVIEW_ACTOR_A_ID = "preview-actor-alpha"
PREVIEW_ACTOR_B_ID = "preview-actor-bravo"
PREVIEW_GRANT_A = "launch-grant:preview_alpha_grant_00000001"
# A second grant for the same selected record exists only so BFF tests can
# exercise authorized distinct-create conflict without making a consumed grant
# an actor/session oracle.
PREVIEW_GRANT_A_SECONDARY = "launch-grant:preview_alpha_grant_00000002"
PREVIEW_GRANT_B = "launch-grant:preview_bravo_grant_00000001"
PREVIEW_REVISION_A = "world-revision:preview_alpha_revision_0001"
PREVIEW_REVISION_B = "world-revision:preview_bravo_revision_0001"
PREVIEW_SELECTED_RECORD_A = "selected-record:preview_alpha_0001"
PREVIEW_SELECTED_RECORD_B = "selected-record:preview_bravo_0001"
PREVIEW_POLICY_ID = "policy:conference-preview-v1"
PREVIEW_NOTICE_VERSION = "notice:conference-preview-v1"
PREVIEW_OLD_ACTOR_B_CONTEXT = "actor:preview_prior_bravo_context_0001"
PREVIEW_OLD_GRANT_A_SECONDARY = "launch-grant:preview_prior_grant_00000002"
PREVIEW_OLD_GRANT_B = "launch-grant:preview_prior_bravo_grant_00000001"
PREVIEW_EPOCH_LOSS_ACTOR_CONTEXT = "actor:preview_alpha_rotated_0001"
PREVIEW_EPOCH_LOSS_GRANT = "launch-grant:preview_alpha_rotated_00000001"

_SERVICE_GENERATION = 0
_SERVICE_GENERATION_LOCK = threading.Lock()


class PreviewTransportError(Exception):
    """A safe request-boundary failure."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        retry: str = "none",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry = retry


class MutablePreviewClock:
    """A deterministic clock advanced only by the selected simulator scenario."""

    def __init__(self, initial_ms: int = 1_787_400_000_000) -> None:
        self._value = initial_ms
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._value

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("preview clock cannot move backwards")
        with self._lock:
            self._value += milliseconds


class PreviewTokens:
    """Produce process-isolated, high-entropy simulator identities."""

    def __init__(self, generation: int) -> None:
        self._generation = generation
        self._process_nonce = secrets.token_urlsafe(12).replace("-", "_")
        self._sequence = 0
        self._lock = threading.Lock()

    def __call__(self, purpose: str) -> str:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        safe_purpose = "".join(
            character.lower() if character.isalnum() else "_" for character in purpose
        ).strip("_")
        if "epoch" in safe_purpose:
            return f"epoch:{secrets.token_urlsafe(24)}"
        if "cap" in safe_purpose or "authority" in safe_purpose:
            return (
                "session-cap:preview_session_capability_"
                f"{self._process_nonce}_{self._generation:04d}_{sequence:08d}"
            )
        if "reference" in safe_purpose:
            return (
                "session-ref:preview_session_reference_"
                f"{self._process_nonce}_{self._generation:04d}_{sequence:08d}"
            )
        if "selected" in safe_purpose:
            return (
                "selected-record:preview_selected_record_"
                f"{self._process_nonce}_{sequence:08d}"
            )
        if "session" in safe_purpose:
            return (
                "session:preview_session_"
                f"{self._process_nonce}_{self._generation:04d}_{sequence:08d}"
            )
        return (
            f"preview-token:{safe_purpose or 'token'}_"
            f"{self._process_nonce}_{self._generation:04d}_{sequence:08d}"
        )


def _next_service_generation(*, epoch_loss: bool) -> tuple[int, int | None]:
    global _SERVICE_GENERATION
    with _SERVICE_GENERATION_LOCK:
        if epoch_loss:
            _SERVICE_GENERATION += 1
            prior = _SERVICE_GENERATION
        else:
            prior = None
        _SERVICE_GENERATION += 1
        return _SERVICE_GENERATION, prior


def build_lifecycle_service(
    clock: MutablePreviewClock,
    *,
    generation: int,
    actor_a_context: str = PREVIEW_ACTOR_A_CONTEXT,
    grant_a: str = PREVIEW_GRANT_A,
    actor_b_context: str = PREVIEW_ACTOR_B_CONTEXT,
    grant_a_secondary: str = PREVIEW_GRANT_A_SECONDARY,
    grant_b: str = PREVIEW_GRANT_B,
) -> ProductLifecycleService:
    """Construct a simulator-only service with two isolated actors and revisions."""

    now_ms = clock.now_ms()
    seed_expiry = now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE
    policy = LifecyclePolicy(
        absolute_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
        idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
        tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
        failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
        max_active_sessions=8,
        max_tombstones=16,
        max_idempotency_records=32,
        max_launch_grants=8,
    )
    return ProductLifecycleService(
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
                token=actor_a_context,
                actor_id=PREVIEW_ACTOR_A_ID,
                role=ProductRole.ATTENDEE,
                expires_at_ms=seed_expiry,
            ),
            ActorAuthoritySeed(
                token=actor_b_context,
                actor_id=PREVIEW_ACTOR_B_ID,
                role=ProductRole.PRESENTER,
                expires_at_ms=seed_expiry,
            ),
        ),
        launch_grants=(
            LaunchGrantSeed(
                token=grant_a,
                selected_record_id=PREVIEW_SELECTED_RECORD_A,
                actor_id=PREVIEW_ACTOR_A_ID,
                role=ProductRole.ATTENDEE,
                revision_id=PREVIEW_REVISION_A,
                expires_at_ms=seed_expiry,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
                    "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
                },
                policy_id=PREVIEW_POLICY_ID,
                notice_version=PREVIEW_NOTICE_VERSION,
            ),
            LaunchGrantSeed(
                token=grant_b,
                selected_record_id=PREVIEW_SELECTED_RECORD_B,
                actor_id=PREVIEW_ACTOR_B_ID,
                role=ProductRole.PRESENTER,
                revision_id=PREVIEW_REVISION_B,
                expires_at_ms=seed_expiry,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
                    "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
                },
                policy_id=PREVIEW_POLICY_ID,
                notice_version=PREVIEW_NOTICE_VERSION,
            ),
            LaunchGrantSeed(
                token=grant_a_secondary,
                selected_record_id=PREVIEW_SELECTED_RECORD_A,
                actor_id=PREVIEW_ACTOR_A_ID,
                role=ProductRole.ATTENDEE,
                revision_id=PREVIEW_REVISION_A,
                expires_at_ms=seed_expiry,
                allowed_operations=frozenset({ProductOperation.CREATE}),
                limits={
                    "absolute_ttl_ms": 30 * MILLISECONDS_PER_MINUTE,
                    "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
                },
                policy_id=PREVIEW_POLICY_ID,
                notice_version=PREVIEW_NOTICE_VERSION,
            ),
        ),
        policy=policy,
        clock_ms=clock.now_ms,
        token_factory=PreviewTokens(generation),
    )


def _loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost"}


def _validate_preview_bearer(bearer: str) -> str:
    if (
        not bearer
        or "\r" in bearer
        or "\n" in bearer
        or len(bearer) > MAX_HEADER_VALUE_CHARACTERS
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in bearer)
    ):
        raise ValueError("preview bearer must be bounded visible ASCII without spaces")
    return bearer


def load_preview_bearer_file(path: Path) -> str:
    """Read an owner-only, non-symlink token file without exposing its path."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if no_follow is None:
        raise ValueError("preview token files require no-follow file support")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | close_on_exec | no_follow | nonblocking,
        )
    except OSError as error:
        raise ValueError("preview token file is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("preview token file must be a regular owner-only file")
        maximum_file_bytes = MAX_HEADER_VALUE_CHARACTERS + 2
        if metadata.st_size > maximum_file_bytes:
            raise ValueError("preview token file is too large")
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read <= maximum_file_bytes:
            chunk = os.read(descriptor, maximum_file_bytes + 1 - bytes_read)
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum_file_bytes:
            raise ValueError("preview token file is too large")
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("preview token file must contain visible ASCII") from error
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return _validate_preview_bearer(value)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid lifecycle contract artifact: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"lifecycle contract artifact is not an object: {path.name}")
    return value


def validate_contract_root(contract_root: Path) -> Path:
    """Reject any artifact directory other than the exact Phase 1 preview RC."""

    root = contract_root.resolve()
    validator_path = Path(__file__).with_name("product_lifecycle_contract.py")
    try:
        specification = importlib.util.spec_from_file_location(
            "dreamland_product_lifecycle_contract_validator",
            validator_path,
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("lifecycle contract validator could not be loaded")
        validator_module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(validator_module)
        validate = getattr(validator_module, "validate_contract", None)
        if not callable(validate):
            raise RuntimeError("lifecycle contract validator API is unavailable")
        if (
            getattr(validator_module, "SAFE_ERRORS", None) != SAFE_ERROR_DETAILS
            or getattr(validator_module, "MAX_CONTROL_REVISION", None)
            != MAX_CONTROL_REVISION
        ):
            raise RuntimeError("lifecycle contract policy differs from the adapter")
        validation = validate(root)
    except Exception as error:
        raise ValueError(
            "lifecycle preview contract failed complete artifact validation"
        ) from error
    if not isinstance(validation, dict) or (
        validation.get("contract_revision") != CONTRACT_REVISION
        or validation.get("implementation_status") != "integration_preview_only"
        or validation.get("routes") != 5
        or validation.get("capabilities") != 5
    ):
        raise ValueError("lifecycle preview contract validation result is incompatible")

    openapi = _load_json_object(root / "openapi.json")
    manifest = _load_json_object(root / "manifest.json")
    expected_paths = {
        CAPABILITIES_PATH,
        SESSIONS_PATH,
        CURRENT_PATH,
        f"{SESSION_PATH_PREFIX}{{reference}}",
        f"{SESSION_PATH_PREFIX}{{reference}}{SESSION_CLOSE_SUFFIX}",
    }
    if (
        openapi.get("openapi") != "3.1.0"
        or openapi.get("servers") != []
        or openapi.get("info", {}).get("version") != CONTRACT_REVISION
        or set(openapi.get("paths", {})) != expected_paths
        or manifest.get("contract_revision") != CONTRACT_REVISION
    ):
        raise ValueError("lifecycle preview requires the exact v0.2.0-rc.2 artifact")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != set(CAPABILITY_NAMES):
        raise ValueError("lifecycle manifest must contain exactly five capabilities")
    if any(
        not isinstance(value, dict)
        or value.get("enabled") is not False
        or value.get("enabled_revision") is not None
        for value in capabilities.values()
    ):
        raise ValueError("lifecycle preview manifest capabilities must remain disabled")
    for path_item in openapi["paths"].values():
        for operation in path_item.values():
            if (
                not isinstance(operation, dict)
                or operation.get("x-dreamland-availability")
                != "integration-preview-only"
            ):
                raise ValueError("lifecycle operation is not integration-preview-only")
    return root


def _rfc3339(milliseconds: object) -> str:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
        raise ValueError("domain lifecycle timestamp is invalid")
    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _required_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"domain lifecycle {name} is invalid")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"domain lifecycle {name} is invalid")
    return value


def _required_duration(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1_000
        or value > 86_400_000
    ):
        raise ValueError(f"domain lifecycle {name} is invalid")
    return value


def _wire_snapshot(
    domain: object,
    *,
    service_epoch: str,
    observed_at_ms: int,
) -> dict[str, Any]:
    """Map only Phase 1 lifecycle fields into the private wire projection."""

    snapshot = _required_mapping(domain, "snapshot")
    starting = _required_mapping(snapshot.get("starting_revision"), "starting revision")
    launch_binding = _required_mapping(snapshot.get("launch_binding"), "launch binding")
    launch_limits = _required_mapping(launch_binding.get("limits"), "launch limits")
    coordinates = _required_mapping(snapshot.get("coordinates"), "coordinates")
    lifecycle = _required_mapping(snapshot.get("lifecycle"), "lifecycle")
    state = lifecycle.get("state")
    if state == "active":
        expected_controls = ["snapshot", "close"]
        wire_lifecycle: dict[str, Any] = {"state": "active"}
    elif state == "closed":
        expected_controls = []
        wire_lifecycle = {
            "state": "closed",
            "changed_at": _rfc3339(lifecycle.get("terminal_at_ms")),
            "reason_code": "user_closed",
            "safe_message": "Session closed.",
        }
    elif state == "expired":
        reason = lifecycle.get("terminal_reason")
        if reason not in {
            "absolute_expired",
            "idle_expired",
            "service_window_ended",
        }:
            raise ValueError("domain lifecycle expiry reason is invalid")
        expected_controls = []
        wire_lifecycle = {
            "state": "expired",
            "changed_at": _rfc3339(lifecycle.get("terminal_at_ms")),
            "reason_code": reason,
            "safe_message": "Session expired.",
        }
    else:
        raise ValueError("domain lifecycle state is invalid")
    if snapshot.get("available_controls") != expected_controls:
        raise ValueError("domain lifecycle controls contradict lifecycle state")
    control_revision = coordinates.get("control_revision")
    if (
        isinstance(control_revision, bool)
        or not isinstance(control_revision, int)
        or control_revision < 1
        or control_revision > MAX_CONTROL_REVISION
    ):
        raise ValueError("domain lifecycle control revision is invalid")
    created_at_ms = lifecycle.get("created_at_ms")
    absolute_ttl_ms = _required_duration(
        launch_limits.get("absolute_ttl_ms"), "absolute launch duration"
    )
    idle_ttl_ms = _required_duration(
        launch_limits.get("idle_ttl_ms"), "idle launch duration"
    )
    if idle_ttl_ms > absolute_ttl_ms:
        raise ValueError("domain lifecycle launch limits are contradictory")
    return {
        "model": "product-session-lifecycle-preview",
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        },
        "service_epoch": service_epoch,
        "session_id": _required_string(snapshot.get("session_id"), "session ID"),
        "reference": _required_string(snapshot.get("reference"), "reference"),
        "authority_class": _required_string(
            snapshot.get("authority_class"), "authority class"
        ),
        "starting_world": {
            "selected_record_id": _required_string(
                starting.get("selected_record_id"), "selected record"
            ),
            "immutable_revision": _required_string(
                starting.get("revision_id"), "immutable revision"
            ),
        },
        "launch_context": {
            "policy_id": _required_string(
                launch_binding.get("policy_id"), "launch policy"
            ),
            "notice_version": _required_string(
                launch_binding.get("notice_version"), "launch notice"
            ),
            "limits": {
                "absolute_ttl_ms": absolute_ttl_ms,
                "idle_ttl_ms": idle_ttl_ms,
            },
        },
        "lifecycle": wire_lifecycle,
        "available_controls": expected_controls,
        "control_revision": control_revision,
        "created_at": _rfc3339(created_at_ms),
        "observed_at": _rfc3339(observed_at_ms),
        "expires_at": _rfc3339(lifecycle.get("absolute_expires_at_ms")),
        "idle_expires_at": _rfc3339(lifecycle.get("idle_expires_at_ms")),
    }


def _wire_result(domain: object, *, observed_at_ms: int) -> dict[str, Any]:
    result = _required_mapping(domain, "result")
    kind = result.get("kind")
    service_epoch = _required_string(result.get("service_epoch"), "service epoch")
    if kind == "no_current_session":
        return {"result": "none"}
    if kind == "session_created":
        return {
            "result": "created",
            "replayed": result.get("replayed") is True,
            "session_capability": _required_string(
                result.get("session_authority"), "session capability"
            ),
            "snapshot": _wire_snapshot(
                result.get("snapshot"),
                service_epoch=service_epoch,
                observed_at_ms=observed_at_ms,
            ),
        }
    if kind == "current_session":
        return {
            "result": "current_active",
            "session_capability": _required_string(
                result.get("session_authority"), "session capability"
            ),
            "snapshot": _wire_snapshot(
                result.get("snapshot"),
                service_epoch=service_epoch,
                observed_at_ms=observed_at_ms,
            ),
        }
    if kind == "current_session_terminal":
        return {
            "result": "current_terminal",
            "snapshot": _wire_snapshot(
                result.get("snapshot"),
                service_epoch=service_epoch,
                observed_at_ms=observed_at_ms,
            ),
        }
    if kind == "session_snapshot":
        return {
            "result": "snapshot",
            "snapshot": _wire_snapshot(
                result.get("snapshot"),
                service_epoch=service_epoch,
                observed_at_ms=observed_at_ms,
            ),
        }
    if kind == "session_closed":
        return {
            "result": "closed",
            "replayed": result.get("replayed") is True,
            "snapshot": _wire_snapshot(
                result.get("snapshot"),
                service_epoch=service_epoch,
                observed_at_ms=observed_at_ms,
            ),
        }
    raise ValueError("domain lifecycle result kind is invalid")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _safe_problem(
    code: str,
    request_id: str,
) -> dict[str, Any]:
    if code not in SAFE_ERROR_DETAILS:
        code = "internal_error"
    message, retry, retry_after_ms = SAFE_ERROR_DETAILS[code]
    return {
        "error": {
            "code": code,
            "message": message,
            "retry": retry,
            "retry_after_ms": retry_after_ms,
            "request_id": request_id,
        }
    }


def _domain_problem(error: ProductLifecycleError, request_id: str) -> dict[str, Any]:
    return _safe_problem(error.code, request_id)


class LifecyclePreviewHarness:
    """Own the domain service and deterministic transport-fault state."""

    def __init__(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown lifecycle preview scenario")
        self.scenario = scenario
        self.clock = MutablePreviewClock()
        generation, prior_generation = _next_service_generation(
            epoch_loss=scenario == "epoch-loss"
        )
        self.old_epoch: str | None = None
        if prior_generation is not None:
            prior_service = build_lifecycle_service(
                self.clock,
                generation=prior_generation,
                actor_a_context=PREVIEW_ACTOR_A_CONTEXT,
                grant_a=PREVIEW_GRANT_A,
                actor_b_context=PREVIEW_OLD_ACTOR_B_CONTEXT,
                grant_a_secondary=PREVIEW_OLD_GRANT_A_SECONDARY,
                grant_b=PREVIEW_OLD_GRANT_B,
            )
            self.old_epoch = prior_service.service_epoch
            prior_service.create(
                actor_authority=PREVIEW_ACTOR_A_CONTEXT,
                launch_grant=PREVIEW_GRANT_A,
                expected_epoch=self.old_epoch,
                idempotency_key="preview-old-create-key-0001",
            )
        current_actor_context = (
            PREVIEW_EPOCH_LOSS_ACTOR_CONTEXT
            if scenario == "epoch-loss"
            else PREVIEW_ACTOR_A_CONTEXT
        )
        current_grant = (
            PREVIEW_EPOCH_LOSS_GRANT if scenario == "epoch-loss" else PREVIEW_GRANT_A
        )
        self.current_actor_context = current_actor_context
        self.current_grant = current_grant
        self.service = build_lifecycle_service(
            self.clock,
            generation=generation,
            actor_a_context=current_actor_context,
            grant_a=current_grant,
        )
        self._state_lock = threading.Lock()
        self._create_response_dropped = False
        self._close_response_dropped = False
        self._expire_on_read = False

    def note_created(self) -> None:
        if self.scenario != "expires-on-next-read":
            return
        with self._state_lock:
            self._expire_on_read = True

    def expire_before_read_if_selected(self) -> None:
        with self._state_lock:
            if not self._expire_on_read:
                return
            self._expire_on_read = False
            # Keep selection, clock movement, and expiry as one harness action.
            # Concurrent readers must not observe active state after the one-shot
            # scenario has been claimed but before the service is expired.
            self.clock.advance(10 * MILLISECONDS_PER_MINUTE)
            self.service.expire_due()

    def should_drop_committed_response(self, operation: str) -> bool:
        with self._state_lock:
            if (
                operation == "create"
                and self.scenario == "create-response-dropped-after-commit"
                and not self._create_response_dropped
            ):
                self._create_response_dropped = True
                return True
            if (
                operation == "close"
                and self.scenario == "close-response-dropped-after-commit"
                and not self._close_response_dropped
            ):
                self._close_response_dropped = True
                return True
        return False


class LifecyclePreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = MAX_CONCURRENT_CONNECTIONS

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
        self.preview = LifecyclePreviewHarness(scenario)
        self._request_sequence = 0
        self._request_lock = threading.Lock()
        self._connection_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        self._connection_count_lock = threading.Lock()
        self._active_connections = 0
        self._maximum_observed_connections = 0
        self._synchronous_request = False
        super().__init__(address, LifecyclePreviewHandler)

    def handle_request_synchronously(self) -> None:
        """Accept and finish one connection before returning to the caller."""

        self._synchronous_request = True
        try:
            self.handle_request()
        finally:
            self._synchronous_request = False

    def _release_connection_slot(self) -> None:
        with self._connection_count_lock:
            self._active_connections -= 1
        self._connection_slots.release()

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
        return f"request:lifecycle-preview-{sequence:08d}"

    @property
    def active_connections(self) -> int:
        with self._connection_count_lock:
            return self._active_connections

    @property
    def maximum_observed_connections(self) -> int:
        with self._connection_count_lock:
            return self._maximum_observed_connections

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        # ThreadingMixIn otherwise admits an unbounded worker per accepted
        # connection.  Blocking the accept loop here keeps both live workers
        # and the kernel listen backlog bounded; read/deadline enforcement
        # guarantees that a stalled admitted client eventually releases a slot.
        self._connection_slots.acquire()
        with self._connection_count_lock:
            self._active_connections += 1
            self._maximum_observed_connections = max(
                self._maximum_observed_connections,
                self._active_connections,
            )
        if self._synchronous_request:
            try:
                HTTPServer.process_request(self, request, client_address)
            finally:
                self._release_connection_slot()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_connection_slot()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_connection_slot()

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        # The preview has no access/error log because parsed request material can
        # contain private authority.  Client-visible failures are normalized by
        # the handler; transport aborts are intentionally silent.
        return


class LifecyclePreviewHandler(BaseHTTPRequestHandler):
    server: LifecyclePreviewServer
    server_version = "DREAMLANDLifecyclePreview/0.2"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_READ_TIMEOUT_SECONDS

    def setup(self) -> None:
        super().setup()
        self._connection_deadline = threading.Timer(
            CONNECTION_DEADLINE_SECONDS,
            self._expire_connection,
        )
        self._connection_deadline.daemon = True
        self._connection_deadline.start()

    def finish(self) -> None:
        self._connection_deadline.cancel()
        try:
            super().finish()
        except OSError:
            # A deadline or peer abort can close the socket during final flush.
            return

    def _expire_connection(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            return

    def log_message(self, format: str, *args: object) -> None:
        # Request paths and headers may be sensitive.  The explicit preview has
        # no access log; tests assert behavior from client-visible responses.
        return

    def handle_one_request(self) -> None:
        # ``BaseHTTPRequestHandler`` can emit an error response before it
        # delegates to ``parse_request`` (for an overlong request line) or
        # before it finishes parsing the next request's headers.  Reset both
        # framing markers at the true per-request entry point so a prior
        # keep-alive request cannot influence either parser-failure path.
        self._request_body_consumed = False
        self.headers = None
        super().handle_one_request()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if not parsed:
            return False
        major_version = int(self.request_version[5:].partition(".")[0])
        if major_version == 0:
            # BaseHTTPRequestHandler otherwise suppresses status and headers for
            # HTTP/0.9.  Reject it through the same bearer-first, display-safe
            # problem path as every other unsupported request.
            self.send_error(HTTPStatus.BAD_REQUEST)
            return False
        if self.server._synchronous_request:
            # A one-request subprocess must not wait for another request on an
            # HTTP/1.1 keep-alive connection after its response is complete.
            self.close_connection = True
        return True

    def send_response(self, code: int, message: str | None = None) -> None:
        """Apply the unread-body close invariant to every derived transport."""

        self._close_for_unread_request_body()
        super().send_response(code, message)

    def _send_json(
        self,
        status: HTTPStatus,
        value: Mapping[str, Any],
        *,
        content_type: str = "application/json",
    ) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def _close_for_unread_request_body(self) -> None:
        """Close when this response can leave framed request bytes unread."""

        if getattr(self, "_request_body_consumed", False):
            return
        headers = getattr(self, "headers", None)
        if headers is None:
            self.close_connection = True
            return
        if headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            return
        content_length_values = headers.get_all("Content-Length", [])
        if not content_length_values:
            return
        if len(content_length_values) != 1:
            self.close_connection = True
            return
        try:
            length = int(content_length_values[0])
        except ValueError:
            self.close_connection = True
            return
        if length != 0:
            self.close_connection = True

    def _send_problem(
        self,
        status: HTTPStatus,
        code: str,
        request_id: str,
    ) -> None:
        self._send_json(
            status,
            _safe_problem(code, request_id),
            content_type="application/problem+json",
        )

    def _drop_connection(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def _service_bearer_authorized(self) -> bool | None:
        headers = getattr(self, "headers", None)
        if headers is None:
            return None
        values = headers.get_all("Authorization", [])
        supplied = values[0] if len(values) == 1 else ""
        expected = "Bearer " + self.server.preview_bearer
        try:
            return hmac.compare_digest(
                supplied.encode("ascii"),
                expected.encode("ascii"),
            )
        except UnicodeEncodeError:
            return False

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace every stdlib parser/default HTML error with a safe problem."""

        del message, explain
        self.close_connection = True
        # parse_request leaves HTTP/0.9 in place for several malformed request
        # lines, which would otherwise suppress all response headers.
        self.request_version = "HTTP/1.1"
        request_id = self.server.next_request_id()
        authorized = self._service_bearer_authorized()
        if authorized is False:
            status = HTTPStatus.UNAUTHORIZED
            error_code = "unauthorized"
        elif code == HTTPStatus.NOT_IMPLEMENTED:
            # A syntactically valid but unknown method must still pass the
            # service-bearer gate, then behaves like every unavailable route.
            status = HTTPStatus.NOT_FOUND
            error_code = "not_found"
        else:
            # Parser failures cannot safely recover or echo parser text.  When
            # headers were unavailable, no authority determination is possible.
            status = HTTPStatus.BAD_REQUEST
            error_code = "invalid_request"
        self._send_problem(status, error_code, request_id)

    def _require_service_bearer(self, request_id: str) -> None:
        if self._service_bearer_authorized() is not True:
            raise PreviewTransportError(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "Private lifecycle preview authority is required.",
            )

    def _required_header(self, name: str, pattern: re.Pattern[str]) -> str:
        values = self.headers.get_all(name, [])
        value = values[0] if len(values) == 1 else None
        if value is None or not value.strip():
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                f"The required {name} header is missing.",
            )
        if len(value) > MAX_HEADER_VALUE_CHARACTERS or not pattern.fullmatch(value):
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                f"The {name} header is malformed.",
            )
        return value

    def _gate_expected_epoch(self, *, mutation: bool) -> None:
        values = self.headers.get_all(EXPECTED_EPOCH_HEADER, [])
        value = values[0] if len(values) == 1 else None
        if value is None or not value.strip():
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "expected_epoch_required",
                "Expected product service epoch evidence is required.",
            )
        if not SERVICE_EPOCH_PATTERN.fullmatch(value):
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The expected service epoch is malformed.",
            )
        if not hmac.compare_digest(
            value,
            self.server.preview.service.service_epoch,
        ):
            raise PreviewTransportError(
                HTTPStatus.CONFLICT,
                "outcome_unknown" if mutation else "session_lost",
                "The supplied service epoch is no longer current.",
            )

    def _read_json_object(self, expected_keys: frozenset[str]) -> dict[str, Any]:
        # Any framing rejection before the complete declared body has been read
        # leaves bytes whose meaning cannot safely be recovered on HTTP/1.1.
        # Fail closed until exact body consumption proves the connection is
        # synchronized, then restore the request's original keep-alive choice.
        close_after_request = self.close_connection
        self.close_connection = True
        content_type_values = self.headers.get_all("Content-Type", [])
        if len(content_type_values) > 1:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Content-Type must be supplied exactly once.",
            )
        content_type = content_type_values[0] if content_type_values else ""
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise PreviewTransportError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "The lifecycle preview accepts application/json only.",
            )
        if self.headers.get("Transfer-Encoding") is not None:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Chunked request bodies are unavailable in the lifecycle preview.",
            )
        content_length_values = self.headers.get_all("Content-Length", [])
        if not content_length_values:
            raise PreviewTransportError(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "A bounded Content-Length is required.",
            )
        if len(content_length_values) > 1:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Content-Length must be supplied exactly once.",
            )
        raw_length = content_length_values[0]
        try:
            length = int(raw_length)
        except ValueError as error:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Content-Length must be an integer.",
            ) from error
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise PreviewTransportError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "The lifecycle preview request body is too large.",
            )
        try:
            body = self.rfile.read(length)
        except TimeoutError as error:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The lifecycle preview request body was incomplete.",
            ) from error
        if len(body) != length:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The lifecycle preview request body was incomplete.",
            )
        self._request_body_consumed = True
        self.close_connection = close_after_request
        try:
            value = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "The lifecycle preview request body is not valid JSON.",
            ) from error
        if not isinstance(value, dict) or frozenset(value) != expected_keys:
            raise PreviewTransportError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "The lifecycle preview request body has unexpected fields.",
            )
        return value

    def _route(self) -> tuple[str, str | None]:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            return "not-found", None
        if parsed.path == CAPABILITIES_PATH:
            return "capabilities", None
        if parsed.path == SESSIONS_PATH:
            return "create", None
        if parsed.path == CURRENT_PATH:
            return "current", None
        if not parsed.path.startswith(SESSION_PATH_PREFIX):
            return "not-found", None
        remainder = parsed.path.removeprefix(SESSION_PATH_PREFIX)
        if remainder.endswith(SESSION_CLOSE_SUFFIX):
            encoded_reference = remainder.removesuffix(SESSION_CLOSE_SUFFIX)
            operation = "close"
        else:
            encoded_reference = remainder
            operation = "snapshot"
        if len(encoded_reference) > 420:
            return "not-found", None
        reference = unquote(encoded_reference)
        if not SESSION_REFERENCE_PATTERN.fullmatch(reference):
            return "not-found", None
        return operation, reference

    def _capabilities(self, request_id: str) -> dict[str, Any]:
        return {
            "contract": {
                "name": CONTRACT_NAME,
                "revision": CONTRACT_REVISION,
                "compatibility": CONTRACT_COMPATIBILITY,
            },
            "service_epoch": self.server.preview.service.service_epoch,
            "admission": {
                "state": "closed",
                "reason": "integration_preview_only",
            },
            "capabilities": {
                name: {
                    "declared_revision": CONTRACT_REVISION,
                    "enabled": False,
                    "enabled_revision": None,
                    "reason": "incomplete_integration_preview",
                }
                for name in CAPABILITY_NAMES
            },
            "request_id": request_id,
        }

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        request_id: str,
    ) -> Mapping[str, Any]:
        if operation == "capabilities":
            return self._capabilities(request_id)
        expected_epoch = self._required_header(
            EXPECTED_EPOCH_HEADER, SERVICE_EPOCH_PATTERN
        )
        actor_authority = self._required_header(
            ACTOR_CONTEXT_HEADER, ACTOR_CONTEXT_PATTERN
        )
        if operation == "current":
            self.server.preview.expire_before_read_if_selected()
            return _wire_result(
                self.server.preview.service.current(
                    actor_authority=actor_authority,
                    expected_epoch=expected_epoch,
                ),
                observed_at_ms=self.server.preview.clock.now_ms(),
            )
        if operation == "snapshot" and reference is not None:
            self.server.preview.expire_before_read_if_selected()
            session_authority = self._required_header(
                SESSION_CAPABILITY_HEADER, SESSION_CAPABILITY_PATTERN
            )
            return _wire_result(
                self.server.preview.service.snapshot(
                    actor_authority=actor_authority,
                    session_authority=session_authority,
                    reference=reference,
                    expected_epoch=expected_epoch,
                ),
                observed_at_ms=self.server.preview.clock.now_ms(),
            )
        raise PreviewTransportError(
            HTTPStatus.NOT_FOUND,
            "not_found",
            "The requested lifecycle preview operation is unavailable.",
        )

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        request_id: str,
    ) -> Mapping[str, Any] | None:
        expected_epoch = self._required_header(
            EXPECTED_EPOCH_HEADER, SERVICE_EPOCH_PATTERN
        )
        actor_authority = self._required_header(
            ACTOR_CONTEXT_HEADER, ACTOR_CONTEXT_PATTERN
        )
        idempotency_key = self._required_header(
            IDEMPOTENCY_HEADER, IDEMPOTENCY_KEY_PATTERN
        )
        if operation == "create":
            body = self._read_json_object(frozenset({"launch_grant"}))
            launch_grant = body["launch_grant"]
            if not isinstance(launch_grant, str) or not LAUNCH_GRANT_PATTERN.fullmatch(
                launch_grant
            ):
                raise PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "launch_grant must be an opaque string.",
                )
            domain_result = self.server.preview.service.create(
                actor_authority=actor_authority,
                launch_grant=launch_grant,
                expected_epoch=expected_epoch,
                idempotency_key=idempotency_key,
            )
            self.server.preview.note_created()
            if self.server.preview.should_drop_committed_response("create"):
                self._drop_connection()
                return None
            return _wire_result(
                domain_result,
                observed_at_ms=self.server.preview.clock.now_ms(),
            )
        if operation == "close" and reference is not None:
            session_authority = self._required_header(
                SESSION_CAPABILITY_HEADER, SESSION_CAPABILITY_PATTERN
            )
            body = self._read_json_object(frozenset({"expected_control_revision"}))
            control_revision = body["expected_control_revision"]
            if (
                not isinstance(control_revision, int)
                or isinstance(control_revision, bool)
                or control_revision < 1
                or control_revision > MAX_CONTROL_REVISION
            ):
                raise PreviewTransportError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "expected_control_revision must be an integer.",
                )
            domain_result = self.server.preview.service.close(
                actor_authority=actor_authority,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                idempotency_key=idempotency_key,
                expected_control_revision=control_revision,
            )
            if self.server.preview.should_drop_committed_response("close"):
                self._drop_connection()
                return None
            return _wire_result(
                domain_result,
                observed_at_ms=self.server.preview.clock.now_ms(),
            )
        raise PreviewTransportError(
            HTTPStatus.NOT_FOUND,
            "not_found",
            "The requested lifecycle preview operation is unavailable.",
        )

    def _handle(self, method: str) -> None:
        request_id = self.server.next_request_id()
        value: Mapping[str, Any] | None
        try:
            self._require_service_bearer(request_id)
            operation, reference = self._route()
            if method == "GET" and operation == "capabilities":
                value = self._dispatch_get(operation, reference, request_id)
            elif method == "GET" and operation in {"current", "snapshot"}:
                self._gate_expected_epoch(mutation=False)
                value = self._dispatch_get(operation, reference, request_id)
            elif method == "POST" and operation in {"create", "close"}:
                self._gate_expected_epoch(mutation=True)
                value = self._dispatch_post(operation, reference, request_id)
            else:
                raise PreviewTransportError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "The requested lifecycle preview operation is unavailable.",
                )
            if value is not None:
                status = (
                    HTTPStatus.CREATED
                    if method == "POST" and operation == "create"
                    else HTTPStatus.OK
                )
                self._send_json(status, value)
        except PreviewTransportError as error:
            self._send_problem(
                error.status,
                error.code,
                request_id,
            )
        except ProductLifecycleError as error:
            problem = _domain_problem(error, request_id)
            try:
                status = HTTPStatus(error.status)
            except (TypeError, ValueError):
                status = HTTPStatus.INTERNAL_SERVER_ERROR
            if error.code not in SAFE_ERROR_DETAILS:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
            self._send_json(
                status,
                problem,
                content_type="application/problem+json",
            )
        except Exception:
            # Do not log exception text: domain inputs include private authority.
            self._send_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                request_id,
            )

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def do_TRACE(self) -> None:
        self._handle("TRACE")

    def do_CONNECT(self) -> None:
        self._handle("CONNECT")


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
) -> LifecyclePreviewServer:
    """Build, but do not start, the explicit loopback preview server."""

    if not _loopback_host(host):
        raise ValueError(
            "the lifecycle preview supports only loopback hosts 127.0.0.1 or localhost"
        )
    if port < 0 or port > 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown lifecycle preview scenario")
    validated_contract_root = validate_contract_root(contract_root)
    return LifecyclePreviewServer(
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
    startup: dict[str, Any] = {
        "contract_revision": CONTRACT_REVISION,
        "host": address,
        "port": bound_port,
        "scenario": scenario,
        "test_only": True,
    }
    if scenario == "epoch-loss":
        startup["previous_service_epoch"] = server.preview.old_epoch
    print(
        json.dumps(startup, sort_keys=True),
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
        description="Serve the integration-only DREAMLAND lifecycle preview."
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
            else load_preview_bearer_file(arguments.token_file)
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
        print(f"lifecycle preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
