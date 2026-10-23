"""Native SSH parent-connection lobby and fresh simulated visit ownership."""

from __future__ import annotations

import logging
import textwrap
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from dreamland.agent_tracing import record_langfuse_session_end
from dreamland.config import LobbySettings
from dreamland.lobby_catalog import LobbyCatalog, LobbyCatalogError
from dreamland.lobby_content import LobbyContent
from dreamland.lobby_identity import PublicSimulationRegistry
from dreamland.lobby_input import LobbyClosed, LobbyInput
from dreamland.lobby_intro import run_intro
from dreamland.lobby_presentation import (
    LobbyPresentation,
    Text,
    normalize_terminal_type,
    welcome_banner_lines,
)
from dreamland.ssh_auth import SIMPLE_LOGIN_SUFFIX, is_fast_login
from dreamland.ssh_sessions import ShellSession

LOGGER = logging.getLogger("dreamland.lifecycle")
PLAIN_PRESENTATION = LobbyPresentation()

CONTINUE_PROMPT = (
    "Press any key to continue, or Q to quit.",
    "Any key: continue\nQ: quit",
)
WELCOME_PROMPT = CONTINUE_PROMPT
AGREEMENT_PROMPT = (
    "Type i agree and press Enter to accept the guidelines and choose a world.\n"
    "Type Q and press Enter to disconnect.",
    "i agree: accept\nQ: quit\nThen press Enter",
)
WORLD_PROMPT = (
    "Enter a number and press Enter to start that world.\n"
    "Type R and press Enter to refresh, or Q and Enter to disconnect.",
    "Number: start\nR: refresh; Q: quit\nThen press Enter",
)


class SSHLobby:
    def __init__(
        self,
        catalog: LobbyCatalog,
        identities: PublicSimulationRegistry,
        content: LobbyContent,
        settings: LobbySettings,
        *,
        fast_backend: Any = None,
    ) -> None:
        self.catalog = catalog
        self.identities = identities
        self.content = content
        self.settings = settings
        self.fast_backend = fast_backend
        # Invalid initial copy fails before listener admission, never into direct mode.
        self.content.snapshot()

    def run(
        self,
        channel: Any,
        username: str,
        backend: Any,
        connection_metadata: Any,
        *,
        console: Any = None,
        stop_event: threading.Event,
        width: int = 80,
        height: int = 24,
        terminal_type: str = "",
        dimensions: Callable[[], tuple[int, int]] | None = None,
        parent_connection_id: str | None = None,
    ) -> None:
        reader = LobbyInput(
            channel,
            stop_event=stop_event,
            idle_timeout_seconds=self.settings.idle_timeout_seconds,
            max_input_bytes=self.settings.max_input_bytes,
        )
        parent_id = parent_connection_id or uuid.uuid4().hex
        LOGGER.info("SSH lobby opened parent=%s ssh_username=%r", parent_id, username)
        simple_login = username.endswith(SIMPLE_LOGIN_SUFFIX)
        presentation = LobbyPresentation.select(
            terminal_type,
            color="off" if simple_login else self.settings.color,
            clear_screen=self.settings.clear_screen,
        )
        color_used = False

        def current_dimensions() -> tuple[int, int]:
            nonlocal color_used
            size = dimensions() if dimensions else (width, height)
            color_used |= presentation.for_size(*size).color
            return size

        initial_width, initial_height = dimensions() if dimensions else (width, height)
        initial_mode = presentation.for_size(initial_width, initial_height)
        LOGGER.info(
            "SSH lobby presentation parent=%s term=%s width=%s height=%s color=%s clear=%s simple=%s",
            parent_id,
            normalize_terminal_type(terminal_type) or "missing",
            initial_width,
            initial_height,
            initial_mode.color,
            initial_mode.clear,
            simple_login,
        )

        def screen(text: str | list[Text], **kwargs: Any) -> None:
            self._screen(
                reader,
                text,
                width,
                height,
                presentation=presentation,
                dimensions=current_dimensions,
                **kwargs,
            )

        reason = "failure"
        try:
            fast_login = is_fast_login(username)
            if fast_login:
                if self.fast_backend is None:
                    raise ValueError("Fast lobby backend is unavailable.")
                backend = self.fast_backend
            service_tier = (
                getattr(getattr(backend, "agent", None), "openai_service_tier", None)
                or "project_default"
            )
            LOGGER.info(
                "SSH lobby model routing parent=%s fast=%s service_tier=%s",
                parent_id,
                fast_login,
                service_tier,
            )
            notice = self.content.snapshot()
            intro = run_intro(
                reader,
                mode="off" if simple_login else self.settings.intro_animation,
                terminal_type=terminal_type,
                presentation=initial_mode,
                dimensions=current_dimensions,
            )
            LOGGER.debug(
                "SSH lobby introduction parent=%s reason=%s frames=%s bytes=%s",
                parent_id,
                intro.reason,
                intro.frames,
                intro.bytes_sent,
            )
            screen(
                notice.welcome,
                heading="WELCOME",
                prompt=WELCOME_PROMPT,
                keypress=True,
                banner=self.settings.banner,
            )
            reader.any_key()
            screen(
                notice.guidelines + f"\n\nReport findings to {notice.contact}.",
                heading="GUIDELINES",
                prompt=AGREEMENT_PROMPT,
            )
            attempts = 0
            while True:
                answer = reader.line()
                if answer == "q":
                    raise LobbyClosed("quit")
                if answer == "i agree":
                    break
                attempts += 1
                self._invalid(reader, attempts)
                current_width, current_height = current_dimensions()
                self._prompt(
                    reader,
                    AGREEMENT_PROMPT,
                    current_width,
                    current_height,
                    presentation=presentation,
                )
            accepted_at = datetime.now(UTC).isoformat()
            LOGGER.info(
                "SSH lobby agreement parent=%s notice=%s accepted_at=%s",
                parent_id,
                notice.version,
                accepted_at,
            )
            pending: list[ShellSession] = []
            attempts = 0
            status = ""
            last_ended: tuple[str, str] | None = None
            while True:
                pending = [shell for shell in pending if shell.cleanup_pending]
                snapshot = self.catalog.snapshot()
                menu = []
                if last_ended is not None:
                    menu.append(Text(f"Last ended world: {last_ended[1]}"))
                if status:
                    menu.append(Text(status))
                if pending:
                    menu.append(
                        Text(
                            "Previous work is still stopping. Please refresh and try again shortly."
                            if len(pending) >= 4
                            else "Previous work is still stopping."
                        )
                    )
                menu.append(Text("Every selection starts a fresh, isolated session."))
                if not snapshot.entries:
                    menu.insert(
                        0,
                        Text(
                            "No worlds are available right now. Try R to refresh later."
                        ),
                    )
                for number, entry in enumerate(snapshot.entries, 1):
                    menu.append(Text(f"[{number}] {entry.title}", "selection"))
                    menu.append(
                        Text(f"    {entry.description}", continues_paragraph=True)
                    )
                screen(
                    menu,
                    heading="CHOOSE A WORLD",
                    prompt=WORLD_PROMPT,
                    reference=last_ended[0] if last_ended else "",
                )
                answer = reader.line()
                if answer == "q":
                    raise LobbyClosed("quit")
                if answer == "r":
                    continue
                if answer is None or not answer.isascii() or not answer.isdecimal():
                    attempts += 1
                    status = self._invalid(reader, attempts, emit=False)
                    continue
                try:
                    entry, profile = self.catalog.resolve_selection(
                        snapshot, int(answer)
                    )
                except LobbyCatalogError:
                    attempts += 1
                    status = self._invalid(
                        reader,
                        attempts,
                        "That world is unavailable or the menu changed. Please select again.",
                        emit=False,
                    )
                    continue
                pending = [shell for shell in pending if shell.cleanup_pending]
                if len(pending) >= 4:
                    continue
                attempts = 0
                status = ""
                record = self.identities.allocate(
                    entry.profile,
                    parent_connection_id=parent_id,
                    notice_version=notice.version,
                    accepted_at=accepted_at,
                )
                shell = None
                session = None
                started = False
                visit_reason = "failed_start"
                visit_presentation = presentation.for_size(*current_dimensions())
                LOGGER.info(
                    "SSH lobby visit entering parent=%s public_id=%s profile=%s entry_username=%s",
                    parent_id,
                    record.public_id,
                    entry.profile,
                    entry.entry_username,
                )
                try:
                    session = backend.create_session(
                        entry.entry_username,
                        connection_metadata,
                        state_profile=profile,
                    )
                    self.identities.bind(record.public_id, session)
                    LOGGER.info(
                        "SSH lobby visit created parent=%s public_id=%s session=%s profile=%s entry_username=%s service_tier=%s",
                        parent_id,
                        record.public_id,
                        session.langfuse_trace_id,
                        entry.profile,
                        entry.entry_username,
                        service_tier,
                    )
                    shell = ShellSession(
                        reader.shell_channel(),
                        entry.entry_username,
                        backend,
                        connection_metadata,
                        session=session,
                        console=console,
                        lobby_mode=True,
                        stop_event=stop_event,
                    )
                    reader.send(
                        visit_presentation.reset
                        + visit_presentation.style(
                            f"\n\n[DREAMLAND simulation {record.public_id} Started]\n",
                            "reference",
                        )
                    )
                    reader.send(f"Include this ID in reports to {notice.contact}.\n\n")
                    reader.send(visit_presentation.reset)
                    last_ended = None
                    started = True
                    outcome = shell.run()
                    visit_reason = outcome.reason
                except (OSError, EOFError):
                    visit_reason = "disconnect"
                    raise LobbyClosed("disconnect") from None
                except Exception as exc:
                    LOGGER.warning(
                        "SSH lobby visit failure public_id=%s session=%s error=%s",
                        record.public_id,
                        getattr(session, "langfuse_trace_id", None),
                        type(exc).__name__,
                    )
                    visit_reason = "failure" if started else "failed_start"
                    status = "The world could not continue. Please select again."
                finally:
                    if shell is not None:
                        # Idempotent even if sending Started failed before run().
                        shell.close()
                        if shell.cleanup_pending:
                            pending.append(shell)
                    elif session is not None:
                        release = getattr(backend, "release_session", None)
                        if release is not None:
                            release(session)
                    self.identities.end(record.public_id, visit_reason)
                    LOGGER.info(
                        "SSH lobby visit ended parent=%s public_id=%s session=%s profile=%s entry_username=%s reason=%s",
                        parent_id,
                        record.public_id,
                        getattr(session, "langfuse_trace_id", None),
                        entry.profile,
                        entry.entry_username,
                        visit_reason,
                    )
                    if session is not None:
                        self._trace_end(console, session, visit_reason)
                if visit_reason in {"disconnect", "shutdown"}:
                    raise LobbyClosed(visit_reason)
                if started:
                    last_ended = (record.public_id, entry.title)
                    visit_presentation = presentation.for_size(*current_dimensions())
                    reader.send(
                        visit_presentation.style(
                            f"\n[DREAMLAND simulation {record.public_id} Ended]\n",
                            "reference",
                        )
                    )
                    reader.send(
                        f"Report findings to {notice.contact} with this ID.\n\n"
                    )
                    if visit_reason == "operator_close":
                        status = (
                            "The visit was closed by the operator. Please select again."
                        )
                # Lobby idleness is suspended while a world owns the channel.
                reader.resume_idle()
                reader.discard_boundary(
                    shell.boundary_input if shell is not None else b""
                )
        except LobbyClosed as exc:
            reason = exc.reason
            if reason == "timeout":
                reader.send("\nLobby idle timeout. Reconnect when you are ready.\n")
            elif reason == "input_limit":
                reader.send("\nLobby input limit reached. Please reconnect.\n")
        except (OSError, EOFError):
            reason = "disconnect"
        except Exception as exc:
            LOGGER.warning(
                "SSH lobby failed parent=%s error=%s", parent_id, type(exc).__name__
            )
            try:
                reader.send("\nThe lobby is unavailable. Please reconnect later.\n")
            except (OSError, EOFError):
                pass
        finally:
            if color_used and not channel.closed:
                try:
                    reader.send(presentation.reset)
                except (OSError, EOFError):
                    pass
            LOGGER.info("SSH lobby ended parent=%s reason=%s", parent_id, reason)
            if not channel.closed:
                try:
                    channel.send_exit_status(0 if reason == "quit" else 1)
                except (OSError, EOFError):
                    pass

    def _invalid(
        self,
        reader: LobbyInput,
        attempts: int,
        message: str = "That input is not valid for this screen.",
        *,
        emit: bool = True,
    ) -> str:
        if attempts >= self.settings.max_invalid_attempts:
            raise LobbyClosed("input_limit")
        if emit:
            reader.send(message + "\n")
        return message

    @classmethod
    def _screen(
        cls,
        reader: LobbyInput,
        text: str | list[Text],
        width: int,
        height: int,
        *,
        heading: str = "",
        prompt: tuple[str, str] | None = None,
        keypress: bool = False,
        presentation: LobbyPresentation = PLAIN_PRESENTATION,
        dimensions: Callable[[], tuple[int, int]] | None = None,
        reference: str = "",
        banner: str | None = None,
    ) -> None:
        fragments = [Text(text)] if isinstance(text, str) else text
        blocks: list[list[Text]] = []
        block: list[Text] = []
        for fragment in fragments:
            if block and not fragment.continues_paragraph:
                blocks.append(block)
                block = []
            for raw_line in fragment.text.splitlines():
                if not raw_line.strip():
                    if block:
                        blocks.append(block)
                        block = []
                else:
                    block.append(Text(raw_line, fragment.role))
        if block:
            blocks.append(block)
        if (
            banner is not None
            and heading == "WELCOME"
            and blocks
            and len(blocks[0]) == 1
            and blocks[0][0].text == "DREAMLAND"
        ):
            # The optional leading wordmark is presentation, now supplied by
            # the large/simple heading. Preserve every other notice paragraph.
            blocks = blocks[1:]
        more_prompt = CONTINUE_PROMPT
        number = 0
        previous_size = None
        pages: list[list[list[Text]]] = []
        sources: dict[int, tuple[int, Text, int]] = {}
        while True:
            current_width, current_height = (
                dimensions() if dimensions else (width, height)
            )
            columns = max(1, min(current_width or 80, 80))
            rows = max(1, min(current_height or 24, 100))
            mode = presentation.for_size(columns, rows)
            compact = rows < 12 or columns < 40
            margin = "  " if columns >= 40 else ""
            content_columns = columns - 2 * len(margin)
            if columns < 20 or rows < 5:
                # No viewport this small can hold the full action contract.
                # Stream plain text at its real width, without artificial paging.
                remaining = (
                    [block for page in pages for block in page] if pages else blocks
                )
                lines = ["Small terminal: plain output."]
                if banner is not None and heading == "WELCOME":
                    lines.append("DREAMLAND")
                if reference:
                    lines.append(reference)
                lines.extend(line.text for block in remaining for line in block)
                reader.send(
                    "\n".join(
                        part
                        for line in lines
                        for part in textwrap.wrap(line, width=columns)
                    )
                    + "\n"
                )
                if prompt is not None:
                    cls._prompt(reader, prompt, columns, rows, keypress=keypress)
                return
            header = []
            if heading and not compact:
                inner_columns = content_columns - 2
                label = f"{heading} | DREAMLAND"
                header = [
                    Text(
                        margin
                        + "+"
                        + "--[ ]--[ ]--[ ]".ljust(inner_columns, "-")
                        + "+",
                        "rule",
                    ),
                    Text(margin + f"| {label.ljust(inner_columns - 2)} |", "heading"),
                    Text(margin + "+" + "-" * inner_columns + "+", "rule"),
                ]
            reference_lines = (
                textwrap.wrap(
                    ("Last ended: " if not compact else "") + reference,
                    width=content_columns,
                )
                if reference
                else []
            )
            tight = bool(reference and rows < 8 and prompt == WORLD_PROMPT)
            footer_rows = len(cls._prompt_lines(more_prompt, columns, compact, True))
            if prompt is not None:
                footer_rows = max(
                    footer_rows,
                    len(
                        cls._prompt_lines(
                            prompt,
                            columns,
                            compact,
                            keypress,
                            tight=tight,
                        )
                    ),
                )
            if banner is not None and heading == "WELCOME" and number == 0:
                body_rows = max(0, len(blocks) - 1) + sum(
                    len(
                        textwrap.wrap(
                            line.text,
                            width=content_columns,
                            subsequent_indent="    "
                            if line.text.startswith("    ")
                            else "",
                        )
                    )
                    for block in blocks
                    for line in block
                )
                artwork = welcome_banner_lines(
                    banner=banner,
                    presentation=mode,
                    columns=content_columns,
                    rows=rows,
                    body_rows=body_rows,
                    footer_rows=footer_rows,
                    leading_rows=int(not compact),
                )
                header = [Text(margin + line, "heading") for line in artwork]
                LOGGER.debug(
                    "SSH lobby welcome banner mode=%s width=%s height=%s",
                    "large" if len(artwork) > 1 else "simple",
                    columns,
                    rows,
                )
            elif banner is not None and heading == "WELCOME":
                # A long welcome uses the same one-row heading budget on every
                # page. Widening later must not introduce the initial artwork.
                header = [Text(margin + "DREAMLAND / WELCOME", "heading")]
            page_lines = (
                rows - len(header) - len(reference_lines) - footer_rows - (not compact)
            )
            if page_lines < 1:
                header = []
                page_lines = rows - len(reference_lines) - footer_rows - (not compact)
            if previous_size != (columns, rows):
                if pages:
                    # Recover unread source suffixes: widening rejoins wrapped
                    # lines too. Offsets preserve split words and paragraph roles.
                    remaining_blocks: dict[int, list[Text]] = {}
                    seen: set[int] = set()
                    for remaining_page in pages:
                        for remaining_block in remaining_page:
                            for line in remaining_block:
                                origin = sources.get(id(line))
                                if origin is None:
                                    continue  # Renderer-inserted paragraph gap.
                                block_id, source, offset = origin
                                if id(source) in seen:
                                    continue
                                seen.add(id(source))
                                indent = (
                                    "    " if source.text.startswith("    ") else ""
                                )
                                suffix = (
                                    source.text
                                    if offset == 0
                                    else indent + source.text[offset:].lstrip()
                                )
                                remaining_blocks.setdefault(block_id, []).append(
                                    Text(suffix, source.role)
                                )
                    blocks = list(remaining_blocks.values())
                sources = {}
                wrapped = []
                for block_id, block in enumerate(blocks):
                    wrapped_block = []
                    for line in block:
                        offset = 0
                        for part in textwrap.wrap(
                            line.text,
                            width=content_columns,
                            subsequent_indent="    "
                            if line.text.startswith("    ")
                            else "",
                        ):
                            rendered = Text(part, line.role)
                            sources[id(rendered)] = (block_id, line, offset)
                            wrapped_block.append(rendered)
                            start = line.text.index(part.strip(), offset)
                            offset = start + len(part.strip())
                    wrapped.append(wrapped_block)
                pages = cls._paginate(wrapped, page_lines)
                previous_size = (columns, rows)
            number += 1
            total = number + len(pages) - 1
            page_blocks = pages.pop(0)
            LOGGER.debug(
                "SSH lobby screen section=%s page=%s total=%s width=%s height=%s color=%s clear=%s",
                heading,
                number,
                total,
                columns,
                rows,
                mode.color,
                mode.clear,
            )
            body: list[Text] = []
            for block in page_blocks:
                if body:
                    body.append(Text(""))
                body.extend(block)
            reader.send(mode.start_screen)
            if not compact:
                reader.send("\n")
            reader.send(
                "\n".join(
                    [mode.style(line.text, line.role) for line in header]
                    + [
                        mode.style(margin + line, "reference")
                        for line in reference_lines
                    ]
                    + [
                        mode.style(margin + line.text if line.text else "", line.role)
                        for line in body
                    ]
                )
                + "\n"
            )
            if pages:
                cls._prompt(
                    reader,
                    more_prompt,
                    columns,
                    rows,
                    keypress=True,
                    page=(number, total),
                    presentation=mode,
                )
                reader.any_key()
            else:
                if prompt is not None:
                    cls._prompt(
                        reader,
                        prompt,
                        columns,
                        rows,
                        keypress=keypress,
                        page=(number, total),
                        presentation=mode,
                        tight=tight,
                    )
                return

    @staticmethod
    def _paginate(blocks: list[list[Text]], budget: int) -> list[list[list[Text]]]:
        # Preserve paragraph boundaries for wrapping unshown text after resize.
        if (
            len(blocks) > 1
            and len(blocks[-1]) <= 2
            and len(blocks[-2]) + 1 + len(blocks[-1]) <= budget
        ):
            blocks[-2:] = [blocks[-2] + [Text("")] + blocks[-1]]
        pages: list[list[list[Text]]] = []
        page: list[list[Text]] = []
        used = 0
        for block in blocks:
            if page and used + 1 + len(block) > budget:
                pages.append(page)
                page = []
                used = 0
            while len(block) > budget:
                pages.append([block[:budget]])
                block = block[budget:]
            if block:
                used += len(block) + bool(page)
                page.append(block)
        if page or not pages:
            pages.append(page)
        return pages

    @staticmethod
    def _prompt_lines(
        prompt: tuple[str, str],
        columns: int,
        compact: bool,
        keypress: bool,
        page: tuple[int, int] | None = None,
        *,
        tight: bool = False,
    ) -> list[str]:
        margin = "  " if columns >= 40 else ""
        content_columns = columns - 2 * len(margin)
        if tight:
            return ["#:start R:refresh", "Q:quit; Enter> "]
        lines = []
        if not compact:
            label = f"-- Page {page[0]} of {page[1]} " if page else ""
            lines.append(margin + label.ljust(content_columns, "-"))
        for line in prompt[1 if compact else 0].splitlines():
            lines.extend(
                margin + part for part in textwrap.wrap(line, width=content_columns)
            )
        if not keypress:
            lines.append(margin + ("> " if columns > 1 else ">"))
        return lines

    @classmethod
    def _prompt(
        cls,
        reader: LobbyInput,
        prompt: tuple[str, str],
        width: int,
        height: int,
        *,
        keypress: bool = False,
        page: tuple[int, int] | None = None,
        presentation: LobbyPresentation = PLAIN_PRESENTATION,
        tight: bool = False,
    ) -> None:
        columns = max(1, min(width or 80, 80))
        rows = max(1, min(height or 24, 100))
        compact = rows < 12 or columns < 40
        mode = presentation.for_size(columns, rows)
        lines = cls._prompt_lines(prompt, columns, compact, keypress, page, tight=tight)
        reader.send(
            "\n".join(
                mode.style(line, "rule" if not compact and index == 0 else "action")
                for index, line in enumerate(lines)
            )
        )

    @staticmethod
    def _trace_end(console: Any, session: Any, reason: str) -> None:
        settings = getattr(console, "langfuse_settings", None)
        if settings is not None:
            record_langfuse_session_end(
                enabled=settings.enabled,
                public_key=settings.public_key,
                secret_key=settings.secret_key,
                host=settings.host,
                session=session,
                reason=reason,
            )
