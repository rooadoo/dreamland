"""Input semantics shared by terminal and headless command entry points."""

from dreamland.models import InteractiveProgram

BLANK_INPUT_SHELLS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})


def is_command_shell(program: InteractiveProgram) -> bool:
    """Recognize an ordinary shell, without treating its editor as identity."""
    name = program.name.strip().lower()
    executable = program.command.rsplit("/", 1)[-1].strip().lower()
    return name in BLANK_INPUT_SHELLS and executable in BLANK_INPUT_SHELLS


def ignores_blank_input(program: InteractiveProgram, command: str) -> bool:
    """Only an idle known shell can discard a whitespace-only submission.

    input_mode=line marks a shell awaiting read/continuation/heredoc input.
    Unknown modes and existing non-command phases conservatively accept input.
    Outside an idle shell, callers must preserve even whitespace-only secrets.
    """
    if command.strip() or not is_command_shell(program):
        return False
    return not has_pending_shell_input(program)


def has_pending_shell_input(program: InteractiveProgram) -> bool:
    """An explicit input phase also prevents shell builtin/exit interception."""
    return (
        program.metadata.get("input_mode", "command") != "command"
        or program.metadata.get("phase", "") not in ("", "command", "ready", "shell")
        or program.prompt_template in ("> ", "heredoc> ", "quote> ", "dquote> ")
    )
