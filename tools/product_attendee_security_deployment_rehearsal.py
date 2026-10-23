#!/usr/bin/env python3
"""Run the provider-free Phase 9C private-boundary deployment rehearsal.

The public ``run`` command owns disposable owner-private fixture material and
drives the immutable Product RC4 generated client through real loopback
subprocesses.  The internal fixture keeps the real supervised server and real
worker-process pool, replacing only provider execution with a deterministic
finite collaborator.  Importing this module starts no listener, process,
worker, timer, credential read, or DREAMLAND composition.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamland.product.execution import (
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputPublisher,
    ProductOutputStream,
)
from dreamland.state_profiles import StateProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.4"
SCHEMA = "dreamland.attendee-deployment-rehearsal/v1"
SCOPE = "loopback_provider_free"
PRODUCTION_CAPABILITY_EVIDENCE = False
FIXTURE_KIND = "attendee_deployment_rehearsal_fixture"
REPORT_KIND = "attendee_deployment_rehearsal_report"
BLOCK_COMMAND = "rehearsal:block-until-cancelled"
COMPLETE_COMMAND = "rehearsal:complete"
BEFORE_OUTPUT = "provider-free-before-cancel\n"
AFTER_OUTPUT = "provider-free-after-cancel\n"
WORKER_CAPACITY = 2
CONTEXT_CAPACITY = 4
SESSION_CAPACITY = 4
CONNECTION_CAPACITY = 8
STARTUP_TIMEOUT_SECONDS = 35.0
REQUEST_POLL_SECONDS = 0.05
COMMAND_TIMEOUT_SECONDS = 8.0
BLOCK_SAFETY_SECONDS = 30.0
PROCESS_MARGIN_SECONDS = 5.0
MAX_CAPTURE_BYTES = 1_048_576
PUBLIC_RUN_TIMEOUT_SECONDS = 300
CUTOFF_ADMISSION_MS = 8_000
CUTOFF_RECOVERY_MS = 25_000

_SIGNAL_DEFERRAL_DEPTH = 0
_DEFERRED_REHEARSAL_SIGNAL: int | None = None

_FIXED_CASES = (
    "contract_identity",
    "context_replay_conflict",
    "context_session_capacity",
    "worker_capacity_cancellation",
    "recovery_cleanup",
    "unread_body_pipeline_close",
    "connection_saturation",
    "transport_authority_restart",
    "cutoff_terminal_recovery",
    "signal_convergence",
    "privacy_redaction",
)
_SAFE_CODES = frozenset(
    {
        "authority_denied",
        "attendee_context_capacity_exhausted",
        "capacity_exhausted",
        "idempotency_conflict",
        "invalid_request",
        "logical_rate_limited",
        "outcome_unknown",
        "service_closed",
        "session_lost",
        "unauthorized",
        "unsupported_media_type",
    }
)
_REPORT_ROOT_KEYS = frozenset(
    {
        "kind",
        "schema",
        "scope",
        "production_capability_evidence",
        "contract",
        "limits",
        "checks",
        "summary",
    }
)
_CHECK_KEYS = frozenset({"id", "passed", "metrics", "codes"})
_SAFE_METRIC_KEYS = frozenset(
    {
        "active_sessions",
        "capabilities_enabled",
        "checks_passed",
        "checks_total",
        "contexts",
        "dispatches",
        "elapsed_ms",
        "maximum_connections",
        "operations",
        "output_events",
        "sessions",
        "signal_exit_code",
        "saturated_shutdown_ms",
        "workers",
    }
)

_EXPECTED_CHECK_EVIDENCE: dict[
    str,
    tuple[Mapping[str, int | None], tuple[str, ...]],
] = {
    "contract_identity": (
        {"operations": 25, "capabilities_enabled": 0},
        (),
    ),
    "context_replay_conflict": ({}, ("idempotency_conflict",)),
    "context_session_capacity": (
        {"contexts": 4, "sessions": 4, "active_sessions": 4},
        ("logical_rate_limited", "attendee_context_capacity_exhausted"),
    ),
    "worker_capacity_cancellation": (
        {"workers": 2, "dispatches": 2, "output_events": 2},
        ("capacity_exhausted",),
    ),
    "recovery_cleanup": ({"contexts": 4, "sessions": 4}, ()),
    "unread_body_pipeline_close": ({}, ("unsupported_media_type",)),
    "connection_saturation": (
        {"maximum_connections": 8, "elapsed_ms": None},
        (),
    ),
    "transport_authority_restart": (
        {"signal_exit_code": 128 + signal.SIGTERM},
        (
            "unauthorized",
            "unauthorized",
            "session_lost",
            "outcome_unknown",
            "outcome_unknown",
            "authority_denied",
        ),
    ),
    "cutoff_terminal_recovery": (
        {"dispatches": 1, "output_events": 1, "signal_exit_code": 0},
        ("idempotency_conflict", "service_closed"),
    ),
    "signal_convergence": (
        {
            "signal_exit_code": 128 + signal.SIGINT,
            "dispatches": 0,
            "saturated_shutdown_ms": None,
        },
        (),
    ),
    "privacy_redaction": ({}, ()),
}


class _RehearsalSignal(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid rehearsal arguments")


@contextmanager
def _defer_rehearsal_signal() -> Iterator[None]:
    """Defer the public signal exception across child registration."""

    global _DEFERRED_REHEARSAL_SIGNAL, _SIGNAL_DEFERRAL_DEPTH
    _SIGNAL_DEFERRAL_DEPTH += 1
    try:
        yield
    finally:
        _SIGNAL_DEFERRAL_DEPTH -= 1
        if _SIGNAL_DEFERRAL_DEPTH == 0 and _DEFERRED_REHEARSAL_SIGNAL is not None:
            signum = _DEFERRED_REHEARSAL_SIGNAL
            _DEFERRED_REHEARSAL_SIGNAL = None
            raise _RehearsalSignal(signum)


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_deployment_rehearsal",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _load_rc4_client(contract_root: Path) -> ModuleType:
    root = contract_root.resolve()
    deployment = _load_tool("product_attendee_security_deployment")
    deployment.validate_contract_root(root)
    path = root / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_rc4_rehearsal_client",
        path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("RC4 generated client is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    if (
        module.CONTRACT_REVISION != deployment.CONTRACT_REVISION
        or len(module.EXPECTED_OPERATIONS) != 25
    ):
        raise ValueError("RC4 generated client identity is incompatible")
    return module


class DeterministicRehearsalExecutor:
    """Finite worker-side executor with no provider dependency."""

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult:
        if request.canonical_command == BLOCK_COMMAND:
            emit(ProductOutputChunk(ProductOutputStream.STDOUT, BEFORE_OUTPUT))
            safety_deadline = time.monotonic() + BLOCK_SAFETY_SECONDS
            while not request.cancelled() and time.monotonic() < safety_deadline:
                time.sleep(0.01)
            if not request.cancelled():
                return ProductExecutionResult.failed(
                    "rehearsal_cancellation_not_observed",
                    usage_tokens=2,
                    usage_spend_units=2,
                )
            emit(ProductOutputChunk(ProductOutputStream.STDOUT, AFTER_OUTPUT))
            return ProductExecutionResult.interrupted(
                "cancelled",
                usage_tokens=1,
                usage_spend_units=1,
            )
        if request.canonical_command != COMPLETE_COMMAND:
            return ProductExecutionResult.failed("provider_request_invalid")
        emit(ProductOutputChunk(ProductOutputStream.STDOUT, "provider-free-ok\n"))
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=1,
            usage_spend_units=1,
        )


class DeterministicRehearsalWorkerFactory:
    """Picklable real-pool worker factory that deliberately reads no secret."""

    def __init__(self, **_values: object) -> None:
        pass

    def __call__(self) -> DeterministicRehearsalExecutor:
        return DeterministicRehearsalExecutor()


@dataclass(slots=True, repr=False)
class _PrivateWorkflowState:
    gateway_context_id: str
    bootstrap_key: str
    actor: str
    selection_id: str
    notice_version: str
    exchange_key: str
    launch_grant: str
    create_key: str
    session_authority: str
    reference: str
    control_revision: int
    slot_generation: int
    command_id: str | None = None
    command_key: str | None = None


@dataclass(frozen=True, slots=True)
class _Problem:
    status: int
    code: str
    retry: str
    retry_after_ms: int | None


@dataclass(frozen=True, slots=True)
class _Check:
    identifier: str
    metrics: Mapping[str, int | bool] = field(default_factory=dict)
    codes: tuple[str, ...] = ()

    def public(self) -> dict[str, object]:
        if self.identifier not in _FIXED_CASES:
            raise ValueError("rehearsal check identifier is not allowlisted")
        if not set(self.metrics).issubset(_SAFE_METRIC_KEYS):
            raise ValueError("rehearsal check metric is not allowlisted")
        if any(code not in _SAFE_CODES for code in self.codes):
            raise ValueError("rehearsal check code is not allowlisted")
        for value in self.metrics.values():
            if isinstance(value, bool):
                continue
            if not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
                raise ValueError("rehearsal check metric is invalid")
        _validate_check_evidence(self.identifier, self.metrics, self.codes)
        return {
            "id": self.identifier,
            "passed": True,
            "metrics": dict(sorted(self.metrics.items())),
            "codes": list(self.codes),
        }


def _validate_check_evidence(
    identifier: str,
    metrics: Mapping[str, object],
    codes: Sequence[object],
) -> None:
    expected = _EXPECTED_CHECK_EVIDENCE.get(identifier)
    if expected is None:
        raise ValueError("rehearsal check identifier is not allowlisted")
    expected_metrics, expected_codes = expected
    if set(metrics) != set(expected_metrics) or tuple(codes) != expected_codes:
        raise ValueError("rehearsal check evidence diverged")
    for name, expected_value in expected_metrics.items():
        value = metrics[name]
        if expected_value is None:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 60_000
            ):
                raise ValueError("rehearsal dynamic metric is invalid")
        elif value != expected_value:
            raise ValueError("rehearsal check metric diverged")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _key(label: str) -> str:
    return f"rehearsal:{label}:{secrets.token_urlsafe(18)}"


def _write_owner_file(path: Path, text: str, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = path
    if replace:
        target = path.with_name(path.name + ".replacement")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if replace:
        os.replace(target, path)


def _write_profile(path: Path) -> None:
    profile = StateProfile(
        name="attendee-deployment-rehearsal-fixture",
        level="world",
        created_at="2026-09-02T00:00:00+00:00",
        state={
            "world_state": {
                "name": "attendee-deployment-rehearsal-world",
                "entry_host": "console",
                "active_host": "console",
                "hosts_by_name": {
                    "console": {
                        "hostname": "console",
                        "login_banner": "",
                        "purpose": {"theme": "Finite provider-free rehearsal"},
                        "shell": {
                            "default": {
                                "name": "bash",
                                "command": "/bin/bash",
                                "argv": ["bash"],
                            }
                        },
                    }
                },
            }
        },
    )
    _write_owner_file(
        path,
        json.dumps(profile.to_document(), sort_keys=True) + "\n",
    )


def _strict_problem(
    client_module: ModuleType,
    action: Callable[[], object],
    *,
    status: int,
    code: str,
    safe_error_details: Mapping[str, tuple[int, str, str, int | None]],
) -> _Problem:
    try:
        action()
    except client_module.ProductSessionPreviewError as error:
        payload = error.payload
        required = {"contract", "preview", "error"}
        if (
            error.status != status
            or not required.issubset(payload)
            or set(payload) - required - {"service_epoch"}
        ):
            observed_code = payload.get("error", {}).get("code")
            raise AssertionError(
                "private error envelope diverged "
                f"status={error.status} code={observed_code} "
                f"keys={sorted(payload)}"
            ) from None
        detail = payload.get("error")
        required_detail = {
            "code",
            "message",
            "retry",
            "retry_after_ms",
            "request_id",
        }
        if (
            not isinstance(detail, dict)
            or not required_detail.issubset(detail)
            or set(detail) - required_detail - {"context"}
        ):
            raise AssertionError("private error detail diverged") from None
        expected = safe_error_details[code]
        if (
            detail["code"] != code
            or expected[0] != status
            or detail["message"] != expected[1]
            or detail["retry"] != expected[2]
            or detail["retry_after_ms"] != expected[3]
        ):
            raise AssertionError(
                "private error tuple diverged "
                f"status={error.status}/{expected[0]} code={detail['code']} "
                f"retry={detail['retry']} after={detail['retry_after_ms']}"
            ) from None
        return _Problem(
            status=error.status,
            code=code,
            retry=detail["retry"],
            retry_after_ms=detail["retry_after_ms"],
        )
    raise AssertionError(f"expected private error {code}")


class _FixtureProcess:
    """Own one real supervised fixture subprocess and its captured output."""

    def __init__(
        self,
        *,
        contract_root: Path,
        root: Path,
        token_file: Path,
        bootstrap_file: Path,
        profile_path: Path,
        provider_file: Path,
        artifact_root: Path,
        close_after_ms: int,
        recovery_after_ms: int,
        sentinels: list[str],
        window_after_readiness: bool = False,
    ) -> None:
        self.contract_root = contract_root
        self.root = root
        self.token_file = token_file
        self.bootstrap_file = bootstrap_file
        self.profile_path = profile_path
        self.provider_file = provider_file
        self.artifact_root = artifact_root
        self.close_after_ms = close_after_ms
        self.recovery_after_ms = recovery_after_ms
        self.sentinels = sentinels
        self.window_after_readiness = window_after_readiness
        self.process: subprocess.Popen[str] | None = None
        self.announcement: dict[str, Any] = {}
        self.diagnostic: dict[str, Any] = {}
        self.stdout_text = ""
        self.stderr_text = ""
        self._stderr_stream: Any | None = None
        self._stderr_path = root / f"fixture-stderr-{secrets.token_hex(8)}.log"
        self._process_group_id: int | None = None
        self._finished_cleanly = False
        self._forced_cleanup_complete = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.announcement['port']}"

    def start(self) -> _FixtureProcess:
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "_serve-fixture",
            "--contract-root",
            str(self.contract_root),
            "--token-file",
            str(self.token_file),
            "--bootstrap-file",
            str(self.bootstrap_file),
            "--profile-path",
            str(self.profile_path),
            "--provider-file",
            str(self.provider_file),
            "--artifact-root",
            str(self.artifact_root),
            "--close-after-ms",
            str(self.close_after_ms),
            "--recovery-after-ms",
            str(self.recovery_after_ms),
        ]
        if self.window_after_readiness:
            command.append("--window-after-readiness")
        try:
            _write_owner_file(self._stderr_path, "")
            self._stderr_stream = self._stderr_path.open("w", encoding="utf-8")
            # The public signal handlers normally raise to unwind the
            # enclosing ExitStack.  Defer that exception across fork/exec and
            # registration so it can never land after the child exists but
            # before its isolated process group is recorded for cleanup.  The
            # signal itself remains unblocked in the child.
            with _defer_rehearsal_signal():
                process = subprocess.Popen(
                    command,
                    cwd=REPOSITORY_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=self._stderr_stream,
                    text=True,
                    start_new_session=True,
                )
                self.process = process
                self._process_group_id = process.pid
            return self._complete_start()
        except BaseException:
            self._force_stop()
            raise

    def _complete_start(self) -> _FixtureProcess:
        process = self._require_process()
        if process.stdout is None:
            raise RuntimeError("rehearsal fixture stdout is unavailable")
        readable, _, _ = select.select(
            [process.stdout],
            [],
            [],
            STARTUP_TIMEOUT_SECONDS,
        )
        if not readable:
            raise TimeoutError("rehearsal fixture startup deadline expired")
        line = process.stdout.readline()
        if not line:
            self._collect_after_exit()
            raise RuntimeError("rehearsal fixture exited before readiness")
        self.stdout_text = line
        try:
            announcement = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("rehearsal fixture readiness was not JSON") from error
        if (
            not isinstance(announcement, dict)
            or announcement.get("kind")
            != "public_attendee_private_deployment_checkpoint"
            or announcement.get("contract", {}).get("revision") != "0.8.0-rc.4"
            or announcement.get("host") != "127.0.0.1"
            or announcement.get("resources", {}).get("physical_workers")
            != WORKER_CAPACITY
            or announcement.get("resources", {}).get("context_capacity")
            != CONTEXT_CAPACITY
            or announcement.get("resources", {}).get("active_sessions")
            != SESSION_CAPACITY
            or announcement.get("security", {}).get("connection_limit")
            != CONNECTION_CAPACITY
        ):
            raise RuntimeError("rehearsal fixture readiness diverged")
        self.announcement = announcement
        self._assert_captured_safe()
        return self

    def stop(self, signum: int) -> int:
        process = self._require_process()
        if process.poll() is None:
            process.send_signal(signum)
        return self._finish(expected={128 + signum})

    def finish_scheduled(self) -> int:
        return self._finish(expected={0})

    def _finish(self, *, expected: set[int]) -> int:
        process = self._require_process()
        shutdown_ms = self.announcement.get("supervisor", {}).get(
            "shutdown_upper_bound_ms",
            0,
        )
        timeout = max(5.0, shutdown_ms / 1_000 + PROCESS_MARGIN_SECONDS)
        try:
            remaining, _unused = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self._force_stop()
            raise TimeoutError("rehearsal fixture shutdown deadline expired") from error
        self.stdout_text += remaining
        self._collect_after_exit()
        if process.returncode not in expected:
            raise RuntimeError("rehearsal fixture returned an unexpected status")
        lines = [line for line in self.stdout_text.splitlines()[1:] if line.strip()]
        if len(lines) != 1:
            raise RuntimeError("rehearsal fixture emitted unexpected stdout")
        try:
            diagnostic = json.loads(lines[0])
        except json.JSONDecodeError as error:
            raise RuntimeError("rehearsal fixture diagnostic was not JSON") from error
        if not isinstance(diagnostic, dict) or diagnostic.get("kind") != FIXTURE_KIND:
            raise RuntimeError("rehearsal fixture diagnostic diverged")
        self.diagnostic = diagnostic
        self._assert_captured_safe()
        self._finished_cleanly = True
        return process.returncode

    def _collect_after_exit(self) -> None:
        stream = self._stderr_stream
        if stream is not None and not stream.closed:
            stream.flush()
            stream.close()
        self._stderr_stream = None
        try:
            data = self._stderr_path.read_bytes()
        except FileNotFoundError:
            data = b""
        if len(data) > MAX_CAPTURE_BYTES:
            raise RuntimeError("rehearsal fixture stderr exceeded its bound")
        self.stderr_text = data.decode("utf-8", errors="strict")

    def _assert_captured_safe(self) -> None:
        captured = self.stdout_text + self.stderr_text
        for sentinel in self.sentinels:
            if sentinel and sentinel in captured:
                raise AssertionError("private rehearsal value crossed fixture output")
        if "Traceback" in captured:
            raise AssertionError("rehearsal fixture emitted a traceback")

    def _force_stop(self) -> None:
        if self._finished_cleanly or self._forced_cleanup_complete:
            self._collect_after_exit()
            return
        process = self.process
        process_group_id = self._process_group_id
        if process_group_id is not None:
            try:
                # A crashed fixture leader can leave spawned workers in its
                # isolated process group.  Kill the recorded group even after
                # the leader has exited so those descendants cannot survive a
                # failed or interrupted public rehearsal.
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            try:
                remaining, _unused = process.communicate(timeout=5)
                self.stdout_text += remaining
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()
                remaining, _unused = process.communicate(timeout=2)
                self.stdout_text += remaining
        self._collect_after_exit()
        self._forced_cleanup_complete = True

    def _require_process(self) -> subprocess.Popen[str]:
        if self.process is None:
            raise RuntimeError("rehearsal fixture was not started")
        return self.process


def _serve_fixture(arguments: argparse.Namespace) -> int:
    deployment = _load_tool("product_attendee_security_deployment")
    bearer = deployment._LIFECYCLE.load_preview_bearer_file(arguments.token_file)
    bff_authority = deployment._SECURITY.load_bootstrap(arguments.bootstrap_file)
    digest = deployment._LIVE.authored_digest(arguments.profile_path)
    settings = deployment._LIVE.AttendeeLiveProviderSettings(
        model="openai:gpt-provider-free-rehearsal",
        system_prompt_name="system.md",
        max_output_tokens=128,
    )
    if arguments.window_after_readiness:
        contract_root = deployment.validate_contract_root(arguments.contract_root)
        deployment._configure_deployment_transport()
        runtime = deployment._LIVE.start_live_runtime(
            artifact_root=arguments.artifact_root,
            authored_profile_path=arguments.profile_path,
            authored_profile_digest=digest,
            provider_credential_file=arguments.provider_file,
            provider_settings=settings,
            worker_capacity=WORKER_CAPACITY,
            max_commands_per_session=16,
            provider_worker_factory=DeterministicRehearsalWorkerFactory,
        )
        now_ms = deployment._utc_ms()
        window = deployment._SECURITY.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=now_ms - 2_000,
            admission_closes_at_ms=now_ms + arguments.close_after_ms,
            recovery_closes_at_ms=now_ms + arguments.recovery_after_ms,
        )
        runtime = replace(
            runtime,
            composition=replace(
                runtime.composition,
                availability_window=window,
                bootstrap_closes_at_admission=True,
            ),
        )
        try:
            server = deployment.AttendeeSecurityDeploymentServer(
                ("127.0.0.1", 0),
                bearer=bearer,
                bff_authority=bff_authority,
                scenario=deployment.SCENARIOS[0],
                contract_root=contract_root,
                runtime=runtime,
                harness_factory=deployment.AttendeeSecurityDeploymentHarness,
            )
        except Exception:
            runtime.provider_pool.shutdown(
                timeout_ms=runtime.pool_limits.shutdown_timeout_ms
            )
            raise
    else:
        now_ms = deployment._utc_ms()
        window = deployment._SECURITY.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=now_ms - 2_000,
            admission_closes_at_ms=now_ms + arguments.close_after_ms,
            recovery_closes_at_ms=now_ms + arguments.recovery_after_ms,
        )
        server = deployment.build_deployment_server(
            host="127.0.0.1",
            port=0,
            bearer=bearer,
            bff_authority=bff_authority,
            scenario=deployment.SCENARIOS[0],
            contract_root=arguments.contract_root,
            artifact_root=arguments.artifact_root,
            authored_profile_path=arguments.profile_path,
            authored_profile_digest=digest,
            provider_credential_file=arguments.provider_file,
            provider_settings=settings,
            availability_window=window,
            worker_capacity=WORKER_CAPACITY,
            max_commands_per_session=16,
            provider_worker_factory=DeterministicRehearsalWorkerFactory,
        )
    pool = server.preview.runtime.provider_pool
    try:
        status = deployment.serve_built_server(server)
    finally:
        snapshot = pool.snapshot()
        diagnostic = {
            "kind": FIXTURE_KIND,
            "lifecycle": snapshot.lifecycle.value,
            "worker_capacity": snapshot.worker_capacity,
            "ready_workers": snapshot.ready_workers,
            "active_workers": snapshot.active_workers,
            "available_workers": snapshot.available_workers,
            "accepted_dispatches": snapshot.accepted_dispatches,
            "completed_dispatches": snapshot.completed_dispatches,
            "ambiguous_dispatches": snapshot.ambiguous_dispatches,
            "locally_forced_workers": snapshot.locally_forced_workers,
            "maximum_observed_connections": server.maximum_observed_connections,
        }
        print(json.dumps(diagnostic, sort_keys=True), flush=True)
    return status


def _remember_private(sentinels: list[str], *values: object) -> None:
    for value in values:
        if isinstance(value, str) and value and value not in sentinels:
            sentinels.append(value)


def _new_workflow(
    client_module: ModuleType,
    client: Any,
    *,
    epoch: str,
    label: str,
    sentinels: list[str],
) -> _PrivateWorkflowState:
    gateway_context_id = _opaque("gateway-context:")
    bootstrap_key = _key(f"bootstrap-{label}")
    _remember_private(sentinels, gateway_context_id, bootstrap_key)
    bootstrap = client.bootstrap_attendee_context(
        epoch=epoch,
        key=bootstrap_key,
        gateway_context_id=gateway_context_id,
    )
    actor = bootstrap["actor_context"]
    options = client.attendee_launch_options(epoch=epoch, actor=actor)
    if len(options["options"]) != 1:
        raise AssertionError("rehearsal attendee option count diverged")
    option = options["options"][0]
    selection_id = option["launch_selection_id"]
    notice_version = option["notice_version"]
    exchange_key = _key(f"exchange-{label}")
    selection = client.exchange_attendee_launch_selection(
        epoch=epoch,
        actor=actor,
        key=exchange_key,
        launch_selection_id=selection_id,
        notice_version_presented=notice_version,
    )
    launch_grant = selection["launch_grant"]
    create_key = _key(f"create-{label}")
    created = client.create(
        epoch=epoch,
        actor=actor,
        key=create_key,
        launch_grant=launch_grant,
    )
    client_module.validate_snapshot(created["snapshot"])
    snapshot = created["snapshot"]
    state = _PrivateWorkflowState(
        gateway_context_id=gateway_context_id,
        bootstrap_key=bootstrap_key,
        actor=actor,
        selection_id=selection_id,
        notice_version=notice_version,
        exchange_key=exchange_key,
        launch_grant=launch_grant,
        create_key=create_key,
        session_authority=created["session_authority"],
        reference=snapshot["reference"],
        control_revision=snapshot["coordinates"]["control_revision"],
        slot_generation=snapshot["coordinates"]["command_slot_generation"],
    )
    _remember_private(
        sentinels,
        actor,
        selection_id,
        exchange_key,
        launch_grant,
        create_key,
        state.session_authority,
        state.reference,
    )
    return state


def _submit_blocking(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
    label: str,
    sentinels: list[str],
) -> None:
    key = _key(f"command-{label}")
    accepted = client.submit(
        epoch=epoch,
        actor=state.actor,
        capability=state.session_authority,
        reference=state.reference,
        key=key,
        command=BLOCK_COMMAND,
        expected_slot_generation=state.slot_generation,
    )
    state.command_id = accepted["command_id"]
    state.command_key = key
    state.slot_generation = accepted["slot_generation"]
    _remember_private(sentinels, key, state.command_id, BLOCK_COMMAND)


def _wait_for_command(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if state.command_id is None:
        raise ValueError("workflow has no command")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.command_result(
            epoch=epoch,
            actor=state.actor,
            capability=state.session_authority,
            reference=state.reference,
            command_id=state.command_id,
        )
        result = payload.get("command", {}).get("result")
        if result is not None:
            return payload
        time.sleep(REQUEST_POLL_SECONDS)
    raise TimeoutError("rehearsal command did not reach terminal truth")


def _wait_for_output(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
) -> None:
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        events = client.events(
            epoch=epoch,
            actor=state.actor,
            capability=state.session_authority,
            reference=state.reference,
        )
        if any(event.get("type") == "command.output" for event in events["events"]):
            return
        time.sleep(REQUEST_POLL_SECONDS)
    raise TimeoutError("rehearsal command did not publish initial output")


def _wait_for_terminal_current(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.current(epoch=epoch, actor=state.actor)
        kind = payload.get("kind")
        if kind == "current_session_terminal":
            latest = payload.get("snapshot", {}).get("latest_command")
            usage = latest.get("result", {}).get("usage") if latest else None
            if isinstance(usage, Mapping) and usage.get("state") == "settled":
                return payload
        if kind != "current_session":
            if kind != "current_session_terminal":
                raise AssertionError("cutoff current-session recovery truth diverged")
        time.sleep(REQUEST_POLL_SECONDS)
    raise TimeoutError("cutoff did not publish settled terminal recovery truth")


def _cancel_and_validate(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
    label: str,
    sentinels: list[str],
) -> int:
    if state.command_id is None:
        raise ValueError("workflow has no command")
    key = _key(f"cancel-{label}")
    _remember_private(sentinels, key)
    client.cancel(
        epoch=epoch,
        actor=state.actor,
        capability=state.session_authority,
        reference=state.reference,
        command_id=state.command_id,
        key=key,
        slot_generation=state.slot_generation,
    )
    terminal = _wait_for_command(client, state, epoch=epoch)
    result = terminal["command"]["result"]
    if result["outcome"] != "interrupted" or result["reason"] != "cancelled":
        raise AssertionError("cancelled rehearsal command terminal truth diverged")
    events = client.events(
        epoch=epoch,
        actor=state.actor,
        capability=state.session_authority,
        reference=state.reference,
    )
    output_count = _validate_fenced_output(events)
    snapshot = client.snapshot(
        epoch=epoch,
        actor=state.actor,
        capability=state.session_authority,
        reference=state.reference,
    )["snapshot"]
    if snapshot["coordinates"]["accepted_state_revision"] != 0:
        raise AssertionError("cancelled command changed accepted state")
    state.control_revision = snapshot["coordinates"]["control_revision"]
    state.slot_generation = snapshot["coordinates"]["command_slot_generation"]
    return output_count


def _validate_fenced_output(events: Mapping[str, object]) -> int:
    page = events.get("events")
    if not isinstance(page, list):
        raise AssertionError("rehearsal output event page diverged")
    output_events = [event for event in page if event.get("type") == "command.output"]
    published = [
        event.get("data", {}).get("text")
        for event in output_events
        if event.get("data", {}).get("text")
    ]
    final_markers = sum(
        event.get("data", {}).get("final") is True for event in output_events
    )
    if published != [BEFORE_OUTPUT] or final_markers != 2:
        raise AssertionError("late provider-free output crossed cancellation fence")
    return len(published)


def _validate_terminal_snapshot_fence(
    client_module: Any,
    current: Mapping[str, object],
    state: _PrivateWorkflowState,
    *,
    expected_tokens: int,
    expected_spend_units: int,
) -> int:
    snapshot = client_module.validate_snapshot(current.get("snapshot"))
    coordinates = snapshot["coordinates"]
    lifecycle = snapshot.get("lifecycle")
    recovery = snapshot.get("recovery")
    if (
        snapshot.get("reference") != state.reference
        or not isinstance(lifecycle, Mapping)
        or lifecycle.get("state") != "expired"
        or lifecycle.get("terminal_reason") != "absolute_expired"
        or not isinstance(recovery, Mapping)
        or recovery.get("tombstone") is not True
        or snapshot.get("available_controls") != []
        or coordinates["control_revision"] != state.control_revision + 1
        or coordinates["command_slot_generation"] != state.slot_generation
        or coordinates["accepted_state_revision"] != 0
    ):
        raise AssertionError("cutoff terminal session truth diverged")
    if snapshot["command_slot"] != {
        "state": "terminal",
        "generation": state.slot_generation,
        "active_command_id": None,
    }:
        raise AssertionError("cutoff command slot did not become terminal")
    latest = snapshot.get("latest_command")
    if not isinstance(latest, Mapping):
        raise AssertionError("cutoff terminal command truth is unavailable")
    result = latest.get("result")
    if (
        latest.get("command_id") != state.command_id
        or latest.get("slot_generation") != state.slot_generation
        or latest.get("status") != "failed"
        or not isinstance(result, Mapping)
        or result.get("outcome") != "failed"
        or result.get("reason") != "session_terminated"
        or result.get("output_clipped") is not False
        or latest.get("output")
        != {
            "bytes": len(BEFORE_OUTPUT.encode("utf-8")),
            "chunks": 1,
            "clipped": False,
        }
    ):
        raise AssertionError("cutoff command terminal truth diverged")
    usage = result.get("usage")
    if (
        not isinstance(usage, Mapping)
        or usage.get("state") != "settled"
        or usage.get("tokens") != expected_tokens
        or usage.get("spend_units") != expected_spend_units
    ):
        raise AssertionError("cutoff provider deadline accounting diverged")
    transcript = snapshot.get("transcript")
    if not isinstance(transcript, Mapping):
        raise AssertionError("cutoff terminal transcript is unavailable")
    entries = transcript.get("entries")
    if not isinstance(entries, list):
        raise AssertionError("cutoff terminal transcript entries diverged")
    projected_entries = [
        (
            entry.get("kind"),
            entry.get("text"),
            entry.get("chunk_index"),
            entry.get("final"),
            entry.get("command_id"),
        )
        for entry in entries
        if isinstance(entry, Mapping)
    ]
    if projected_entries != [
        ("command", BLOCK_COMMAND, None, None, state.command_id),
        ("stdout", BEFORE_OUTPUT, 0, False, state.command_id),
        ("stdout", "", 1, True, state.command_id),
        ("stderr", "", 0, True, state.command_id),
    ]:
        raise AssertionError("cutoff terminal transcript ordering diverged")
    output_entries = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("kind") in {"stdout", "stderr"}
    ]
    published = [entry.get("text") for entry in output_entries if entry.get("text")]
    final_markers = sum(entry.get("final") is True for entry in output_entries)
    if published != [BEFORE_OUTPUT] or final_markers != 2:
        raise AssertionError("late provider-free output crossed cutoff fence")
    return len(published)


def _close_and_revoke(
    client: Any,
    state: _PrivateWorkflowState,
    *,
    epoch: str,
    label: str,
    sentinels: list[str],
) -> None:
    close_key = _key(f"close-{label}")
    revoke_key = _key(f"revoke-{label}")
    _remember_private(sentinels, close_key, revoke_key)
    client.close(
        epoch=epoch,
        actor=state.actor,
        capability=state.session_authority,
        reference=state.reference,
        key=close_key,
        expected_control_revision=state.control_revision,
    )
    revoked = client.revoke_attendee_context(
        epoch=epoch,
        actor=state.actor,
        key=revoke_key,
        gateway_context_id=state.gateway_context_id,
    )
    if revoked["outcome"] not in {"revoked", "already_revoked"}:
        raise AssertionError("rehearsal context cleanup diverged")


def _raw_unread_body_pipeline_probe(
    *,
    port: int,
    bearer: str,
    epoch: str,
    sentinels: list[str],
    safe_error_details: Mapping[str, tuple[int, str, str, int | None]],
) -> _Problem:
    key = _key("raw-framing")
    _remember_private(sentinels, key)
    pipelined = (
        "GET /product/v1/capabilities HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {bearer}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    request = (
        "POST /product/v1/attendee/contexts HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {bearer}\r\n"
        f"X-Dreamland-Expected-Epoch: {epoch}\r\n"
        f"Idempotency-Key: {key}\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(pipelined)}\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.settimeout(4)
        connection.sendall(request + pipelined)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            chunk = connection.recv(4_096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_CAPTURE_BYTES:
                raise AssertionError("raw rehearsal response exceeded its bound")
    header_bytes, separator, body = bytes(response).partition(b"\r\n\r\n")
    if not separator or response.count(b"HTTP/1.1 ") != 1:
        raise AssertionError("rejected body was parsed as a pipelined request")
    lines = header_bytes.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {
        name.strip().lower(): value.strip()
        for line in lines[1:]
        for name, value in (line.split(":", 1),)
    }
    if (
        status != 415
        or headers.get("connection", "").lower() != "close"
        or headers.get("cache-control") != "no-store"
        or headers.get("x-content-type-options") != "nosniff"
        or headers.get("x-dreamland-contract-revision") != "0.8.0-rc.4"
        or headers.get("content-type") != "application/problem+json"
    ):
        raise AssertionError("raw rehearsal error headers diverged")
    length = int(headers["content-length"])
    if len(body) != length:
        raise AssertionError("raw rehearsal response length diverged")
    payload = json.loads(body)
    if set(payload) != {"contract", "preview", "error"}:
        raise AssertionError("raw rehearsal problem envelope diverged")
    detail = payload["error"]
    expected = safe_error_details["unsupported_media_type"]
    if (
        status != expected[0]
        or detail.get("code") != "unsupported_media_type"
        or detail.get("message") != expected[1]
        or detail.get("retry") != expected[2]
        or detail.get("retry_after_ms") != expected[3]
    ):
        raise AssertionError("raw rehearsal problem tuple diverged")
    return _Problem(
        status=status,
        code=detail["code"],
        retry=detail["retry"],
        retry_after_ms=detail["retry_after_ms"],
    )


def _connection_saturation_probe(client: Any, *, port: int) -> int:
    stalled: list[socket.socket] = []
    try:
        for _index in range(CONNECTION_CAPACITY):
            connection = socket.create_connection(("127.0.0.1", port), timeout=3)
            connection.sendall(
                b"GET /product/v1/capabilities HTTP/1.1\r\nHost: stalled"
            )
            stalled.append(connection)
        result: list[object] = []

        def request_after_release() -> None:
            try:
                result.append(client.capabilities())
            except BaseException as error:
                result.append(error)

        thread = threading.Thread(target=request_after_release)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=5)
        elapsed_ms = int((time.monotonic() - started) * 1_000)
        if (
            thread.is_alive()
            or len(result) != 1
            or isinstance(result[0], BaseException)
        ):
            raise AssertionError("connection saturation did not recover within bounds")
        return elapsed_ms
    finally:
        for connection in stalled:
            connection.close()


def _stop_under_connection_saturation(
    fixture: _FixtureProcess,
    *,
    signum: int,
    connection_deadline_seconds: float,
) -> tuple[int, int]:
    if connection_deadline_seconds <= 0:
        raise ValueError("connection deadline must be positive")
    stalled: list[socket.socket] = []
    feed_stop = threading.Event()
    feeder: threading.Thread | None = None
    try:
        partial_header = (
            b"GET /product/v1/capabilities HTTP/1.1\r\nHost: stalled-rehearsal"
        )
        for _index in range(CONNECTION_CAPACITY):
            connection = socket.create_connection(
                ("127.0.0.1", fixture.announcement["port"]),
                timeout=3,
            )
            connection.sendall(partial_header)
            stalled.append(connection)

        # Keep the incomplete header reads active so the absolute connection
        # timers, rather than the shorter idle read timeout, own this probe.
        # This makes the two advertised deadline intervals observable: one for
        # an occupied slot, then one for the ninth accepted connection.
        def feed_partial_headers() -> None:
            while not feed_stop.wait(0.25):
                for connection in tuple(stalled):
                    try:
                        connection.sendall(b"x")
                    except OSError:
                        continue

        feeder = threading.Thread(
            target=feed_partial_headers,
            name="dreamland-rehearsal-slow-header-feeder",
        )
        feeder.start()
        # Give the bounded server time to occupy every admitted handler slot,
        # then enqueue one additional accepted connection so the accept loop
        # blocks on admission.  Signal teardown must wait for one deadline to
        # release a slot and a second deadline to join that ninth handler.
        time.sleep(0.25)
        waiting = socket.create_connection(
            ("127.0.0.1", fixture.announcement["port"]),
            timeout=3,
        )
        waiting.sendall(partial_header)
        stalled.append(waiting)
        time.sleep(0.25)

        started = time.monotonic()
        status = fixture.stop(signum)
        elapsed_ms = int((time.monotonic() - started) * 1_000)
        lower_bound_ms = max(
            1,
            int((2 * connection_deadline_seconds - 2.0) * 1_000),
        )
        upper_bound_ms = int(
            fixture.announcement["supervisor"]["shutdown_upper_bound_ms"]
            + PROCESS_MARGIN_SECONDS * 1_000
        )
        if not lower_bound_ms <= elapsed_ms <= upper_bound_ms:
            raise AssertionError("saturated signal shutdown missed its bound")
        return status, elapsed_ms
    finally:
        feed_stop.set()
        if feeder is not None:
            feeder.join(timeout=2)
            if feeder.is_alive():
                raise RuntimeError("slow-header feeder did not stop")
        for connection in stalled:
            connection.close()


def _assert_fixture_converged(
    fixture: _FixtureProcess,
    *,
    dispatches: int,
    maximum_connections: int | None = None,
) -> None:
    diagnostic = fixture.diagnostic
    if (
        diagnostic.get("lifecycle") != "closed"
        or diagnostic.get("worker_capacity") != WORKER_CAPACITY
        or diagnostic.get("ready_workers") != 0
        or diagnostic.get("active_workers") != 0
        or diagnostic.get("available_workers") != 0
        or diagnostic.get("accepted_dispatches") != dispatches
        or diagnostic.get("completed_dispatches") != dispatches
        or diagnostic.get("ambiguous_dispatches") != 0
        or diagnostic.get("locally_forced_workers") != 0
    ):
        raise AssertionError(
            f"rehearsal fixture resources did not converge: {diagnostic!r}"
        )
    if (
        maximum_connections is not None
        and diagnostic.get("maximum_observed_connections") != maximum_connections
    ):
        raise AssertionError("rehearsal fixture connection bound diverged")


def _fixture_paths(root: Path, suffix: str) -> tuple[Path, Path]:
    artifact_root = root / f"private-artifacts-{suffix}"
    artifact_root.mkdir(mode=0o700)
    return artifact_root, root / f"provider-{suffix}.key"


def _run_rehearsal(contract_root: Path) -> dict[str, object]:
    started = time.monotonic()
    deployment = _load_tool("product_attendee_security_deployment")
    root = deployment.validate_contract_root(contract_root)
    deployment._configure_deployment_transport()
    client_module = _load_rc4_client(root)
    safe_error_details = deployment._CORE.SAFE_ERROR_DETAILS
    checks: list[_Check] = []
    sentinels: list[str] = [
        BLOCK_COMMAND,
        COMPLETE_COMMAND,
        BEFORE_OUTPUT,
        AFTER_OUTPUT,
    ]

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("contract_revision") != "0.8.0-rc.4"
        or len(client_module.EXPECTED_OPERATIONS) != 25
        or any(
            value.get("enabled") is not False
            for value in manifest.get("capabilities", {}).values()
        )
    ):
        raise AssertionError("rehearsal contract identity diverged")
    checks.append(
        _Check(
            "contract_identity",
            metrics={"operations": 25, "capabilities_enabled": 0},
        )
    )

    with (
        tempfile.TemporaryDirectory(prefix="dreamland-attendee-rehearsal-") as tmp,
        ExitStack() as fixture_cleanup,
    ):
        private_root = Path(tmp)
        token_file = private_root / "product.token"
        bootstrap_file = private_root / "bootstrap.json"
        profile_path = private_root / "profile.json"
        bearer_one = _opaque("attendee-security-bearer:")
        bff_one = _opaque("bff-authority:")
        _write_owner_file(token_file, bearer_one + "\n")
        _write_owner_file(
            bootstrap_file,
            json.dumps({"bff_authority": bff_one}, sort_keys=True) + "\n",
        )
        _write_profile(profile_path)
        artifact_one, provider_file = _fixture_paths(private_root, "one")
        # A one-byte, owner-only file passes the parent's metadata check but
        # is not a valid provider credential after stripping.  The rehearsal
        # can succeed only while the deterministic collaborator prevents any
        # worker from reading or using provider credential content.
        _write_owner_file(provider_file, "\n")
        _remember_private(
            sentinels,
            str(private_root),
            str(token_file),
            str(bootstrap_file),
            str(profile_path),
            str(artifact_one),
            str(provider_file),
            bearer_one,
            bff_one,
            deployment._LIVE.authored_digest(profile_path),
            "attendee-deployment-rehearsal-fixture",
            "attendee-deployment-rehearsal-world",
        )

        first = _FixtureProcess(
            contract_root=root,
            root=private_root,
            token_file=token_file,
            bootstrap_file=bootstrap_file,
            profile_path=profile_path,
            provider_file=provider_file,
            artifact_root=artifact_one,
            close_after_ms=60_000,
            recovery_after_ms=70_000,
            sentinels=sentinels,
        )
        fixture_cleanup.callback(first._force_stop)
        first.start()
        client_one = client_module.ProductSessionPreviewClient(
            first.base_url, bearer_one
        )
        capabilities = client_one.capabilities()
        epoch_one = capabilities["service_epoch"]
        _remember_private(sentinels, epoch_one)

        replay_gateway = _opaque("gateway-context:")
        replay_key = _key("bootstrap-replay")
        _remember_private(sentinels, replay_gateway, replay_key)
        first_bootstrap = client_one.bootstrap_attendee_context(
            epoch=epoch_one,
            key=replay_key,
            gateway_context_id=replay_gateway,
        )
        replayed_bootstrap = client_one.bootstrap_attendee_context(
            epoch=epoch_one,
            key=replay_key,
            gateway_context_id=replay_gateway,
        )
        if replayed_bootstrap.get("replayed") is not True:
            raise AssertionError("bootstrap retry did not replay")
        actor_zero = first_bootstrap["actor_context"]
        _remember_private(sentinels, actor_zero)
        conflict_gateway = _opaque("gateway-context:")
        _remember_private(sentinels, conflict_gateway)
        conflict = _strict_problem(
            client_module,
            lambda: client_one.bootstrap_attendee_context(
                epoch=epoch_one,
                key=replay_key,
                gateway_context_id=conflict_gateway,
            ),
            status=409,
            code="idempotency_conflict",
            safe_error_details=safe_error_details,
        )
        checks.append(_Check("context_replay_conflict", codes=(conflict.code,)))

        # Finish the already bootstrapped first workflow without consuming a
        # second context slot, then construct the other three workflows.
        options = client_one.attendee_launch_options(epoch=epoch_one, actor=actor_zero)
        option = options["options"][0]
        exchange_zero = _key("exchange-zero")
        selection_zero = client_one.exchange_attendee_launch_selection(
            epoch=epoch_one,
            actor=actor_zero,
            key=exchange_zero,
            launch_selection_id=option["launch_selection_id"],
            notice_version_presented=option["notice_version"],
        )
        create_zero = _key("create-zero")
        created_zero = client_one.create(
            epoch=epoch_one,
            actor=actor_zero,
            key=create_zero,
            launch_grant=selection_zero["launch_grant"],
        )
        client_module.validate_snapshot(created_zero["snapshot"])
        snapshot_zero = created_zero["snapshot"]
        workflow_zero = _PrivateWorkflowState(
            gateway_context_id=replay_gateway,
            bootstrap_key=replay_key,
            actor=actor_zero,
            selection_id=option["launch_selection_id"],
            notice_version=option["notice_version"],
            exchange_key=exchange_zero,
            launch_grant=selection_zero["launch_grant"],
            create_key=create_zero,
            session_authority=created_zero["session_authority"],
            reference=snapshot_zero["reference"],
            control_revision=snapshot_zero["coordinates"]["control_revision"],
            slot_generation=snapshot_zero["coordinates"]["command_slot_generation"],
        )
        _remember_private(
            sentinels,
            exchange_zero,
            create_zero,
            workflow_zero.selection_id,
            workflow_zero.launch_grant,
            workflow_zero.session_authority,
            workflow_zero.reference,
        )
        workflows = [workflow_zero]
        for index in range(1, CONTEXT_CAPACITY):
            workflows.append(
                _new_workflow(
                    client_module,
                    client_one,
                    epoch=epoch_one,
                    label=str(index),
                    sentinels=sentinels,
                )
            )
        fifth_gateway = _opaque("gateway-context:")
        fifth_key = _key("fifth-context")
        _remember_private(sentinels, fifth_gateway, fifth_key)
        logical_capacity = _strict_problem(
            client_module,
            lambda: client_one.bootstrap_attendee_context(
                epoch=epoch_one,
                key=fifth_key,
                gateway_context_id=fifth_gateway,
            ),
            status=429,
            code="logical_rate_limited",
            safe_error_details=safe_error_details,
        )
        time.sleep(1.1)
        context_capacity = _strict_problem(
            client_module,
            lambda: client_one.bootstrap_attendee_context(
                epoch=epoch_one,
                key=fifth_key,
                gateway_context_id=fifth_gateway,
            ),
            status=503,
            code="attendee_context_capacity_exhausted",
            safe_error_details=safe_error_details,
        )
        checks.append(
            _Check(
                "context_session_capacity",
                metrics={"contexts": 4, "sessions": 4, "active_sessions": 4},
                codes=(logical_capacity.code, context_capacity.code),
            )
        )

        saturation_ms = _connection_saturation_probe(
            client_one,
            port=first.announcement["port"],
        )
        checks.append(
            _Check(
                "connection_saturation",
                metrics={
                    "maximum_connections": CONNECTION_CAPACITY,
                    "elapsed_ms": saturation_ms,
                },
            )
        )

        _submit_blocking(
            client_one,
            workflows[0],
            epoch=epoch_one,
            label="zero",
            sentinels=sentinels,
        )
        _submit_blocking(
            client_one,
            workflows[1],
            epoch=epoch_one,
            label="one",
            sentinels=sentinels,
        )
        _wait_for_output(client_one, workflows[0], epoch=epoch_one)
        _wait_for_output(client_one, workflows[1], epoch=epoch_one)
        third_key = _key("third-physical-worker")
        _remember_private(sentinels, third_key)
        physical_capacity = _strict_problem(
            client_module,
            lambda: client_one.submit(
                epoch=epoch_one,
                actor=workflows[2].actor,
                capability=workflows[2].session_authority,
                reference=workflows[2].reference,
                key=third_key,
                command=BLOCK_COMMAND,
                expected_slot_generation=workflows[2].slot_generation,
            ),
            status=503,
            code="capacity_exhausted",
            safe_error_details=safe_error_details,
        )
        output_events = _cancel_and_validate(
            client_one,
            workflows[0],
            epoch=epoch_one,
            label="zero",
            sentinels=sentinels,
        )
        output_events += _cancel_and_validate(
            client_one,
            workflows[1],
            epoch=epoch_one,
            label="one",
            sentinels=sentinels,
        )
        checks.append(
            _Check(
                "worker_capacity_cancellation",
                metrics={
                    "workers": WORKER_CAPACITY,
                    "dispatches": 2,
                    "output_events": output_events,
                },
                codes=(physical_capacity.code,),
            )
        )

        raw_problem = _raw_unread_body_pipeline_probe(
            port=first.announcement["port"],
            bearer=bearer_one,
            epoch=epoch_one,
            sentinels=sentinels,
            safe_error_details=safe_error_details,
        )
        checks.append(_Check("unread_body_pipeline_close", codes=(raw_problem.code,)))

        for index, workflow in enumerate(workflows):
            _close_and_revoke(
                client_one,
                workflow,
                epoch=epoch_one,
                label=str(index),
                sentinels=sentinels,
            )
        checks.append(
            _Check(
                "recovery_cleanup",
                metrics={"contexts": CONTEXT_CAPACITY, "sessions": SESSION_CAPACITY},
            )
        )

        bearer_two = _opaque("attendee-security-bearer:")
        _remember_private(sentinels, bearer_two)
        _write_owner_file(token_file, bearer_two + "\n", replace=True)
        # Running state is immutable: changing the file does not hot-reload the
        # already constructed transport authority.
        client_one.capabilities()
        no_hot_reload = _strict_problem(
            client_module,
            lambda: client_module.ProductSessionPreviewClient(
                first.base_url,
                bearer_two,
            ).capabilities(),
            status=401,
            code="unauthorized",
            safe_error_details=safe_error_details,
        )
        term_status = first.stop(signal.SIGTERM)
        _assert_fixture_converged(
            first,
            dispatches=2,
            maximum_connections=CONNECTION_CAPACITY,
        )

        bff_two = _opaque("bff-authority:")
        _remember_private(sentinels, bff_two)
        _write_owner_file(
            bootstrap_file,
            json.dumps({"bff_authority": bff_two}, sort_keys=True) + "\n",
            replace=True,
        )
        _write_owner_file(provider_file, "\n", replace=True)
        artifact_two, _unused_provider_two = _fixture_paths(private_root, "two")
        second = _FixtureProcess(
            contract_root=root,
            root=private_root,
            token_file=token_file,
            bootstrap_file=bootstrap_file,
            profile_path=profile_path,
            provider_file=provider_file,
            artifact_root=artifact_two,
            close_after_ms=60_000,
            recovery_after_ms=70_000,
            sentinels=sentinels,
        )
        fixture_cleanup.callback(second._force_stop)
        second.start()
        old_bearer = _strict_problem(
            client_module,
            lambda: client_module.ProductSessionPreviewClient(
                second.base_url,
                bearer_one,
            ).capabilities(),
            status=401,
            code="unauthorized",
            safe_error_details=safe_error_details,
        )
        client_two = client_module.ProductSessionPreviewClient(
            second.base_url, bearer_two
        )
        epoch_two = client_two.capabilities()["service_epoch"]
        _remember_private(sentinels, epoch_two)
        if epoch_two == epoch_one:
            raise AssertionError("rehearsal restart did not rotate the epoch")
        stale_read = _strict_problem(
            client_module,
            lambda: client_two.current(epoch=epoch_one, actor=workflows[0].actor),
            status=410,
            code="session_lost",
            safe_error_details=safe_error_details,
        )
        stale_close_key = _key("stale-close")
        _remember_private(sentinels, stale_close_key)
        stale_mutation = _strict_problem(
            client_module,
            lambda: client_two.close(
                epoch=epoch_one,
                actor=workflows[0].actor,
                capability=workflows[0].session_authority,
                reference=workflows[0].reference,
                key=stale_close_key,
                expected_control_revision=workflows[0].control_revision,
            ),
            status=410,
            code="outcome_unknown",
            safe_error_details=safe_error_details,
        )
        stale_revoke_key = _key("stale-revoke")
        _remember_private(sentinels, stale_revoke_key)
        stale_revoke = _strict_problem(
            client_module,
            lambda: client_two.revoke_attendee_context(
                epoch=epoch_one,
                actor=workflows[0].actor,
                key=stale_revoke_key,
                gateway_context_id=workflows[0].gateway_context_id,
            ),
            status=410,
            code="outcome_unknown",
            safe_error_details=safe_error_details,
        )
        old_authority = _strict_problem(
            client_module,
            lambda: client_two.attendee_launch_options(
                epoch=epoch_two,
                actor=workflows[0].actor,
            ),
            status=403,
            code="authority_denied",
            safe_error_details=safe_error_details,
        )
        new_gateway = _opaque("gateway-context:")
        new_bootstrap_key = _key("explicit-new-context")
        _remember_private(sentinels, new_gateway, new_bootstrap_key)
        new_context = client_two.bootstrap_attendee_context(
            epoch=epoch_two,
            key=new_bootstrap_key,
            gateway_context_id=new_gateway,
        )
        new_actor = new_context["actor_context"]
        new_revoke_key = _key("explicit-new-revoke")
        _remember_private(sentinels, new_actor, new_revoke_key)
        client_two.revoke_attendee_context(
            epoch=epoch_two,
            actor=new_actor,
            key=new_revoke_key,
            gateway_context_id=new_gateway,
        )
        int_status, saturated_shutdown_ms = _stop_under_connection_saturation(
            second,
            signum=signal.SIGINT,
            connection_deadline_seconds=(
                deployment._LIFECYCLE.CONNECTION_DEADLINE_SECONDS
            ),
        )
        _assert_fixture_converged(
            second,
            dispatches=0,
            maximum_connections=CONNECTION_CAPACITY,
        )
        checks.append(
            _Check(
                "transport_authority_restart",
                metrics={"signal_exit_code": term_status},
                codes=(
                    no_hot_reload.code,
                    old_bearer.code,
                    stale_read.code,
                    stale_mutation.code,
                    stale_revoke.code,
                    old_authority.code,
                ),
            )
        )
        checks.append(
            _Check(
                "signal_convergence",
                metrics={
                    "signal_exit_code": int_status,
                    "dispatches": 0,
                    "saturated_shutdown_ms": saturated_shutdown_ms,
                },
            )
        )

        # A short third epoch proves no-request cutoff cancellation and the
        # retained terminal-recovery listener before scheduled exit.
        bearer_three = _opaque("attendee-security-bearer:")
        bff_three = _opaque("bff-authority:")
        _remember_private(sentinels, bearer_three, bff_three)
        _write_owner_file(token_file, bearer_three + "\n", replace=True)
        _write_owner_file(
            bootstrap_file,
            json.dumps({"bff_authority": bff_three}, sort_keys=True) + "\n",
            replace=True,
        )
        _write_owner_file(provider_file, "\n", replace=True)
        artifact_three, _unused_provider_three = _fixture_paths(private_root, "three")
        cutoff = _FixtureProcess(
            contract_root=root,
            root=private_root,
            token_file=token_file,
            bootstrap_file=bootstrap_file,
            profile_path=profile_path,
            provider_file=provider_file,
            artifact_root=artifact_three,
            close_after_ms=CUTOFF_ADMISSION_MS,
            recovery_after_ms=CUTOFF_RECOVERY_MS,
            sentinels=sentinels,
            window_after_readiness=True,
        )
        fixture_cleanup.callback(cutoff._force_stop)
        cutoff.start()
        client_three = client_module.ProductSessionPreviewClient(
            cutoff.base_url,
            bearer_three,
        )
        epoch_three = client_three.capabilities()["service_epoch"]
        _remember_private(sentinels, epoch_three)
        cutoff_workflow = _new_workflow(
            client_module,
            client_three,
            epoch=epoch_three,
            label="cutoff",
            sentinels=sentinels,
        )
        _submit_blocking(
            client_three,
            cutoff_workflow,
            epoch=epoch_three,
            label="cutoff",
            sentinels=sentinels,
        )
        _wait_for_output(client_three, cutoff_workflow, epoch=epoch_three)
        cutoff_at_ms = deployment._canonical_utc_milliseconds(
            cutoff.announcement["availability"]["admission_closes_at"]
        )
        remaining_to_cutoff_seconds = (cutoff_at_ms - deployment._utc_ms()) / 1_000
        if remaining_to_cutoff_seconds > -0.25:
            time.sleep(max(0.0, remaining_to_cutoff_seconds + 0.25))
        current = _wait_for_terminal_current(
            client_three,
            cutoff_workflow,
            epoch=epoch_three,
        )
        cutoff_output_count = _validate_terminal_snapshot_fence(
            client_module,
            current,
            cutoff_workflow,
            expected_tokens=cutoff.announcement["resources"][
                "token_reservation_per_command"
            ],
            expected_spend_units=cutoff.announcement["resources"][
                "spend_reservation_per_command"
            ],
        )
        replay_after_cutoff = client_three.bootstrap_attendee_context(
            epoch=epoch_three,
            key=cutoff_workflow.bootstrap_key,
            gateway_context_id=cutoff_workflow.gateway_context_id,
        )
        if replay_after_cutoff.get("replayed") is not True:
            raise AssertionError("cutoff lost exact bootstrap replay")
        cutoff_conflict_gateway = _opaque("gateway-context:")
        _remember_private(sentinels, cutoff_conflict_gateway)
        cutoff_conflict = _strict_problem(
            client_module,
            lambda: client_three.bootstrap_attendee_context(
                epoch=epoch_three,
                key=cutoff_workflow.bootstrap_key,
                gateway_context_id=cutoff_conflict_gateway,
            ),
            status=409,
            code="idempotency_conflict",
            safe_error_details=safe_error_details,
        )
        closed_gateway = _opaque("gateway-context:")
        closed_key = _key("post-cutoff-bootstrap")
        _remember_private(sentinels, closed_gateway, closed_key)
        service_closed = _strict_problem(
            client_module,
            lambda: client_three.bootstrap_attendee_context(
                epoch=epoch_three,
                key=closed_key,
                gateway_context_id=closed_gateway,
            ),
            status=410,
            code="service_closed",
            safe_error_details=safe_error_details,
        )
        cutoff_revoke_key = _key("cutoff-revoke")
        _remember_private(sentinels, cutoff_revoke_key)
        client_three.revoke_attendee_context(
            epoch=epoch_three,
            actor=cutoff_workflow.actor,
            key=cutoff_revoke_key,
            gateway_context_id=cutoff_workflow.gateway_context_id,
        )
        scheduled_status = cutoff.finish_scheduled()
        _assert_fixture_converged(cutoff, dispatches=1)
        checks.append(
            _Check(
                "cutoff_terminal_recovery",
                metrics={
                    "dispatches": 1,
                    "output_events": cutoff_output_count,
                    "signal_exit_code": scheduled_status,
                },
                codes=(cutoff_conflict.code, service_closed.code),
            )
        )

        # All subprocess output is retained only long enough to scan it. The
        # report below cannot include these captures or any private workflow.
        for fixture in (first, second, cutoff):
            fixture._assert_captured_safe()

    checks.append(_Check("privacy_redaction"))
    elapsed_ms = int((time.monotonic() - started) * 1_000)
    public_checks = [check.public() for check in checks]
    if {check["id"] for check in public_checks} != set(_FIXED_CASES):
        raise AssertionError("rehearsal case matrix is incomplete")
    report: dict[str, object] = {
        "kind": REPORT_KIND,
        "schema": SCHEMA,
        "scope": SCOPE,
        "production_capability_evidence": PRODUCTION_CAPABILITY_EVIDENCE,
        "contract": {
            "name": manifest["contract_name"],
            "revision": manifest["contract_revision"],
            "compatibility": manifest["compatibility"],
            "backend_commit": manifest["backend_commit"],
            "manifest_sha256": _sha256(root / "manifest.json"),
            "openapi_sha256": _sha256(root / "openapi.json"),
            "generated_client_sha256": _sha256(
                root / "generated" / "product_session_client.py"
            ),
        },
        "limits": {
            "contexts": CONTEXT_CAPACITY,
            "sessions": SESSION_CAPACITY,
            "workers": WORKER_CAPACITY,
            "connections": CONNECTION_CAPACITY,
        },
        "checks": public_checks,
        "summary": {
            "passed": True,
            "checks_passed": len(public_checks),
            "checks_total": len(public_checks),
            "elapsed_ms": elapsed_ms,
        },
    }
    _validate_report(report, sentinels=sentinels)
    return report


def _run_rehearsal_with_signal_cleanup(contract_root: Path) -> dict[str, object]:
    global _DEFERRED_REHEARSAL_SIGNAL, _SIGNAL_DEFERRAL_DEPTH
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("rehearsal signals require the main thread")
    if _SIGNAL_DEFERRAL_DEPTH or _DEFERRED_REHEARSAL_SIGNAL is not None:
        raise RuntimeError("rehearsal signal state is not clean")
    prior_handlers: dict[int, Any] = {}
    interrupted: list[int | None] = [None]

    def interrupt(signum: int, _frame: object) -> None:
        global _DEFERRED_REHEARSAL_SIGNAL
        if interrupted[0] is None:
            interrupted[0] = signum
            if _SIGNAL_DEFERRAL_DEPTH:
                _DEFERRED_REHEARSAL_SIGNAL = signum
                return
            raise _RehearsalSignal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        return _run_rehearsal(contract_root)
    finally:
        _SIGNAL_DEFERRAL_DEPTH = 0
        _DEFERRED_REHEARSAL_SIGNAL = None
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)


def _validate_report(report: Mapping[str, object], *, sentinels: Sequence[str]) -> None:
    forbidden_keys = {
        "actor",
        "authority",
        "bearer",
        "command",
        "credential",
        "gateway_context_id",
        "idempotency",
        "output",
        "path",
        "port",
        "profile",
        "request_id",
        "secret",
        "session_authority",
        "state",
        "token",
    }

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str) or key.lower() in forbidden_keys:
                    raise ValueError("private key crossed rehearsal evidence")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    # Inspect privacy-sensitive key names before schema checks so this guard is
    # independently effective even for an otherwise malformed report.
    visit(report)
    if set(report) != _REPORT_ROOT_KEYS:
        raise ValueError("rehearsal report root is not allowlisted")
    if (
        report.get("kind") != REPORT_KIND
        or report.get("schema") != SCHEMA
        or report.get("scope") != SCOPE
        or report.get("production_capability_evidence") is not False
    ):
        raise ValueError("rehearsal report identity diverged")
    contract = report.get("contract")
    if not isinstance(contract, Mapping) or set(contract) != {
        "name",
        "revision",
        "compatibility",
        "backend_commit",
        "manifest_sha256",
        "openapi_sha256",
        "generated_client_sha256",
    }:
        raise ValueError("rehearsal contract evidence is not allowlisted")
    if (
        contract.get("name") != "dreamland-private-product-api"
        or contract.get("revision") != "0.8.0-rc.4"
        or contract.get("compatibility") != "unfrozen-release-candidate"
    ):
        raise ValueError("rehearsal contract evidence diverged")
    backend_commit = contract.get("backend_commit")
    if (
        not isinstance(backend_commit, str)
        or len(backend_commit) != 40
        or any(character not in "0123456789abcdef" for character in backend_commit)
    ):
        raise ValueError("rehearsal backend commit is invalid")
    for name in (
        "manifest_sha256",
        "openapi_sha256",
        "generated_client_sha256",
    ):
        value = contract.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("rehearsal public hash is invalid")
    limits = report.get("limits")
    if limits != {
        "contexts": CONTEXT_CAPACITY,
        "sessions": SESSION_CAPACITY,
        "workers": WORKER_CAPACITY,
        "connections": CONNECTION_CAPACITY,
    }:
        raise ValueError("rehearsal limit evidence diverged")
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != len(_FIXED_CASES):
        raise ValueError("rehearsal check evidence is incomplete")
    identifiers: set[str] = set()
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != _CHECK_KEYS:
            raise ValueError("rehearsal check evidence is not allowlisted")
        identifier = check.get("id")
        if (
            not isinstance(identifier, str)
            or identifier not in _FIXED_CASES
            or identifier in identifiers
            or check.get("passed") is not True
        ):
            raise ValueError("rehearsal check result diverged")
        identifiers.add(identifier)
        metrics = check.get("metrics")
        codes = check.get("codes")
        if (
            not isinstance(metrics, Mapping)
            or not set(metrics).issubset(_SAFE_METRIC_KEYS)
            or not isinstance(codes, list)
            or any(code not in _SAFE_CODES for code in codes)
        ):
            raise ValueError("rehearsal check observation is not allowlisted")
        for value in metrics.values():
            if isinstance(value, bool):
                continue
            if not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
                raise ValueError("rehearsal check metric is invalid")
        _validate_check_evidence(identifier, metrics, codes)
    if identifiers != set(_FIXED_CASES):
        raise ValueError("rehearsal case matrix diverged")
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "passed",
        "checks_passed",
        "checks_total",
        "elapsed_ms",
    }:
        raise ValueError("rehearsal summary is not allowlisted")
    if (
        summary.get("passed") is not True
        or summary.get("checks_passed") != len(_FIXED_CASES)
        or summary.get("checks_total") != len(_FIXED_CASES)
        or isinstance(summary.get("elapsed_ms"), bool)
        or not isinstance(summary.get("elapsed_ms"), int)
        or not 0 <= summary["elapsed_ms"] <= 2**53 - 1
    ):
        raise ValueError("rehearsal summary diverged")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    for sentinel in sentinels:
        if sentinel and sentinel in encoded:
            raise AssertionError("private rehearsal value crossed report output")


def _positive_milliseconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "value must be positive milliseconds"
        ) from error
    if not 1_001 <= parsed <= 600_000:
        raise argparse.ArgumentTypeError(
            "value must be between 1001 and 600000 milliseconds"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Run the provider-free private attendee deployment rehearsal."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the complete provider-free rehearsal")
    run.add_argument("--contract-root", type=Path, default=DEFAULT_CONTRACT_ROOT)
    fixture = commands.add_parser("_serve-fixture", help=argparse.SUPPRESS)
    fixture.add_argument("--contract-root", type=Path, required=True)
    fixture.add_argument("--token-file", type=Path, required=True)
    fixture.add_argument("--bootstrap-file", type=Path, required=True)
    fixture.add_argument("--profile-path", type=Path, required=True)
    fixture.add_argument("--provider-file", type=Path, required=True)
    fixture.add_argument("--artifact-root", type=Path, required=True)
    fixture.add_argument(
        "--close-after-ms",
        type=_positive_milliseconds,
        required=True,
    )
    fixture.add_argument(
        "--recovery-after-ms",
        type=_positive_milliseconds,
        required=True,
    )
    fixture.add_argument("--window-after-readiness", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments: argparse.Namespace | None = None
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "_serve-fixture":
            if arguments.recovery_after_ms <= arguments.close_after_ms:
                raise ValueError("fixture recovery must follow admission close")
            return _serve_fixture(arguments)
        report = _run_rehearsal_with_signal_cleanup(arguments.contract_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except _RehearsalSignal as interruption:
        return 128 + interruption.signum
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    except Exception:
        if arguments is not None and arguments.command == "_serve-fixture":
            raise
        print(
            json.dumps(
                {
                    "error": {
                        "code": "rehearsal_failed",
                        "retry": "after_review",
                    }
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
