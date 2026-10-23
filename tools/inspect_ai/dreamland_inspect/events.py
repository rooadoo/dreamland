"""Command event records captured from DREAMLAND client sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """User-visible command execution artifact for evaluation."""

    command_id: str
    command: str
    stdout: str
    stderr: str = ""
    exit_status: int = 0
    duration_ms: int | None = None
    session_id: str | None = None
    hostname: str | None = None
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandEvent:
        return cls(
            command_id=str(data.get("command_id") or data.get("id") or ""),
            command=str(data.get("command") or ""),
            stdout=str(data.get("stdout") or ""),
            stderr=str(data.get("stderr") or ""),
            exit_status=int(data.get("exit_status") or 0),
            duration_ms=_optional_int(data.get("duration_ms")),
            session_id=_optional_str(data.get("session_id")),
            hostname=_optional_str(data.get("hostname")),
            cwd=_optional_str(data.get("cwd")),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "command_id": self.command_id,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_status": self.exit_status,
        }
        if self.duration_ms is not None:
            data["duration_ms"] = self.duration_ms
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.hostname is not None:
            data["hostname"] = self.hostname
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


def load_events_jsonl(path: str | Path) -> list[CommandEvent]:
    events: list[CommandEvent] = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"invalid JSONL at line {line_number}: expected object")
            events.append(CommandEvent.from_dict(data))
    return events


def write_events_jsonl(path: str | Path, events: list[CommandEvent]) -> None:
    with Path(path).open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event.to_dict(), sort_keys=True))
            stream.write("\n")


def append_event_jsonl(path: str | Path, event: CommandEvent) -> None:
    event_path = Path(path)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event.to_dict(), sort_keys=True))
        stream.write("\n")


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
