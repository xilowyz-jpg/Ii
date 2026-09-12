"""Data sources and frame alignment."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator

from fxagents.types import Candle

Frame = tuple[datetime, dict[str, Candle]]


class DataSource(ABC):
    """Produces closed bars for one instrument."""

    @abstractmethod
    def candles(self, instrument: str, granularity: str, count: int) -> list[Candle]:
        ...


def align(series: dict[str, list[Candle]]) -> Iterator[Frame]:
    """Merge per-instrument series into timestamp-ordered frames.

    Instruments that have no bar at a given timestamp are simply absent from
    that frame -- the runner skips them rather than carrying a stale price.
    """
    indexed: dict[str, dict[datetime, Candle]] = {
        sym: {c.ts: c for c in candles} for sym, candles in series.items()
    }
    stamps = sorted({ts for per_sym in indexed.values() for ts in per_sym})
    for ts in stamps:
        bars = {sym: per_sym[ts] for sym, per_sym in indexed.items() if ts in per_sym}
        if bars:
            yield ts, bars


GRANULARITY_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D": 1440,
}


def granularity_minutes(granularity: str) -> int:
    g = granularity.upper()
    if g not in GRANULARITY_MINUTES:
        raise ValueError(
            f"unknown granularity {granularity!r}; expected one of "
            f"{', '.join(GRANULARITY_MINUTES)}"
        )
    return GRANULARITY_MINUTES[g]
