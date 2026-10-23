"""Command-event recording helpers for DREAMLAND evaluation runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dreamland_inspect.events import CommandEvent, append_event_jsonl


class CommandEventRecorder:
    """Append DREAMLAND command executions to a JSONL artifact."""

    def __init__(self, path: str | Path, *, reset: bool = False) -> None:
        self.path = Path(path)
        if reset:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def append(self, event: CommandEvent) -> CommandEvent:
        append_event_jsonl(self.path, event)
        return event

    def record(
        self,
        *,
        command_id: str,
        command: str,
        stdout: str,
        stderr: str = "",
        exit_status: int = 0,
        duration_ms: int | None = None,
        session_id: str | None = None,
        hostname: str | None = None,
        cwd: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandEvent:
        event = CommandEvent(
            command_id=command_id,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_status=exit_status,
            duration_ms=duration_ms,
            session_id=session_id,
            hostname=hostname,
            cwd=cwd,
            metadata=metadata or {},
        )
        return self.append(event)
