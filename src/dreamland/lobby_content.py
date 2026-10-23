"""Bounded, plain-text operator copy, captured once per parent connection."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

MAX_CONTENT_BYTES = 16_384
LANGFUSE_DISCLOSURE = "Diagnostic/model traces are also stored in the configured Langfuse tracing service."


class LobbyContentError(ValueError):
    pass


def read_bounded_file(path: Path, *, max_bytes: int, name: str) -> bytes:
    """Read one regular-file descriptor without blocking on FIFOs or devices."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LobbyContentError(f"{name} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise LobbyContentError(f"{name} exceeds {max_bytes} bytes")
        return data
    finally:
        os.close(descriptor)


def validate_public_text(
    value: object,
    *,
    name: str,
    max_length: int,
    multiline: bool = False,
) -> str:
    """Reject control, bidi, and invisible formatting characters at publication."""
    if not isinstance(value, str) or not value.strip():
        raise LobbyContentError(f"{name} must be nonempty plain text")
    if len(value) > max_length:
        raise LobbyContentError(f"{name} exceeds {max_length} characters")
    for char in value:
        if multiline and char == "\n":
            continue
        if unicodedata.category(char).startswith("C") or char in "\u2028\u2029":
            raise LobbyContentError(
                f"{name} contains unsupported control or formatting characters"
            )
    return value.strip()


@dataclass(frozen=True, slots=True)
class LobbyNotice:
    version: str
    welcome: str
    guidelines: str
    contact: str


class LobbyContent:
    def __init__(
        self,
        welcome_path: Path | None = None,
        guidelines_path: Path | None = None,
        contact_path: Path | None = None,
        *,
        langfuse_enabled: bool = False,
    ) -> None:
        self.welcome_path = welcome_path
        self.guidelines_path = guidelines_path
        self.contact_path = contact_path
        self.langfuse_enabled = langfuse_enabled

    def snapshot(self) -> LobbyNotice:
        welcome = self._read("welcome", self.welcome_path)
        guidelines = self._read("guidelines", self.guidelines_path)
        contact = self._read("contact", self.contact_path, multiline=False)
        if self.langfuse_enabled:
            guidelines += "\n\n" + LANGFUSE_DISCLOSURE
        # The digest covers exactly the three displayed texts, including deployment
        # disclosure. Changing a file never mutates a previously returned notice.
        payload = json.dumps([welcome, guidelines, contact], ensure_ascii=True)
        version = hashlib.sha256(payload.encode("ascii")).hexdigest()
        return LobbyNotice(
            version=version, welcome=welcome, guidelines=guidelines, contact=contact
        )

    @staticmethod
    def _read(name: str, path: Path | None, *, multiline: bool = True) -> str:
        try:
            if path is None:
                value = (
                    files("dreamland.lobby_text")
                    .joinpath(f"{name}.txt")
                    .read_text(encoding="utf-8")
                )
            else:
                data = read_bounded_file(
                    path, max_bytes=MAX_CONTENT_BYTES, name=f"lobby {name}"
                )
                value = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LobbyContentError(f"could not read UTF-8 lobby {name} text") from exc
        value = value.replace("\r\n", "\n").strip("\n")
        return validate_public_text(
            value,
            name=f"lobby {name}",
            max_length=MAX_CONTENT_BYTES if multiline else 256,
            multiline=multiline,
        )
