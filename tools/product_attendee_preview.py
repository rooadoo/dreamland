#!/usr/bin/env python3
"""Serve the provider-free Phase 7C public-attendee integration preview.

This executable is explicit, loopback-only BFF composition.  Importing it
creates no listener, worker, provider, credential, profile lookup, or enabled
product capability.  The ordinary DREAMLAND runtime does not import it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from dreamland.product.attendee import (
    AttendeeBffAuthoritySeed,
    AttendeeContextPolicy,
    AttendeeSessionCoreService,
    PublicAttendeeSessionService,
)
from dreamland.product.core import ProductCorePolicy
from dreamland.product.launch_selection import (
    LaunchOfferingSeed,
    LaunchRevisionSeed,
    LaunchSelectionActorSeed,
    LaunchSelectionError,
    LaunchSelectionOrigin,
    LaunchSelectionPolicy,
    LaunchSelectionRegistry,
)
from dreamland.product.lifecycle import (
    ActorAuthoritySeed,
    ImmutableRevisionSeed,
    LifecyclePolicy,
    ProductLifecycleError,
    ProductOperation,
    ProductRole,
)


def _load_tool(name: str) -> ModuleType:
    path = Path(__file__).with_name(name + ".py")
    specification = importlib.util.spec_from_file_location(
        f"dreamland_{name}_for_attendee_preview",
        path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_SELECTION: Any = _load_tool("product_launch_selection_preview")
_COMBINED: Any = _SELECTION._COMBINED
_STAGE: Any = _SELECTION._STAGE
_CORE: Any = _SELECTION._CORE
_LIFECYCLE: Any = _SELECTION._LIFECYCLE

CONTRACT_REVISION = "0.7.0-rc.3"
PREVIEW_EXECUTOR = "deterministic-attendee-no-provider-v1"
ATTENDEE_CONTEXTS_PATH = "/product/v1/attendee/contexts"
ATTENDEE_LAUNCH_OPTIONS_PATH = "/product/v1/attendee/launch-options"
ATTENDEE_LAUNCH_SELECTIONS_PATH = "/product/v1/attendee/launch-selections"
PRIVATE_BFF_AUTHORITY = "bff-authority:attendee-preview-gateway-00000001"
ATTENDEE_SELECTION_ID = "launch-selection:attendee_authored_preview_00000001"
ATTENDEE_NOTICE_VERSION = "notice:attendee-preview-v1"
ATTENDEE_REVISION_LABEL = "Pinned authored attendee preview"
ATTENDEE_CONTEXT_CAPACITY = 4
SCENARIOS = ("attendee-happy",)
MILLISECONDS_PER_MINUTE = 60_000

_GATEWAY_CONTEXT_PATTERN = re.compile(r"^gateway-context:[A-Za-z0-9_-]{24,128}$")
_LAUNCH_SELECTION_ID_PATTERN = re.compile(r"^launch-selection:[A-Za-z0-9_-]{24,128}$")
_NOTICE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class _RegistryTokens:
    """Give the attendee registry the exact adjacent-core epoch."""

    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch
        self._sequence = 0

    def __call__(self, purpose: str) -> str:
        if purpose == "epoch":
            return self._service_epoch
        if purpose != "launch-grant":
            raise ValueError("unsupported attendee-selection token purpose")
        self._sequence += 1
        return f"launch-grant:attendee_preview_candidate_{self._sequence:08d}"


class _CoreTokens:
    """Retain hardened preview token shapes while pinning one service epoch."""

    def __init__(self, service_epoch: str) -> None:
        self._service_epoch = service_epoch
        self._delegate = _LIFECYCLE.PreviewTokens(7)

    def __call__(self, purpose: str) -> str:
        if purpose == "service_epoch":
            return self._service_epoch
        return self._delegate(purpose)


class _SessionRoutingService:
    """Route shared session operations without exposing the attendee core."""

    def __init__(
        self,
        *,
        stage_service: Any,
        attendee_service: PublicAttendeeSessionService,
        presenter_actor_context: str = _SELECTION.PRESENTER_CONTEXT,
    ) -> None:
        if stage_service.service_epoch != attendee_service.service_epoch:
            raise ValueError("preview session services must share one epoch")
        self._stage = stage_service
        self._attendee = attendee_service
        self._presenter_actor_context = presenter_actor_context

    @property
    def service_epoch(self) -> str:
        return self._attendee.service_epoch

    def create(self, **kwargs: object) -> dict[str, object]:
        return self._call("create", kwargs)

    def current(self, **kwargs: object) -> dict[str, object]:
        return self._call("current", kwargs)

    def snapshot(self, **kwargs: object) -> dict[str, object]:
        return self._call("snapshot", kwargs)

    def submit_command(self, **kwargs: object) -> dict[str, object]:
        return self._call("submit_command", kwargs)

    def cancel_command(self, **kwargs: object) -> dict[str, object]:
        return self._call("cancel_command", kwargs)

    def command_result(self, **kwargs: object) -> dict[str, object]:
        return self._call("command_result", kwargs)

    def events(self, **kwargs: object) -> dict[str, object]:
        return self._call("events", kwargs)

    def transcript(self, **kwargs: object) -> dict[str, object]:
        return self._call("transcript", kwargs)

    def reset(self, **kwargs: object) -> dict[str, object]:
        return self._call("reset", kwargs)

    def switch(self, **kwargs: object) -> dict[str, object]:
        return self._call("switch", kwargs)

    def close(self, **kwargs: object) -> dict[str, object]:
        return self._call("close", kwargs)

    def _call(
        self,
        operation: str,
        kwargs: Mapping[str, object],
    ) -> dict[str, object]:
        actor = kwargs.get("actor_authority")
        target = self._attendee
        if isinstance(actor, str) and hmac.compare_digest(
            actor,
            self._presenter_actor_context,
        ):
            target = self._stage
        elif not self._attendee.owns_actor_authority(actor):
            raise ProductLifecycleError(
                code="authority_denied",
                message="The requested product resource is unavailable.",
                status=403,
            )
        method = getattr(target, operation)
        return method(**dict(kwargs))


class AttendeePreviewHarness(_SELECTION.LaunchSelectionPreviewHarness):
    """Own separate Presenter and attendee services on one admission ledger."""

    def __init__(self, scenario: str, *, clock: Any | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown attendee preview scenario")
        # Keep all 21 predecessor routes available through the proven harness.
        super().__init__(_SELECTION.SCENARIOS[0], clock=clock)
        self.scenario = scenario
        self.stage_service = self.service
        self.presenter_actor_context = _SELECTION.PRESENTER_CONTEXT
        self.bff_authority = PRIVATE_BFF_AUTHORITY
        self.attendee_service = self._build_attendee_service()
        # Generic product-session routes select the owning facade from the
        # actor authority.  Attendee traffic never reaches the generic core,
        # while predecessor Presenter sessions retain all RC2 operations.
        self.service = _SessionRoutingService(
            stage_service=self.stage_service,
            attendee_service=self.attendee_service,
            presenter_actor_context=self.presenter_actor_context,
        )

    def _build_attendee_service(self) -> PublicAttendeeSessionService:
        now_ms = self.clock.now_ms()
        actor_expiry_ms = now_ms + 24 * 60 * MILLISECONDS_PER_MINUTE
        document = _authored_document()
        revision = _revision_seed(document)
        actors = tuple(
            LaunchSelectionActorSeed(
                authority=f"actor:attendee_preview_context_{index:04d}",
                actor_id=f"attendee-preview-{index:04d}",
                role=ProductRole.ATTENDEE,
                expires_at_ms=actor_expiry_ms,
            )
            for index in range(1, ATTENDEE_CONTEXT_CAPACITY + 1)
        )
        clock_ms = self.clock.now_ms
        registry = LaunchSelectionRegistry(
            revisions=(revision,),
            offerings=(
                LaunchOfferingSeed(
                    offering_id="offering:attendee-authored-preview",
                    launch_selection_id=ATTENDEE_SELECTION_ID,
                    revision_id=revision.revision_id,
                    origin=LaunchSelectionOrigin.AUTHORED_PROFILE_SNAPSHOT,
                    title="Authored attendee world",
                    source_label="Authored",
                    revision_label=ATTENDEE_REVISION_LABEL,
                    role=ProductRole.ATTENDEE,
                    allowed_operations=frozenset(
                        {ProductOperation.CREATE, ProductOperation.SWITCH}
                    ),
                    limits={
                        "absolute_ttl_ms": 20 * MILLISECONDS_PER_MINUTE,
                        "idle_ttl_ms": 10 * MILLISECONDS_PER_MINUTE,
                        "max_commands": 8,
                        "max_command_bytes": 4_096,
                        "command_reserved_tokens": 32,
                        "command_reserved_spend_units": 32,
                        "command_max_attempts": 1,
                        "command_deadline_ms": 10_000,
                    },
                    policy_id="policy:attendee-preview-v1",
                    notice_version=ATTENDEE_NOTICE_VERSION,
                    expires_at_ms=now_ms + 60 * MILLISECONDS_PER_MINUTE,
                    launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                ),
            ),
            actors=actors,
            policy=LaunchSelectionPolicy(
                max_revisions=4,
                max_offerings=4,
                max_actor_authorities=ATTENDEE_CONTEXT_CAPACITY,
                max_active_launch_grants=16,
                max_idempotency_records=64,
                max_token_history=128,
                max_revision_bytes=4 * 1024 * 1024,
                max_launch_grant_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idempotency_ttl_ms=30 * MILLISECONDS_PER_MINUTE,
            ),
            allowed_operation_families=frozenset(
                {ProductOperation.CREATE, ProductOperation.SWITCH}
            ),
            clock_ms=clock_ms,
            monotonic_ms=clock_ms,
            token_factory=_RegistryTokens(self.stage_service.service_epoch),
        )
        core = AttendeeSessionCoreService(
            revisions=(ImmutableRevisionSeed(revision.revision_id, document),),
            actors=tuple(
                ActorAuthoritySeed(
                    token=actor.authority,
                    actor_id=actor.actor_id,
                    role=actor.role,
                    expires_at_ms=actor.expires_at_ms,
                )
                for actor in actors
            ),
            executor=_CORE.DeterministicPreviewExecutor(),
            submit_task=self.submit_task,
            lifecycle_policy=LifecyclePolicy(
                absolute_ttl_ms=20 * MILLISECONDS_PER_MINUTE,
                idle_ttl_ms=10 * MILLISECONDS_PER_MINUTE,
                tombstone_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                failed_operation_ttl_ms=5 * MILLISECONDS_PER_MINUTE,
                max_active_sessions=4,
                max_tombstones=8,
                max_idempotency_records=128,
                max_launch_grants=16,
                max_actor_authorities=ATTENDEE_CONTEXT_CAPACITY,
                max_revisions=4,
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
            # Sharing the existing Stage ledger proves attendee sessions use
            # STANDARD admission and cannot consume its Presenter reservation.
            admission=self.stage_service.admission,
            clock_ms=clock_ms,
            monotonic_ms=clock_ms,
            token_factory=_CoreTokens(self.stage_service.service_epoch),
        )
        return PublicAttendeeSessionService(
            registry=registry,
            core=core,
            context_actors=actors,
            bff_authorities=(
                AttendeeBffAuthoritySeed(
                    authority=PRIVATE_BFF_AUTHORITY,
                    authority_id="bff:attendee-preview",
                    expires_at_ms=actor_expiry_ms,
                ),
            ),
            policy=AttendeeContextPolicy(
                max_contexts=ATTENDEE_CONTEXT_CAPACITY,
                context_ttl_ms=60 * MILLISECONDS_PER_MINUTE,
                max_bootstrap_idempotency_records=16,
                max_exchange_idempotency_records=64,
                admission_opens_at_ms=now_ms,
                admission_closes_at_ms=now_ms + 12 * 60 * MILLISECONDS_PER_MINUTE,
            ),
        )


class AttendeePreviewServer(_SELECTION.LaunchSelectionPreviewServer):
    """The RC2 transport plus three private attendee facade routes."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bearer: str,
        scenario: str,
        contract_root: Path,
        harness_factory: Callable[[str], AttendeePreviewHarness] = (
            AttendeePreviewHarness
        ),
    ) -> None:
        super().__init__(
            address,
            bearer=bearer,
            scenario=scenario,
            contract_root=contract_root,
            harness_factory=harness_factory,
        )
        # The parent chooses its own handler explicitly; replace it before any
        # request is accepted.  HTTPServer stores the class as a plain field.
        self.RequestHandlerClass = AttendeePreviewHandler


class AttendeePreviewHandler(_SELECTION.LaunchSelectionPreviewHandler):
    server: AttendeePreviewServer
    server_version = "DREAMLANDAttendeePreview/0.7"

    def _additional_get_operations(self) -> frozenset[str]:
        return super()._additional_get_operations() | frozenset(
            {"attendee-launch-options"}
        )

    def _additional_post_operations(self) -> frozenset[str]:
        return super()._additional_post_operations() | frozenset(
            {"attendee-launch-selection"}
        )

    def _route(self) -> tuple[str, str | None, str | None, str]:
        parsed = urlsplit(self.path)
        if parsed.fragment:
            return "not-found", None, None, parsed.query
        if parsed.path == ATTENDEE_CONTEXTS_PATH:
            # Reusing the transport's canonical create operation gives this
            # independently named mutation its contract-required HTTP 201.
            return "create", None, None, parsed.query
        if parsed.path == ATTENDEE_LAUNCH_OPTIONS_PATH:
            return "attendee-launch-options", None, None, parsed.query
        if parsed.path == ATTENDEE_LAUNCH_SELECTIONS_PATH:
            return "attendee-launch-selection", None, None, parsed.query
        if parsed.path in {_CORE.SESSIONS_PATH, _CORE.CURRENT_PATH} or (
            parsed.path.startswith(_CORE.SESSION_PATH_PREFIX)
        ):
            # Presenter Stage intentionally hides generic create/current/switch
            # in its narrower facade.  RC3 needs those published shared routes
            # for attendee sessions and retains them for Presenter sessions.
            return _CORE.ProductCorePreviewHandler._route(self)
        return super()._route()

    def _dispatch_get(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if operation == "attendee-launch-options":
            if _CORE._query_pairs(raw_query):
                raise _not_found()
            expected_epoch, actor_context = self._read_context()
            try:
                domain = self.server.preview.attendee_service.options(
                    actor_authority=actor_context,
                    expected_epoch=expected_epoch,
                )
            except LaunchSelectionError as error:
                raise _selection_lifecycle_error(error) from None
            return _success(domain, request_id)

        path = urlsplit(self.path).path
        if path == _STAGE.STAGE_CURRENT_PATH and operation == "current":
            if _CORE._query_pairs(raw_query):
                raise _not_found()
            expected_epoch, actor_context = self._read_context()
            domain = self.server.preview.stage_service.current(
                actor_authority=actor_context,
                expected_epoch=expected_epoch,
            )
            return _success(domain, request_id)
        if operation in {"xray", "xray-detail"}:
            return self._dispatch_stage_xray(
                operation,
                reference,
                command_id,
                raw_query,
                request_id,
            )
        return super()._dispatch_get(
            operation, reference, command_id, raw_query, request_id
        )

    def _dispatch_post(
        self,
        operation: str,
        reference: str | None,
        command_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if urlsplit(self.path).path == ATTENDEE_CONTEXTS_PATH:
            return self._bootstrap_context(raw_query, request_id)
        if operation == "attendee-launch-selection":
            return self._exchange_attendee_selection(raw_query, request_id)

        path = urlsplit(self.path).path
        if path == _STAGE.STAGE_LAUNCH_PATH and operation == "create":
            value = self._dispatch_stage_create(raw_query, request_id)
            if self.server.preview.should_drop_stage_launch_response():
                self._drop_connection()
                return None
            return value
        if (
            path.startswith(_STAGE.STAGE_SESSION_PREFIX)
            and path.endswith("/replace")
            and operation == "switch"
        ):
            return self._dispatch_stage_replace(reference, raw_query, request_id)
        return super()._dispatch_post(
            operation, reference, command_id, raw_query, request_id
        )

    def _bootstrap_context(
        self, raw_query: str, request_id: str
    ) -> Mapping[str, object]:
        if _CORE._query_pairs(raw_query):
            raise _not_found()
        expected_epoch = self._required_header(
            _CORE.EXPECTED_EPOCH_HEADER,
            _CORE.SERVICE_EPOCH_PATTERN,
        )
        idempotency_key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _CORE.IDEMPOTENCY_KEY_PATTERN,
        )
        body = self._read_json_object(frozenset({"gateway_context_id"}))
        gateway_context_id = body["gateway_context_id"]
        if (
            not isinstance(gateway_context_id, str)
            or _GATEWAY_CONTEXT_PATTERN.fullmatch(gateway_context_id) is None
        ):
            raise _invalid_request("The attendee gateway context is invalid.")
        try:
            domain = self.server.preview.attendee_service.bootstrap_context(
                bff_authority=self.server.preview.bff_authority,
                expected_epoch=expected_epoch,
                gateway_context_id=gateway_context_id,
                idempotency_key=idempotency_key,
            )
        except LaunchSelectionError as error:
            raise _selection_lifecycle_error(error) from None
        expires_at = domain.get("actor_expires_at", domain.get("expires_at"))
        projection = {
            "kind": domain.get("kind"),
            "service_epoch": domain.get("service_epoch"),
            "replayed": domain.get("replayed"),
            "authority_class": "attendee",
            "actor_context": domain.get("actor_context"),
            "actor_expires_at": expires_at,
        }
        return _success(projection, request_id)

    def _exchange_attendee_selection(
        self, raw_query: str, request_id: str
    ) -> Mapping[str, object]:
        if _CORE._query_pairs(raw_query):
            raise _not_found()
        expected_epoch, actor_context = self._read_context()
        idempotency_key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER,
            _CORE.IDEMPOTENCY_KEY_PATTERN,
        )
        body = self._read_json_object(
            frozenset({"launch_selection_id", "notice_version_presented"})
        )
        launch_selection_id = body["launch_selection_id"]
        notice_version = body["notice_version_presented"]
        if (
            not isinstance(launch_selection_id, str)
            or _LAUNCH_SELECTION_ID_PATTERN.fullmatch(launch_selection_id) is None
            or not isinstance(notice_version, str)
            or _NOTICE_VERSION_PATTERN.fullmatch(notice_version) is None
        ):
            raise _invalid_request("The attendee launch request is invalid.")
        try:
            domain = self.server.preview.attendee_service.exchange(
                actor_authority=actor_context,
                expected_epoch=expected_epoch,
                launch_selection_id=launch_selection_id,
                notice_version_presented=notice_version,
                idempotency_key=idempotency_key,
            )
        except LaunchSelectionError as error:
            raise _selection_lifecycle_error(error) from None
        return _success(domain, request_id)

    def _dispatch_stage_create(
        self, raw_query: str, request_id: str
    ) -> Mapping[str, object]:
        if _CORE._query_pairs(raw_query):
            raise _not_found()
        expected_epoch, actor_context = self._read_context()
        key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER, _CORE.IDEMPOTENCY_KEY_PATTERN
        )
        body = self._read_json_object(frozenset({"launch_grant"}))
        launch_grant = body["launch_grant"]
        if (
            not isinstance(launch_grant, str)
            or _CORE.LAUNCH_GRANT_PATTERN.fullmatch(launch_grant) is None
        ):
            raise _invalid_request("The launch grant is invalid.")
        domain = self.server.preview.stage_service.create(
            actor_authority=actor_context,
            launch_grant=launch_grant,
            expected_epoch=expected_epoch,
            idempotency_key=key,
        )
        return _success(domain, request_id)

    def _dispatch_stage_replace(
        self,
        reference: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if reference is None or _CORE._query_pairs(raw_query):
            raise _not_found()
        expected_epoch, actor_context, session_authority = self._session_context()
        key = self._required_header(
            _CORE.IDEMPOTENCY_HEADER, _CORE.IDEMPOTENCY_KEY_PATTERN
        )
        body = self._read_json_object(
            frozenset({"expected_control_revision", "launch_grant"})
        )
        control_revision = _CORE._required_generation(
            body["expected_control_revision"], allow_zero=False
        )
        launch_grant = body["launch_grant"]
        if (
            not isinstance(launch_grant, str)
            or _CORE.LAUNCH_GRANT_PATTERN.fullmatch(launch_grant) is None
        ):
            raise _invalid_request("The launch grant is invalid.")
        domain = self.server.preview.stage_service.switch(
            actor_authority=actor_context,
            session_authority=session_authority,
            reference=reference,
            expected_epoch=expected_epoch,
            idempotency_key=key,
            expected_control_revision=control_revision,
            launch_grant=launch_grant,
        )
        return _success(domain, request_id)

    def _dispatch_stage_xray(
        self,
        operation: str,
        reference: str | None,
        entity_id: str | None,
        raw_query: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if reference is None:
            raise _not_found()
        expected_epoch, actor_context, session_authority = self._session_context()
        query = _CORE._query_pairs(raw_query)
        if operation == "xray":
            if query:
                raise _not_found()
            result = self.server.preview.stage_service.xray_snapshot(
                actor_authority=actor_context,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
            )
        else:
            if entity_id is None or set(query) != {
                "accepted_state_revision",
                "xray_stream_id",
                "xray_sequence",
            }:
                raise _invalid_request(
                    "The Stage X-ray detail precondition is invalid."
                )
            try:
                accepted_revision = _COMBINED._xray_counter(
                    query["accepted_state_revision"], allow_zero=True
                )
                sequence = _COMBINED._xray_counter(
                    query["xray_sequence"], allow_zero=False
                )
                stream_id = query["xray_stream_id"]
                if _COMBINED.XRAY_STREAM_PATTERN.fullmatch(stream_id) is None:
                    raise ValueError("invalid X-ray stream")
            except ValueError as error:
                raise _invalid_request(
                    "The Stage X-ray detail precondition is invalid."
                ) from error
            result = self.server.preview.stage_service.xray_detail(
                actor_authority=actor_context,
                session_authority=session_authority,
                reference=reference,
                expected_epoch=expected_epoch,
                entity_id=entity_id,
                expected_accepted_state_revision=accepted_revision,
                expected_xray_stream_id=stream_id,
                expected_xray_sequence=sequence,
            )
        return _success(result, request_id)


def _success(domain: Mapping[str, object], request_id: str) -> dict[str, object]:
    response = _CORE._decorate_success(domain, request_id=request_id)
    response.pop("transport_request_id", None)
    return response


def _not_found() -> Exception:
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus.NOT_FOUND, "not_found", "Unavailable route."
    )


def _invalid_request(message: str) -> Exception:
    return _LIFECYCLE.PreviewTransportError(
        HTTPStatus.BAD_REQUEST, "invalid_request", message
    )


def _selection_lifecycle_error(error: LaunchSelectionError) -> ProductLifecycleError:
    translated = {
        "request_invalid": "invalid_request",
        "service_epoch_changed": "session_lost",
        "selection_unavailable": "launch_selection_unavailable",
        "capacity_exhausted": "launch_selection_capacity_exhausted",
        "context_expired": "authority_denied",
    }.get(error.code, error.code)
    detail = _CORE.SAFE_ERROR_DETAILS.get(translated)
    if detail is None:
        raise RuntimeError("unexpected attendee domain error")
    status, message, retry, retry_after_ms = detail
    return ProductLifecycleError(
        code=translated,
        message=message,
        status=status,
        retry=retry,
        retry_after_ms=retry_after_ms,
    )


def _revision_seed(document: Mapping[str, object]) -> LaunchRevisionSeed:
    canonical = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return LaunchRevisionSeed(
        revision_id=f"world-revision:sha256:{digest}",
        document=document,
    )


def _authored_document() -> dict[str, object]:
    return {
        "kind": "dreamland.authored_world_revision",
        "schema_revision": "1",
        "profile": {
            "level": "world",
            "state": {
                "world_state": {
                    "name": "authored-attendee-preview",
                    "entry_host": "public-console",
                    "active_host": "public-console",
                    "hosts_by_name": {
                        "public-console": {
                            "hostname": "public-console",
                            "login_banner": "Attendee console ready.\n",
                            "purpose": {"theme": "Authored attendee preview"},
                        }
                    },
                }
            },
        },
    }


def _configure_transport() -> None:
    """Configure only the dynamically isolated explicit preview stack."""
    _SELECTION._configure_transport()
    for module in (_SELECTION, _COMBINED, _STAGE, _CORE):
        module.CONTRACT_REVISION = CONTRACT_REVISION
    _STAGE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_EXECUTOR = PREVIEW_EXECUTOR
    _CORE.PREVIEW_PROVIDER_CALLS = False
    _CORE.PREVIEW_TEST_ONLY = True
    additions = {
        "attendee_context_capacity_exhausted": (
            503,
            "Attendee context capacity is exhausted for this service epoch.",
            "none",
            None,
        ),
        "attendee_context_idempotency_capacity_exhausted": (
            503,
            "Attendee context replay capacity is temporarily exhausted.",
            "after_delay",
            1_000,
        ),
        "notice_version_mismatch": (
            409,
            "The presented privacy notice version does not match this offering.",
            "none",
            None,
        ),
        "privacy_notice_unavailable": (
            503,
            "The attendee privacy notice is unavailable.",
            "none",
            None,
        ),
        "service_not_open": (
            503,
            "Attendee admission is not open yet.",
            "after_delay",
            1_000,
        ),
        "service_closed": (
            410,
            "Attendee admission is closed.",
            "none",
            None,
        ),
    }
    for code, detail in additions.items():
        existing = _CORE.SAFE_ERROR_DETAILS.get(code)
        if existing is not None and existing != detail:
            raise RuntimeError("attendee safe error configuration conflicts")
        _CORE.SAFE_ERROR_DETAILS[code] = detail


def validate_contract_root(contract_root: Path) -> Path:
    root = contract_root.resolve()
    tool_path = Path(__file__).with_name("product_attendee_contract.py")
    specification = importlib.util.spec_from_file_location(
        "dreamland_product_attendee_preview_contract_validator",
        tool_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("attendee contract validator is unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        result = validator.validate_contract(root)
    except Exception as error:
        raise ValueError("attendee contract failed validation") from error
    finally:
        sys.modules.pop(specification.name, None)
    if (
        result.get("contract_revision") != CONTRACT_REVISION
        or result.get("implementation_status") != "integration_preview_only"
        or result.get("capabilities_enabled") != 0
        or result.get("routes") != 24
    ):
        raise ValueError("attendee contract is incompatible")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if any(
        not isinstance(value, Mapping) or value.get("enabled") is not False
        for value in manifest.get("capabilities", {}).values()
    ):
        raise ValueError("attendee capabilities must remain disabled")
    return root


def build_preview_server(
    *,
    host: str,
    port: int,
    bearer: str,
    scenario: str,
    contract_root: Path,
) -> AttendeePreviewServer:
    if not _LIFECYCLE._loopback_host(host):
        raise ValueError("attendee preview supports only loopback hosts")
    if not 0 <= port <= 65_535:
        raise ValueError("preview port must be between 0 and 65535")
    _LIFECYCLE._validate_preview_bearer(bearer)
    if scenario not in SCENARIOS:
        raise ValueError("unknown attendee preview scenario")
    root = validate_contract_root(contract_root)
    _configure_transport()
    return AttendeePreviewServer(
        (host, port),
        bearer=bearer,
        scenario=scenario,
        contract_root=root,
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
                "capabilities_enabled": 0,
                "contract_revision": CONTRACT_REVISION,
                "executor": PREVIEW_EXECUTOR,
                "host": address,
                "integration_only": True,
                "live_capability_evidence": False,
                "normal_runtime_composition": False,
                "port": bound_port,
                "provider_calls": False,
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
        description="Serve the provider-free public-attendee preview."
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
            else _LIFECYCLE.load_preview_bearer_file(arguments.token_file)
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
        print(f"Attendee preview error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
