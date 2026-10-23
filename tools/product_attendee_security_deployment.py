#!/usr/bin/env python3
"""Serve the explicit Phase 9C private-attendee deployment checkpoint.

This executable supervises the Phase 9B loopback-only provider composition.  It
adds an exact, finite availability window and process-signal handling without
adding a public listener, browser authority, hot reload, or an ordinary
DREAMLAND startup import.  Importing this module creates no listener, provider
worker, timer, credential, artifact, or runtime composition.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

logger = logging.getLogger(__name__)


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_security_deployment",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_LIVE: Any = _load_tool("product_attendee_security_live")
_SECURITY: Any = _LIVE._SECURITY
_CORE: Any = _LIVE._CORE
_LIFECYCLE: Any = _LIVE._LIFECYCLE

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.8.0-rc.4"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
DEPLOYMENT_KIND = "public_attendee_private_deployment_checkpoint"
DEPLOYMENT_EXECUTOR = _LIVE.LIVE_EXECUTOR
SCENARIOS = ("attendee-security-deployment",)

PRIVATE_ROUTE_ALLOWLIST = _LIVE.PRIVATE_ROUTE_ALLOWLIST
HANDLE_REQUEST_POLL_SECONDS = 0.05
WATCH_POLL_SECONDS = 0.05
SIGNAL_EXIT_CODES = {
    signal.SIGINT: 128 + int(signal.SIGINT),
    signal.SIGTERM: 128 + int(signal.SIGTERM),
}
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _canonical_utc_milliseconds(value: str) -> int:
    """Parse only canonical UTC seconds or millisecond timestamps."""

    if not isinstance(value, str) or not value.endswith("Z"):
        raise argparse.ArgumentTypeError("value must be canonical UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "value must be a valid canonical UTC timestamp"
        ) from error
    if parsed.tzinfo != UTC or parsed.microsecond % 1_000:
        raise argparse.ArgumentTypeError(
            "value must use whole seconds or exactly millisecond precision"
        )
    since_epoch = parsed - _UNIX_EPOCH
    milliseconds = (
        since_epoch.days * 86_400_000
        + since_epoch.seconds * 1_000
        + since_epoch.microseconds // 1_000
    )
    if milliseconds <= 0:
        raise argparse.ArgumentTypeError("value must be after the Unix epoch")
    if _format_utc_milliseconds(milliseconds) != value:
        raise argparse.ArgumentTypeError(
            "value must be canonical UTC with seconds or milliseconds"
        )
    return milliseconds


def _format_utc_milliseconds(value: int) -> str:
    timespec = "seconds" if value % 1_000 == 0 else "milliseconds"
    return (
        (_UNIX_EPOCH + timedelta(milliseconds=value))
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


def _configure_deployment_transport() -> None:
    """Select the RC4 identity only inside this explicit process."""

    _SECURITY.CONTRACT_NAME = CONTRACT_NAME
    _SECURITY.CONTRACT_REVISION = CONTRACT_REVISION
    _SECURITY.CONTRACT_COMPATIBILITY = CONTRACT_COMPATIBILITY
    _SECURITY.PREVIEW_EXECUTOR = DEPLOYMENT_EXECUTOR
    _SECURITY.PREVIEW_KIND = DEPLOYMENT_KIND
    _SECURITY._configure_transport()
    _SECURITY._ATTENDEE._STAGE.PREVIEW_EXECUTOR = DEPLOYMENT_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = DEPLOYMENT_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = True
    _CORE.PREVIEW_TEST_ONLY = True


def validate_contract_root(contract_root: Path) -> Path:
    """Validate the exact disabled-capability RC4 deployment contract."""

    root = contract_root.resolve()
    tool_path = Path(__file__).with_name(
        "product_attendee_security_deployment_contract.py"
    )
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_security_deployment_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee deployment contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("attendee deployment contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 25
    ):
        raise ValueError("attendee deployment contract is incompatible")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("attendee deployment contract is unavailable") from error
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
        raise ValueError("attendee deployment capabilities must remain disabled")
    return root


class AttendeeSecurityDeploymentHarness(_LIVE.AttendeeSecurityLiveHarness):
    """Add time-driven drain and recovery-close signals to the live harness."""

    def __init__(self, **kwargs: object) -> None:
        scenario = kwargs.pop("scenario", SCENARIOS[0])
        if scenario not in SCENARIOS:
            raise ValueError("unknown attendee deployment scenario")
        runtime = kwargs.get("runtime")
        if (
            not isinstance(runtime, _LIVE.LiveAttendeeRuntime)
            or runtime.composition.bootstrap_closes_at_admission is not True
        ):
            raise ValueError(
                "attendee deployment requires exact bootstrap cutoff enforcement"
            )
        self._availability_stop = threading.Event()
        self._admission_closed = threading.Event()
        self._recovery_closed = threading.Event()
        self._availability_thread: threading.Thread | None = None
        super().__init__(scenario=_LIVE.SCENARIOS[0], **kwargs)
        self.scenario = scenario
        self._availability_thread = threading.Thread(
            target=self._watch_availability,
            name="dreamland-attendee-deployment-availability",
        )
        self._availability_thread.start()

    @property
    def admission_closed(self) -> threading.Event:
        return self._admission_closed

    @property
    def recovery_closed(self) -> threading.Event:
        return self._recovery_closed

    def _clock_values(self) -> tuple[int, int]:
        registry = self.service._registry
        wall_ms = registry._clock_ms()
        monotonic_ms = registry._monotonic_ms()
        if (
            isinstance(wall_ms, bool)
            or not isinstance(wall_ms, int)
            or wall_ms < 0
            or isinstance(monotonic_ms, bool)
            or not isinstance(monotonic_ms, int)
            or monotonic_ms < 0
        ):
            raise RuntimeError("attendee deployment clock is invalid")
        return wall_ms, monotonic_ms

    def _wait_for_boundary(
        self,
        *,
        wall_boundary_ms: int,
        monotonic_boundary_ms: int,
    ) -> bool:
        while not self._availability_stop.is_set():
            wall_ms, monotonic_ms = self._clock_values()
            remaining_ms = min(
                wall_boundary_ms - wall_ms,
                monotonic_boundary_ms - monotonic_ms,
            )
            if remaining_ms <= 0:
                return True
            self._availability_stop.wait(min(remaining_ms / 1_000, WATCH_POLL_SECONDS))
        return False

    def _watch_availability(self) -> None:
        """Fence admission with no request, then end retained recovery."""

        window = self.resolved_availability_window
        admission_deadline_ms = self.service._admission_closes_deadline_ms
        recovery_deadline_ms = admission_deadline_ms + (
            window.recovery_closes_at_ms - window.admission_closes_at_ms
        )
        try:
            if not self._wait_for_boundary(
                wall_boundary_ms=window.admission_closes_at_ms,
                monotonic_boundary_ms=admission_deadline_ms,
            ):
                return
            # Expiry installs authoritative terminal truth before drain rejects
            # fresh bootstrap, exchange, create, command, reset, and switch.
            self.service.expire_due()
            self.begin_drain()
            self._admission_closed.set()
            logger.info("attendee_deployment_admission_closed")
            if not self._wait_for_boundary(
                wall_boundary_ms=window.recovery_closes_at_ms,
                monotonic_boundary_ms=recovery_deadline_ms,
            ):
                return
            logger.info("attendee_deployment_recovery_closed")
        except Exception:
            # A watcher failure cannot leave a nominally serving process open.
            logger.exception("attendee_deployment_availability_watch_failed")
            try:
                self.begin_drain()
            except Exception:
                logger.exception("attendee_deployment_failure_drain_failed")
        finally:
            if self._admission_closed.is_set() or not self._availability_stop.is_set():
                self._recovery_closed.set()

    def close(self) -> None:
        self._availability_stop.set()
        thread = self._availability_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        super().close()


class AttendeeSecurityDeploymentServer(_LIVE.AttendeeSecurityLiveServer):
    """Join every admitted handler before closing the deployment harness."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._deployment_close_lock = threading.Lock()
        self._deployment_closed = False
        super().__init__(*args, **kwargs)

    def server_close(self) -> None:
        with self._deployment_close_lock:
            if self._deployment_closed:
                return
            self._deployment_closed = True
        try:
            ThreadingHTTPServer.server_close(self)
        finally:
            self.preview.close()


def build_deployment_server(
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
    provider_settings: Any,
    availability_window: Any,
    context_capacity: int = _LIVE.DEFAULT_CONTEXT_CAPACITY,
    active_session_capacity: int = _LIVE.DEFAULT_ACTIVE_SESSION_CAPACITY,
    connection_capacity: int = _LIVE.DEFAULT_CONNECTION_CAPACITY,
    worker_capacity: int = _LIVE.DEFAULT_WORKER_CAPACITY,
    max_commands_per_session: int = _LIVE.DEFAULT_MAX_COMMANDS_PER_SESSION,
    max_output_chunks_per_command: int = _LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND,
    contract_validator: Callable[[Path], Path] = validate_contract_root,
    provider_worker_factory: Callable[..., Any] = _LIVE.AttendeeLiveWorkerFactory,
    provider_pool_factory: Callable[..., Any] = _LIVE.AttendeeProviderPool,
    harness_factory: Callable[..., AttendeeSecurityDeploymentHarness] = (
        AttendeeSecurityDeploymentHarness
    ),
    server_factory: Callable[..., AttendeeSecurityDeploymentServer] = (
        AttendeeSecurityDeploymentServer
    ),
    utc_ms: Callable[[], int] = _utc_ms,
) -> AttendeeSecurityDeploymentServer:
    """Ready the provider, apply the exact window, then bind loopback."""

    window_type = _SECURITY.AttendeeSecurityAvailabilityWindow
    if not isinstance(availability_window, window_type):
        raise TypeError(
            "availability_window must be AttendeeSecurityAvailabilityWindow"
        )
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("attendee deployment supports only loopback hosts")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("attendee deployment port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if (
        not isinstance(bff_authority, str)
        or _SECURITY._BFF_AUTHORITY_PATTERN.fullmatch(bff_authority) is None
    ):
        raise ValueError("private attendee BFF authority is malformed")
    if scenario not in SCENARIOS:
        raise ValueError("unknown attendee deployment scenario")
    if utc_ms() >= availability_window.admission_closes_at_ms:
        raise ValueError("attendee deployment admission already closed")

    root = contract_validator(contract_root)
    _configure_deployment_transport()
    runtime = _LIVE.start_live_runtime(
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
    server: AttendeeSecurityDeploymentServer | None = None
    try:
        composition = replace(
            runtime.composition,
            availability_window=availability_window,
            bootstrap_closes_at_admission=True,
        )
        runtime = replace(runtime, composition=composition)
        server = server_factory(
            (host, port),
            bearer=bearer,
            bff_authority=bff_authority,
            scenario=SCENARIOS[0],
            contract_root=root,
            runtime=runtime,
            harness_factory=harness_factory,
        )
        if utc_ms() >= availability_window.admission_closes_at_ms:
            raise ValueError("attendee deployment admission closed during startup")
        return server
    except Exception:
        if server is not None:
            server.server_close()
        else:
            try:
                runtime.provider_pool.shutdown(
                    timeout_ms=runtime.pool_limits.shutdown_timeout_ms
                )
            except Exception:
                logger.exception("attendee_deployment_listener_rollback_failed")
        raise


def startup_announcement(
    server: AttendeeSecurityDeploymentServer,
) -> dict[str, object]:
    """Return readiness and lifecycle facts without secret material."""

    announcement = _LIVE.startup_announcement(server)
    window = server.preview.resolved_availability_window
    contract = dict(announcement["contract"])
    contract["revision"] = CONTRACT_REVISION
    announcement["contract"] = contract
    announcement["kind"] = DEPLOYMENT_KIND
    announcement["scenario"] = SCENARIOS[0]
    announcement["availability"] = {
        "opens_at": _format_utc_milliseconds(window.admission_opens_at_ms),
        "admission_closes_at": _format_utc_milliseconds(window.admission_closes_at_ms),
        "recovery_closes_at": _format_utc_milliseconds(window.recovery_closes_at_ms),
        "clock_policy": "startup_utc_anchor_plus_monotonic_elapsed",
    }
    announcement["supervisor"] = {
        "accepted_signals": ["SIGINT", "SIGTERM"],
        "hot_reload": False,
        "rotation": "restart_required",
        "automatic_authority_remint": False,
        "automatic_redispatch": False,
        "shutdown_upper_bound_ms": (
            server.preview.runtime.pool_limits.shutdown_upper_bound_ms()
            # Under full connection saturation the accept loop may already
            # have accepted one additional socket while it waits for a bounded
            # handler slot.  One connection deadline releases that slot; a
            # second bounds the newly admitted handler before
            # ``server_close`` finishes joining request threads.
            + int(2 * _LIFECYCLE.CONNECTION_DEADLINE_SECONDS * 1_000)
            + int(HANDLE_REQUEST_POLL_SECONDS * 1_000)
        ),
    }
    return announcement


def _orderly_close(server: AttendeeSecurityDeploymentServer) -> None:
    try:
        server.begin_drain()
    except Exception:
        logger.exception("attendee_deployment_begin_drain_failed")
    server.server_close()


def serve_built_server(
    server: AttendeeSecurityDeploymentServer,
    *,
    install_signal_handlers: bool = True,
    announcement_factory: Callable[
        [AttendeeSecurityDeploymentServer], Mapping[str, object]
    ] = startup_announcement,
) -> int:
    """Serve until a signal or the exact recovery boundary, then converge."""

    if (
        install_signal_handlers
        and threading.current_thread() is not threading.main_thread()
    ):
        raise ValueError("attendee deployment signals require the main thread")

    stop = threading.Event()
    signal_number: list[int | None] = [None]
    prior_handlers: dict[int, Any] = {}

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        if signal_number[0] is None:
            signal_number[0] = signum
        stop.set()

    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    try:
        print(
            json.dumps(dict(announcement_factory(server)), sort_keys=True), flush=True
        )
        server.timeout = HANDLE_REQUEST_POLL_SECONDS
        while not stop.is_set() and not server.preview.recovery_closed.is_set():
            server.handle_request()
    finally:
        try:
            _orderly_close(server)
        finally:
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)
    selected = signal_number[0]
    return SIGNAL_EXIT_CODES.get(selected, 0)


def serve_deployment(**kwargs: object) -> int:
    """Build and serve under handlers installed before provider startup."""

    if threading.current_thread() is not threading.main_thread():
        raise ValueError("attendee deployment must start on the main thread")
    stop = threading.Event()
    signal_number: list[int | None] = [None]
    prior_handlers: dict[int, Any] = {}

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        if signal_number[0] is None:
            signal_number[0] = signum
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    server: AttendeeSecurityDeploymentServer | None = None
    try:
        server = build_deployment_server(**kwargs)
        if stop.is_set():
            _orderly_close(server)
        else:
            print(json.dumps(startup_announcement(server), sort_keys=True), flush=True)
            server.timeout = HANDLE_REQUEST_POLL_SECONDS
            while not stop.is_set() and not server.preview.recovery_closed.is_set():
                server.handle_request()
            _orderly_close(server)
        return SIGNAL_EXIT_CODES.get(signal_number[0], 0)
    finally:
        if server is not None:
            server.server_close()
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the private attendee deployment checkpoint on loopback."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create owner-only credentials")
    initialize.add_argument("--token-file", required=True, type=Path)
    initialize.add_argument("--bootstrap-file", required=True, type=Path)
    digest = commands.add_parser(
        "digest", help="print the immutable digest pin for one authored profile"
    )
    digest.add_argument("--profile-path", required=True, type=Path)
    serve = commands.add_parser("serve", help="start the supervised loopback listener")
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
        "--max-output-tokens", required=True, type=_LIVE._positive_integer
    )
    serve.add_argument("--system-prompt", default="system.md")
    serve.add_argument(
        "--context-capacity",
        type=_LIVE._context_capacity,
        default=_LIVE.DEFAULT_CONTEXT_CAPACITY,
    )
    serve.add_argument(
        "--active-session-capacity",
        type=_LIVE._active_session_capacity,
        default=_LIVE.DEFAULT_ACTIVE_SESSION_CAPACITY,
    )
    serve.add_argument(
        "--connection-capacity",
        type=_LIVE._connection_capacity,
        default=_LIVE.DEFAULT_CONNECTION_CAPACITY,
    )
    serve.add_argument(
        "--worker-capacity",
        type=_LIVE._worker_capacity,
        default=_LIVE.DEFAULT_WORKER_CAPACITY,
    )
    serve.add_argument(
        "--max-commands",
        type=_LIVE._positive_integer,
        default=_LIVE.DEFAULT_MAX_COMMANDS_PER_SESSION,
    )
    serve.add_argument(
        "--max-output-chunks-per-command",
        type=_LIVE._output_chunk_limit,
        default=_LIVE.MAX_OUTPUT_CHUNKS_PER_COMMAND,
    )
    serve.add_argument(
        "--reasoning-effort", choices=("none", "low", "medium", "high", "xhigh")
    )
    serve.add_argument("--reasoning-summary", choices=("auto", "concise", "detailed"))
    serve.add_argument("--verbosity", choices=("low", "medium", "high"))
    serve.add_argument("--speed", choices=("fast",))
    serve.add_argument("--prompt-cache", choices=("automatic", "off"))
    serve.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    serve.add_argument(
        "--availability-opens-at",
        required=True,
        type=_canonical_utc_milliseconds,
    )
    serve.add_argument(
        "--availability-closes-at",
        required=True,
        type=_canonical_utc_milliseconds,
    )
    serve.add_argument(
        "--recovery-closes-at",
        required=True,
        type=_canonical_utc_milliseconds,
    )
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
                        "authored_revision_digest": _LIVE.authored_digest(
                            arguments.profile_path
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0

        availability_window = _SECURITY.AttendeeSecurityAvailabilityWindow(
            admission_opens_at_ms=arguments.availability_opens_at,
            admission_closes_at_ms=arguments.availability_closes_at,
            recovery_closes_at_ms=arguments.recovery_closes_at,
        )
        bearer = _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
        bff_authority = _SECURITY.load_bootstrap(arguments.bootstrap_file)
        provider_settings = _LIVE.AttendeeLiveProviderSettings(
            model=arguments.model,
            system_prompt_name=arguments.system_prompt,
            max_output_tokens=arguments.max_output_tokens,
            reasoning_effort=arguments.reasoning_effort,
            reasoning_summary=arguments.reasoning_summary,
            verbosity=arguments.verbosity,
            speed=arguments.speed,
            prompt_cache=arguments.prompt_cache,
        )
        return serve_deployment(
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
            availability_window=availability_window,
            context_capacity=arguments.context_capacity,
            active_session_capacity=arguments.active_session_capacity,
            connection_capacity=arguments.connection_capacity,
            worker_capacity=arguments.worker_capacity,
            max_commands_per_session=arguments.max_commands,
            max_output_chunks_per_command=(arguments.max_output_chunks_per_command),
        )
    except KeyboardInterrupt:
        return SIGNAL_EXIT_CODES[signal.SIGINT]
    except (OSError, ValueError, _LIVE.AttendeeProviderPoolError) as error:
        print(f"Attendee deployment error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
