#!/usr/bin/env python3
"""Provider-free deployment subprocess for the attendee canary tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from dreamland.product.execution import (
    ProductExecutionRequest,
    ProductExecutionResult,
    ProductOutputChunk,
    ProductOutputPublisher,
    ProductOutputStream,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_PATH = REPOSITORY_ROOT / "tools" / "product_attendee_security_deployment.py"
FIXTURE_KIND = "attendee_deployment_canary_fixture"
EXPECTED_COMMAND = "uname -a"
EXPECTED_OUTPUT_BYTES = 64 * 1_024
EXPECTED_OUTPUT_CHUNKS = 240


def _load_deployment() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "dreamland_attendee_canary_fixture_deployment",
        DEPLOYMENT_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("canary fixture deployment is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


class DeterministicCanaryExecutor:
    """One finite executor that cannot construct or contact a provider."""

    def execute(
        self,
        request: ProductExecutionRequest,
        emit: ProductOutputPublisher,
    ) -> ProductExecutionResult:
        if request.canonical_command != EXPECTED_COMMAND:
            return ProductExecutionResult.failed("provider_request_invalid")
        chunks = [
            f"{index:03d}:" + ("é" * 133) + "\n"
            for index in range(EXPECTED_OUTPUT_CHUNKS - 1)
        ]
        chunks.append("239:" + ("é" * 381) + "\n")
        if (
            len(chunks) != EXPECTED_OUTPUT_CHUNKS
            or sum(len(chunk.encode("utf-8")) for chunk in chunks)
            != EXPECTED_OUTPUT_BYTES
        ):
            raise RuntimeError("provider-free boundary fixture diverged")
        for index, chunk in enumerate(chunks):
            stream = (
                ProductOutputStream.STDOUT
                if index % 2 == 0
                else ProductOutputStream.STDERR
            )
            if emit(ProductOutputChunk(stream, chunk)) is False:
                raise RuntimeError("provider-free boundary publication was rejected")
        return ProductExecutionResult.completed_exited(
            exit_status=0,
            usage_tokens=request.max_tokens,
            usage_spend_units=request.max_spend_units,
        )


class DeterministicCanaryWorkerFactory:
    """Picklable worker factory that deliberately ignores credential content."""

    def __init__(self, **_values: object) -> None:
        pass

    def __call__(self) -> DeterministicCanaryExecutor:
        return DeterministicCanaryExecutor()


def _diagnostic(pool: object) -> dict[str, object]:
    snapshot = pool.snapshot()
    return {
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
    }


def main(argv: list[str] | None = None) -> int:
    deployment = _load_deployment()
    arguments = deployment._parser().parse_args(argv)
    if arguments.command != "serve":
        raise ValueError("canary fixture supports serve only")
    if (
        arguments.host != "127.0.0.1"
        or arguments.port != 0
        or arguments.worker_capacity != 1
        or arguments.max_commands != 1
        or arguments.max_output_chunks_per_command != EXPECTED_OUTPUT_CHUNKS
    ):
        raise ValueError("canary fixture requires exact loopback limits")

    window = deployment._SECURITY.AttendeeSecurityAvailabilityWindow(
        admission_opens_at_ms=arguments.availability_opens_at,
        admission_closes_at_ms=arguments.availability_closes_at,
        recovery_closes_at_ms=arguments.recovery_closes_at,
    )
    settings = deployment._LIVE.AttendeeLiveProviderSettings(
        model=arguments.model,
        system_prompt_name=arguments.system_prompt,
        max_output_tokens=arguments.max_output_tokens,
        reasoning_effort=arguments.reasoning_effort,
        reasoning_summary=arguments.reasoning_summary,
        verbosity=arguments.verbosity,
        speed=arguments.speed,
        prompt_cache=arguments.prompt_cache,
    )
    server = deployment.build_deployment_server(
        host=arguments.host,
        port=arguments.port,
        bearer=deployment._LIFECYCLE.load_preview_bearer_file(arguments.token_file),
        bff_authority=deployment._SECURITY.load_bootstrap(arguments.bootstrap_file),
        scenario=arguments.scenario,
        contract_root=arguments.contract_root,
        artifact_root=arguments.artifact_root,
        authored_profile_path=arguments.authored_profile_path,
        authored_profile_digest=arguments.authored_profile_digest,
        provider_credential_file=arguments.provider_credential_file,
        provider_settings=settings,
        availability_window=window,
        worker_capacity=arguments.worker_capacity,
        max_commands_per_session=arguments.max_commands,
        max_output_chunks_per_command=(arguments.max_output_chunks_per_command),
        provider_worker_factory=DeterministicCanaryWorkerFactory,
    )
    pool = server.preview.runtime.provider_pool
    try:
        return_code = deployment.serve_built_server(server)
    finally:
        print(json.dumps(_diagnostic(pool), sort_keys=True), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
