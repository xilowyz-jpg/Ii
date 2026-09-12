"""Building higher-timeframe candles from a single base feed.

A multi-timeframe strategy reads H4 and daily structure while executing on M5.
Feeding it three separate data streams invites two classic bugs: the streams
drift out of sync, and the higher-timeframe bar the strategy reads turns out to
include the future relative to the M5 bar it is trading.

So there is one feed -- the finest one -- and every higher timeframe is built
from it here. The rule that makes it safe:

    a higher-timeframe bar becomes visible only once it has CLOSED.

While the 13:00 H4 bar is forming, `closed()` still returns the 09:00 bar. A
strategy therefore cannot read the high of an H4 bar that has not finished,
which is the exact mistake that makes multi-timeframe backtests lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fxagents.types import Candle

MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D": 1440, "W": 10080,
}


def period_minutes(timeframe: str) -> int:
    tf = timeframe.upper()
    if tf not in MINUTES:
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {', '.join(MINUTES)}")
    return MINUTES[tf]


def bucket_start(ts: datetime, minutes: int, offset_minutes: int = 0) -> datetime:
    """The opening timestamp of the period `ts` falls into.

    `offset_minutes` shifts the grid, which daily bars need: the FX day
    conventionally rolls at 17:00 New York, not at 00:00 UTC.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((ts - epoch).total_seconds() // 60) - offset_minutes
    floored = (elapsed // minutes) * minutes + offset_minutes
    return epoch + timedelta(minutes=floored)


@dataclass(slots=True)
class Aggregator:
    """Folds base candles into one higher timeframe.

    Only ever exposes bars that have closed. `update` returns the bar that just
    closed, if this candle started a new period, so callers can react to a
    close rather than poll for one.
    """

    timeframe: str
    offset_minutes: int = 0
    history: int = 600
    closed_bars: list[Candle] = field(default_factory=list)
    _forming: Candle | None = field(default=None, init=False)
    _bucket: datetime | None = field(default=None, init=False)

    @property
    def minutes(self) -> int:
        return period_minutes(self.timeframe)

    def update(self, candle: Candle) -> Candle | None:
        """Fold one base candle in. Returns a higher-TF bar if one just closed."""
        bucket = bucket_start(candle.ts, self.minutes, self.offset_minutes)
        just_closed: Candle | None = None

        if self._bucket is not None and bucket != self._bucket:
            just_closed = self._forming
            if just_closed is not None:
                self.closed_bars.append(just_closed)
                if len(self.closed_bars) > self.history:
                    del self.closed_bars[0]
            self._forming = None

        if self._forming is None:
            self._forming = Candle(
                ts=bucket,
                open=candle.open, high=candle.high, low=candle.low,
                close=candle.close, volume=candle.volume,
            )
        else:
            f = self._forming
            self._forming = Candle(
                ts=f.ts,
                open=f.open,
                high=max(f.high, candle.high),
                low=min(f.low, candle.low),
                close=candle.close,
                volume=f.volume + candle.volume,
            )
        self._bucket = bucket
        return just_closed

    def closed(self, n: int = 1) -> list[Candle]:
        """The last `n` CLOSED bars, oldest first. Never the forming one."""
        return self.closed_bars[-n:] if n else list(self.closed_bars)

    @property
    def last_closed(self) -> Candle | None:
        return self.closed_bars[-1] if self.closed_bars else None

    @property
    def forming(self) -> Candle | None:
        """The in-progress bar.

        Exposed for reporting only. A strategy that reads this is reading a bar
        whose high, low and close are not yet final -- which is fine live, and
        a lookahead bug the moment the same code runs over history, because the
        backtest builds it from candles the live system had not seen yet.
        """
        return self._forming


@dataclass(slots=True)
class MultiTimeframe:
    """A bundle of aggregators driven by one base feed.

    >>> mtf = MultiTimeframe(["M15", "H4", "D"])
    >>> mtf.update(m5_candle)          # doctest: +SKIP
    >>> mtf["H4"].last_closed          # doctest: +SKIP
    """

    timeframes: list[str]
    daily_offset_minutes: int = 0
    history: int = 600
    aggregators: dict[str, Aggregator] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.aggregators = {
            tf.upper(): Aggregator(
                tf.upper(),
                offset_minutes=self.daily_offset_minutes if tf.upper() in ("D", "W") else 0,
                history=self.history,
            )
            for tf in self.timeframes
        }

    def update(self, candle: Candle) -> dict[str, Candle]:
        """Fold a base candle into every timeframe. Returns the bars that closed."""
        return {
            tf: closed
            for tf, agg in self.aggregators.items()
            if (closed := agg.update(candle)) is not None
        }

    def __getitem__(self, timeframe: str) -> Aggregator:
        return self.aggregators[timeframe.upper()]

    def __contains__(self, timeframe: str) -> bool:
        return timeframe.upper() in self.aggregators
