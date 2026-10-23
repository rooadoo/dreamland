import asyncio
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlparse

from dreamland.admin_interface import AdminServiceError, DreamlandAdminService
from dreamland.config import ApiSettings, api_host_is_loopback
from dreamland.console_introspection import IntrospectionUnavailable
from dreamland.mcp_agent_guide import mcp_agent_guide_text
from dreamland.runtime_logging import configure_mcp_logging

LIFECYCLE_LOGGER = logging.getLogger("dreamland.lifecycle")
API_VERSION = "v0"
MCP_ENDPOINT = "/mcp"
CLIENT_MCP_ENDPOINT = "/client-mcp"
CLIENT_SESSION_TAG = "client"

CLIENT_MCP_GUIDE = """# Command Client MCP Guide

Use this endpoint to create a command session, run commands, and end the
session when finished.

Tools:

- client_session_create: start a command session.
- client_run_command: run one command in an existing session.
- client_session_exit: end one exact session id.

Only command output fields are returned. Treat stdout, stderr, exit_status,
interrupted, and signal as the command result.
"""


class _AdminDependencies(NamedTuple):
    FastAPI: Any
    FastMCP: Any
    JSONResponse: Any
    Request: Any
    StreamingResponse: Any
    StarletteHTTPException: type[Exception]
    uvicorn: Any


class DreamlandAdminAPIServer:
    def __init__(
        self,
        *,
        settings: ApiSettings,
        service: DreamlandAdminService,
    ) -> None:
        if not settings.enabled:
            raise ValueError("API server requires enabled API settings.")
        if settings.token is None:
            raise ValueError("API server requires a bearer token.")
        if not api_host_is_loopback(settings.host):
            raise ValueError("API server host must be a loopback address.")
        self.settings = settings
        self.service = service
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._startup_failed_event = threading.Event()
        self._startup_error: BaseException | None = None
        self._server: Any | None = None
        self._bound_port: int | None = None
        self._deps: _AdminDependencies | None = None

    @property
    def bound_port(self) -> int:
        return self._bound_port if self._bound_port is not None else self.settings.port

    def start_in_thread(self) -> threading.Thread:
        self._reset_startup_state()
        self._deps = _load_admin_dependencies()
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def serve_forever(self) -> None:
        self._reset_startup_state()
        LIFECYCLE_LOGGER.info(
            "Admin API server starting host=%s port=%s",
            self.settings.host,
            self.settings.port,
        )
        server: Any | None = None
        sock: socket.socket | None = None
        try:
            deps = self._deps or _load_admin_dependencies()
            sock = _bind_socket(self.settings.host, self.settings.port)
            self._bound_port = int(sock.getsockname()[1])
            app = _build_admin_app(
                service=self.service,
                token=self.settings.token or "",
                deps=deps,
            )
            config = deps.uvicorn.Config(
                app,
                host=self.settings.host,
                port=self._bound_port,
                log_level="warning",
                access_log=False,
                lifespan="on",
            )
            server = deps.uvicorn.Server(config)
            self._server = server
            self._ready_event.set()
            LIFECYCLE_LOGGER.info(
                "Admin API server listening host=%s port=%s",
                self.settings.host,
                self._bound_port,
            )
            asyncio.run(server.serve(sockets=[sock]))
        except BaseException as exc:
            if not self._ready_event.is_set():
                if sock is not None:
                    sock.close()
                self._startup_error = exc
                self._startup_failed_event.set()
                LIFECYCLE_LOGGER.exception(
                    "Admin API server failed to start host=%s port=%s",
                    self.settings.host,
                    self.settings.port,
                )
            else:
                LIFECYCLE_LOGGER.exception("Admin API server stopped after error")
            if threading.current_thread() is self._thread:
                return
            raise
        finally:
            self._server = None
            if server is not None:
                self._ready_event.clear()
            LIFECYCLE_LOGGER.info("Admin API server stopped")

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.is_set():
                return True
            if self._startup_failed_event.is_set():
                self._raise_startup_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._startup_failed_event.is_set():
                    self._raise_startup_error()
                return False
            self._ready_event.wait(min(remaining, 0.05))

    def request_stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True

    def stop(self) -> None:
        LIFECYCLE_LOGGER.info("Admin API server stopping")
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _reset_startup_state(self) -> None:
        self._ready_event.clear()
        self._startup_failed_event.clear()
        self._startup_error = None

    def _raise_startup_error(self) -> None:
        exc = self._startup_error
        if exc is None:
            raise RuntimeError("Dreamland Admin API server failed to start.")
        raise RuntimeError(
            f"Dreamland Admin API server failed to start: {exc}"
        ) from exc


def _load_admin_dependencies() -> _AdminDependencies:
    try:
        import uvicorn
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse
        from mcp.server.fastmcp import FastMCP
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError as exc:
        raise RuntimeError(
            "The admin API requires optional admin dependencies. "
            "Install DREAMLAND with the 'admin' extra to enable [api]."
        ) from exc
    return _AdminDependencies(
        FastAPI=FastAPI,
        FastMCP=FastMCP,
        JSONResponse=JSONResponse,
        Request=Request,
        StreamingResponse=StreamingResponse,
        StarletteHTTPException=StarletteHTTPException,
        uvicorn=uvicorn,
    )


def _bind_socket(host: str, port: int) -> socket.socket:
    errors: list[OSError] = []
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(socket.SOMAXCONN)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise OSError(f"Could not bind admin API socket for {host}:{port}")


def _build_admin_app(
    *,
    service: DreamlandAdminService,
    token: str,
    deps: _AdminDependencies,
) -> Any:
    mcp = _build_mcp_server(service=service, deps=deps)
    client_mcp = _build_client_mcp_server(service=service, deps=deps)

    @asynccontextmanager
    async def lifespan(app: object) -> Any:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            await stack.enter_async_context(client_mcp.session_manager.run())
            yield

    app = deps.FastAPI(lifespan=lifespan)
    Request = deps.Request

    @app.middleware("http")
    async def require_admin_auth(request: Any, call_next: Callable[[Any], Any]) -> Any:
        if not _authorized_header(request.headers.get("authorization", ""), token):
            return _error_response(
                deps,
                401,
                code="unauthorized",
                message="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if _is_mcp_path(request.url.path) and not _origin_allowed(
            request.headers.get("origin")
        ):
            return _error_response(
                deps,
                403,
                code="forbidden",
                message="origin not allowed",
            )
        if request.method not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "HEAD",
        }:
            return _error_response(
                deps,
                405,
                code="method_not_allowed",
                message="method not allowed",
            )
        return await call_next(request)

    @app.exception_handler(AdminServiceError)
    async def admin_service_error(request: object, exc: AdminServiceError) -> Any:
        return _error_response(
            deps,
            _status_for_error_code(exc.code),
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(IntrospectionUnavailable)
    async def introspection_unavailable(
        request: object,
        exc: IntrospectionUnavailable,
    ) -> Any:
        return _error_response(deps, 503, code="unavailable", message=str(exc))

    @app.exception_handler(ValueError)
    async def value_error(request: object, exc: ValueError) -> Any:
        return _error_response(deps, 400, code="bad_request", message=str(exc))

    @app.exception_handler(deps.StarletteHTTPException)
    async def http_error(request: object, exc: Exception) -> Any:
        status_code = int(getattr(exc, "status_code", 500))
        if status_code == 404:
            return _error_response(
                deps,
                404,
                code="not_found",
                message="endpoint not found",
            )
        if status_code == 405:
            return _error_response(
                deps,
                405,
                code="method_not_allowed",
                message="method not allowed",
            )
        return _error_response(
            deps,
            status_code,
            code="http_error",
            message=str(getattr(exc, "detail", "HTTP error")),
        )

    @app.get("/api/v0/health")
    async def health() -> Any:
        return _json_response(
            deps,
            {
                "status": "ok",
                "api_version": API_VERSION,
            },
        )

    @app.get("/api/v0/status")
    async def status() -> Any:
        return _json_response(deps, service.status())

    @app.get("/api/v0/capabilities")
    async def capabilities() -> Any:
        return _json_response(deps, service.capabilities())

    @app.get("/api/v0/config/effective")
    async def effective_config() -> Any:
        return _json_response(deps, service.config_effective())

    @app.get("/api/v0/logs")
    async def logs(request: Request) -> Any:  # type: ignore[valid-type]
        query = _query(request)
        return _json_response(
            deps,
            service.recent_logs(
                limit=_query_int(query, "limit", default=20),
                after_sequence=_query_optional_int(query, "after"),
            ),
        )

    @app.get("/api/v0/events")
    async def events(request: Request) -> Any:  # type: ignore[valid-type]
        query = _query(request)
        return _sse_response(
            deps,
            _log_event_stream(
                service=service,
                request=request,
                after_sequence=_query_int(query, "after", default=0),
                limit=_query_int(query, "limit", default=20),
                timeout_seconds=_query_int(query, "timeout", default=15),
                once=_query_bool(query, "once", default=False),
            ),
        )

    @app.get("/api/v0/sessions")
    async def sessions(request: Request) -> Any:  # type: ignore[valid-type]
        query = _query(request)
        return _json_response(
            deps,
            service.sessions(limit=_query_int(query, "limit", default=20)),
        )

    @app.get("/api/v0/live-sessions")
    async def live_sessions() -> Any:
        return _json_response(deps, service.live_sessions())

    @app.get("/api/v0/live-sessions/{session_id}")
    async def live_session(session_id: str) -> Any:
        return _json_response(deps, service.live_session(session_id))

    @app.get("/api/v0/sessions/{session_id}")
    async def session(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.session(
                session_id,
                historical_limit=_query_int(
                    query,
                    "historical_limit",
                    default=100,
                ),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/state")
    async def session_state(session_id: str) -> Any:
        return _json_response(deps, service.live_session_state(session_id))

    @app.get("/api/v0/sessions/{session_id}/trace")
    async def session_trace(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.session_trace(
                session_id=session_id,
                limit=_query_int(query, "limit", default=20),
                after_sequence=_query_optional_int(query, "after"),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/enrichment-events")
    async def session_enrichment_events(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.session_enrichment_events(
                session_id=session_id,
                job_id=_query_optional_text(query, "job_id"),
                limit=_query_int(query, "limit", default=20),
                after_sequence=_query_optional_int(query, "after"),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        service.session_event_stream(session_id)
        query = _query(request)
        return _sse_response(
            deps,
            _trace_event_stream(
                service=service,
                request=request,
                session_id=session_id,
                after_sequence=_query_int(query, "after", default=0),
                limit=_query_int(query, "limit", default=20),
                timeout_seconds=_query_int(query, "timeout", default=15),
                once=_query_bool(query, "once", default=False),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/commands")
    async def session_commands(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.list_historical_commands(
                session_id=session_id,
                limit=_query_int(query, "limit", default=20),
                filter_text=_query_optional_text(query, "filter"),
                sort=_query_text(query, "sort", default="time"),
                descending=_query_bool(query, "descending", default=True),
                include_io_summary=_query_bool(
                    query,
                    "include_io_summary",
                    default=True,
                ),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/commands/{command_id}")
    async def session_command(
        session_id: str,
        command_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.inspect_command(
                session_id=session_id,
                target=command_id,
                limit=_query_int(query, "limit", default=20),
                detail=_query_text(query, "detail", default="full"),
            ),
        )

    @app.get("/api/v0/sessions/{session_id}/state-delta")
    async def session_state_delta(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        query = _query(request)
        return _json_response(
            deps,
            service.state_delta(
                session_id=session_id,
                from_target=_query_optional_text(query, "from"),
                to_target=_query_text(query, "to", default="@-1"),
                limit=_query_int(query, "limit", default=20),
                sections=tuple(query.get("section", ())),
            ),
        )

    @app.get("/api/v0/state-profiles")
    async def state_profiles() -> Any:
        return _json_response(deps, service.list_state_profiles())

    @app.get("/api/v0/state-profiles/default")
    async def default_state_profile() -> Any:
        return _json_response(deps, service.default_state_profile())

    @app.get("/api/v0/state-profiles/{name}")
    async def state_profile(name: str) -> Any:
        return _json_response(deps, service.state_profile(name))

    @app.post("/api/v0/state-profiles")
    async def save_state_profile(request: Request) -> Any:  # type: ignore[valid-type]
        body = await _json_body(request)
        return _json_response(
            deps,
            service.save_state_profile(
                name=_required_text(body, "name"),
                level=_required_text(body, "level"),
                session_id=_required_text(body, "session_id"),
                comment=_optional_text_body(body, "comment"),
            ),
        )

    @app.post("/api/v0/sessions/{session_id}/state-profile")
    async def apply_state_profile(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.apply_state_profile(
                name=_required_text(body, "name"),
                session_id=session_id,
            ),
        )

    @app.put("/api/v0/state-profiles/default")
    async def set_default_state_profile(
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.set_default_state_profile(_required_text(body, "name")),
        )

    @app.delete("/api/v0/state-profiles/default")
    async def clear_default_state_profile() -> Any:
        return _json_response(deps, service.clear_default_state_profile())

    @app.delete("/api/v0/state-profiles/{name}")
    async def delete_state_profile(name: str) -> Any:
        return _json_response(deps, service.delete_state_profile(name))

    @app.post("/api/v0/spiritguide/profiles")
    async def create_spiritguide_profile(
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.create_spiritguide_profile(
                config_overrides=_optional_object_body(body, "config_overrides"),
                name=_required_text(body, "name"),
                instruction=_required_text(body, "instruction"),
                level=_optional_text_body(body, "level") or "world",
                dry_run=_body_bool(body, "dry_run", default=False),
                comment=_optional_text_body(body, "comment"),
            ),
        )

    @app.post("/api/v0/spiritguide/profiles/{name}")
    async def update_spiritguide_profile(
        name: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.update_spiritguide_profile(
                config_overrides=_optional_object_body(body, "config_overrides"),
                name=name,
                instruction=_required_text(body, "instruction"),
                level=_optional_text_body(body, "level"),
                dry_run=_body_bool(body, "dry_run", default=False),
                comment=_optional_text_body(body, "comment"),
            ),
        )

    @app.post("/api/v0/spiritguide/profiles/{source_name}/fork")
    async def fork_spiritguide_profile(
        source_name: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.fork_spiritguide_profile(
                config_overrides=_optional_object_body(body, "config_overrides"),
                source_name=source_name,
                name=_required_text(body, "name"),
                instruction=_optional_text_body(body, "instruction"),
                level=_optional_text_body(body, "level"),
                dry_run=_body_bool(body, "dry_run", default=False),
                comment=_optional_text_body(body, "comment"),
            ),
        )

    @app.post("/api/v0/sessions/{session_id}/spiritguide")
    async def spiritguide(
        session_id: str,
        request: Request,  # type: ignore[valid-type]
    ) -> Any:
        body = await _json_body(request)
        return _json_response(
            deps,
            service.run_spiritguide(
                session_id=session_id,
                instruction=_required_text(body, "instruction"),
                dry_run=_body_bool(body, "dry_run", default=False),
            ),
        )

    @app.post("/api/v0/introspection/flush")
    async def flush_introspection() -> Any:
        service.flush_introspection()
        return _json_response(deps, {"status": "ok"})

    @app.post("/api/v0/shutdown")
    async def shutdown() -> Any:
        return _json_response(deps, service.shutdown())

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def api_not_found(path: str) -> Any:
        return _error_response(
            deps,
            404,
            code="not_found",
            message="endpoint not found",
        )

    _include_mcp_routes(app, mcp.streamable_http_app())
    _include_mcp_routes(app, client_mcp.streamable_http_app())
    return app


def _build_mcp_server(
    *,
    service: DreamlandAdminService,
    deps: _AdminDependencies,
) -> Any:
    root_logger = logging.getLogger()
    root_handlers_before = tuple(root_logger.handlers)
    root_level_before = root_logger.level
    mcp = deps.FastMCP(
        "DREAMLAND Admin",
        json_response=True,
        streamable_http_path=MCP_ENDPOINT,
    )
    configure_mcp_logging(
        root_handlers_before=root_handlers_before,
        root_level_before=root_level_before,
    )

    @mcp.resource("dreamland://health")
    def mcp_health() -> str:
        """DREAMLAND API health."""
        return _json_text({"status": "ok", "api_version": API_VERSION})

    @mcp.resource("dreamland://status")
    def mcp_status() -> str:
        """DREAMLAND runtime status."""
        return _json_text(service.status())

    @mcp.resource("dreamland://capabilities")
    def mcp_capabilities() -> str:
        """DREAMLAND admin capability flags."""
        return _json_text(service.capabilities())

    @mcp.resource("dreamland://agent-guide")
    def mcp_agent_guide() -> str:
        """External-agent guide for using DREAMLAND MCP workflows."""
        return mcp_agent_guide_text()

    @mcp.resource("dreamland://config/effective")
    def mcp_config_effective() -> str:
        """Redacted effective DREAMLAND configuration."""
        return _json_text(service.config_effective())

    @mcp.resource("dreamland://logs")
    def mcp_logs() -> str:
        """Recent DREAMLAND runtime logs."""
        return _json_text(service.recent_logs(limit=20))

    @mcp.resource("dreamland://events")
    def mcp_events() -> str:
        """DREAMLAND event stream discovery metadata."""
        return _json_text(service.event_stream())

    @mcp.resource("dreamland://sessions")
    def mcp_sessions() -> str:
        """Combined live and Langfuse session summaries."""
        return _json_text(service.sessions(limit=20))

    @mcp.resource("dreamland://live-sessions")
    def mcp_live_sessions() -> str:
        """Live in-memory session summaries."""
        return _json_text(service.live_sessions())

    @mcp.resource("dreamland://live-sessions/{session_id}")
    def mcp_live_session(session_id: str) -> str:
        """Live in-memory session summary by session id."""
        return _json_text(service.live_session(session_id))

    @mcp.resource("dreamland://sessions/{session_id}")
    def mcp_session(session_id: str) -> str:
        """Unified live and historical session inspection by session id."""
        return _json_text(service.session(session_id, historical_limit=100))

    @mcp.resource("dreamland://sessions/{session_id}/events")
    def mcp_session_events(session_id: str) -> str:
        """DREAMLAND session event stream discovery metadata."""
        return _json_text(service.session_event_stream(session_id))

    @mcp.resource("dreamland://sessions/{session_id}/state")
    def mcp_session_state(session_id: str) -> str:
        """Live session state snapshot by session id."""
        return _json_text(service.live_session_state(session_id))

    @mcp.resource("dreamland://sessions/{session_id}/trace")
    def mcp_session_trace(session_id: str) -> str:
        """Recent live trace records by session id."""
        return _json_text(service.session_trace(session_id=session_id, limit=20))

    @mcp.resource("dreamland://sessions/{session_id}/enrichment-events")
    def mcp_session_enrichment_events(session_id: str) -> str:
        """Retained enrichment lifecycle events by session id."""
        return _json_text(
            service.session_enrichment_events(session_id=session_id, limit=20)
        )

    @mcp.resource("dreamland://sessions/{session_id}/commands")
    def mcp_session_commands(session_id: str) -> str:
        """Historical command summaries by session id."""
        return _json_text(
            service.list_historical_commands(session_id=session_id, limit=20)
        )

    @mcp.resource("dreamland://sessions/{session_id}/commands/{command_id}")
    def mcp_session_command(session_id: str, command_id: str) -> str:
        """Historical command inspection by session and command id."""
        return _json_text(
            service.inspect_command(
                session_id=session_id,
                target=command_id,
                limit=20,
                detail="compact",
            )
        )

    @mcp.resource("dreamland://sessions/{session_id}/state-delta")
    def mcp_session_state_delta(session_id: str) -> str:
        """Historical state delta for a session."""
        return _json_text(
            service.state_delta(
                session_id=session_id,
                from_target=None,
                to_target="@-1",
                limit=20,
            )
        )

    @mcp.resource("dreamland://state-profiles")
    def mcp_state_profiles() -> str:
        """Saved DREAMLAND state profile summaries."""
        return _json_text(service.list_state_profiles())

    @mcp.resource("dreamland://state-profiles/default")
    def mcp_default_state_profile() -> str:
        """Current default state profile."""
        return _json_text(service.default_state_profile())

    @mcp.resource("dreamland://state-profiles/{name}")
    def mcp_state_profile(name: str) -> str:
        """Saved state profile detail by name."""
        return _json_text(service.state_profile(name))

    @mcp.resource("dreamland://structured-failures")
    def mcp_structured_failures() -> str:
        """Captured structured command-agent failure summaries."""
        return _json_text(service.list_structured_failures())

    @mcp.resource("dreamland://structured-failures/{failure_id}")
    def mcp_structured_failure(failure_id: str) -> str:
        """Structured command-agent failure capsule by id."""
        return _json_text(service.structured_failure(failure_id))

    @mcp.resource("dreamland://structured-failures/{failure_id}/replay-parser")
    def mcp_structured_failure_replay_parser(failure_id: str) -> str:
        """Replay one structured failure raw response through the parser."""
        return _json_text(service.replay_structured_failure_parser(failure_id))

    @mcp.prompt(
        name="dreamland_session_testing_workflow",
        description="Guide an agent through native DREAMLAND MCP session testing.",
    )
    def dreamland_session_testing_workflow() -> str:
        return (
            "External-agent workflow for testing DREAMLAND through MCP:\n"
            "1. Read dreamland://agent-guide, dreamland://status, and "
            "dreamland://capabilities.\n"
            "2. Create a tagged session with dreamland_session_create. Include "
            "agent id, run id, and scenario id tags when possible.\n"
            "3. Save the full session_id. Automated agents should use full "
            "session ids and full direct command ids.\n"
            "4. Run ordinary user-visible commands with "
            "dreamland_session_run_command. Treat stdout, stderr, exit_status, "
            "interrupted, and signal as the result a real user would see.\n"
            "5. Save each returned command_id for correlation and later "
            "inspection.\n"
            "6. Inspect live behavior with dreamland_session_trace_read, then "
            "use dreamland_session_commands_list and "
            "dreamland_session_command_inspect with direct command ids.\n"
            "7. Do not run !dreamland inside shell sessions. Use explicit MCP "
            "resources or tools for admin and operator actions.\n"
            "8. Prefer DREAMLAND MCP while the session is live. Use Langfuse "
            "MCP mainly after live context is gone or for telemetry parity.\n"
            "9. Close sessions with dreamland_sessions_close when finished. "
            "Prefer exact session_ids with origin='mcp'. Use filtered cleanup "
            "only after dry_run=true and human/operator approval."
        )

    @mcp.prompt(
        name="dreamland_session_lifecycle_workflow",
        description="Guide an agent through tagged DREAMLAND session lifecycle cleanup.",
    )
    def dreamland_session_lifecycle_workflow() -> str:
        return (
            "External-agent lifecycle workflow for DREAMLAND MCP sessions:\n"
            "1. Tag every MCP-created test session with agent id, run id, and "
            "scenario id when possible.\n"
            "2. Use a label for stable run names. Use origin='mcp' for "
            "agent-owned cleanup.\n"
            "3. Review live sessions with dreamland_live_sessions_list or "
            "dreamland_sessions_list using tags, label, and origin filters.\n"
            "4. Prefer exact cleanup with dreamland_sessions_close using "
            "session_ids=[session_id], origin='mcp', and dry_run=false.\n"
            "5. Use tag, label, or origin-only cleanup only when exact session "
            "ids are unavailable or an operator asks for grouped cleanup.\n"
            "6. Before any non-id cleanup, call dreamland_sessions_close with "
            "dry_run=true and the intended filters, report matches, and get "
            "human/operator approval.\n"
            "7. Only after approval, repeat dreamland_sessions_close with "
            "dry_run=false and the same filters.\n"
            "8. Avoid abbreviated ids in automated workflows."
        )

    @mcp.prompt(
        name="dreamland_command_diagnostics_workflow",
        description="Guide an agent through MCP command diagnosis.",
    )
    def dreamland_command_diagnostics_workflow() -> str:
        return (
            "External-agent command diagnostics workflow:\n"
            "1. Start with the user-visible result from "
            "dreamland_session_run_command: command, stdout, stderr, "
            "exit_status, interrupted, signal, session_id, and command_id.\n"
            "2. Read live input/output records with "
            "dreamland_session_trace_read.\n"
            "3. Inspect the command by direct command_id with "
            "dreamland_session_command_inspect. Relative targets such as @-1 "
            "are mainly for human console use.\n"
            "4. Use dreamland_session_state_delta when the diagnosis depends "
            "on cwd, shell stack, environment, or other state changes.\n"
            "5. Call dreamland_introspection_flush before relying on recent "
            "historical Langfuse-backed inspection.\n"
            "6. Prefer DREAMLAND MCP for live sessions. Use Langfuse MCP for "
            "historical analysis or explicit telemetry parity checks.\n"
            "7. Report user-visible output separately from diagnostic metadata."
        )

    if service.mcp_write_tools:

        @mcp.tool()
        def dreamland_introspection_flush() -> dict[str, str]:
            """Flush pending Langfuse telemetry before historical inspection."""
            service.flush_introspection()
            return {"status": "ok"}

        @mcp.tool()
        def dreamland_logs_read(
            limit: int = 20,
            after_sequence: int | None = None,
        ) -> str:
            """Read DREAMLAND runtime logs with limit and cursor controls."""
            return _json_text(
                service.recent_logs(
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    after_sequence=_mcp_optional_nonnegative_int(
                        after_sequence,
                        "after_sequence",
                    ),
                )
            )

        @mcp.tool()
        def dreamland_sessions_list(
            limit: int = 20,
            tags: list[str] | None = None,
            label: str | None = None,
            origin: str | None = None,
        ) -> str:
            """List live and historical sessions with optional live metadata filters."""
            return _json_text(
                service.sessions(
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    tags=tags,
                    label=label,
                    origin=origin,
                )
            )

        @mcp.tool()
        def dreamland_live_sessions_list(
            tags: list[str] | None = None,
            label: str | None = None,
            origin: str | None = None,
        ) -> str:
            """List live sessions with optional tag, label, and origin filters."""
            return _json_text(
                service.live_sessions(tags=tags, label=label, origin=origin)
            )

        @mcp.tool()
        def dreamland_session_tag_groups(origin: str | None = None) -> str:
            """Group live sessions by tag, optionally filtered by origin."""
            return _json_text(service.live_session_tag_groups(origin=origin))

        @mcp.tool()
        def dreamland_session_inspect(
            session_id: str,
            historical_limit: int = 100,
        ) -> str:
            """Inspect unified live and historical session availability."""
            return _json_text(
                service.session(
                    session_id,
                    historical_limit=_mcp_nonnegative_int(
                        historical_limit,
                        "historical_limit",
                    ),
                )
            )

        @mcp.tool()
        def dreamland_session_trace_read(
            session_id: str,
            limit: int = 20,
            after_sequence: int | None = None,
        ) -> str:
            """Read live user-visible input/output trace records for a session."""
            return _json_text(
                service.session_trace(
                    session_id=session_id,
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    after_sequence=_mcp_optional_nonnegative_int(
                        after_sequence,
                        "after_sequence",
                    ),
                )
            )

        @mcp.tool()
        def dreamland_session_enrichment_events_read(
            session_id: str,
            limit: int = 20,
            job_id: str | None = None,
            after_sequence: int | None = None,
        ) -> str:
            """Read retained enrichment lifecycle events for a session or job."""
            return _json_text(
                service.session_enrichment_events(
                    session_id=session_id,
                    job_id=job_id,
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    after_sequence=_mcp_optional_nonnegative_int(
                        after_sequence,
                        "after_sequence",
                    ),
                )
            )

        @mcp.tool()
        def dreamland_session_commands_list(
            session_id: str,
            limit: int = 20,
            filter_text: str | None = None,
            sort: str = "time",
            descending: bool = True,
            include_io_summary: bool = True,
        ) -> str:
            """List command summaries keyed by direct command ids."""
            return _json_text(
                service.list_historical_commands(
                    session_id=session_id,
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    filter_text=filter_text,
                    sort=sort,
                    descending=descending,
                    include_io_summary=include_io_summary,
                )
            )

        @mcp.tool()
        def dreamland_session_command_inspect(
            session_id: str,
            target: str | None = None,
            limit: int = 20,
            detail: str = "compact",
        ) -> str:
            """Inspect one command by direct command_id; @ targets are for humans."""
            return _json_text(
                service.inspect_command(
                    session_id=session_id,
                    target=target,
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    detail=detail,
                )
            )

        @mcp.tool()
        def dreamland_session_state_delta(
            session_id: str,
            to_target: str = "@-1",
            from_target: str | None = None,
            limit: int = 20,
            sections: list[str] | None = None,
        ) -> str:
            """Inspect state changes between command ids or relative targets."""
            return _json_text(
                service.state_delta(
                    session_id=session_id,
                    from_target=from_target,
                    to_target=to_target,
                    limit=_mcp_nonnegative_int(limit, "limit"),
                    sections=tuple(sections or ()),
                )
            )

        @mcp.tool()
        def dreamland_session_create(
            username: str = "user",
            tags: list[str] | None = None,
            label: str | None = None,
            metadata: dict[str, object] | None = None,
            state_profile: str | None = None,
            config_overrides: dict[str, object] | None = None,
            model: str | None = None,
            openai_service_tier: str | None = None,
        ) -> str:
            """Create a session with an optional profile, command model, and serving tier.

            model uses provider:model and inherits the configured command model
            when omitted. openai_service_tier defaults to default (standard
            processing) for OpenAI if absent from both direct arguments and
            config_overrides.agent. Direct arguments alias those agent fields;
            conflicting values are rejected. Other role settings inherit from
            the process; nested null clears optional controls. Each session owns
            its runtime and returns the resolved evaluation_config.
            """
            return _json_text(
                service.create_mcp_session(
                    state_profile=state_profile,
                    config_overrides=config_overrides,
                    model=model,
                    openai_service_tier=openai_service_tier,
                    username=username,
                    tags=tags,
                    label=label,
                    metadata=metadata,
                )
            )

        @mcp.tool()
        def dreamland_session_run_command(session_id: str, command: str) -> str:
            """Run a user command and return stdout, stderr, status, and command_id."""
            return _json_text(
                service.run_mcp_session_command(
                    session_id=session_id,
                    command=command,
                )
            )

        @mcp.tool()
        def dreamland_sessions_close(
            session_ids: list[str] | None = None,
            tags: list[str] | None = None,
            label: str | None = None,
            origin: str | None = None,
            dry_run: bool = True,
        ) -> str:
            """Close live sessions; prefer exact ids with origin=mcp, dry-run filters."""
            return _json_text(
                service.close_live_sessions(
                    session_ids=session_ids,
                    tags=tags,
                    label=label,
                    origin=origin,
                    dry_run=dry_run,
                )
            )

        @mcp.tool()
        def dreamland_state_profile_save(
            name: str,
            level: str,
            session_id: str,
            comment: str | None = None,
        ) -> str:
            """Save a state profile from a live DREAMLAND session."""
            return _json_text(
                service.save_state_profile(
                    name=name,
                    level=level,
                    session_id=session_id,
                    comment=comment,
                )
            )

        @mcp.tool()
        def dreamland_state_profile_apply(name: str, session_id: str) -> str:
            """Apply a saved state profile to a live DREAMLAND session."""
            return _json_text(
                service.apply_state_profile(name=name, session_id=session_id)
            )

        @mcp.tool()
        def dreamland_state_profile_set_default(name: str) -> str:
            """Set the process default DREAMLAND state profile."""
            return _json_text(service.set_default_state_profile(name))

        @mcp.tool()
        def dreamland_state_profile_clear_default() -> str:
            """Clear the process default DREAMLAND state profile."""
            return _json_text(service.clear_default_state_profile())

        @mcp.tool()
        def dreamland_state_profile_delete(name: str) -> str:
            """Delete a saved DREAMLAND state profile."""
            return _json_text(service.delete_state_profile(name))

        @mcp.tool()
        def dreamland_spiritguide_profile_create(
            name: str,
            instruction: str,
            level: str = "world",
            dry_run: bool = False,
            comment: str | None = None,
            config_overrides: dict[str, object] | None = None,
        ) -> str:
            """Create a detached DREAMLAND state profile with Spiritguide."""
            return _json_text(
                service.create_spiritguide_profile(
                    config_overrides=config_overrides,
                    name=name,
                    instruction=instruction,
                    level=level,
                    dry_run=dry_run,
                    comment=comment,
                )
            )

        @mcp.tool()
        def dreamland_spiritguide_profile_update(
            name: str,
            instruction: str,
            level: str | None = None,
            dry_run: bool = False,
            comment: str | None = None,
            config_overrides: dict[str, object] | None = None,
        ) -> str:
            """Update a detached DREAMLAND state profile with Spiritguide."""
            return _json_text(
                service.update_spiritguide_profile(
                    config_overrides=config_overrides,
                    name=name,
                    instruction=instruction,
                    level=level,
                    dry_run=dry_run,
                    comment=comment,
                )
            )

        @mcp.tool()
        def dreamland_spiritguide_profile_fork(
            source_name: str,
            name: str,
            instruction: str | None = None,
            level: str | None = None,
            dry_run: bool = False,
            comment: str | None = None,
            config_overrides: dict[str, object] | None = None,
        ) -> str:
            """Fork a detached DREAMLAND state profile, optionally with Spiritguide."""
            return _json_text(
                service.fork_spiritguide_profile(
                    config_overrides=config_overrides,
                    source_name=source_name,
                    name=name,
                    instruction=instruction,
                    level=level,
                    dry_run=dry_run,
                    comment=comment,
                )
            )

        @mcp.tool()
        def dreamland_structured_failures_list() -> str:
            """List captured structured command-agent failure capsules."""
            return _json_text(service.list_structured_failures())

        @mcp.tool()
        def dreamland_structured_failure_show(failure_id: str) -> str:
            """Show one structured command-agent failure capsule by id."""
            return _json_text(service.structured_failure(failure_id))

        @mcp.tool()
        def dreamland_structured_failure_replay_parser(failure_id: str) -> str:
            """Replay one capsule raw response through the current parser."""
            return _json_text(service.replay_structured_failure_parser(failure_id))

        @mcp.tool()
        def dreamland_spiritguide(
            session_id: str,
            instruction: str,
            dry_run: bool = False,
        ) -> str:
            """Run the DREAMLAND spiritguide against a live session."""
            return _json_text(
                service.run_spiritguide(
                    session_id=session_id,
                    instruction=instruction,
                    dry_run=dry_run,
                )
            )

        @mcp.tool()
        def dreamland_shutdown() -> str:
            """Request shutdown of the running DREAMLAND process."""
            return _json_text(service.shutdown())

    return mcp


def _build_client_mcp_server(
    *,
    service: DreamlandAdminService,
    deps: _AdminDependencies,
) -> Any:
    mcp = deps.FastMCP(
        "Command Client",
        json_response=True,
        streamable_http_path=CLIENT_MCP_ENDPOINT,
    )

    @mcp.resource("client://guide")
    def client_guide() -> str:
        """Minimal command-session client guide."""
        return CLIENT_MCP_GUIDE.strip()

    if service.mcp_write_tools:

        @mcp.tool()
        def client_session_create(username: str = "user") -> dict[str, object]:
            """Start a command session."""
            try:
                result = service.create_mcp_session(
                    username=username,
                    tags=[CLIENT_SESSION_TAG],
                )
            except AdminServiceError as exc:
                raise ValueError(_client_error_message(exc)) from exc
            return {
                "session_id": result.session_id,
                "username": result.username,
                "hostname": result.hostname,
                "cwd": result.cwd,
                "state_profile": result.state_profile,
            }

        @mcp.tool()
        def client_run_command(session_id: str, command: str) -> dict[str, object]:
            """Run one command in an existing session."""
            try:
                result = service.run_mcp_session_command(
                    session_id=session_id,
                    command=command,
                )
            except AdminServiceError as exc:
                raise ValueError(_client_error_message(exc)) from exc
            return {
                "session_id": result.session_id,
                "command": result.command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_status": result.exit_status,
                "interrupted": result.interrupted,
                "signal": result.signal,
                "username": result.context.username,
                "hostname": result.context.hostname,
                "cwd": result.context.cwd,
                "active_program": result.context.active_program,
                "prompt": result.context.prompt,
                "context": {
                    "username": result.context.username,
                    "hostname": result.context.hostname,
                    "cwd": result.context.cwd,
                    "active_program": result.context.active_program,
                    "prompt": result.context.prompt,
                    "label": result.context.label,
                },
            }

        @mcp.tool()
        def client_session_exit(session_id: str) -> dict[str, object]:
            """End one exact session id."""
            try:
                result = service.close_live_sessions(
                    session_ids=[session_id],
                    tags=[CLIENT_SESSION_TAG],
                    origin="mcp",
                    dry_run=False,
                )
            except AdminServiceError as exc:
                raise ValueError(_client_error_message(exc)) from exc
            return {
                "session_id": session_id,
                "closed": session_id in result.closed,
            }

    return mcp


def _include_mcp_routes(app: Any, mcp_app: Any) -> None:
    mcp_app.router.redirect_slashes = False
    for route in mcp_app.routes:
        app.router.routes.append(route)


def _is_mcp_path(path: str) -> bool:
    return path.startswith(MCP_ENDPOINT) or path.startswith(CLIENT_MCP_ENDPOINT)


def _client_error_message(exc: AdminServiceError) -> str:
    if exc.code == "not_found":
        return "session not found"
    if exc.code == "bad_request":
        if "dreamland" in exc.message.lower():
            return "command is not available"
        return exc.message.replace("MCP", "client")
    if exc.code == "unavailable":
        return "client session service is not available"
    return "client session request failed"


def _authorized_header(header: str, token: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return secrets.compare_digest(value, token)


def _mcp_nonnegative_int(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _mcp_optional_nonnegative_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    return _mcp_nonnegative_int(value, name)


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlparse(origin)
    host = parsed.hostname
    return host is not None and api_host_is_loopback(host)


def _json_response(
    deps: _AdminDependencies,
    payload: object,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    return deps.JSONResponse(
        content=_to_json_value(payload),
        status_code=status_code,
        headers=headers,
    )


def _sse_response(deps: _AdminDependencies, stream: AsyncIterator[str]) -> Any:
    return deps.StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _log_event_stream(
    *,
    service: DreamlandAdminService,
    request: Any,
    after_sequence: int,
    limit: int,
    timeout_seconds: int,
    once: bool,
) -> AsyncIterator[str]:
    current_sequence = after_sequence
    timeout = 0.0 if once else float(max(1, timeout_seconds))
    while True:
        records = await asyncio.to_thread(
            service.wait_for_logs,
            after_sequence=current_sequence,
            limit=limit,
            timeout=timeout,
        )
        for record in records:
            current_sequence = max(current_sequence, record.sequence)
            yield _sse_event(
                event="log",
                event_id=str(record.sequence),
                payload={
                    "sequence": record.sequence,
                    "type": "log",
                    "source": "dreamland.runtime",
                    "data": {"text": record.text},
                },
            )
        if once or await request.is_disconnected():
            return
        if not records:
            yield ": heartbeat\n\n"


async def _trace_event_stream(
    *,
    service: DreamlandAdminService,
    request: Any,
    session_id: str,
    after_sequence: int,
    limit: int,
    timeout_seconds: int,
    once: bool,
) -> AsyncIterator[str]:
    current_sequence = after_sequence
    timeout = 0.0 if once else float(max(1, timeout_seconds))
    while True:
        records = await asyncio.to_thread(
            service.wait_for_session_trace,
            session_id=session_id,
            after_sequence=current_sequence,
            limit=limit,
            timeout=timeout,
        )
        for record in records:
            current_sequence = max(current_sequence, record.sequence)
            yield _sse_event(
                event="trace",
                event_id=str(record.sequence),
                payload={
                    "sequence": record.sequence,
                    "type": "trace",
                    "source": "dreamland.trace",
                    "data": {
                        "session_id": record.session_id,
                        "text": record.text,
                    },
                },
            )
        if once or await request.is_disconnected():
            return
        if not records:
            yield ": heartbeat\n\n"


def _sse_event(*, event: str, event_id: str, payload: object) -> str:
    text = _json_text(payload)
    return f"id: {event_id}\nevent: {event}\ndata: {text}\n\n"


def _error_response(
    deps: _AdminDependencies,
    status_code: int,
    *,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    return _json_response(
        deps,
        {"error": {"code": code, "message": message, "details": details or {}}},
        status_code=status_code,
        headers=headers,
    )


def _json_text(value: object) -> str:
    return json.dumps(_to_json_value(value), sort_keys=True)


def _to_json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _query(request: Any) -> dict[str, list[str]]:
    return parse_qs(str(request.url.query), keep_blank_values=True)


def _query_int(
    query: dict[str, list[str]],
    name: str,
    *,
    default: int,
) -> int:
    value = _query_optional_int(query, name)
    return default if value is None else value


def _query_optional_int(query: dict[str, list[str]], name: str) -> int | None:
    values = query.get(name)
    if not values or values[0] == "":
        return None
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _query_text(
    query: dict[str, list[str]],
    name: str,
    *,
    default: str,
) -> str:
    value = _query_optional_text(query, name)
    return default if value is None else value


def _query_optional_text(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values or values[0] == "":
        return None
    return values[0]


def _query_bool(
    query: dict[str, list[str]],
    name: str,
    *,
    default: bool,
) -> bool:
    value = _query_optional_text(query, name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


async def _json_body(request: Any) -> dict[str, object]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _required_text(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_object_body(
    body: dict[str, object], name: str
) -> dict[str, object] | None:
    value = body.get(name)
    if value is not None and not isinstance(value, dict):
        raise AdminServiceError("bad_request", f"{name} must be an object or null")
    return value


def _optional_text_body(body: dict[str, object], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    return value or None


def _body_bool(body: dict[str, object], name: str, *, default: bool) -> bool:
    value = body.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def _status_for_error_code(code: str) -> int:
    if code == "not_found":
        return 404
    if code == "unavailable":
        return 503
    if code == "provider_error":
        return 502
    return 400
