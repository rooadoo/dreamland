from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace

from dreamland import agent_shell
from dreamland.agent_builtins import is_builtin_command
from dreamland.models import (
    CommandContext,
    CommandHistoryEntry,
    CommandResult,
    CommandStateEffect,
    CommandStateUpdate,
    SessionState,
    ShellStackTransition,
)
from dreamland.session_state import (
    active_shell_frame,
    append_host_command_history,
    apply_command_state_update,
)
from dreamland.world_state import active_host_ref, sync_active_host

COMMAND_LOGGER = logging.getLogger("dreamland.commands")


def plan_builtin_command_list(
    session: SessionState,
    command: str,
) -> list[tuple[str, CommandResult]] | None:
    """Evaluate an eligible builtin list against a detached session.

    Every syntactic segment must be a supported builtin, including unreachable
    branches. If an executed builtin needs the model, discard all preparation.
    This helper publishes no lifecycle events and does not reconcile network
    connections or invoke an agent.
    """

    from dreamland.agent_core import handle_builtin_command_context

    analysis = agent_shell.analyze_command(command)
    if analysis.kind != "list":
        return None
    program = active_shell_frame(session).interactive_program
    if not all(
        is_builtin_command(segment.command, program) for segment in analysis.segments
    ):
        COMMAND_LOGGER.debug("command.routing route=whole_line reason=nonbuiltin")
        return None
    preview = deepcopy(session)
    plan: list[tuple[str, CommandResult]] = []
    last_result = CommandResult()
    for segment in analysis.segments:
        if segment.operator_before == "&&" and last_result.exit_status != 0:
            continue
        if segment.operator_before == "||" and last_result.exit_status == 0:
            continue
        context = _builtin_context(preview, segment.command)
        result = handle_builtin_command_context(context)
        if result is None:
            COMMAND_LOGGER.debug(
                "command.routing route=whole_line reason=builtin_deferred"
            )
            return None
        exiting = agent_shell.split_command(segment.command)[0] in {"exit", "logout"}
        if exiting:
            if len(context.shell_stack) <= 1:
                result = replace(
                    result,
                    metadata={**result.metadata, "dreamland.shell_exit": True},
                )
            elif result.state_update.shell_stack is not None:
                result = replace(
                    result,
                    state_update=replace(
                        result.state_update,
                        shell_stack=None,
                        shell_stack_transition=ShellStackTransition(operation="pop"),
                    ),
                )
        entry = CommandHistoryEntry(
            cwd=context.cwd,
            command=segment.command,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.exit_status,
        )
        preview.world_state = append_host_command_history(
            preview.world_state, context.hostname, entry
        )
        preview.history.append(entry)
        apply_command_state_update(
            preview, result.state_update, reconcile_network=False
        )
        plan.append((segment.command, result))
        last_result = result
        if exiting:
            break
    COMMAND_LOGGER.debug(
        "command.routing route=builtin_list segments=%s executed=%s",
        len(analysis.segments),
        len(plan),
    )
    return plan


def _builtin_context(session: SessionState, command: str) -> CommandContext:
    frame = active_shell_frame(session)
    session.world_state = sync_active_host(
        session.world_state, frame.host_identity.hostname
    )
    return CommandContext(
        username=frame.username,
        login_username=session.login_username or session.username,
        cwd=frame.cwd,
        hostname=frame.host_identity.hostname,
        host_identity=frame.host_identity,
        command=command,
        langfuse_trace_id="",
        interactive_program=frame.interactive_program,
        shell_environment=frame.shell_environment,
        shell_stack=tuple(session.shell_stack) or (frame,),
        world_state=session.world_state,
        active_host=active_host_ref(session.world_state),
    )


def aggregate_builtin_plan(plan: list[tuple[str, CommandResult]]) -> CommandResult:
    """Combine evaluated builtins into a single result for direct agent callers.

    Builtins only change the active frame's cwd/environment and may finally pop
    it. Coalescing those already evaluated changes preserves final state and
    keeps even very long local lists below the model effect-count bound. A pop
    remains a separate ordered effect so parent-frame state stays untouched.
    """

    if not plan:
        return CommandResult()
    effects: list[CommandStateEffect] = []
    pending_commands: list[str] = []
    pending_update = CommandStateUpdate()
    for command, result in plan:
        update = result.state_update
        if (
            any(
                value is not None
                for value in (
                    update.host_identity,
                    update.interactive_program,
                    update.shell_stack,
                    update.world_state,
                )
            )
            or update.effects
        ):
            raise ValueError("builtin plan contains an unsupported state change")
        if update.cwd is not None or update.shell_environment is not None:
            pending_commands.append(command)
            pending_update = CommandStateUpdate(
                cwd=update.cwd if update.cwd is not None else pending_update.cwd,
                shell_environment=(
                    update.shell_environment
                    if update.shell_environment is not None
                    else pending_update.shell_environment
                ),
            )
        if update.shell_stack_transition is not None:
            if update.shell_stack_transition.operation != "pop":
                raise ValueError("builtin plan contains a non-pop shell transition")
            if pending_commands:
                effects.append(
                    CommandStateEffect("; ".join(pending_commands), pending_update)
                )
                pending_commands = []
                pending_update = CommandStateUpdate()
            effects.append(
                CommandStateEffect(
                    command,
                    CommandStateUpdate(
                        shell_stack_transition=update.shell_stack_transition
                    ),
                )
            )
    if pending_commands:
        effects.append(CommandStateEffect("; ".join(pending_commands), pending_update))
    last_result = plan[-1][1]
    return CommandResult(
        stdout="".join(result.stdout for _, result in plan),
        stderr="".join(result.stderr for _, result in plan),
        exit_status=last_result.exit_status,
        interrupted=last_result.interrupted,
        signal=last_result.signal,
        metadata=dict(last_result.metadata),
        state_update=CommandStateUpdate(effects=tuple(effects)),
    )
