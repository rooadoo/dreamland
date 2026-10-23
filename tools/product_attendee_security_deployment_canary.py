#!/usr/bin/env python3
"""Run one opt-in live-provider command through the supervised attendee target.

The canary owns a disposable loopback deployment subprocess and drives the
immutable Product RC4 generated client through one attendee workflow. Importing
this module starts no listener, process, worker, timer, provider client, file
read, or ordinary DREAMLAND composition. Provider use occurs only through the
explicit ``run`` command.
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
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "product-api" / "v0.8.0-rc.4"
DEPLOYMENT_TOOL = Path(__file__).with_name("product_attendee_security_deployment.py")

REPORT_KIND = "attendee_deployment_provider_canary_report"
REPORT_SCHEMA = "dreamland.attendee-deployment-provider-canary/v1"
LIVE_SCOPE = "loopback_real_provider_single_command"
FIXTURE_SCOPE = "loopback_provider_free_canary_fixture"
PRODUCTION_CAPABILITY_EVIDENCE = False

CONTRACT_NAME = "dreamland-private-product-api"
CONTRACT_REVISION = "0.8.0-rc.4"
CONTRACT_COMPATIBILITY = "unfrozen-release-candidate"
CONTRACT_OPERATIONS = 25
PRIVATE_ROUTE_COUNT = 16
CONTRACT_FILES = 193
MANIFEST_SHA256 = "64454ebf7da717fdfd78ccc25b99681f07ee2225ce52b15836c171bc1d571567"
OPENAPI_SHA256 = "e2d0e920cec59abda59b735b16b38627896eef405123e866ed1030702fafba80"
GENERATED_CLIENT_SHA256 = (
    "bd450ee334d0ec9adb8da1048607d421a580ae698b983a88073ebf16b97a1176"
)
CONTRACT_TREE_SHA256 = (
    "4cc7f99a7afbd13261a2d81a50e40da1aa22e7627b7664d539bc743ff55177f3"
)

CANARY_COMMAND = "uname -a"
WORKER_CAPACITY = 1
COMMAND_LIMIT = 1
PROVIDER_ATTEMPT_LIMIT = 1
CANARY_MAX_OUTPUT_BYTES_PER_COMMAND = 64 * 1_024
# The accepted-event cursor must remain replayable after the command reaches
# terminal truth. One running event, an optional state event, two final stream
# markers, and the terminal event leave 251 of the 256 retained event slots for
# data. A limit of 240 preserves an explicit margin without changing the
# supervised target's default when this canary is not used.
CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND = 240
CANARY_PUBLICATION_CHUNK_BYTES = 4 * 1_024
CANARY_MINIMUM_TOKEN_RESERVATION = 8_192
CANARY_MAX_OUTPUT_TOKENS = CANARY_MINIMUM_TOKEN_RESERVATION
CANARY_SPEND_RESERVATION_PER_COMMAND = 1_000
DEPLOYMENT_EXECUTOR = "private-attendee-live-provider-pool-v1"
DEPLOYMENT_SCENARIO = "attendee-security-deployment"
STARTUP_TIMEOUT_SECONDS = 45.0
COMMAND_TIMEOUT_SECONDS = 150.0
REQUEST_POLL_SECONDS = 0.1
AVAILABILITY_WINDOW_MS = 240_000
RECOVERY_WINDOW_MS = 60_000
PROCESS_MARGIN_SECONDS = 5.0
PROCESS_GROUP_CONVERGENCE_SECONDS = 5.0
PROCESS_GROUP_POLL_SECONDS = 0.05
MAX_SUPERVISOR_SHUTDOWN_BOUND_MS = 30_000
MAX_CAPTURE_BYTES = 1_048_576
MAX_PAGES = 128
PAGINATION_TIMEOUT_SECONDS = 20.0

LIVE_NOTICE_VERSION = "notice:attendee-live-development-v1"
LIVE_POLICY_ID = "policy:attendee-live-authored-v1"

_REPORT_ROOT_KEYS = frozenset(
    {
        "kind",
        "schema",
        "scope",
        "production_capability_evidence",
        "live_provider_execution_evidence",
        "contract",
        "configuration",
        "privacy",
        "limits",
        "evidence",
        "summary",
    }
)
_FORBIDDEN_REPORT_KEYS = frozenset(
    {
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
)

_SIGNAL_DEFERRAL_DEPTH = 0
_DEFERRED_CANARY_SIGNAL: int | None = None


class _CanarySignal(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid canary arguments")


@contextmanager
def _defer_canary_signal() -> Iterator[None]:
    """Defer asynchronous unwind until a new process group is registered."""

    global _DEFERRED_CANARY_SIGNAL, _SIGNAL_DEFERRAL_DEPTH
    _SIGNAL_DEFERRAL_DEPTH += 1
    try:
        yield
    finally:
        _SIGNAL_DEFERRAL_DEPTH -= 1
        if _SIGNAL_DEFERRAL_DEPTH == 0 and _DEFERRED_CANARY_SIGNAL is not None:
            signum = _DEFERRED_CANARY_SIGNAL
            _DEFERRED_CANARY_SIGNAL = None
            raise _CanarySignal(signum)


@dataclass(frozen=True, slots=True)
class _CanaryConfiguration:
    contract_root: Path
    profile_path: Path
    profile_digest: str
    credential_file: Path
    model: str
    max_output_tokens: int
    system_prompt: str
    reasoning_effort: str | None
    reasoning_summary: str | None
    verbosity: str | None
    speed: str | None
    prompt_cache: str | None


@dataclass(frozen=True, slots=True)
class _PrivateFiles:
    root: Path
    token_file: Path
    bootstrap_file: Path
    artifact_root: Path
    bearer: str
    bff_authority: str


@dataclass(slots=True, repr=False)
class _Workflow:
    gateway_context_id: str
    actor: str
    session_authority: str
    reference: str
    control_revision: int
    slot_generation: int
    command_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CanaryEvidence:
    terminal_outcome: str
    revision_effect: str
    published_bytes: int
    published_chunks: int
    supervisor_exit_code: int
    elapsed_ms: int
    fixture_diagnostic: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _UsageReservation:
    tokens: int
    spend_units: int


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_deployment_canary",
        path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee deployment dependency is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> tuple[int, str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return len(files), digest.hexdigest()


def _validate_exact_contract(root: Path, deployment: ModuleType) -> dict[str, object]:
    try:
        normalized = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("attendee deployment RC4 root is unavailable") from error
    manifest_path = normalized / "manifest.json"
    openapi_path = normalized / "openapi.json"
    client_path = normalized / "generated" / "product_session_client.py"
    try:
        exact = (
            _sha256(manifest_path) == MANIFEST_SHA256
            and _sha256(openapi_path) == OPENAPI_SHA256
            and _sha256(client_path) == GENERATED_CLIENT_SHA256
            and _tree_digest(normalized) == (CONTRACT_FILES, CONTRACT_TREE_SHA256)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(
            "attendee deployment canary requires exact published RC4"
        ) from error
    if not exact:
        raise ValueError("attendee deployment canary requires exact published RC4")
    if deployment.validate_contract_root(normalized).resolve() != normalized:
        raise ValueError("attendee deployment RC4 root diverged")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("attendee deployment RC4 manifest is unavailable") from error
    if (
        manifest.get("contract_name") != CONTRACT_NAME
        or manifest.get("contract_revision") != CONTRACT_REVISION
        or manifest.get("compatibility") != CONTRACT_COMPATIBILITY
        or not isinstance(manifest.get("backend_commit"), str)
        or len(manifest["backend_commit"]) != 40
        or any(
            value.get("enabled") is not False
            for value in manifest.get("capabilities", {}).values()
        )
    ):
        raise ValueError("attendee deployment RC4 identity diverged")
    return manifest


def _load_rc4_client(root: Path, deployment: ModuleType) -> ModuleType:
    path = root / "generated" / "product_session_client.py"
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_rc4_attendee_deployment_canary_client",
        path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee deployment RC4 client is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    prior_bytecode_policy = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        specification.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior_bytecode_policy
    if (
        module.CONTRACT_NAME != deployment.CONTRACT_NAME
        or module.CONTRACT_REVISION != CONTRACT_REVISION
        or module.CONTRACT_COMPATIBILITY != CONTRACT_COMPATIBILITY
        or len(module.EXPECTED_OPERATIONS) != CONTRACT_OPERATIONS
    ):
        raise ValueError("attendee deployment RC4 client identity diverged")
    return module


def _absolute_path(value: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("canary path is invalid") from error


def _preflight(
    arguments: argparse.Namespace,
) -> tuple[_CanaryConfiguration, ModuleType, ModuleType, dict[str, object], Any]:
    deployment = _load_tool("product_attendee_security_deployment")
    contract_root = _absolute_path(arguments.contract_root)
    manifest = _validate_exact_contract(contract_root, deployment)
    client_module = _load_rc4_client(contract_root, deployment)

    profile_path = _absolute_path(arguments.profile_path)
    profile_digest = deployment._LIVE.authored_digest(profile_path)
    if profile_digest != arguments.profile_digest:
        raise ValueError("canary authored profile digest diverged")

    # This is metadata-only validation. Credential content remains child-local.
    from dreamland.product.attendee_live import (
        AttendeeLiveProviderSettings,
        validate_attendee_live_credential_file,
        validate_attendee_live_environment,
    )

    credential_file = validate_attendee_live_credential_file(
        _absolute_path(arguments.provider_credential_file)
    )
    validate_attendee_live_environment()
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
    if not 1 <= provider_settings.max_output_tokens <= CANARY_MAX_OUTPUT_TOKENS:
        raise ValueError("canary max output tokens exceed the bounded limit")
    configuration = _CanaryConfiguration(
        contract_root=contract_root,
        profile_path=profile_path,
        profile_digest=profile_digest,
        credential_file=credential_file,
        model=provider_settings.model,
        max_output_tokens=provider_settings.max_output_tokens,
        system_prompt=provider_settings.system_prompt_name,
        reasoning_effort=provider_settings.reasoning_effort,
        reasoning_summary=provider_settings.reasoning_summary,
        verbosity=provider_settings.verbosity,
        speed=provider_settings.speed,
        prompt_cache=provider_settings.prompt_cache,
    )
    return configuration, deployment, client_module, manifest, provider_settings


def _initialize_private_files(root: Path, deployment: ModuleType) -> _PrivateFiles:
    token_file = root / "product.token"
    bootstrap_file = root / "bootstrap.json"
    artifact_root = root / "artifacts"
    artifact_root.mkdir(mode=0o700)
    deployment._SECURITY.initialize_private_files(
        token_file=token_file,
        bootstrap_file=bootstrap_file,
    )
    return _PrivateFiles(
        root=root,
        token_file=token_file,
        bootstrap_file=bootstrap_file,
        artifact_root=artifact_root,
        bearer=deployment._LIFECYCLE.load_preview_bearer_file(token_file),
        bff_authority=deployment._SECURITY.load_bootstrap(bootstrap_file),
    )


def _availability_arguments(deployment: ModuleType) -> tuple[str, str, str]:
    now_ms = time.time_ns() // 1_000_000
    opens_ms = max(1, now_ms - 1_000)
    closes_ms = now_ms + AVAILABILITY_WINDOW_MS
    recovery_ms = closes_ms + RECOVERY_WINDOW_MS
    return tuple(
        deployment._format_utc_milliseconds(value)
        for value in (opens_ms, closes_ms, recovery_ms)
    )


def _live_deployment_command(
    configuration: _CanaryConfiguration,
    private: _PrivateFiles,
    availability: tuple[str, str, str],
) -> list[str]:
    opens_at, closes_at, recovery_at = availability
    command = [
        sys.executable,
        "-B",
        str(DEPLOYMENT_TOOL),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--token-file",
        str(private.token_file),
        "--bootstrap-file",
        str(private.bootstrap_file),
        "--contract-root",
        str(configuration.contract_root),
        "--artifact-root",
        str(private.artifact_root),
        "--authored-profile-path",
        str(configuration.profile_path),
        "--authored-profile-digest",
        configuration.profile_digest,
        "--provider-credential-file",
        str(configuration.credential_file),
        "--model",
        configuration.model,
        "--max-output-tokens",
        str(configuration.max_output_tokens),
        "--system-prompt",
        configuration.system_prompt,
        "--worker-capacity",
        str(WORKER_CAPACITY),
        "--max-commands",
        str(COMMAND_LIMIT),
        "--max-output-chunks-per-command",
        str(CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND),
        "--availability-opens-at",
        opens_at,
        "--availability-closes-at",
        closes_at,
        "--recovery-closes-at",
        recovery_at,
    ]
    for flag, value in (
        ("--reasoning-effort", configuration.reasoning_effort),
        ("--reasoning-summary", configuration.reasoning_summary),
        ("--verbosity", configuration.verbosity),
        ("--speed", configuration.speed),
        ("--prompt-cache", configuration.prompt_cache),
    ):
        if value is not None:
            command.extend((flag, value))
    return command


def _write_owner_file(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group_id: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_alive(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))
    return True


class _DeploymentProcess:
    """Own the supervised deployment subprocess and its complete process group."""

    def __init__(self, *, command: Sequence[str], private_root: Path) -> None:
        self.command = list(command)
        self.private_root = private_root
        self.process: subprocess.Popen[str] | None = None
        self.process_group_id: int | None = None
        self.announcement: dict[str, Any] = {}
        self.stdout_text = ""
        self.stderr_text = ""
        self._stderr_path = private_root / "deployment.stderr"
        self._stderr_stream: Any | None = None
        self._finished_cleanly = False
        self._forced_cleanup_complete = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.announcement['port']}"

    def start(self) -> _DeploymentProcess:
        try:
            _write_owner_file(self._stderr_path, "")
            self._stderr_stream = self._stderr_path.open("w", encoding="utf-8")
            with _defer_canary_signal():
                process = subprocess.Popen(
                    self.command,
                    cwd=REPOSITORY_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=self._stderr_stream,
                    text=True,
                    start_new_session=True,
                )
                self.process = process
                self.process_group_id = process.pid
            self._read_announcement()
            return self
        except BaseException:
            self.force_stop()
            raise

    def _read_announcement(self) -> None:
        process = self._require_process()
        if process.stdout is None:
            raise RuntimeError("canary deployment stdout is unavailable")
        readable, _, _ = select.select(
            [process.stdout],
            [],
            [],
            STARTUP_TIMEOUT_SECONDS,
        )
        if not readable:
            raise TimeoutError("canary deployment startup deadline expired")
        line = process.stdout.readline(MAX_CAPTURE_BYTES + 1)
        if not line or len(line.encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise RuntimeError("canary deployment readiness is unavailable")
        self.stdout_text = line
        try:
            announcement = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("canary deployment readiness was not JSON") from error
        if not isinstance(announcement, dict):
            raise RuntimeError("canary deployment readiness diverged")
        self.announcement = announcement

    def stop(
        self, *, expect_fixture_diagnostic: bool = False
    ) -> Mapping[str, object] | None:
        process = self._require_process()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        timeout = self._shutdown_timeout_seconds()
        try:
            remaining, _unused = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self._kill_group()
            process.communicate(timeout=5)
            self._collect_stderr()
            raise TimeoutError("canary deployment shutdown deadline expired") from error
        self.stdout_text += remaining
        self._collect_stderr()
        if process.returncode != 128 + signal.SIGTERM:
            raise RuntimeError("canary deployment returned an unexpected status")
        if self.stderr_text:
            raise RuntimeError("canary deployment emitted stderr")
        trailing = [line for line in remaining.splitlines() if line.strip()]
        diagnostic: Mapping[str, object] | None = None
        if expect_fixture_diagnostic:
            if len(trailing) != 1:
                raise RuntimeError("canary fixture diagnostic is unavailable")
            try:
                candidate = json.loads(trailing[0])
            except json.JSONDecodeError as error:
                raise RuntimeError("canary fixture diagnostic was not JSON") from error
            if not isinstance(candidate, Mapping):
                raise RuntimeError("canary fixture diagnostic diverged")
            diagnostic = candidate
        elif trailing:
            raise RuntimeError("canary deployment emitted unexpected stdout")
        process_group_id = self.process_group_id
        if process_group_id is not None and not _wait_for_process_group_exit(
            process_group_id,
            timeout=PROCESS_GROUP_CONVERGENCE_SECONDS,
        ):
            self._kill_group()
            _wait_for_process_group_exit(
                process_group_id,
                timeout=PROCESS_GROUP_CONVERGENCE_SECONDS,
            )
            raise RuntimeError("canary deployment process group did not converge")
        self._finished_cleanly = True
        return diagnostic

    def _shutdown_timeout_seconds(self) -> float:
        bound = self.announcement.get("supervisor", {}).get(
            "shutdown_upper_bound_ms",
            0,
        )
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            bound = 0
        bound = min(bound, MAX_SUPERVISOR_SHUTDOWN_BOUND_MS)
        return max(5.0, bound / 1_000 + PROCESS_MARGIN_SECONDS)

    def _collect_stderr(self) -> None:
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
            raise RuntimeError("canary deployment stderr exceeded its bound")
        self.stderr_text = data.decode("utf-8", errors="strict")

    def _kill_group(self) -> None:
        process_group_id = self.process_group_id
        if process_group_id is None:
            return
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def force_stop(self) -> None:
        if self._finished_cleanly or self._forced_cleanup_complete:
            return
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=self._shutdown_timeout_seconds())
            except (OSError, subprocess.TimeoutExpired):
                self._kill_group()
                try:
                    process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self.process_group_id is not None and _process_group_alive(
            self.process_group_id
        ):
            self._kill_group()
            _wait_for_process_group_exit(
                self.process_group_id,
                timeout=PROCESS_GROUP_CONVERGENCE_SECONDS,
            )
            if process is not None:
                try:
                    process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        try:
            self._collect_stderr()
        except (OSError, UnicodeError, RuntimeError):
            pass
        self._forced_cleanup_complete = True

    def _require_process(self) -> subprocess.Popen[str]:
        if self.process is None:
            raise RuntimeError("canary deployment was not started")
        return self.process


def _validate_announcement(
    announcement: Mapping[str, object],
    configuration: _CanaryConfiguration,
) -> _UsageReservation:
    contract = announcement.get("contract")
    provider = announcement.get("provider")
    privacy = announcement.get("privacy")
    resources = announcement.get("resources")
    output_publication = announcement.get("output_publication")
    deployment = announcement.get("deployment")
    supervisor = announcement.get("supervisor")
    if (
        announcement.get("kind") != "public_attendee_private_deployment_checkpoint"
        or announcement.get("host") != "127.0.0.1"
        or isinstance(announcement.get("port"), bool)
        or not isinstance(announcement.get("port"), int)
        or not 1 <= announcement["port"] <= 65_535
        or announcement.get("integration_only") is not True
        or announcement.get("normal_runtime_composition") is not False
        or announcement.get("provider_calls") is not True
        or announcement.get("capabilities_enabled") != 0
        or announcement.get("route_count") != PRIVATE_ROUTE_COUNT
        or announcement.get("executor") != DEPLOYMENT_EXECUTOR
        or announcement.get("scenario") != DEPLOYMENT_SCENARIO
        or announcement.get("test_only") is not True
        or contract
        != {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
        }
        or not isinstance(provider, Mapping)
        or provider.get("lifecycle") != "ready"
        or provider.get("worker_capacity") != WORKER_CAPACITY
        or provider.get("ready_workers") != WORKER_CAPACITY
        or provider.get("model") != configuration.model
        or provider.get("max_output_tokens") != configuration.max_output_tokens
        or provider.get("upstream_reachability_checked") is not False
        or privacy
        != {
            "notice_version": LIVE_NOTICE_VERSION,
            "policy_id": LIVE_POLICY_ID,
        }
        or not isinstance(resources, Mapping)
        or resources.get("physical_workers") != WORKER_CAPACITY
        or resources.get("effective_command_limit") != COMMAND_LIMIT
        or not isinstance(output_publication, Mapping)
        or output_publication.get("flush_bytes") != CANARY_PUBLICATION_CHUNK_BYTES
        or output_publication.get("flush_on_newline") is not True
        or output_publication.get("max_bytes_per_command")
        != CANARY_MAX_OUTPUT_BYTES_PER_COMMAND
        or output_publication.get("max_chunks_per_command")
        != CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND
        or deployment
        != {
            "authority": "owner_only_private_bff",
            "browser_direct_supported": False,
            "exposure": "loopback_only",
            "production_capabilities": False,
        }
        or not isinstance(supervisor, Mapping)
        or supervisor.get("accepted_signals") != ["SIGINT", "SIGTERM"]
        or supervisor.get("hot_reload") is not False
        or supervisor.get("rotation") != "restart_required"
        or supervisor.get("automatic_authority_remint") is not False
        or supervisor.get("automatic_redispatch") is not False
        or isinstance(supervisor.get("shutdown_upper_bound_ms"), bool)
        or not isinstance(supervisor.get("shutdown_upper_bound_ms"), int)
        or not 1
        <= supervisor["shutdown_upper_bound_ms"]
        <= MAX_SUPERVISOR_SHUTDOWN_BOUND_MS
    ):
        raise RuntimeError("canary deployment readiness diverged")
    tokens = resources.get("token_reservation_per_command")
    spend_units = resources.get("spend_reservation_per_command")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**63
        for value in (tokens, spend_units)
    ) or (
        tokens != max(CANARY_MINIMUM_TOKEN_RESERVATION, configuration.max_output_tokens)
        or spend_units != CANARY_SPEND_RESERVATION_PER_COMMAND
    ):
        raise RuntimeError("canary deployment reservation diverged")
    return _UsageReservation(tokens=tokens, spend_units=spend_units)


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _key(label: str) -> str:
    return f"canary:{label}:{secrets.token_urlsafe(18)}"


def _remember_private(sentinels: list[str], *values: object) -> None:
    for value in values:
        if isinstance(value, str) and value and value not in sentinels:
            sentinels.append(value)


def _validate_capabilities(payload: Mapping[str, object]) -> str:
    capabilities = payload.get("capabilities")
    epoch = payload.get("service_epoch")
    if (
        not isinstance(epoch, str)
        or not isinstance(capabilities, Mapping)
        or len(capabilities) != 5
        or any(
            not isinstance(value, Mapping)
            or value.get("enabled") is not False
            or value.get("enabled_revision") is not None
            for value in capabilities.values()
        )
    ):
        raise AssertionError("canary capability truth diverged")
    return epoch


def _collect_pages(
    fetch: Callable[[Mapping[str, object] | None], Mapping[str, object]],
    *,
    item_key: str,
    position_key: str,
    after: Mapping[str, object] | None = None,
) -> tuple[list[Mapping[str, object]], Mapping[str, object]]:
    items: list[Mapping[str, object]] = []
    cursor = after
    deadline = time.monotonic() + PAGINATION_TIMEOUT_SECONDS
    for _unused in range(MAX_PAGES):
        if time.monotonic() >= deadline:
            raise TimeoutError("canary projection deadline expired")
        page = fetch(cursor)
        page_items = page.get(item_key)
        position = page.get(position_key)
        if not isinstance(page_items, list) or not isinstance(position, Mapping):
            raise AssertionError("canary paginated projection diverged")
        for item in page_items:
            if not isinstance(item, Mapping):
                raise AssertionError("canary projection item diverged")
            items.append(item)
        if not page_items:
            return items, position
        if cursor is not None and dict(position) == dict(cursor):
            raise AssertionError("canary projection cursor did not advance")
        cursor = position
    raise AssertionError("canary projection exceeded its page bound")


def _validate_output_projection(
    projection: Sequence[tuple[str, str, bool, int]],
) -> None:
    """Require one contiguous data sequence and final marker per stream."""

    for stream in ("stdout", "stderr"):
        entries = [entry for entry in projection if entry[0] == stream]
        if not entries or [entry[3] for entry in entries] != list(range(len(entries))):
            raise AssertionError("canary output stream sequence diverged")
        data_entries = entries[:-1]
        final_entry = entries[-1]
        if (
            any(final or not text for _, text, final, _index in data_entries)
            or final_entry[1] != ""
            or final_entry[2] is not True
        ):
            raise AssertionError("canary output stream finalization diverged")


def _validate_terminal_publication(
    client_module: ModuleType,
    client: Any,
    workflow: _Workflow,
    *,
    epoch: str,
    accepted_cursor: Mapping[str, object],
    reservation: _UsageReservation,
    sentinels: list[str],
) -> tuple[str, str, int, int]:
    if workflow.command_id is None:
        raise ValueError("canary workflow has no command")
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    terminal: Mapping[str, object] | None = None
    while time.monotonic() < deadline:
        payload = client.command_result(
            epoch=epoch,
            actor=workflow.actor,
            capability=workflow.session_authority,
            reference=workflow.reference,
            command_id=workflow.command_id,
        )
        if payload.get("terminal") is True:
            terminal = payload
            break
        time.sleep(REQUEST_POLL_SECONDS)
    if terminal is None:
        raise TimeoutError("canary command did not reach terminal truth")

    command = terminal.get("command")
    if not isinstance(command, Mapping):
        raise AssertionError("canary terminal command is unavailable")
    result = command.get("result")
    usage = result.get("usage") if isinstance(result, Mapping) else None
    if (
        command.get("command_id") != workflow.command_id
        or command.get("slot_generation") != workflow.slot_generation
        or command.get("status") != "completed"
        or not isinstance(result, Mapping)
        or result.get("outcome") != "completed"
        or result.get("guest_termination") != {"kind": "exited", "exit_status": 0}
        or result.get("output_clipped") is not False
        or not isinstance(usage, Mapping)
        or usage.get("state") != "settled"
        or usage.get("tokens") != reservation.tokens
        or usage.get("spend_units") != reservation.spend_units
    ):
        raise AssertionError("canary provider execution did not complete")
    effect = result.get("state_effect")
    if not isinstance(effect, Mapping) or effect.get("kind") not in {
        "committed",
        "none",
        "rejected",
    }:
        raise AssertionError("canary revision effect diverged")
    revision_effect = str(effect["kind"])

    snapshot_payload = client.snapshot(
        epoch=epoch,
        actor=workflow.actor,
        capability=workflow.session_authority,
        reference=workflow.reference,
    )
    snapshot = client_module.validate_snapshot(snapshot_payload.get("snapshot"))
    latest = snapshot.get("latest_command")
    output_summary = latest.get("output") if isinstance(latest, Mapping) else None
    if (
        not isinstance(latest, Mapping)
        or dict(latest) != dict(command)
        or latest.get("command_id") != workflow.command_id
        or latest.get("status") != "completed"
        or not isinstance(output_summary, Mapping)
        or output_summary.get("clipped") is not False
    ):
        raise AssertionError("canary terminal snapshot diverged")
    coordinates = snapshot.get("coordinates")
    if (
        not isinstance(coordinates, Mapping)
        or coordinates.get("command_slot_generation") != workflow.slot_generation
    ):
        raise AssertionError("canary snapshot coordinates diverged")
    workflow.control_revision = coordinates["control_revision"]
    accepted_state_revision = coordinates["accepted_state_revision"]

    events, event_position = _collect_pages(
        lambda after: client.events(
            epoch=epoch,
            actor=workflow.actor,
            capability=workflow.session_authority,
            reference=workflow.reference,
            after=after,
        ),
        item_key="events",
        position_key="position",
        after=accepted_cursor,
    )
    accepted_sequence = accepted_cursor.get("sequence")
    accepted_stream = accepted_cursor.get("stream_id")
    accepted_event_id = accepted_cursor.get("event_id")
    snapshot_event_position = coordinates.get("event")
    event_sequences = [event.get("sequence") for event in events]
    final_event = events[-1] if events else None
    if (
        isinstance(accepted_sequence, bool)
        or not isinstance(accepted_sequence, int)
        or accepted_sequence <= 0
        or not isinstance(accepted_stream, str)
        or not accepted_stream
        or not isinstance(accepted_event_id, str)
        or not accepted_event_id
        or event_sequences
        != list(range(accepted_sequence + 1, accepted_sequence + 1 + len(events)))
        or any(event.get("stream_id") != accepted_stream for event in events)
        or event_position != snapshot_event_position
        or not isinstance(final_event, Mapping)
        or dict(event_position)
        != {
            "stream_id": final_event.get("stream_id"),
            "sequence": final_event.get("sequence"),
            "event_id": final_event.get("event_id"),
        }
    ):
        raise AssertionError("canary event replay sequence diverged")
    event_id_prefix = accepted_event_id.rsplit(":", 1)[0]
    if accepted_event_id != f"{event_id_prefix}:{accepted_sequence}" or any(
        event.get("event_id") != f"{event_id_prefix}:{event['sequence']}"
        for event in events
    ):
        raise AssertionError("canary event replay identity diverged")
    command_events = [
        event for event in events if event.get("command_id") == workflow.command_id
    ]
    event_types = [event.get("type") for event in command_events]
    allowed_event_types = {
        "command.running",
        "command.output",
        "state.accepted",
        "command.completed",
    }
    expected_state_events = 1 if revision_effect == "committed" else 0
    if (
        not event_types
        or event_types[0] != "command.running"
        or event_types[-1] != "command.completed"
        or event_types.count("command.running") != 1
        or event_types.count("command.completed") != 1
        or event_types.count("state.accepted") != expected_state_events
        or any(event_type not in allowed_event_types for event_type in event_types)
    ):
        raise AssertionError("canary command lifecycle events diverged")
    running = next(
        event for event in command_events if event.get("type") == "command.running"
    )
    completed = command_events[-1]
    if (
        running.get("criticality") != "informational"
        or running.get("data") != {"slot_generation": workflow.slot_generation}
        or completed.get("criticality") != "authoritative"
        or completed.get("data")
        != {
            "slot_generation": workflow.slot_generation,
            "accepted_state_revision": accepted_state_revision,
            "outcome": "completed",
            "output_clipped": False,
        }
    ):
        raise AssertionError("canary command lifecycle data diverged")
    state_events = [
        event for event in command_events if event.get("type") == "state.accepted"
    ]
    if revision_effect == "committed":
        state_effect = result["state_effect"]
        state_event = state_events[0]
        state_data = state_event.get("data")
        if (
            state_effect.get("accepted_state_revision") != accepted_state_revision
            or state_event.get("criticality") != "authoritative"
            or not isinstance(state_data, Mapping)
            or state_data.get("source") != "foreground_command"
            or state_data.get("slot_generation") != workflow.slot_generation
            or state_data.get("accepted_state_revision") != accepted_state_revision
            or state_data.get("previous_accepted_state_revision")
            != accepted_state_revision - 1
            or not isinstance(state_data.get("replacement_sha256"), str)
            or len(state_data["replacement_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in state_data["replacement_sha256"]
            )
        ):
            raise AssertionError("canary accepted-state event diverged")
    output_events = [
        event for event in command_events if event.get("type") == "command.output"
    ]
    if not output_events:
        raise AssertionError("canary command published no output events")

    event_projection: list[tuple[str, str, bool, int]] = []
    event_transcript_positions: dict[tuple[str, int], int] = {}
    for event in output_events:
        data = event.get("data")
        if not isinstance(data, Mapping):
            raise AssertionError("canary output event data diverged")
        stream = data.get("stream")
        text = data.get("text")
        final = data.get("final")
        chunk_index = data.get("chunk_index")
        transcript_position = data.get("transcript_position")
        if (
            stream not in {"stdout", "stderr"}
            or not isinstance(text, str)
            or not isinstance(final, bool)
            or isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or chunk_index < 0
            or isinstance(transcript_position, bool)
            or not isinstance(transcript_position, int)
            or transcript_position <= 0
            or event.get("criticality") != "informational"
            or data.get("encoding") != "utf-8"
            or data.get("projection") != "inert-text-v1"
            or data.get("transformed") is not False
            or data.get("clipped") is not False
            or data.get("omitted_before_bytes") != 0
            or data.get("omitted_after_bytes") != 0
        ):
            raise AssertionError("canary output event diverged")
        event_projection.append((stream, text, final, chunk_index))
        correlation = (stream, chunk_index)
        if correlation in event_transcript_positions:
            raise AssertionError("canary output event correlation diverged")
        event_transcript_positions[correlation] = transcript_position
        _remember_private(sentinels, text)
    _validate_output_projection(event_projection)

    published_bytes = sum(
        len(text.encode("utf-8")) for _, text, _, _ in event_projection
    )
    published_chunks = sum(not final for _, _text, final, _index in event_projection)
    if (
        published_bytes <= 0
        or output_summary.get("bytes") != published_bytes
        or output_summary.get("chunks") != published_chunks
    ):
        raise AssertionError("canary output accounting diverged")

    def fetch_transcript(
        after: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        page = client.transcript(
            epoch=epoch,
            actor=workflow.actor,
            capability=workflow.session_authority,
            reference=workflow.reference,
            after=after,
        )
        if (
            page.get("omitted_before_entries") != 0
            or page.get("omitted_after_entries") != 0
        ):
            raise AssertionError("canary transcript retention diverged")
        return page

    transcript_entries, transcript_position = _collect_pages(
        fetch_transcript,
        item_key="entries",
        position_key="position",
    )
    snapshot_transcript = snapshot.get("transcript")
    transcript_entry_positions = [entry.get("position") for entry in transcript_entries]
    transcript_id = transcript_position.get("transcript_id")
    final_transcript_entry = transcript_entries[-1] if transcript_entries else None
    transcript_id_pattern = getattr(client_module, "TRANSCRIPT_ID_RE", None)
    entry_id_pattern = getattr(client_module, "ENTRY_ID_RE", None)
    if (
        any(
            isinstance(position, bool) or not isinstance(position, int)
            for position in transcript_entry_positions
        )
        or transcript_entry_positions != list(range(1, len(transcript_entries) + 1))
        or not isinstance(transcript_id, str)
        or transcript_id_pattern is None
        or transcript_id_pattern.fullmatch(transcript_id) is None
        or entry_id_pattern is None
        or not isinstance(final_transcript_entry, Mapping)
        or dict(transcript_position)
        != {
            "transcript_id": final_transcript_entry.get("transcript_id"),
            "position": final_transcript_entry.get("position"),
            "entry_id": final_transcript_entry.get("entry_id"),
        }
        or transcript_position != coordinates.get("transcript")
        or not isinstance(snapshot_transcript, Mapping)
        or snapshot_transcript.get("entries") != transcript_entries
        or snapshot_transcript.get("position") != transcript_position
        or snapshot_transcript.get("omitted_before_entries") != 0
        or snapshot_transcript.get("omitted_after_entries") != 0
    ):
        raise AssertionError("canary transcript replay sequence diverged")
    transcript_id_part = transcript_id.removeprefix("transcript:")
    for entry, position in zip(
        transcript_entries,
        transcript_entry_positions,
        strict=True,
    ):
        entry_id = entry.get("entry_id")
        if (
            entry.get("transcript_id") != transcript_id
            or not isinstance(entry_id, str)
            or entry_id_pattern.fullmatch(entry_id) is None
            or entry_id != f"entry:{transcript_id_part}:{position}"
        ):
            raise AssertionError("canary transcript identity diverged")
    command_entries = [
        entry
        for entry in transcript_entries
        if entry.get("kind") == "command"
        and entry.get("command_id") == workflow.command_id
    ]
    command_entry = command_entries[0] if len(command_entries) == 1 else None
    if (
        not isinstance(command_entry, Mapping)
        or command_entry.get("text") != CANARY_COMMAND
        or command_entry.get("encoding") != "utf-8"
        or command_entry.get("projection") != "inert-text-v1"
        or command_entry.get("transformed") is not False
        or command_entry.get("clipped") is not False
        or command_entry.get("omitted_before_bytes") != 0
        or command_entry.get("omitted_after_bytes") != 0
        or command_entry.get("chunk_index") is not None
        or command_entry.get("final") is not None
    ):
        raise AssertionError("canary transcript command diverged")
    same_command_entries = [
        entry
        for entry in transcript_entries
        if entry.get("command_id") == workflow.command_id
    ]
    if (
        not same_command_entries
        or same_command_entries[0] is not command_entry
        or any(
            entry.get("kind") not in {"command", "stdout", "stderr"}
            for entry in same_command_entries
        )
    ):
        raise AssertionError("canary transcript lifecycle diverged")
    transcript_projection: list[tuple[str, str, bool, int]] = []
    transcript_positions: set[int] = set()
    for entry in transcript_entries:
        kind = entry.get("kind")
        if (
            kind not in {"stdout", "stderr"}
            or entry.get("command_id") != workflow.command_id
        ):
            continue
        text = entry.get("text")
        final = entry.get("final")
        chunk_index = entry.get("chunk_index")
        position = entry.get("position")
        if (
            not isinstance(text, str)
            or not isinstance(final, bool)
            or isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or chunk_index < 0
            or isinstance(position, bool)
            or not isinstance(position, int)
            or position <= 0
            or position in transcript_positions
            or entry.get("encoding") != "utf-8"
            or entry.get("projection") != "inert-text-v1"
            or entry.get("clipped") is not False
            or entry.get("transformed") is not False
            or entry.get("omitted_before_bytes") != 0
            or entry.get("omitted_after_bytes") != 0
        ):
            raise AssertionError("canary transcript output diverged")
        transcript_positions.add(position)
        if event_transcript_positions.get((kind, chunk_index)) != position:
            raise AssertionError("canary event and transcript correlation diverged")
        transcript_projection.append((kind, text, final, chunk_index))
        _remember_private(sentinels, text)
    _validate_output_projection(transcript_projection)
    if transcript_projection != event_projection:
        raise AssertionError("canary event and transcript publication diverged")
    return (
        str(result["outcome"]),
        revision_effect,
        published_bytes,
        published_chunks,
    )


def _drive_generated_client_workflow(
    client_module: ModuleType,
    client: Any,
    *,
    reservation: _UsageReservation,
    sentinels: list[str],
) -> tuple[str, str, int, int]:
    capabilities = client.capabilities()
    epoch = _validate_capabilities(capabilities)
    gateway_context_id = _opaque("gateway-context:")
    bootstrap_key = _key("bootstrap")
    _remember_private(sentinels, epoch, gateway_context_id, bootstrap_key)
    bootstrap = client.bootstrap_attendee_context(
        epoch=epoch,
        key=bootstrap_key,
        gateway_context_id=gateway_context_id,
    )
    actor = bootstrap["actor_context"]
    options = client.attendee_launch_options(epoch=epoch, actor=actor)
    option_values = options.get("options")
    if not isinstance(option_values, list) or len(option_values) != 1:
        raise AssertionError("canary attendee launch option diverged")
    option = option_values[0]
    if (
        not isinstance(option, Mapping)
        or option.get("notice_version") != LIVE_NOTICE_VERSION
        or option.get("origin") != {"kind": "authored_profile_snapshot"}
    ):
        raise AssertionError("canary authored launch option diverged")
    selection_id = option["launch_selection_id"]
    exchange_key = _key("exchange")
    selection = client.exchange_attendee_launch_selection(
        epoch=epoch,
        actor=actor,
        key=exchange_key,
        launch_selection_id=selection_id,
        notice_version_presented=LIVE_NOTICE_VERSION,
    )
    launch_grant = selection["launch_grant"]
    create_key = _key("create")
    created = client.create(
        epoch=epoch,
        actor=actor,
        key=create_key,
        launch_grant=launch_grant,
    )
    snapshot = client_module.validate_snapshot(created.get("snapshot"))
    binding = snapshot.get("launch_binding")
    if (
        not isinstance(binding, Mapping)
        or binding.get("notice_version") != LIVE_NOTICE_VERSION
        or binding.get("policy_id") != LIVE_POLICY_ID
        or not isinstance(binding.get("limits"), Mapping)
        or binding["limits"].get("max_commands") != COMMAND_LIMIT
        or binding["limits"].get("command_max_attempts") != PROVIDER_ATTEMPT_LIMIT
        or binding["limits"].get("command_reserved_tokens") != reservation.tokens
        or binding["limits"].get("command_reserved_spend_units")
        != reservation.spend_units
    ):
        raise AssertionError("canary launch privacy or command limit diverged")
    workflow = _Workflow(
        gateway_context_id=gateway_context_id,
        actor=actor,
        session_authority=created["session_authority"],
        reference=snapshot["reference"],
        control_revision=snapshot["coordinates"]["control_revision"],
        slot_generation=snapshot["coordinates"]["command_slot_generation"],
    )
    command_key = _key("submit")
    accepted = client.submit(
        epoch=epoch,
        actor=workflow.actor,
        capability=workflow.session_authority,
        reference=workflow.reference,
        key=command_key,
        command=CANARY_COMMAND,
        expected_slot_generation=workflow.slot_generation,
    )
    if (
        accepted.get("kind") != "command_accepted"
        or accepted.get("replayed") is not False
        or accepted.get("canonical_command") != CANARY_COMMAND
    ):
        raise AssertionError("canary command acceptance diverged")
    workflow.command_id = accepted["command_id"]
    workflow.slot_generation = accepted["slot_generation"]
    accepted_cursor = accepted.get("event_cursor")
    if not isinstance(accepted_cursor, Mapping):
        raise AssertionError("canary accepted event cursor diverged")
    _remember_private(
        sentinels,
        actor,
        selection_id,
        exchange_key,
        launch_grant,
        create_key,
        workflow.session_authority,
        workflow.reference,
        command_key,
        workflow.command_id,
        CANARY_COMMAND,
    )

    outcome, revision_effect, published_bytes, published_chunks = (
        _validate_terminal_publication(
            client_module,
            client,
            workflow,
            epoch=epoch,
            accepted_cursor=accepted_cursor,
            reservation=reservation,
            sentinels=sentinels,
        )
    )
    close_key = _key("close")
    closed = client.close(
        epoch=epoch,
        actor=workflow.actor,
        capability=workflow.session_authority,
        reference=workflow.reference,
        key=close_key,
        expected_control_revision=workflow.control_revision,
    )
    closed_snapshot = client_module.validate_snapshot(closed.get("snapshot"))
    if (
        closed.get("kind") != "session_closed"
        or closed_snapshot.get("lifecycle", {}).get("state") != "closed"
        or closed_snapshot.get("lifecycle", {}).get("terminal_reason") != "closed"
    ):
        raise AssertionError("canary session close diverged")
    revoke_key = _key("revoke")
    revoked = client.revoke_attendee_context(
        epoch=epoch,
        actor=workflow.actor,
        key=revoke_key,
        gateway_context_id=workflow.gateway_context_id,
    )
    if (
        revoked.get("kind") != "attendee_context_revoked"
        or revoked.get("outcome") != "revoked"
    ):
        raise AssertionError("canary attendee context revoke diverged")
    _remember_private(sentinels, close_key, revoke_key)
    return outcome, revision_effect, published_bytes, published_chunks


def _validate_fixture_diagnostic(diagnostic: Mapping[str, object] | None) -> None:
    if (
        not isinstance(diagnostic, Mapping)
        or diagnostic.get("kind") != "attendee_deployment_canary_fixture"
        or diagnostic.get("lifecycle") != "closed"
        or diagnostic.get("worker_capacity") != WORKER_CAPACITY
        or diagnostic.get("ready_workers") != 0
        or diagnostic.get("active_workers") != 0
        or diagnostic.get("available_workers") != 0
        or diagnostic.get("accepted_dispatches") != 1
        or diagnostic.get("completed_dispatches") != 1
        or diagnostic.get("ambiguous_dispatches") != 0
        or diagnostic.get("locally_forced_workers") != 0
    ):
        raise AssertionError("canary fixture resources did not converge")


def _build_report(
    *,
    scope: str,
    manifest: Mapping[str, object],
    provider_settings: Any,
    evidence: _CanaryEvidence,
) -> dict[str, object]:
    reference = provider_settings.model_reference
    return {
        "kind": REPORT_KIND,
        "schema": REPORT_SCHEMA,
        "scope": scope,
        "production_capability_evidence": PRODUCTION_CAPABILITY_EVIDENCE,
        "live_provider_execution_evidence": scope == LIVE_SCOPE,
        "contract": {
            "name": CONTRACT_NAME,
            "revision": CONTRACT_REVISION,
            "compatibility": CONTRACT_COMPATIBILITY,
            "backend_commit": manifest["backend_commit"],
            "manifest_sha256": MANIFEST_SHA256,
            "openapi_sha256": OPENAPI_SHA256,
            "generated_client_sha256": GENERATED_CLIENT_SHA256,
            "tree_files": CONTRACT_FILES,
            "tree_sha256": CONTRACT_TREE_SHA256,
        },
        "configuration": {
            "provider": reference.provider,
            "model": reference.model,
            "max_output_tokens": provider_settings.max_output_tokens,
        },
        "privacy": {
            "notice_version": LIVE_NOTICE_VERSION,
            "policy_id": LIVE_POLICY_ID,
        },
        "limits": {
            "physical_workers": WORKER_CAPACITY,
            "commands_per_session": COMMAND_LIMIT,
            "logical_provider_attempts_per_command": PROVIDER_ATTEMPT_LIMIT,
            "output_bytes_per_command": CANARY_MAX_OUTPUT_BYTES_PER_COMMAND,
            "output_chunks_per_command": CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND,
        },
        "evidence": {
            "provider_client_ready": True,
            "commands_submitted": 1,
            "terminal_outcome": evidence.terminal_outcome,
            "revision_effect": evidence.revision_effect,
            "usage_accounting": "settled_reservation",
            "publication_complete": True,
            "published_bytes": evidence.published_bytes,
            "published_chunks": evidence.published_chunks,
            "snapshot_validated": True,
            "events_validated": True,
            "transcript_validated": True,
            "session_closed": True,
            "context_revoked": True,
            "supervisor_exit_code": evidence.supervisor_exit_code,
            "process_group_converged": True,
        },
        "summary": {
            "passed": True,
            "elapsed_ms": evidence.elapsed_ms,
        },
    }


def _reject_private_report_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_REPORT_KEYS:
                raise ValueError("private key crossed canary evidence")
            _reject_private_report_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private_report_keys(item)


def _bounded_public_text(value: object, *, maximum: int = 512) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= maximum
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def _validate_report(report: Mapping[str, object], *, sentinels: Sequence[str]) -> None:
    _reject_private_report_keys(report)
    if set(report) != _REPORT_ROOT_KEYS:
        raise ValueError("canary report root is not allowlisted")
    scope = report.get("scope")
    if (
        report.get("kind") != REPORT_KIND
        or report.get("schema") != REPORT_SCHEMA
        or scope not in {LIVE_SCOPE, FIXTURE_SCOPE}
        or report.get("production_capability_evidence") is not False
        or report.get("live_provider_execution_evidence") is not (scope == LIVE_SCOPE)
    ):
        raise ValueError("canary report identity diverged")

    contract = report.get("contract")
    if not isinstance(contract, Mapping) or set(contract) != {
        "name",
        "revision",
        "compatibility",
        "backend_commit",
        "manifest_sha256",
        "openapi_sha256",
        "generated_client_sha256",
        "tree_files",
        "tree_sha256",
    }:
        raise ValueError("canary contract evidence is not allowlisted")
    if (
        contract.get("name") != CONTRACT_NAME
        or contract.get("revision") != CONTRACT_REVISION
        or contract.get("compatibility") != CONTRACT_COMPATIBILITY
        or contract.get("manifest_sha256") != MANIFEST_SHA256
        or contract.get("openapi_sha256") != OPENAPI_SHA256
        or contract.get("generated_client_sha256") != GENERATED_CLIENT_SHA256
        or contract.get("tree_files") != CONTRACT_FILES
        or contract.get("tree_sha256") != CONTRACT_TREE_SHA256
    ):
        raise ValueError("canary contract evidence diverged")
    backend_commit = contract.get("backend_commit")
    if (
        not isinstance(backend_commit, str)
        or len(backend_commit) != 40
        or any(character not in "0123456789abcdef" for character in backend_commit)
    ):
        raise ValueError("canary backend commit is invalid")

    configuration = report.get("configuration")
    if not isinstance(configuration, Mapping) or set(configuration) != {
        "provider",
        "model",
        "max_output_tokens",
    }:
        raise ValueError("canary configuration evidence is not allowlisted")
    if (
        not _bounded_public_text(configuration.get("provider"))
        or not _bounded_public_text(configuration.get("model"))
        or isinstance(configuration.get("max_output_tokens"), bool)
        or not isinstance(configuration.get("max_output_tokens"), int)
        or not 1 <= configuration["max_output_tokens"] <= CANARY_MAX_OUTPUT_TOKENS
    ):
        raise ValueError("canary configuration evidence is invalid")
    if report.get("privacy") != {
        "notice_version": LIVE_NOTICE_VERSION,
        "policy_id": LIVE_POLICY_ID,
    }:
        raise ValueError("canary privacy evidence diverged")
    if report.get("limits") != {
        "physical_workers": WORKER_CAPACITY,
        "commands_per_session": COMMAND_LIMIT,
        "logical_provider_attempts_per_command": PROVIDER_ATTEMPT_LIMIT,
        "output_bytes_per_command": CANARY_MAX_OUTPUT_BYTES_PER_COMMAND,
        "output_chunks_per_command": CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND,
    }:
        raise ValueError("canary limit evidence diverged")

    evidence = report.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != {
        "provider_client_ready",
        "commands_submitted",
        "terminal_outcome",
        "revision_effect",
        "usage_accounting",
        "publication_complete",
        "published_bytes",
        "published_chunks",
        "snapshot_validated",
        "events_validated",
        "transcript_validated",
        "session_closed",
        "context_revoked",
        "supervisor_exit_code",
        "process_group_converged",
    }:
        raise ValueError("canary execution evidence is not allowlisted")
    for name in (
        "provider_client_ready",
        "publication_complete",
        "snapshot_validated",
        "events_validated",
        "transcript_validated",
        "session_closed",
        "context_revoked",
        "process_group_converged",
    ):
        if evidence.get(name) is not True:
            raise ValueError("canary execution evidence diverged")
    if (
        evidence.get("commands_submitted") != 1
        or evidence.get("terminal_outcome") != "completed"
        or evidence.get("revision_effect") not in {"committed", "none", "rejected"}
        or evidence.get("usage_accounting") != "settled_reservation"
        or evidence.get("supervisor_exit_code") != 128 + signal.SIGTERM
    ):
        raise ValueError("canary terminal evidence diverged")
    for name in ("published_bytes", "published_chunks"):
        value = evidence.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1
            <= value
            <= (
                CANARY_MAX_OUTPUT_BYTES_PER_COMMAND
                if name == "published_bytes"
                else CANARY_MAX_OUTPUT_CHUNKS_PER_COMMAND
            )
        ):
            raise ValueError("canary publication evidence is invalid")

    summary = report.get("summary")
    if (
        not isinstance(summary, Mapping)
        or set(summary) != {"passed", "elapsed_ms"}
        or summary.get("passed") is not True
        or isinstance(summary.get("elapsed_ms"), bool)
        or not isinstance(summary.get("elapsed_ms"), int)
        or not 0 <= summary["elapsed_ms"] <= 600_000
    ):
        raise ValueError("canary summary diverged")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    for sentinel in sentinels:
        if sentinel and sentinel in encoded:
            raise AssertionError("private canary value crossed report output")


def _execute_canary(
    arguments: argparse.Namespace,
    *,
    scope: str = LIVE_SCOPE,
    command_factory: Callable[
        [_CanaryConfiguration, _PrivateFiles, tuple[str, str, str]], list[str]
    ] = _live_deployment_command,
    expect_fixture_diagnostic: bool = False,
) -> dict[str, object]:
    if getattr(arguments, "acknowledge_provider_spend", False) is not True:
        raise ValueError("canary provider spend was not acknowledged")
    if scope not in {LIVE_SCOPE, FIXTURE_SCOPE}:
        raise ValueError("canary scope is invalid")
    if (scope == FIXTURE_SCOPE) is not expect_fixture_diagnostic:
        raise ValueError("canary fixture evidence mode diverged")
    started = time.monotonic()
    configuration, deployment, client_module, manifest, provider_settings = _preflight(
        arguments
    )
    sentinels = [
        str(configuration.profile_path),
        configuration.profile_digest,
        str(configuration.credential_file),
        CANARY_COMMAND,
    ]
    with (
        tempfile.TemporaryDirectory(prefix="dreamland-attendee-live-canary-") as tmp,
        ExitStack() as cleanup,
    ):
        private = _initialize_private_files(Path(tmp), deployment)
        _remember_private(
            sentinels,
            str(private.root),
            str(private.token_file),
            str(private.bootstrap_file),
            str(private.artifact_root),
            private.bearer,
            private.bff_authority,
        )
        availability = _availability_arguments(deployment)
        process = _DeploymentProcess(
            command=command_factory(configuration, private, availability),
            private_root=private.root,
        )
        cleanup.callback(process.force_stop)
        process.start()
        reservation = _validate_announcement(process.announcement, configuration)
        client = client_module.ProductSessionPreviewClient(
            process.base_url,
            private.bearer,
        )
        outcome, revision_effect, published_bytes, published_chunks = (
            _drive_generated_client_workflow(
                client_module,
                client,
                reservation=reservation,
                sentinels=sentinels,
            )
        )
        diagnostic = process.stop(expect_fixture_diagnostic=expect_fixture_diagnostic)
        if expect_fixture_diagnostic:
            _validate_fixture_diagnostic(diagnostic)
        evidence = _CanaryEvidence(
            terminal_outcome=outcome,
            revision_effect=revision_effect,
            published_bytes=published_bytes,
            published_chunks=published_chunks,
            supervisor_exit_code=128 + signal.SIGTERM,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
            fixture_diagnostic=diagnostic,
        )
        report = _build_report(
            scope=scope,
            manifest=manifest,
            provider_settings=provider_settings,
            evidence=evidence,
        )
        _validate_report(report, sentinels=sentinels)
        return report


def _run_with_signal_cleanup(
    action: Callable[[], dict[str, object]],
) -> dict[str, object]:
    global _DEFERRED_CANARY_SIGNAL, _SIGNAL_DEFERRAL_DEPTH
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("canary signals require the main thread")
    if _SIGNAL_DEFERRAL_DEPTH or _DEFERRED_CANARY_SIGNAL is not None:
        raise RuntimeError("canary signal state is not clean")
    prior_handlers: dict[int, Any] = {}
    interrupted: list[int | None] = [None]

    def interrupt(signum: int, _frame: object) -> None:
        global _DEFERRED_CANARY_SIGNAL
        if interrupted[0] is None:
            interrupted[0] = signum
            if _SIGNAL_DEFERRAL_DEPTH:
                _DEFERRED_CANARY_SIGNAL = signum
                return
            raise _CanarySignal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        return action()
    finally:
        _SIGNAL_DEFERRAL_DEPTH = 0
        _DEFERRED_CANARY_SIGNAL = None
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)


def _canary_output_tokens(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "max output tokens must be a positive integer"
        ) from error
    if not 1 <= parsed <= CANARY_MAX_OUTPUT_TOKENS:
        raise argparse.ArgumentTypeError(
            f"max output tokens must be between 1 and {CANARY_MAX_OUTPUT_TOKENS}"
        )
    return parsed


def _profile_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != len("sha256:") + 64
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise argparse.ArgumentTypeError("profile digest must be canonical sha256")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=(
            "Run one explicit real-provider command through the private supervised "
            "attendee deployment."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="spend at most one logical provider attempt")
    run.add_argument(
        "--acknowledge-provider-spend",
        action="store_true",
        required=True,
    )
    run.add_argument("--contract-root", type=Path, default=DEFAULT_CONTRACT_ROOT)
    run.add_argument("--profile-path", type=Path, required=True)
    run.add_argument("--profile-digest", type=_profile_digest, required=True)
    run.add_argument("--provider-credential-file", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--max-output-tokens", type=_canary_output_tokens, required=True)
    run.add_argument("--system-prompt", default="system.md")
    run.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
    )
    run.add_argument(
        "--reasoning-summary",
        choices=("auto", "concise", "detailed"),
    )
    run.add_argument("--verbosity", choices=("low", "medium", "high"))
    run.add_argument("--speed", choices=("fast",))
    run.add_argument("--prompt-cache", choices=("automatic", "off"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments: argparse.Namespace | None = None
    try:
        arguments = _parser().parse_args(argv)
        report = _run_with_signal_cleanup(lambda: _execute_canary(arguments))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except _CanarySignal as interruption:
        return 128 + interruption.signum
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    except Exception:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "attendee_live_canary_failed",
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
