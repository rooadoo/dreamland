from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LineBuffer:
    line: str = ""
    cursor: int = 0

    def clear(self) -> None:
        self.line = ""
        self.cursor = 0

    def replace(self, line: str) -> None:
        self.line = line
        self.cursor = len(line)

    def insert(self, text: str) -> None:
        self.line = self.line[: self.cursor] + text + self.line[self.cursor :]
        self.cursor += len(text)

    def backspace(self) -> bool:
        if self.cursor == 0:
            return False
        self.line = self.line[: self.cursor - 1] + self.line[self.cursor :]
        self.cursor -= 1
        return True

    def delete(self) -> bool:
        if self.cursor >= len(self.line):
            return False
        self.line = self.line[: self.cursor] + self.line[self.cursor + 1 :]
        return True

    def move_home(self) -> int:
        distance = self.cursor
        self.cursor = 0
        return distance

    def move_end(self) -> int:
        distance = len(self.line) - self.cursor
        self.cursor = len(self.line)
        return distance

    def move_left(self) -> bool:
        if self.cursor == 0:
            return False
        self.cursor -= 1
        return True

    def move_right(self) -> bool:
        if self.cursor >= len(self.line):
            return False
        self.cursor += 1
        return True

    def kill_to_start(self) -> str:
        killed = self.line[: self.cursor]
        self.line = self.line[self.cursor :]
        self.cursor = 0
        return killed

    def kill_to_end(self) -> str:
        killed = self.line[self.cursor :]
        self.line = self.line[: self.cursor]
        return killed

    def delete_word_before_cursor(self) -> str:
        if self.cursor == 0:
            return ""
        delete_from = self.cursor
        while delete_from > 0 and self.line[delete_from - 1].isspace():
            delete_from -= 1
        while delete_from > 0 and not self.line[delete_from - 1].isspace():
            delete_from -= 1
        killed = self.line[delete_from : self.cursor]
        self.line = self.line[:delete_from] + self.line[self.cursor :]
        self.cursor = delete_from
        return killed

    def transpose_characters(self) -> bool:
        if len(self.line) < 2 or self.cursor == 0:
            return False
        left = self.cursor - 2 if self.cursor == len(self.line) else self.cursor - 1
        right = left + 1
        characters = list(self.line)
        characters[left], characters[right] = characters[right], characters[left]
        self.line = "".join(characters)
        if self.cursor < len(self.line):
            self.cursor += 1
        return True
