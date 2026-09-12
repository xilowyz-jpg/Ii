"""Filter agents -- they cannot open a trade, only stop one from opening."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from fxagents.agents.base import AgentContext, FilterAgent
from fxagents.indicators import ATR


# Forex session windows in UTC. Approximate on purpose: they shift by an hour
# with DST, and a strategy that depends on that hour is too fragile to trade.
SESSIONS: dict[str, tuple[time, time]] = {
    "sydney": (time(21, 0), time(6, 0)),
    "tokyo": (time(0, 0), time(9, 0)),
    "london": (time(7, 0), time(16, 0)),
    "newyork": (time(12, 0), time(21, 0)),
}


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end          # window wraps midnight


@dataclass(slots=True)
class SessionFilter(FilterAgent):
    """Only trade during the chosen sessions, and never over the weekend gap.

    Liquidity outside London/New York is thin enough that spreads eat the edge
    of most short-horizon strategies.
    """

    name: str = "session_filter"
    allowed: tuple[str, ...] = ("london", "newyork")
    block_weekend: bool = True
    # The FX week runs Sunday ~21:00 UTC to Friday ~21:00 UTC.
    week_close_hour: int = 21
    week_open_hour: int = 21

    def __post_init__(self) -> None:
        normalised = tuple(name.strip().lower() for name in self.allowed if name.strip())
        unknown = [n for n in normalised if n not in SESSIONS]
        if unknown:
            raise ValueError(
                f"unknown session(s) {unknown}; expected from {sorted(SESSIONS)}"
            )
        self.allowed = normalised

    def allows(self, ctx: AgentContext) -> tuple[bool, str]:
        ts = ctx.ts
        if self.block_weekend:
            wd, hour = ts.weekday(), ts.hour       # Mon=0 .. Sun=6
            closed = (
                wd == 5                                        # Saturday
                or (wd == 4 and hour >= self.week_close_hour)  # Friday evening
                or (wd == 6 and hour < self.week_open_hour)    # Sunday daytime
            )
            if closed:
                return False, "market closed (weekend)"

        if not self.allowed:
            return True, ""
        t = ts.time()
        for name in self.allowed:
            start, end = SESSIONS[name]
            if _in_window(t, start, end):
                return True, ""
        return False, f"outside sessions {'/'.join(self.allowed)} (utc {t:%H:%M})"


@dataclass(slots=True)
class VolatilityFilter(FilterAgent):
    """Refuse dead markets and refuse panics.

    Both extremes break position sizing: near-zero ATR produces absurdly large
    positions, and an ATR spike usually means a gap is already underway.
    """

    name: str = "volatility_filter"
    atr_period: int = 14
    lookback: int = 100
    min_percentile: float = 0.10
    max_percentile: float = 0.98
    _state: dict = field(default_factory=dict)

    def _fresh(self) -> dict:
        return {"atr": ATR(self.atr_period), "hist": []}

    def on_start(self, instruments) -> None:
        self._state = {i.symbol: self._fresh() for i in instruments}

    def allows(self, ctx: AgentContext) -> tuple[bool, str]:
        st = self._state.setdefault(ctx.symbol, self._fresh())
        atr = st["atr"].update(ctx.candle)
        if atr is None:
            return False, "atr warming up"

        hist: list[float] = st["hist"]
        hist.append(atr)
        if len(hist) > self.lookback:
            del hist[0]
        if len(hist) < max(20, self.atr_period):
            return True, ""

        rank = sum(1 for v in hist if v <= atr) / len(hist)
        if rank < self.min_percentile:
            return False, f"volatility at {rank:.0%} pct (too quiet)"
        if rank > self.max_percentile:
            return False, f"volatility at {rank:.0%} pct (spiking)"
        return True, ""


@dataclass(slots=True)
class SpreadFilter(FilterAgent):
    """Refuse a trade whose stop is small relative to the cost of crossing the spread."""

    name: str = "spread_filter"
    min_stop_to_spread: float = 4.0

    def allows(self, ctx: AgentContext) -> tuple[bool, str]:
        # The stop distance is not known at filter time; this agent instead
        # guards the instrument-level case where the spread is simply too wide
        # for the bar's own range to support a trade.
        spread = ctx.instrument.price_from_pips(ctx.instrument.typical_spread_pips)
        if spread <= 0:
            return True, ""
        if ctx.candle.range < self.min_stop_to_spread * spread:
            return False, (
                f"bar range {ctx.instrument.pips(ctx.candle.range):.1f}p < "
                f"{self.min_stop_to_spread}x spread"
            )
        return True, ""
