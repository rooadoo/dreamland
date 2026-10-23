"""DREAMLAND client MCP driver for Inspect evaluation runs."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from dreamland_inspect.drivers.recording import CommandEventRecorder
from dreamland_inspect.events import CommandEvent


class ClientMcpSession(Protocol):
    async def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> Any:
        """Call one tool on the client MCP endpoint."""


@dataclass(frozen=True, slots=True)
class ClientMcpRun:
    session_id: str
    username: str
    hostname: str | None
    cwd: str | None
    state_profile: str | None


class ClientMcpDriver:
    """Run client MCP commands and record user-visible command events."""

    def __init__(
        self,
        *,
        session: ClientMcpSession,
        recorder: CommandEventRecorder,
        run_id: str,
    ) -> None:
        self._session = session
        self._recorder = recorder
        self._run_id = run_id
        self._run: ClientMcpRun | None = None
        self._command_index = 0

    @property
    def run(self) -> ClientMcpRun | None:
        return self._run

    @property
    def command_count(self) -> int:
        return self._command_index

    def record_tool_rejection(
        self,
        command: str,
        *,
        message: str,
        exit_status: int = 2,
    ) -> CommandEvent:
        if self._run is None:
            raise RuntimeError("client MCP run has not been started")

        self._command_index += 1
        command_id = f"{self._run_id}-{self._command_index:04d}"
        return self._recorder.record(
            command_id=command_id,
            command=command,
            stdout="",
            stderr=message,
            exit_status=exit_status,
            duration_ms=0,
            session_id=self._run.session_id,
            hostname=self._run.hostname,
            cwd=self._run.cwd,
            metadata={
                "source": "inspect_harness",
                "run_id": self._run_id,
                "state_profile": self._run.state_profile,
                "rejected_by_harness": True,
                "username": self._run.username,
                "active_program": "bash",
                "context": {
                    "username": self._run.username,
                    "hostname": self._run.hostname,
                    "cwd": self._run.cwd,
                    "active_program": "bash",
                    "label": (
                        f"shell on {self._run.username}@"
                        f"{self._run.hostname}:{self._run.cwd}"
                    ),
                },
            },
        )

    async def start(self, *, username: str = "user") -> ClientMcpRun:
        created = await self._call("client_session_create", {"username": username})
        self._run = ClientMcpRun(
            session_id=str(created["session_id"]),
            username=str(created.get("username") or username),
            hostname=_optional_str(created.get("hostname")),
            cwd=_optional_str(created.get("cwd")),
            state_profile=_optional_str(created.get("state_profile")),
        )
        return self._run

    async def run_command(
        self,
        command: str,
        *,
        timeout_seconds: int | float | None = None,
    ) -> CommandEvent:
        if self._run is None:
            raise RuntimeError("client MCP run has not been started")

        self._command_index += 1
        command_id = f"{self._run_id}-{self._command_index:04d}"
        started = time.monotonic()
        try:
            result = await self._run_client_command(
                command=command,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as exc:
            return self._record_timeout(
                command_id=command_id,
                command=command,
                started=started,
                timeout_seconds=timeout_seconds,
                exc=exc,
            )
        except Exception as exc:
            if not _is_timeout_exception(exc):
                raise
            return self._record_timeout(
                command_id=command_id,
                command=command,
                started=started,
                timeout_seconds=timeout_seconds,
                exc=exc,
            )

        duration_ms = round((time.monotonic() - started) * 1000)
        return self._recorder.record(
            command_id=command_id,
            command=str(result.get("command") or command),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            exit_status=int(result.get("exit_status") or 0),
            duration_ms=duration_ms,
            session_id=str(result.get("session_id") or self._run.session_id),
            hostname=_optional_str(result.get("hostname")) or self._run.hostname,
            cwd=_optional_str(result.get("cwd")) or self._run.cwd,
            metadata={
                "source": "client_mcp",
                "run_id": self._run_id,
                "state_profile": self._run.state_profile,
                "interrupted": bool(result.get("interrupted") or False),
                "signal": result.get("signal"),
                "username": _optional_str(result.get("username")),
                "active_program": _optional_str(result.get("active_program")),
                "prompt": _optional_str(result.get("prompt")),
                "context": _command_context(result),
            },
        )

    async def _run_client_command(
        self,
        *,
        command: str,
        timeout_seconds: int | float | None,
    ) -> dict[str, Any]:
        if timeout_seconds is None or timeout_seconds <= 0:
            return await self._call(
                "client_run_command",
                {"session_id": self._run.session_id, "command": command},
            )
        async with asyncio.timeout(timeout_seconds):
            return await self._call(
                "client_run_command",
                {"session_id": self._run.session_id, "command": command},
            )

    def _record_timeout(
        self,
        *,
        command_id: str,
        command: str,
        started: float,
        timeout_seconds: int | float | None,
        exc: BaseException,
    ) -> CommandEvent:
        timeout_label = (
            f"{timeout_seconds:g} seconds"
            if isinstance(timeout_seconds, int | float) and timeout_seconds > 0
            else "the configured timeout"
        )
        return self._recorder.record(
            command_id=command_id,
            command=command,
            stdout="",
            stderr=f"command timed out after {timeout_label}",
            exit_status=124,
            duration_ms=round((time.monotonic() - started) * 1000),
            session_id=self._run.session_id,
            hostname=self._run.hostname,
            cwd=self._run.cwd,
            metadata={
                "source": "client_mcp",
                "run_id": self._run_id,
                "state_profile": self._run.state_profile,
                "timed_out": True,
                "timeout_seconds": timeout_seconds,
                "timeout_error": type(exc).__name__,
            },
        )

    async def close(self) -> bool:
        if self._run is None:
            return False
        result = await self._call(
            "client_session_exit",
            {"session_id": self._run.session_id},
        )
        self._run = None
        return bool(result.get("closed"))

    async def _call(self, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
        result = await self._session.call_tool(name, arguments)
        return _tool_structured_content(result)


@asynccontextmanager
async def open_http_client_mcp(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: int | float | None = 65,
) -> AsyncIterator[ClientMcpSession]:
    """Open a streamable-HTTP client MCP session."""

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout_seconds,
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


def _tool_structured_content(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        if result.get("isError"):
            raise RuntimeError(_tool_error_message(result))
        structured = result.get("structuredContent", result)
        if isinstance(structured, dict):
            return dict(structured)
    if getattr(result, "isError", False):
        raise RuntimeError(_tool_error_message(result))
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return dict(structured)
    raise RuntimeError("client MCP tool returned no structured content")


def _tool_error_message(result: Any) -> str:
    content = (
        result.get("content")
        if isinstance(result, dict)
        else getattr(result, "content", None)
    )
    if content:
        first = content[0]
        if isinstance(first, dict):
            text = first.get("text")
        else:
            text = getattr(first, "text", None)
        if text:
            return str(text)
    return "client MCP tool call failed"


def _is_timeout_exception(exc: BaseException) -> bool:
    return type(exc).__name__ in {"ReadTimeout", "TimeoutError"}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _command_context(result: Mapping[str, object]) -> dict[str, object]:
    context = result.get("context")
    if isinstance(context, dict):
        return {str(key): item for key, item in context.items()}
    return {
        key: item
        for key in ("username", "hostname", "cwd", "active_program", "prompt")
        if (item := result.get(key)) is not None
    }
