"""Decision journal.

Every non-trivial thing an agent decides -- including refusals -- lands here.
When a backtest does something surprising, the journal is how you find out
which agent wanted it and which one blocked it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal

Level = Literal["signal", "proposal", "order", "fill", "veto", "info"]


@dataclass(frozen=True, slots=True)
class Entry:
    ts: datetime
    level: Level
    agent: str
    instrument: str
    message: str

    def __str__(self) -> str:
        stamp = self.ts.strftime("%Y-%m-%d %H:%M")
        return f"{stamp}  {self.level:<8} {self.agent:<20} {self.instrument:<8} {self.message}"


@dataclass(slots=True)
class Journal:
    entries: list[Entry] = field(default_factory=list)
    enabled: bool = True
    max_entries: int = 50_000

    def record(self, ts: datetime, level: Level, agent: str, instrument: str, message: str) -> None:
        if not self.enabled or len(self.entries) >= self.max_entries:
            return
        self.entries.append(Entry(ts, level, agent, instrument, message))

    def of_level(self, *levels: Level) -> list[Entry]:
        wanted = set(levels)
        return [e for e in self.entries if e.level in wanted]

    def vetoes(self) -> list[Entry]:
        return self.of_level("veto")

    def tail(self, n: int = 30) -> list[Entry]:
        return self.entries[-n:]

    def render(self, entries: Iterable[Entry] | None = None) -> str:
        return "\n".join(str(e) for e in (entries if entries is not None else self.entries))
