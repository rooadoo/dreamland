#!/usr/bin/env python3
"""Serve the explicit attendee-only live provider boundary.

This Phase 9B executable bolts a fixed spawn-process provider pool onto the
Phase 9A private attendee transport.  Importing the module creates no worker,
provider, listener, credential, artifact, timer, or ordinary DREAMLAND runtime
composition.  Only the explicit ``serve`` command imports one exactly pinned
authored revision, starts every provider worker, confirms local readiness, and
then binds a loopback listener.

The browser-facing ``access-ended`` result remains owned by the UI/BFF.  This
backend keeps the existing revocation and restart wire contract: deliberate
revocation uses ``attendee_context_revoked``/``authority_denied`` and a stale
epoch uses ``session_lost`` for reads or ``outcome_unknown`` for mutations.
There is no automatic authority remint, epoch rebase, or provider redispatch.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import stat
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.product.attendee_live import (
    AttendeeLiveProviderSettings,
    AttendeeLiveWorkerFactory,
)
from dreamland.product.attendee_provider import (
    AttendeeProviderLifecycle,
    AttendeeProviderPool,
    AttendeeProviderPoolError,
    AttendeeProviderPoolLimits,
    AttendeeProviderPoolSnapshot,
)
from dreamland.product.authored_artifacts import (
    ImmutableAuthoredStore,
    authored_revision_digest,
    import_configured_authored_profile,
    load_configured_authored_profile,
)
from dreamland.product.live_agent import DEFAULT_PUBLICATION_CHUNK_BYTES

logger = logging.getLogger(__name__)


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_security_live",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_SECURITY: Any = _load_tool("product_attendee_security_preview")
_ATTENDEE: Any = _SECURITY._ATTENDEE
_CORE: Any = _SECURITY._CORE
_LIFECYCLE: Any = _SECURITY._LIFECYCLE

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.8.0-rc.3"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
LIVE_EXECUTOR = "private-attendee-live-provider-pool-v1"
LIVE_KIND = "public_attendee_private_live_integration"
LIVE_ATTENDEE_NOTICE_VERSION = "notice:attendee-live-development-v1"
LIVE_ATTENDEE_POLICY_ID = "policy:attendee-live-authored-v1"
SCENARIOS = ("attendee-security-live",)

PRIVATE_ROUTE_ALLOWLIST = _SECURITY.PRIVATE_ROUTE_ALLOWLIST
ATTENDEE_CONTEXT_CAPACITY = _SECURITY.ATTENDEE_CONTEXT_CAPACITY

DEFAULT_CONTEXT_CAPACITY = ATTENDEE_CONTEXT_CAPACITY
DEFAULT_WORKER_CAPACITY = 2
DEFAULT_CONNECTION_CAPACITY = _SECURITY.ATTENDEE_CONNECTION_CAPACITY
MAXIMUM_CONTEXT_CAPACITY = _SECURITY.MAXIMUM_ATTENDEE_CONTEXT_CAPACITY
MAXIMUM_ACTIVE_SESSION_CAPACITY = _SECURITY.MAXIMUM_ATTENDEE_ACTIVE_SESSION_CAPACITY
MAXIMUM_CONNECTION_CAPACITY = _SECURITY.MAXIMUM_ATTENDEE_CONNECTION_CAPACITY
MAXIMUM_WORKER_CAPACITY = _SECURITY.MAXIMUM_ATTENDEE_WORKER_CAPACITY
DEFAULT_ACTIVE_SESSION_CAPACITY = ATTENDEE_CONTEXT_CAPACITY
MINIMUM_LOGICAL_JOB_CAPACITY = 8
DEFAULT_MAX_COMMANDS_PER_SESSION = 64

MINIMUM_TOKEN_RESERVATION = 8_192
RESERVED_SPEND_UNITS_PER_COMMAND = 1_000
COMMAND_DEADLINE_MS = 120_000
CANCELLATION_DEADLINE_MS = 2_000
STARTUP_TIMEOUT_MS = 30_000
TERMINATE_GRACE_MS = 250
# A request accepted while a retired slot is being replaced must be able to
# wait through old-child containment and the bounded child-bootstrap interval.
DISPATCH_TIMEOUT_MS = STARTUP_TIMEOUT_MS + 2 * TERMINATE_GRACE_MS
SHUTDOWN_TIMEOUT_MS = 10_000

MAX_COMMAND_BYTES = 4_096
MAX_RUNTIME_STATE_BYTES = 4 * 1_024 * 1_024
MAX_IPC_FRAME_BYTES = 5 * 1_024 * 1_024
MAX_OUTPUT_BYTES_PER_COMMAND = 64 * 1_024
# One accepted nonempty UTF-8 chunk contains at least one byte.  The byte
# boundary, not provider/newline segmentation, is therefore authoritative.
MAX_OUTPUT_CHUNKS_PER_COMMAND = MAX_OUTPUT_BYTES_PER_COMMAND
MAX_OUTPUT_CHUNK_BYTES = DEFAULT_PUBLICATION_CHUNK_BYTES


@dataclass(frozen=True, slots=True)
class LiveAttendeeRuntime:
    """Started, secret-free parent-side composition for one listener."""

    settings: AttendeeLiveProviderSettings
    pool_limits: AttendeeProviderPoolLimits
    composition: Any
    provider_pool: Any = field(repr=False)
    ready_snapshot: Any = field(repr=False)


class AttendeeSecurityLiveHarness(_SECURITY.AttendeeSecurityHarness):
    """Own the attendee facade, task threads, and isolated provider pool."""

    def __init__(
        self,
        *,
        bff_authority: str,
        scenario: str,
        runtime: LiveAttendeeRuntime,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown attendee security live scenario")
        if not isinstance(runtime, LiveAttendeeRuntime):
            raise TypeError("runtime must be LiveAttendeeRuntime")
        self.runtime = runtime
        self.provider_pool = runtime.provider_pool
        self._live_close_lock = threading.Lock()
        self._live_draining = False
        self._live_closed = False
        super().__init__(
            bff_authority=bff_authority,
            scenario=_SECURITY.SCENARIOS[0],
            composition=runtime.composition,
        )
        self.scenario = scenario

    def begin_drain(self) -> bool:
        """Reject new work while leaving replay and recovery surfaces alive."""
        with self._live_close_lock:
            if self._live_closed or self._live_draining:
                return False
            self._live_draining = True
        self._begin_drain_dependencies(suppress_errors=False)
        return True

    def _begin_drain_dependencies(self, *, suppress_errors: bool) -> None:
        first_error: Exception | None = None
        try:
            self.service.begin_drain()
        except Exception as error:
            first_error = error
            logger.exception("attendee_live_service_begin_drain_failed")
        try:
            self.provider_pool.begin_drain(
                timeout_ms=self.runtime.pool_limits.shutdown_timeout_ms
            )
        except Exception as error:
            if first_error is None:
                first_error = error
            logger.exception("attendee_live_provider_begin_drain_failed")
        if first_error is not None and not suppress_errors:
            raise first_error

    def close(self) -> None:
        """Fence authority, stop local provider work, then converge threads."""
        with self._live_close_lock:
            if self._live_closed:
                return
            self._live_closed = True
            should_begin_drain = not self._live_draining
            self._live_draining = True

        if should_begin_drain:
            self._begin_drain_dependencies(suppress_errors=True)

        # This ordering is deliberate.  The service fence first publishes the
        # existing terminal/revocation truth and cancellation signals.  The
        # process pool then gets its bounded cooperative/TERM/KILL window.  The
        # task executor is joined only after provider calls can no longer keep
        # a core task alive.
        try:
            self.service.shutdown()
        finally:
            try:
                self.provider_pool.shutdown(
                    timeout_ms=self.runtime.pool_limits.shutdown_timeout_ms
                )
            finally:
                self._workers.shutdown(wait=True, cancel_futures=True)


class AttendeeSecurityLiveServer(_SECURITY.AttendeeSecurityPreviewServer):
    """Loopback-only Phase 9B server with the Phase 9A route allowlist."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        bff_authority: str,
        scenario: str,
        contract_root: Path,
        runtime: LiveAttendeeRuntime,
        harness_factory: Callable[..., AttendeeSecurityLiveHarness] = (
            AttendeeSecurityLiveHarness
        ),
    ) -> None:
        self.preview_bearer = bearer
        self.contract_root = contract_root
        self.preview = harness_factory(
            bff_authority=bff_authority,
            scenario=scenario,
            runtime=runtime,
        )
        self._request_sequence = 0
        self._request_lock = threading.Lock()
        self.connection_capacity = runtime.composition.connection_capacity
        if self.connection_capacity != DEFAULT_CONNECTION_CAPACITY:
            self.request_queue_size = self.connection_capacity
        self._connection_slots = threading.BoundedSemaphore(self.connection_capacity)
        self._connection_count_lock = threading.Lock()
        self._active_connections = 0
        self._maximum_observed_connections = 0
        self._synchronous_request = False
        try:
            ThreadingHTTPServer.__init__(
                self,
                address,
                AttendeeSecurityLiveHandler,
            )
        except Exception:
            self.preview.close()
            raise

    def next_request_id(self) -> str:
        with self._request_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
        return f"request:attendee-security-live-{sequence:08d}"

    def begin_drain(self) -> bool:
        return self.preview.begin_drain()

    def server_close(self) -> None:
        try:
            HTTPServer.server_close(self)
        finally:
            self.preview.close()


class AttendeeSecurityLiveHandler(_SECURITY.AttendeeSecurityPreviewHandler):
    """Unchanged private attendee transport under the Phase 9B identity."""

    server: AttendeeSecurityLiveServer
    server_version = "DREAMLANDAttendeeSecurityLive/0.8"


def authored_digest(source_path: Path) -> str:
    """Calculate the exact metadata-free digest used for the startup pin."""
    return authored_revision_digest(load_configured_authored_profile(source_path))


def _token_reservation(max_output_tokens: int, logical_jobs: int) -> int:
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= 1_000_000_000
    ):
        raise ValueError("max output tokens must be a positive bounded integer")
    reservation = max(MINIMUM_TOKEN_RESERVATION, max_output_tokens)
    budget = reservation * logical_jobs
    if budget >= 2**63:
        raise ValueError("attendee live token budget exceeds the bounded limit")
    return reservation


def _logical_job_capacity(
    max_commands_per_session: int,
    active_session_capacity: int = DEFAULT_ACTIVE_SESSION_CAPACITY,
) -> int:
    if (
        isinstance(max_commands_per_session, bool)
        or not isinstance(max_commands_per_session, int)
        or not 1 <= max_commands_per_session <= 1_000_000_000
    ):
        raise ValueError("max commands per session must be a positive bounded integer")
    if (
        isinstance(active_session_capacity, bool)
        or not isinstance(active_session_capacity, int)
        or not 1 <= active_session_capacity <= MAXIMUM_ACTIVE_SESSION_CAPACITY
    ):
        raise ValueError(
            "active session capacity must be between 1 and "
            f"{MAXIMUM_ACTIVE_SESSION_CAPACITY}"
        )
    if max_commands_per_session > (2**63 - 1) // active_session_capacity:
        raise ValueError("attendee live logical-job capacity exceeds the bounded limit")
    return max(
        MINIMUM_LOGICAL_JOB_CAPACITY,
        active_session_capacity * max_commands_per_session,
    )


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _worker_capacity(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > MAXIMUM_WORKER_CAPACITY:
        raise argparse.ArgumentTypeError(
            f"worker capacity cannot exceed {MAXIMUM_WORKER_CAPACITY}"
        )
    return parsed


def _context_capacity(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > MAXIMUM_CONTEXT_CAPACITY:
        raise argparse.ArgumentTypeError(
            f"context capacity cannot exceed {MAXIMUM_CONTEXT_CAPACITY}"
        )
    return parsed


def _active_session_capacity(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > MAXIMUM_ACTIVE_SESSION_CAPACITY:
        raise argparse.ArgumentTypeError(
            f"active session capacity cannot exceed {MAXIMUM_ACTIVE_SESSION_CAPACITY}"
        )
    return parsed


def _connection_capacity(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > MAXIMUM_CONNECTION_CAPACITY:
        raise argparse.ArgumentTypeError(
            f"connection capacity cannot exceed {MAXIMUM_CONNECTION_CAPACITY}"
        )
    return parsed


def _capacity_envelope(
    *,
    context_capacity: int,
    active_session_capacity: int,
    connection_capacity: int,
    worker_capacity: int,
) -> int:
    values = (
        ("context capacity", context_capacity, MAXIMUM_CONTEXT_CAPACITY),
        (
            "active session capacity",
            active_session_capacity,
            MAXIMUM_ACTIVE_SESSION_CAPACITY,
        ),
        ("connection capacity", connection_capacity, MAXIMUM_CONNECTION_CAPACITY),
        ("worker capacity", worker_capacity, MAXIMUM_WORKER_CAPACITY),
    )
    for name, value, maximum in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be between 1 and {maximum}")
    if active_session_capacity > context_capacity:
        raise ValueError(
            "active session capacity cannot exceed attendee context capacity"
        )
    if worker_capacity > active_session_capacity:
        raise ValueError("worker capacity cannot exceed active session capacity")
    return _SECURITY.derived_fresh_operation_capacity(
        context_capacity=context_capacity,
        physical_worker_capacity=worker_capacity,
    )


def _output_chunk_limit(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > MAX_OUTPUT_CHUNKS_PER_COMMAND:
        raise argparse.ArgumentTypeError(
            "max output chunks per command cannot exceed "
            f"{MAX_OUTPUT_CHUNKS_PER_COMMAND}"
        )
    return parsed


def _private_artifact_root(path: Path) -> Path:
    try:
        root = Path(os.path.abspath(os.fspath(path)))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.lstat(root)
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("attendee live artifact root is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("attendee live artifact root must be owner-private")
    return root


def _pool_limits(
    *,
    worker_capacity: int,
    max_output_chunks_per_command: int = MAX_OUTPUT_CHUNKS_PER_COMMAND,
) -> AttendeeProviderPoolLimits:
    if (
        isinstance(worker_capacity, bool)
        or not isinstance(worker_capacity, int)
        or not 1 <= worker_capacity <= MAXIMUM_WORKER_CAPACITY
    ):
        raise ValueError(
            f"worker capacity must be between 1 and {MAXIMUM_WORKER_CAPACITY}"
        )
    if (
        isinstance(max_output_chunks_per_command, bool)
        or not isinstance(max_output_chunks_per_command, int)
        or not 1 <= max_output_chunks_per_command <= MAX_OUTPUT_CHUNKS_PER_COMMAND
    ):
        raise ValueError(
            "max output chunks per command must be between 1 and "
            f"{MAX_OUTPUT_CHUNKS_PER_COMMAND}"
        )
    limits = AttendeeProviderPoolLimits(
        worker_capacity=worker_capacity,
        startup_timeout_ms=STARTUP_TIMEOUT_MS,
        dispatch_timeout_ms=DISPATCH_TIMEOUT_MS,
        request_timeout_ms=COMMAND_DEADLINE_MS,
        cancellation_grace_ms=CANCELLATION_DEADLINE_MS,
        shutdown_timeout_ms=SHUTDOWN_TIMEOUT_MS,
        terminate_grace_ms=TERMINATE_GRACE_MS,
        max_command_bytes=MAX_COMMAND_BYTES,
        max_state_bytes=MAX_RUNTIME_STATE_BYTES,
        max_output_chunk_bytes=MAX_OUTPUT_CHUNK_BYTES,
        max_output_bytes=MAX_OUTPUT_BYTES_PER_COMMAND,
        max_output_chunks=max_output_chunks_per_command,
        max_ipc_frame_bytes=MAX_IPC_FRAME_BYTES,
    )
    if limits.dispatch_timeout_ms < limits.startup_timeout_ms:
        raise RuntimeError("provider dispatch timeout must cover replacement startup")
    return limits


def start_live_runtime(
    *,
    artifact_root: Path,
    authored_profile_path: Path,
    authored_profile_digest: str,
    provider_credential_file: Path,
    provider_settings: AttendeeLiveProviderSettings,
    context_capacity: int = DEFAULT_CONTEXT_CAPACITY,
    active_session_capacity: int = DEFAULT_ACTIVE_SESSION_CAPACITY,
    connection_capacity: int = DEFAULT_CONNECTION_CAPACITY,
    worker_capacity: int = DEFAULT_WORKER_CAPACITY,
    max_commands_per_session: int = DEFAULT_MAX_COMMANDS_PER_SESSION,
    max_output_chunks_per_command: int = MAX_OUTPUT_CHUNKS_PER_COMMAND,
    provider_worker_factory: Callable[..., Any] = AttendeeLiveWorkerFactory,
    provider_pool_factory: Callable[..., Any] = AttendeeProviderPool,
) -> LiveAttendeeRuntime:
    """Pin the world, construct the pool, and confirm every child is ready."""
    if not isinstance(provider_settings, AttendeeLiveProviderSettings):
        raise TypeError("provider_settings must be AttendeeLiveProviderSettings")
    max_fresh_operations_global = _capacity_envelope(
        context_capacity=context_capacity,
        active_session_capacity=active_session_capacity,
        connection_capacity=connection_capacity,
        worker_capacity=worker_capacity,
    )
    logical_job_capacity = _logical_job_capacity(
        max_commands_per_session,
        active_session_capacity,
    )
    limits = _pool_limits(
        worker_capacity=worker_capacity,
        max_output_chunks_per_command=max_output_chunks_per_command,
    )
    reservation = _token_reservation(
        provider_settings.max_output_tokens,
        logical_job_capacity,
    )
    spend_budget_units = RESERVED_SPEND_UNITS_PER_COMMAND * logical_job_capacity
    if spend_budget_units >= 2**63:
        raise ValueError("attendee live spend budget exceeds the bounded limit")
    private_root = _private_artifact_root(artifact_root)
    revision = import_configured_authored_profile(
        authored_profile_path,
        expected_digest=authored_profile_digest,
        store=ImmutableAuthoredStore(private_root / "authored-revisions"),
    )
    worker_factory = provider_worker_factory(
        settings=provider_settings,
        credential_file=provider_credential_file,
        authored_revision=revision,
    )
    provider_pool = provider_pool_factory(worker_factory, limits=limits)
    composition = _SECURITY.AttendeeSecurityComposition(
        executor=provider_pool,
        revision_document=revision.allocate_document(),
        physical_worker_capacity=worker_capacity,
        active_session_capacity=active_session_capacity,
        logical_job_capacity=logical_job_capacity,
        max_commands_per_session=max_commands_per_session,
        command_deadline_ms=COMMAND_DEADLINE_MS,
        cancellation_deadline_ms=CANCELLATION_DEADLINE_MS,
        max_output_bytes_per_command=MAX_OUTPUT_BYTES_PER_COMMAND,
        max_output_chunks_per_command=max_output_chunks_per_command,
        max_runtime_document_bytes=MAX_RUNTIME_STATE_BYTES,
        reserved_tokens_per_command=reservation,
        reserved_spend_units_per_command=RESERVED_SPEND_UNITS_PER_COMMAND,
        token_budget=reservation * logical_job_capacity,
        spend_budget_units=spend_budget_units,
        notice_version=LIVE_ATTENDEE_NOTICE_VERSION,
        policy_id=LIVE_ATTENDEE_POLICY_ID,
        context_capacity=context_capacity,
        connection_capacity=connection_capacity,
        max_fresh_operations_per_context=(
            _SECURITY.ATTENDEE_FRESH_OPERATIONS_PER_CONTEXT
        ),
        max_fresh_operations_global=max_fresh_operations_global,
        validate_runtime_document=limits.validate_runtime_document,
    )
    if provider_pool.limits.max_state_bytes != composition.max_runtime_document_bytes:
        raise RuntimeError(
            "provider pool and Product core runtime-state limits must match"
        )
    if (
        provider_pool.limits.max_output_chunks
        != composition.max_output_chunks_per_command
    ):
        raise RuntimeError(
            "provider pool and Product core output-chunk limits must match"
        )

    try:
        snapshot = provider_pool.start()
        if (
            snapshot.lifecycle is not AttendeeProviderLifecycle.READY
            or snapshot.worker_capacity != worker_capacity
            or snapshot.ready_workers != worker_capacity
        ):
            raise ValueError("attendee live provider pool did not become ready")
    except Exception:
        try:
            provider_pool.shutdown(timeout_ms=limits.shutdown_timeout_ms)
        except Exception:
            logger.exception("attendee_live_provider_startup_rollback_failed")
        raise
    return LiveAttendeeRuntime(
        settings=provider_settings,
        pool_limits=limits,
        composition=composition,
        provider_pool=provider_pool,
        ready_snapshot=snapshot,
    )


def _configure_live_transport() -> None:
    _SECURITY.CONTRACT_NAME = CONTRACT_NAME
    _SECURITY.CONTRACT_REVISION = CONTRACT_REVISION
    _SECURITY.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    _SECURITY.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _SECURITY.PREVIEW_KIND = LIVE_KIND
    _SECURITY._configure_transport()
    _SECURITY._ATTENDEE._STAGE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = LIVE_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name(
        "product_attendee_security_live_identity_contract.py"
    )
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_live_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee security live contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("attendee security live contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 25
    ):
        raise ValueError("attendee security live contract is incompatible")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        openapi = json.loads((root / "openapi.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("attendee security live contract is unavailable") from error
    capabilities = manifest.get("capabilities")
    if (
        not isinstance(capabilities, Mapping)
        or len(capabilities) != 5
        or any(
            not isinstance(value, Mapping)
            or value.get("enabled") is not False
            or value.get("enabled_revision") is not None
            for value in capabilities.values()
        )
    ):
        raise ValueError("attendee security live capabilities must remain disabled")
    try:
        privacy_identity = openapi["x-dreamland-attendee-provider-boundary"][
            "privacy_identity"
        ]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "attendee security live privacy identity is unavailable"
        ) from error
    if privacy_identity != {
        "notice_version": LIVE_ATTENDEE_NOTICE_VERSION,
        "policy_id": LIVE_ATTENDEE_POLICY_ID,
    }:
        raise ValueError("attendee security live privacy identity is incompatible")
    return root


def build_live_server(
    *,
    host: str,
    port: int,
    bearer: str,
    bff_authority: str,
    scenario: str,
    contract_root: Path,
    artifact_root: Path,
    authored_profile_path: Path,
    authored_profile_digest: str,
    provider_credential_file: Path,
    provider_settings: AttendeeLiveProviderSettings,
    context_capacity: int = DEFAULT_CONTEXT_CAPACITY,
    active_session_capacity: int = DEFAULT_ACTIVE_SESSION_CAPACITY,
    connection_capacity: int = DEFAULT_CONNECTION_CAPACITY,
    worker_capacity: int = DEFAULT_WORKER_CAPACITY,
    max_commands_per_session: int = DEFAULT_MAX_COMMANDS_PER_SESSION,
    max_output_chunks_per_command: int = MAX_OUTPUT_CHUNKS_PER_COMMAND,
    contract_validator: Callable[[Path], Path] = validate_contract_root,
    provider_worker_factory: Callable[..., Any] = AttendeeLiveWorkerFactory,
    provider_pool_factory: Callable[..., Any] = AttendeeProviderPool,
    harness_factory: Callable[..., AttendeeSecurityLiveHarness] = (
        AttendeeSecurityLiveHarness
    ),
    server_factory: Callable[..., AttendeeSecurityLiveServer] = (
        AttendeeSecurityLiveServer
    ),
) -> AttendeeSecurityLiveServer:
    """Validate, ready the whole provider pool, and only then bind loopback."""
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("attendee security live supports only loopback hosts")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("attendee security live port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if (
        not isinstance(bff_authority, str)
        or _SECURITY._BFF_AUTHORITY_PATTERN.fullmatch(bff_authority) is None
    ):
        raise ValueError("private attendee BFF authority is malformed")
    if scenario not in SCENARIOS:
        raise ValueError("unknown attendee security live scenario")
    root = contract_validator(contract_root)
    _configure_live_transport()
    runtime = start_live_runtime(
        artifact_root=artifact_root,
        authored_profile_path=authored_profile_path,
        authored_profile_digest=authored_profile_digest,
        provider_credential_file=provider_credential_file,
        provider_settings=provider_settings,
        context_capacity=context_capacity,
        active_session_capacity=active_session_capacity,
        connection_capacity=connection_capacity,
        worker_capacity=worker_capacity,
        max_commands_per_session=max_commands_per_session,
        max_output_chunks_per_command=max_output_chunks_per_command,
        provider_worker_factory=provider_worker_factory,
        provider_pool_factory=provider_pool_factory,
    )
    try:
        return server_factory(
            (host, port),
            bearer=bearer,
            bff_authority=bff_authority,
            scenario=scenario,
            contract_root=root,
            runtime=runtime,
            harness_factory=harness_factory,
        )
    except Exception:
        try:
            runtime.provider_pool.shutdown(
                timeout_ms=runtime.pool_limits.shutdown_timeout_ms
            )
        except Exception:
            logger.exception("attendee_live_listener_rollback_failed")
        raise


def startup_announcement(server: AttendeeSecurityLiveServer) -> dict[str, object]:
    address, port = server.server_address[:2]
    harness = server.preview
    runtime = harness.runtime
    composition = runtime.composition
    policy = harness.request_admission.policy
    registry_policy = harness.service._registry._policy
    lifecycle_policy = harness.service._core._policy
    context_policy = harness.service._policy
    connection_capacity = getattr(
        server,
        "connection_capacity",
        composition.connection_capacity,
    )
    snapshot: AttendeeProviderPoolSnapshot = runtime.provider_pool.snapshot()
    if (
        snapshot.lifecycle is not AttendeeProviderLifecycle.READY
        or snapshot.ready_workers != snapshot.worker_capacity
    ):
        raise ValueError("attendee live provider pool is not ready")
    return {
        "capabilities_enabled": 0,
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        },
        "deployment": {
            "authority": "owner_only_private_bff",
            "browser_direct_supported": False,
            "exposure": "loopback_only",
            "production_capabilities": False,
        },
        "executor": LIVE_EXECUTOR,
        "host": address,
        "integration_only": True,
        "kind": LIVE_KIND,
        "normal_runtime_composition": False,
        "output_publication": {
            "flush_bytes": MAX_OUTPUT_CHUNK_BYTES,
            "flush_on_newline": True,
            "max_bytes_per_command": MAX_OUTPUT_BYTES_PER_COMMAND,
            "max_chunks_per_command": composition.max_output_chunks_per_command,
        },
        "port": port,
        "provider": {
            "lifecycle": snapshot.lifecycle.value,
            "max_output_tokens": runtime.settings.max_output_tokens,
            "model": runtime.settings.model,
            "readiness_scope": (
                "all_local_worker_processes_and_provider_clients_constructed"
            ),
            "ready_workers": snapshot.ready_workers,
            "upstream_reachability_checked": False,
            "upstream_termination_evidence": (
                snapshot.forced_upstream_termination_evidence.value
            ),
            "worker_capacity": snapshot.worker_capacity,
        },
        "provider_calls": True,
        "privacy": {
            "notice_version": composition.notice_version,
            "policy_id": composition.policy_id,
        },
        "resources": {
            "active_sessions": composition.active_session_capacity,
            "command_deadline_ms": composition.command_deadline_ms,
            "command_size_bytes": MAX_COMMAND_BYTES,
            "context_capacity": composition.context_capacity,
            "dispatch_timeout_ms": runtime.pool_limits.dispatch_timeout_ms,
            "effective_command_limit": composition.max_commands_per_session,
            "hard_maximums": {
                "active_sessions": MAXIMUM_ACTIVE_SESSION_CAPACITY,
                "connections": MAXIMUM_CONNECTION_CAPACITY,
                "contexts": MAXIMUM_CONTEXT_CAPACITY,
                "physical_workers": MAXIMUM_WORKER_CAPACITY,
            },
            "logical_jobs": composition.logical_job_capacity,
            "physical_workers": composition.physical_worker_capacity,
            "retention": {
                "bootstrap_idempotency_records": (
                    context_policy.max_bootstrap_idempotency_records
                ),
                "exchange_idempotency_records": (
                    context_policy.max_exchange_idempotency_records
                ),
                "launch_grants": registry_policy.max_active_launch_grants,
                "lifecycle_idempotency_records": (
                    lifecycle_policy.max_idempotency_records
                ),
                "revoke_idempotency_records": (
                    context_policy.max_revoke_idempotency_records
                ),
                "selection_idempotency_records": (
                    registry_policy.max_idempotency_records
                ),
                "selection_token_history": registry_policy.max_token_history,
                "tombstones": lifecycle_policy.max_tombstones,
            },
            "runtime_state_bytes": runtime.pool_limits.max_state_bytes,
            "shutdown_upper_bound_ms": (runtime.pool_limits.shutdown_upper_bound_ms()),
            "spend_budget_units": composition.spend_budget_units,
            "spend_reservation_per_command": (
                composition.reserved_spend_units_per_command
            ),
            "startup_timeout_ms": runtime.pool_limits.startup_timeout_ms,
            "token_budget": composition.token_budget,
            "token_reservation_per_command": (composition.reserved_tokens_per_command),
        },
        "route_count": len(PRIVATE_ROUTE_ALLOWLIST),
        "scenario": harness.scenario,
        "security": {
            "connection_deadline_seconds": _LIFECYCLE.CONNECTION_DEADLINE_SECONDS,
            "connection_limit": connection_capacity,
            "logical_global_limit": policy.max_fresh_operations_global,
            "logical_per_context_limit": policy.max_fresh_operations_per_context,
            "logical_tracked_contexts": policy.max_tracked_contexts,
            "logical_window_ms": policy.window_ms,
            "max_request_body_bytes": _CORE.MAX_REQUEST_BODY_BYTES,
            "read_timeout_seconds": _LIFECYCLE.CONNECTION_READ_TIMEOUT_SECONDS,
        },
        "test_only": True,
    }


def serve_live(
    *,
    host: str,
    port: int,
    bearer: str,
    bff_authority: str,
    scenario: str,
    contract_root: Path,
    artifact_root: Path,
    authored_profile_path: Path,
    authored_profile_digest: str,
    provider_credential_file: Path,
    provider_settings: AttendeeLiveProviderSettings,
    context_capacity: int = DEFAULT_CONTEXT_CAPACITY,
    active_session_capacity: int = DEFAULT_ACTIVE_SESSION_CAPACITY,
    connection_capacity: int = DEFAULT_CONNECTION_CAPACITY,
    worker_capacity: int,
    max_commands_per_session: int,
    once: bool,
    max_output_chunks_per_command: int = MAX_OUTPUT_CHUNKS_PER_COMMAND,
) -> None:
    server = build_live_server(
        host=host,
        port=port,
        bearer=bearer,
        bff_authority=bff_authority,
        scenario=scenario,
        contract_root=contract_root,
        artifact_root=artifact_root,
        authored_profile_path=authored_profile_path,
        authored_profile_digest=authored_profile_digest,
        provider_credential_file=provider_credential_file,
        provider_settings=provider_settings,
        context_capacity=context_capacity,
        active_session_capacity=active_session_capacity,
        connection_capacity=connection_capacity,
        worker_capacity=worker_capacity,
        max_commands_per_session=max_commands_per_session,
        max_output_chunks_per_command=max_output_chunks_per_command,
    )
    try:
        print(json.dumps(startup_announcement(server), sort_keys=True), flush=True)
        if once:
            server.handle_request_synchronously()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        server.begin_drain()
        return
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the attendee-only private live provider boundary."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    digest = commands.add_parser(
        "digest",
        help="print the immutable digest pin for one trusted authored profile",
    )
    digest.add_argument("--profile-path", required=True, type=Path)
    serve = commands.add_parser("serve", help="start the loopback listener")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file", required=True, type=Path)
    serve.add_argument("--bootstrap-file", required=True, type=Path)
    serve.add_argument("--contract-root", required=True, type=Path)
    serve.add_argument("--artifact-root", required=True, type=Path)
    serve.add_argument("--authored-profile-path", required=True, type=Path)
    serve.add_argument("--authored-profile-digest", required=True)
    serve.add_argument("--provider-credential-file", required=True, type=Path)
    serve.add_argument("--model", required=True)
    serve.add_argument(
        "--max-output-tokens",
        required=True,
        type=_positive_integer,
    )
    serve.add_argument("--system-prompt", default="system.md")
    serve.add_argument(
        "--context-capacity",
        type=_context_capacity,
        default=DEFAULT_CONTEXT_CAPACITY,
    )
    serve.add_argument(
        "--active-session-capacity",
        type=_active_session_capacity,
        default=DEFAULT_ACTIVE_SESSION_CAPACITY,
    )
    serve.add_argument(
        "--connection-capacity",
        type=_connection_capacity,
        default=DEFAULT_CONNECTION_CAPACITY,
    )
    serve.add_argument(
        "--worker-capacity",
        type=_worker_capacity,
        default=DEFAULT_WORKER_CAPACITY,
    )
    serve.add_argument(
        "--max-commands",
        type=_positive_integer,
        default=DEFAULT_MAX_COMMANDS_PER_SESSION,
    )
    serve.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
    )
    serve.add_argument(
        "--reasoning-summary",
        choices=("auto", "concise", "detailed"),
    )
    serve.add_argument("--verbosity", choices=("low", "medium", "high"))
    serve.add_argument("--speed", choices=("fast",))
    serve.add_argument("--prompt-cache", choices=("automatic", "off"))
    serve.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init":
            _SECURITY.initialize_private_files(
                token_file=arguments.token_file,
                bootstrap_file=arguments.bootstrap_file,
            )
            print(json.dumps({"created": True}, sort_keys=True))
            return 0
        if arguments.command == "digest":
            print(
                json.dumps(
                    {
                        "authored_revision_digest": authored_digest(
                            arguments.profile_path
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0

        bearer = _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        bff_authority = _SECURITY.load_bootstrap(arguments.bootstrap_file)
        provider_settings = AttendeeLiveProviderSettings(
            model=arguments.model,
            system_prompt_name=arguments.system_prompt,
            max_output_tokens=arguments.max_output_tokens,
            reasoning_effort=arguments.reasoning_effort,
            reasoning_summary=arguments.reasoning_summary,
            verbosity=arguments.verbosity,
            speed=arguments.speed,
            prompt_cache=arguments.prompt_cache,
        )
        serve_live(
            host=arguments.host,
            port=arguments.port,
            bearer=bearer,
            bff_authority=bff_authority,
            scenario=arguments.scenario,
            contract_root=arguments.contract_root,
            artifact_root=arguments.artifact_root,
            authored_profile_path=arguments.authored_profile_path,
            authored_profile_digest=arguments.authored_profile_digest,
            provider_credential_file=arguments.provider_credential_file,
            provider_settings=provider_settings,
            context_capacity=arguments.context_capacity,
            active_session_capacity=arguments.active_session_capacity,
            connection_capacity=arguments.connection_capacity,
            worker_capacity=arguments.worker_capacity,
            max_commands_per_session=arguments.max_commands,
            once=arguments.once,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, AttendeeProviderPoolError) as error:
        print(f"Attendee security live error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
