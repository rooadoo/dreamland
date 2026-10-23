from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from dreamland.agent_state import (
    host_identity_to_state,
    interactive_program_to_state,
    shell_environment_to_state,
    shell_frame_to_state,
)
from dreamland.models import (
    CommandStateUpdate,
    HostIdentity,
    SessionState,
    default_shell_environment,
    default_shell_frame,
)
from dreamland.session_state import apply_external_state_update, sync_active_shell_frame
from dreamland.spiritguide import Spiritguide, SpiritguideContext, SpiritguideResult
from dreamland.state_profiles import (
    StateProfile,
    StateProfileError,
    StateProfileLevel,
    StateProfileStore,
    create_state_profile,
    state_profile_to_session,
)
from dreamland.world_state import default_interactive_program_from_world_state

LOGGER = logging.getLogger("dreamland.admin")

DETACHED_SPIRITGUIDE_LEVELS = ("world", "system")
BLANK_WORLD_STATE: dict[str, object] = {
    "name": "blank-world",
    "entry_host": "operator-defined",
    "active_host": "operator-defined",
    "purpose": {
        "theme": "Uninitialized operator-built computing environment.",
        "description": (
            "Temporary detached-profile scaffold. Spiritguide should replace this "
            "purpose with the operator's scenario before saving a reusable world."
        ),
    },
    "simulation": {
        "permissiveness": "moderate",
        "missing_detail_policy": "infer_from_operator_direction",
        "contradiction_policy": "prefer_new_operator_direction",
        "infrastructure_bias": "physical_systems",
    },
    "hosts_by_name": {
        "operator-defined": {
            "hostname": "operator-defined",
            "purpose": {
                "theme": "Operator-defined entry host.",
                "description": (
                    "Temporary placeholder host metadata. Replace this with the "
                    "intended entry host before saving a reusable world."
                ),
            },
            "shell": {
                "default": {
                    "name": "bash",
                    "command": "/bin/bash",
                    "argv": ["bash"],
                },
            },
            "filesystem": {"artifacts_by_path": {}},
            "services_by_name": {},
            "facts_by_id": {},
            "command_history": [],
        },
    },
    "generation_policy": {
        "materialization": "just_in_time",
        "liberty": "moderate",
        "liberty_levels": [
            "strict",
            "conservative",
            "moderate",
            "expansive",
            "abundant",
        ],
        "registration_policy": (
            "Register major systems, important services, and user-interacted files "
            "after they are materialized by command output. Avoid registering "
            "incidental listing rows, scan noise, and examples that the user has "
            "not acted on."
        ),
    },
    "expansion_directives_by_id": {},
    "world_facts_by_id": {},
}


@dataclass(frozen=True, slots=True)
class SpiritguideProfileOperationResult:
    action: str
    name: str
    level: StateProfileLevel
    message: str
    dry_run: bool
    saved: bool
    applied: bool
    structured_response_accepted: object
    patch_keys: list[str]
    state_update: dict[str, object]
    metadata: dict[str, object]
    excluded_patch_keys: list[str]
    path: Path | None = None
    source_name: str | None = None


def create_spiritguide_profile(
    *,
    store: StateProfileStore,
    spiritguide: Spiritguide,
    name: str,
    instruction: str,
    level: str = "world",
    dry_run: bool = False,
    comment: str | None = None,
) -> SpiritguideProfileOperationResult:
    level = _detached_level(level)
    _validate_instruction(instruction)
    _ensure_profile_missing(store, name)
    session = _blank_detached_session(store, name)
    return _run_profile_operation(
        store=store,
        spiritguide=spiritguide,
        session=session,
        action="create",
        name=name,
        level=level,
        instruction=instruction,
        dry_run=dry_run,
        comment=comment,
    )


def update_spiritguide_profile(
    *,
    store: StateProfileStore,
    spiritguide: Spiritguide,
    name: str,
    instruction: str,
    level: str | None = None,
    dry_run: bool = False,
    comment: str | None = None,
) -> SpiritguideProfileOperationResult:
    _validate_instruction(instruction)
    source = store.load(name)
    target_level = _detached_level(level or source.level)
    session = _session_from_profile(source, action="update", name=name)
    return _run_profile_operation(
        store=store,
        spiritguide=spiritguide,
        session=session,
        action="update",
        name=name,
        level=target_level,
        instruction=instruction,
        dry_run=dry_run,
        comment=comment if comment is not None else source.comment,
        source_name=name,
    )


def fork_spiritguide_profile(
    *,
    store: StateProfileStore,
    spiritguide: Spiritguide,
    source_name: str,
    name: str,
    instruction: str | None = None,
    level: str | None = None,
    dry_run: bool = False,
    comment: str | None = None,
) -> SpiritguideProfileOperationResult:
    source = store.load(source_name)
    _ensure_profile_missing(store, name)
    target_level = _detached_level(level or source.level)
    session = _session_from_profile(source, action="fork", name=name)
    instruction = (instruction or "").strip()
    if not instruction:
        if dry_run:
            return _copy_profile_result(
                action="fork",
                name=name,
                source=source,
                target_level=target_level,
                dry_run=True,
                comment=comment,
            )
        profile = create_state_profile(
            session,
            name=name,
            level=target_level,
            comment=comment if comment is not None else source.comment,
        )
        path = store.save(profile)
        LOGGER.info(
            "Detached Spiritguide profile forked source=%s name=%s level=%s path=%s",
            source_name,
            name,
            target_level,
            path,
        )
        return _copy_profile_result(
            action="fork",
            name=name,
            source=source,
            target_level=target_level,
            dry_run=False,
            comment=comment,
            path=path,
        )
    return _run_profile_operation(
        store=store,
        spiritguide=spiritguide,
        session=session,
        action="fork",
        name=name,
        level=target_level,
        instruction=instruction,
        dry_run=dry_run,
        comment=comment if comment is not None else source.comment,
        source_name=source_name,
    )


def _run_profile_operation(
    *,
    store: StateProfileStore,
    spiritguide: Spiritguide,
    session: SessionState,
    action: str,
    name: str,
    level: StateProfileLevel,
    instruction: str,
    dry_run: bool,
    comment: str | None,
    source_name: str | None = None,
) -> SpiritguideProfileOperationResult:
    LOGGER.info(
        "Detached Spiritguide profile requested action=%s name=%s source=%s "
        "level=%s dry_run=%s instruction=%r",
        action,
        name,
        source_name,
        level,
        dry_run,
        instruction,
    )
    result = spiritguide.respond(
        SpiritguideContext(
            instruction=instruction,
            session_id=session.langfuse_trace_id,
            session=session,
            dry_run=dry_run,
        )
    )
    patch_keys = _spiritguide_patch_keys(result)
    structured_response_accepted = result.metadata.get("structured_response_accepted")
    state_update_rejected = structured_response_accepted is False
    applied = False
    saved = False
    path: Path | None = None
    if not state_update_rejected:
        candidate = deepcopy(session) if dry_run else session
        apply_external_state_update(candidate, result.state_update)
        if action == "create":
            _finalize_detached_create_session(candidate)
        profile = create_state_profile(
            candidate,
            name=name,
            level=level,
            comment=comment,
        )
        if not dry_run:
            applied = bool(patch_keys)
            path = store.save(profile)
            saved = True
    LOGGER.info(
        "Detached Spiritguide profile completed action=%s name=%s source=%s "
        "level=%s dry_run=%s applied=%s saved=%s",
        action,
        name,
        source_name,
        level,
        dry_run,
        applied,
        saved,
    )
    return SpiritguideProfileOperationResult(
        action=action,
        name=name,
        source_name=source_name,
        level=level,
        message=result.message,
        dry_run=dry_run,
        saved=saved,
        applied=applied,
        structured_response_accepted=structured_response_accepted,
        patch_keys=patch_keys,
        state_update=spiritguide_state_update_to_dict(result.state_update),
        metadata=dict(result.metadata),
        excluded_patch_keys=_excluded_patch_keys(level, patch_keys),
        path=path,
    )


def _copy_profile_result(
    *,
    action: str,
    name: str,
    source: StateProfile,
    target_level: StateProfileLevel,
    dry_run: bool,
    comment: str | None,
    path: Path | None = None,
) -> SpiritguideProfileOperationResult:
    return SpiritguideProfileOperationResult(
        action=action,
        name=name,
        source_name=source.name,
        level=target_level,
        message=(
            f"Forked state profile {source.name!r} to {name!r}."
            if not dry_run
            else f"Would fork state profile {source.name!r} to {name!r}."
        ),
        dry_run=dry_run,
        saved=not dry_run,
        applied=False,
        structured_response_accepted=None,
        patch_keys=[],
        state_update={},
        metadata={"comment": comment or source.comment or ""},
        excluded_patch_keys=[],
        path=path,
    )


def spiritguide_state_update_to_dict(
    update: CommandStateUpdate,
) -> dict[str, object]:
    state: dict[str, object] = {}
    if update.host_identity is not None:
        state["host_identity"] = host_identity_to_state(update.host_identity)
    if update.cwd is not None:
        state["cwd"] = update.cwd
    if update.interactive_program is not None:
        state["interactive_program"] = interactive_program_to_state(
            update.interactive_program
        )
    if update.shell_environment is not None:
        state["shell_environment"] = shell_environment_to_state(
            update.shell_environment
        )
    if update.shell_stack is not None:
        state["shell_stack"] = [
            shell_frame_to_state(frame) for frame in update.shell_stack
        ]
    if update.shell_stack_transition is not None:
        state["shell_stack_transition"] = {
            "operation": update.shell_stack_transition.operation,
            "username": update.shell_stack_transition.username,
            "metadata": dict(update.shell_stack_transition.metadata),
        }
    if update.world_state is not None:
        state["world_state"] = deepcopy(update.world_state)
    return state


def _spiritguide_patch_keys(result: SpiritguideResult) -> list[str]:
    return [
        name
        for name in (
            "host_identity",
            "cwd",
            "interactive_program",
            "shell_environment",
            "shell_stack",
            "shell_stack_transition",
            "world_state",
        )
        if getattr(result.state_update, name) is not None
    ]


def _detached_level(level: str) -> StateProfileLevel:
    if level not in DETACHED_SPIRITGUIDE_LEVELS:
        raise StateProfileError(
            "detached Spiritguide profiles support only world or system level"
        )
    return cast(StateProfileLevel, level)


def _validate_instruction(instruction: str) -> None:
    if not instruction.strip():
        raise StateProfileError("spiritguide profile operation requires instruction")


def _ensure_profile_missing(store: StateProfileStore, name: str) -> None:
    path = store.path_for(name)
    if path.exists():
        raise StateProfileError(f"state profile {name!r} already exists")


def _blank_detached_session(store: StateProfileStore, name: str) -> SessionState:
    del store
    # A world-building scaffold is a draft, never a valid runnable profile.
    # In particular, it has no login_banner for Spiritguide to accidentally reuse.
    hostname = "operator-defined"
    username = "operator"
    cwd = "/home/operator"
    world_state = deepcopy(BLANK_WORLD_STATE)
    program = default_interactive_program_from_world_state(world_state)
    environment = default_shell_environment(
        username=username, hostname=hostname, cwd=cwd, shell=program.command
    )
    return SessionState(
        username=username,
        login_username=username,
        host_identity=HostIdentity(hostname=hostname),
        cwd=cwd,
        interactive_program=program,
        shell_environment=environment,
        shell_stack=[
            default_shell_frame(
                username=username,
                hostname=hostname,
                cwd=cwd,
                interactive_program=program,
                shell_environment=environment,
            )
        ],
        langfuse_trace_id=_detached_session_id("create", name),
        world_state=world_state,
        force_session_state_next_command=True,
    )


def _finalize_detached_create_session(session: SessionState) -> None:
    world_state = deepcopy(session.world_state)
    entry_host = world_state.get("entry_host")
    hosts = world_state.get("hosts_by_name")
    if not isinstance(entry_host, str) or not entry_host.strip():
        raise StateProfileError("Spiritguide must create a concrete entry host")
    entry_ref = entry_host.strip()
    if _is_placeholder_hostname(entry_ref):
        raise StateProfileError("Spiritguide must replace the draft entry host")
    if not isinstance(hosts, dict):
        raise StateProfileError("Spiritguide must register the entry host")
    host = hosts.get(entry_ref)
    if not isinstance(host, dict):
        raise StateProfileError(
            "Spiritguide entry_host must reference a registered host"
        )

    world_state["entry_host"] = entry_ref
    world_state["active_host"] = entry_ref
    for host_ref, metadata in list(hosts.items()):
        if host_ref == entry_ref:
            continue
        if _is_placeholder_host(host_ref, metadata):
            hosts.pop(host_ref, None)

    entry_hostname = str(host.get("hostname") or entry_ref)
    if _is_placeholder_hostname(session.host_identity.hostname):
        session.host_identity = HostIdentity(hostname=entry_hostname)
        sync_active_shell_frame(session)
    session.world_state = world_state


def _is_placeholder_host(host_ref: object, metadata: object) -> bool:
    if isinstance(host_ref, str) and _is_placeholder_hostname(host_ref):
        return True
    if not isinstance(metadata, dict):
        return False
    purpose = metadata.get("purpose")
    if not isinstance(purpose, dict):
        return False
    haystack = " ".join(str(value).lower() for value in purpose.values())
    return "placeholder" in haystack or "operator-defined" in haystack


def _is_placeholder_hostname(hostname: object) -> bool:
    if not isinstance(hostname, str):
        return False
    normalized = hostname.strip().lower()
    return normalized in {
        "operator-defined",
        "replace-me-entry-host",
        "__placeholder_entry_host__",
    }


def _session_from_profile(
    profile: StateProfile,
    *,
    action: str,
    name: str,
) -> SessionState:
    return state_profile_to_session(
        profile,
        current_session_id=_detached_session_id(action, name),
        fallback_username="operator",
        authenticated_username="operator",
    )


def _detached_session_id(action: str, name: str) -> str:
    return uuid4().hex


def _excluded_patch_keys(level: StateProfileLevel, patch_keys: list[str]) -> list[str]:
    allowed = {"world_state"} if level == "world" else {"world_state", "host_identity"}
    return [key for key in patch_keys if key not in allowed]
