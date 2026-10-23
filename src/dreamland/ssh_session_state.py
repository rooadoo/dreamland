from __future__ import annotations

from copy import deepcopy

from dreamland.agent_builtins import supports_builtin_exit
from dreamland.models import (
    CommandHistoryEntry,
    CommandResult,
    CommandStateUpdate,
    SessionState,
    ShellFrame,
)
from dreamland.runtime_logging import trace_shell_stack_event
from dreamland.session_state import (
    SHELL_STACK_EXIT_FIX_MARKER,
    apply_command_state_update,
    pop_active_shell_frame,
    replace_shell_stack,
    validate_command_state_effects,
)


def should_close_shell_session(
    command: str,
    result: CommandResult,
    starting_shell_stack: tuple[ShellFrame, ...] = (),
) -> bool:
    if _result_state_rejected(result):
        return False
    if result.metadata.get("dreamland.shell_exit") is True:
        return not starting_shell_stack or supports_builtin_exit(
            starting_shell_stack[-1].interactive_program
        )
    if _requests_root_shell_exit(result.state_update, starting_shell_stack):
        return True
    if (
        result.state_update.effects
        or result.state_update.shell_stack_transition is not None
    ):
        return False
    if command.strip() not in {"exit", "logout"}:
        return False
    if starting_shell_stack and not supports_builtin_exit(
        starting_shell_stack[-1].interactive_program
    ):
        return False
    if len(starting_shell_stack) > 1:
        return False
    return result.state_update.shell_stack is None


def _result_state_rejected(result: CommandResult) -> bool:
    return result.interrupted or any(
        result.metadata.get(key) is False
        for key in (
            "structured_response_accepted",
            "dreamland.state_effects.accepted",
            "dreamland.transition.accepted",
        )
    )


def _requests_root_shell_exit(
    update: CommandStateUpdate,
    starting_shell_stack: tuple[ShellFrame, ...],
) -> bool:
    """Recognize a final root pop using the same ordered frame semantics."""
    if not starting_shell_stack:
        return False
    try:
        validate_command_state_effects(update)
        updates = (
            tuple(effect.state_update for effect in update.effects)
            if update.effects
            else (update,)
        )
        session = SessionState(
            username=starting_shell_stack[-1].username,
            shell_stack=list(deepcopy(starting_shell_stack)),
        )
        for index, effect in enumerate(updates):
            transition = effect.shell_stack_transition
            if (
                transition is not None
                and transition.operation == "pop"
                and len(session.shell_stack) == 1
            ):
                return index == len(updates) - 1 and supports_builtin_exit(
                    session.shell_stack[-1].interactive_program
                )
            apply_command_state_update(session, effect, reconcile_network=False)
    except (AttributeError, TypeError, ValueError):
        return False
    return False


def apply_nested_exit_fallback(
    session: SessionState,
    command: str,
    result: CommandResult,
    starting_shell_stack: tuple[ShellFrame, ...],
) -> None:
    if _result_state_rejected(result) or result.state_update.effects:
        return
    if command.strip() not in {"exit", "logout"}:
        return
    if starting_shell_stack and not supports_builtin_exit(
        starting_shell_stack[-1].interactive_program
    ):
        return
    if (
        result.state_update.shell_stack is not None
        or result.state_update.shell_stack_transition is not None
        or len(starting_shell_stack) <= 1
    ):
        return
    replace_shell_stack(
        session,
        starting_shell_stack[:-1],
        force_session_state_next_command=True,
    )
    trace_shell_stack_event(
        session.langfuse_trace_id,
        event="nested_exit_fallback_applied",
        marker=SHELL_STACK_EXIT_FIX_MARKER,
        command=command,
        previous_depth=len(starting_shell_stack),
        next_depth=len(session.shell_stack),
        previous_host=starting_shell_stack[-1].host_identity.hostname,
        next_host=session.host_identity.hostname,
        previous_program=starting_shell_stack[-1].interactive_program.name,
        next_program=session.interactive_program.name,
        force_session_state_next_command=session.force_session_state_next_command,
    )


def pop_nested_shell_for_transport_exit(
    session: SessionState, *, signal: str | None = None
) -> bool:
    previous_depth = len(session.shell_stack)
    previous_host = session.host_identity.hostname
    previous_program = session.interactive_program.name
    popped = pop_active_shell_frame(session, force_session_state_next_command=True)
    if popped:
        trace_shell_stack_event(
            session.langfuse_trace_id,
            event="transport_interrupt_pop" if signal else "transport_exit_pop",
            marker=SHELL_STACK_EXIT_FIX_MARKER,
            command="\x03" if signal == "SIGINT" else None,
            previous_depth=previous_depth,
            next_depth=len(session.shell_stack),
            previous_host=previous_host,
            next_host=session.host_identity.hostname,
            previous_program=previous_program,
            next_program=session.interactive_program.name,
            force_session_state_next_command=session.force_session_state_next_command,
        )
    else:
        trace_shell_stack_event(
            session.langfuse_trace_id,
            event="transport_interrupt_noop" if signal else "transport_exit_noop",
            marker=SHELL_STACK_EXIT_FIX_MARKER,
            command="\x03" if signal == "SIGINT" else None,
            previous_depth=previous_depth,
            next_depth=len(session.shell_stack),
            previous_host=previous_host,
            next_host=session.host_identity.hostname,
            previous_program=previous_program,
            next_program=session.interactive_program.name,
            force_session_state_next_command=session.force_session_state_next_command,
        )
    return popped


def record_transport_result(
    session: SessionState,
    command: str,
    cwd: str,
    result: CommandResult,
) -> None:
    session.history.append(
        CommandHistoryEntry(
            cwd=cwd,
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.exit_status,
            interrupted=result.interrupted,
            signal=result.signal,
        )
    )
