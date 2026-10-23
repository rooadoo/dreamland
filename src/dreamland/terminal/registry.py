from __future__ import annotations

import re
from collections.abc import Callable

from dreamland.models import InteractiveProgram
from dreamland.terminal.base import InteractiveEditor, SendText
from dreamland.terminal.readline import BashLineEditor, SecretLineEditor

EditorFactory = Callable[[SendText, str], InteractiveEditor]

EDITOR_FACTORIES: dict[str, EditorFactory] = {
    "bash": BashLineEditor,
}
_EXPLICIT_SECRET_PROMPT = re.compile(
    r"^(?:"
    r"(?:enter|retype)\s+(?:(?:same|new|old|current)\s+)*(?:unix\s+)?(?:password|passphrase)\b"
    r"|(?:\(current\)|current|new|old)\s+(?:unix\s+)?password\b"
    r"|\[sudo\]\s+password\b"
    r"|(?:password|passphrase|verification code|one-time password|otp)\s*:"
    r"|\S+\s+password:\s*$"
    r")",
    re.IGNORECASE,
)


def create_interactive_editor(
    program: InteractiveProgram,
    send: SendText,
    prompt: str,
) -> InteractiveEditor:
    if program_uses_secret_input(program, prompt):
        return SecretLineEditor(send, prompt)
    return editor_factory_for(program)(send, prompt)


def editor_factory_for(program: InteractiveProgram) -> EditorFactory:
    return EDITOR_FACTORIES.get(program.name, BashLineEditor)


def program_uses_secret_input(program: InteractiveProgram, prompt: str) -> bool:
    if program.name not in {"ssh", "sudo", "ssh-keygen", "passwd"}:
        return False
    phase = str(program.metadata.get("phase", "")).lower().replace("-", "_")
    if any(marker in phase for marker in ("password", "passphrase", "secret")):
        return True
    # A prompt can advance before the model refreshes its phase metadata.
    # Recognize an actual secret request, not a secret-related word in a path.
    if _EXPLICIT_SECRET_PROMPT.match(prompt.strip()):
        return True
    # A filename can itself contain a word such as "passphrase". Explicit
    # non-secret phases take precedence over that incidental prompt text.
    if any(
        marker in phase
        for marker in ("filename", "file_name", "overwrite", "hostkey", "host_key")
    ):
        return False
    prompt_lower = prompt.lower()
    return any(
        marker in prompt_lower
        for marker in (
            "password",
            "passphrase",
            "verification code",
            "one-time password",
            "otp",
        )
    )
