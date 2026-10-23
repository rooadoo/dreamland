from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class HistoryNavigator:
    entries: list[str] = field(default_factory=list)
    index: int | None = None
    draft_line: str = ""

    def reset_navigation(self) -> None:
        self.index = None
        self.draft_line = ""

    def add(self, command: str) -> None:
        if command.strip():
            self.entries.append(command)
        self.reset_navigation()

    def discard_latest(self, command: str) -> None:
        if self.entries and self.entries[-1] == command:
            self.entries.pop()
        self.reset_navigation()

    def previous(self, current_line: str) -> str | None:
        if not self.entries:
            return None
        if self.index is None:
            self.draft_line = current_line
            self.index = len(self.entries) - 1
        elif self.index > 0:
            self.index -= 1
        return self.entries[self.index]

    def next(self) -> str | None:
        if self.index is None:
            return None
        if self.index < len(self.entries) - 1:
            self.index += 1
            return self.entries[self.index]
        self.index = None
        replacement = self.draft_line
        self.draft_line = ""
        return replacement

    def reverse_match(
        self,
        query: str,
        start_before: int | None = None,
    ) -> tuple[str, int | None]:
        if not query:
            return "", None
        start = len(self.entries) - 1 if start_before is None else start_before - 1
        for index in range(start, -1, -1):
            candidate = self.entries[index]
            if query in candidate:
                return candidate, index
        return "", None
