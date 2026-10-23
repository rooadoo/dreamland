from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dreamland.terminal.base import SendText
from dreamland.terminal.events import EditorEvent
from dreamland.terminal.history import HistoryNavigator
from dreamland.terminal.line_buffer import LineBuffer


class ReadlineEditorBase:
    def __init__(self, send: SendText, prompt: str) -> None:
        self._send = send
        self._prompt = prompt
        self._buffer = LineBuffer()
        self._history = HistoryNavigator()
        self._escape_buffer = bytearray()
        self._reverse_search: ReverseSearchState | None = None
        self._kill_buffer = ""

    def start_prompt(self, prompt: str) -> None:
        self._prompt = prompt
        self._buffer.clear()
        self._history.reset_navigation()
        self._escape_buffer.clear()
        self._reverse_search = None

    def discard_latest_history(self, command: str) -> None:
        self._history.discard_latest(command)

    def handle_byte(self, byte: int) -> EditorEvent | None:
        if self._reverse_search is not None:
            return self._handle_reverse_search_byte(byte)
        if self._escape_buffer:
            self._escape_buffer.append(byte)
            self._handle_escape_buffer()
            return None
        if byte == 27:
            self._escape_buffer.append(byte)
            return None
        if byte in {10, 13}:
            return self._submit_line()
        if byte == 9:
            self._complete()
            return None
        return self.handle_control_byte(byte)

    def handle_control_byte(self, byte: int) -> EditorEvent | None:
        if byte == 1:
            self._move_home()
            return None
        if byte == 2:
            self._move_left()
            return None
        if byte == 3:
            self._cancel_line()
            return None
        if byte == 4:
            return self._delete_or_exit()
        if byte == 5:
            self._move_end()
            return None
        if byte == 6:
            self._move_right()
            return None
        if byte == 11:
            self._kill_to_end()
            return None
        if byte == 12:
            self._clear_screen()
            return None
        if byte == 14:
            self._history_next()
            return None
        if byte == 16:
            self._history_previous()
            return None
        if byte == 18:
            self._start_reverse_search()
            return None
        if byte == 20:
            self._transpose_characters()
            return None
        if byte == 21:
            self._kill_to_start()
            return None
        if byte == 25:
            self._yank()
            return None
        if byte == 23:
            self._delete_word_before_cursor()
            return None
        if byte == 0:
            self._mark_unsupported()
            return None
        if byte == 31:
            self._undo_unsupported()
            return None
        if byte == 28:
            return EditorEvent(should_exit=True)
        if byte in {8, 127}:
            self._backspace()
            return None
        if 32 <= byte <= 126:
            self._append_printable(byte)
        return None

    def _submit_line(self) -> EditorEvent:
        self._send("\r\n")
        command = self._buffer.line
        self._history.add(command)
        self._buffer.clear()
        return EditorEvent(command=command)

    def _backspace(self) -> None:
        if self._buffer.backspace():
            self._history.reset_navigation()
            self._redraw_line()

    def _append_printable(self, byte: int) -> None:
        character = chr(byte)
        self._buffer.insert(character)
        self._history.reset_navigation()
        if self._buffer.cursor == len(self._buffer.line):
            self._send(character)
        else:
            self._redraw_line()

    def _handle_escape_buffer(self) -> None:
        if len(self._escape_buffer) < 3:
            return
        sequence = bytes(self._escape_buffer)
        self._escape_buffer.clear()
        if sequence == b"\x1b[A":
            self._history_previous()
        elif sequence == b"\x1b[B":
            self._history_next()
        elif sequence == b"\x1b[C":
            self._move_right()
        elif sequence == b"\x1b[D":
            self._move_left()

    def _move_home(self) -> None:
        distance = self._buffer.move_home()
        if distance:
            self._send("\x1b[D" * distance)

    def _move_end(self) -> None:
        distance = self._buffer.move_end()
        if distance:
            self._send("\x1b[C" * distance)

    def _move_left(self) -> None:
        if self._buffer.move_left():
            self._send("\x1b[D")

    def _move_right(self) -> None:
        if self._buffer.move_right():
            self._send("\x1b[C")

    def _history_previous(self) -> None:
        replacement = self._history.previous(self._buffer.line)
        if replacement is not None:
            self._replace_line(replacement)

    def _history_next(self) -> None:
        replacement = self._history.next()
        if replacement is not None:
            self._replace_line(replacement)

    def _replace_line(self, replacement: str) -> None:
        self._buffer.replace(replacement)
        self._redraw_line()

    def _delete_or_exit(self) -> EditorEvent | None:
        if not self._buffer.line:
            return EditorEvent(should_exit=True)
        if self._buffer.delete():
            self._history.reset_navigation()
            self._redraw_line()
        return None

    def _cancel_line(self) -> None:
        self._buffer.clear()
        self._history.reset_navigation()
        self._send("^C\r\n")
        self._send(self._prompt)

    def _kill_to_start(self) -> None:
        killed = self._buffer.kill_to_start()
        if killed:
            self._kill_buffer = killed
            self._history.reset_navigation()
            self._redraw_line()

    def _kill_to_end(self) -> None:
        killed = self._buffer.kill_to_end()
        if killed:
            self._kill_buffer = killed
            self._history.reset_navigation()
            self._redraw_line()

    def _delete_word_before_cursor(self) -> None:
        killed = self._buffer.delete_word_before_cursor()
        if killed:
            self._kill_buffer = killed
            self._history.reset_navigation()
            self._redraw_line()

    def _yank(self) -> None:
        if not self._kill_buffer:
            return
        self._buffer.insert(self._kill_buffer)
        self._history.reset_navigation()
        self._redraw_line()

    def _transpose_characters(self) -> None:
        if self._buffer.transpose_characters():
            self._history.reset_navigation()
            self._redraw_line()

    def _clear_screen(self) -> None:
        self._send("\x1b[H\x1b[2J")
        self._redraw_line()

    def _mark_unsupported(self) -> None:
        self._send("\a")

    def _complete(self) -> None:
        self._send("\a")

    def _undo_unsupported(self) -> None:
        self._send("\a")

    def _start_reverse_search(self) -> None:
        self._reverse_search = ReverseSearchState(original_line=self._buffer.line)
        self._render_reverse_search()

    def _handle_reverse_search_byte(self, byte: int) -> EditorEvent | None:
        search = self._reverse_search
        if search is None:
            return None
        if byte in {10, 13}:
            command = search.match or search.original_line
            self._reverse_search = None
            self._replace_line(command)
            return self._submit_line()
        if byte in {3, 7, 27}:
            original = search.original_line
            self._reverse_search = None
            self._replace_line(original)
            return None
        if byte == 18:
            self._cycle_reverse_search()
            return None
        if byte in {8, 127}:
            if search.query:
                search.query = search.query[:-1]
                search.match_index = None
                self._update_reverse_search_match()
                self._render_reverse_search()
            return None
        if 32 <= byte <= 126:
            search.query += chr(byte)
            search.match_index = None
            self._update_reverse_search_match()
            self._render_reverse_search()
        return None

    def _cycle_reverse_search(self) -> None:
        search = self._reverse_search
        if search is None:
            return
        self._update_reverse_search_match(start_before=search.match_index)
        self._render_reverse_search()

    def _update_reverse_search_match(self, start_before: int | None = None) -> None:
        search = self._reverse_search
        if search is None:
            return
        search.match, search.match_index = self._history.reverse_match(
            search.query,
            start_before=start_before,
        )

    def _render_reverse_search(self) -> None:
        search = self._reverse_search
        if search is None:
            return
        status = "reverse-i-search" if search.match else "failing reverse-i-search"
        line = f"({status})`{search.query}': {search.match}"
        self._send(f"\r{line}\x1b[K")

    def _redraw_line(self) -> None:
        suffix_length = len(self._buffer.line) - self._buffer.cursor
        move_to_cursor = "\x1b[D" * suffix_length
        self._send(f"\r{self._prompt}{self._buffer.line}\x1b[K{move_to_cursor}")


class BashLineEditor(ReadlineEditorBase):
    pass


class SecretLineEditor(ReadlineEditorBase):
    def handle_control_byte(self, byte: int) -> EditorEvent | None:
        # Secret prompts do not offer history. In particular, readline's
        # reverse-search query rendering would otherwise echo newly typed text.
        if byte in {14, 16, 18}:
            return None
        return super().handle_control_byte(byte)

    def _submit_line(self) -> EditorEvent:
        self._send("\r\n")
        command = self._buffer.line
        self._buffer.clear()
        return EditorEvent(command=command)

    def _append_printable(self, byte: int) -> None:
        self._buffer.insert(chr(byte))
        self._history.reset_navigation()

    def _redraw_line(self) -> None:
        self._send(f"\r{self._prompt}\x1b[K")


class CompletionLineEditor(ReadlineEditorBase):
    def __init__(
        self,
        send: SendText,
        prompt: str,
        complete: Callable[[str], str | None],
    ) -> None:
        super().__init__(send, prompt)
        self._completion_callback = complete

    def _complete(self) -> None:
        replacement = self._completion_callback(self._buffer.line)
        if replacement is None or replacement == self._buffer.line:
            self._send("\a")
            return
        self._replace_line(replacement)


@dataclass(slots=True)
class ReverseSearchState:
    original_line: str
    query: str = ""
    match: str = ""
    match_index: int | None = None
