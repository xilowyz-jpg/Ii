"""Streaming technical indicators.

Each indicator consumes one value (or one candle) at a time and returns its
current reading, or None while it is still warming up. Streaming rather than
vectorised on purpose: the backtest and the live loop then run the exact same
code path, so a strategy cannot accidentally see the future.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from fxagents.types import Candle


class Indicator:
    """Base class. `ready` tells you whether `value` means anything yet."""

    __slots__ = ()

    @property
    def ready(self) -> bool:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    @property
    def value(self) -> float | None:  # pragma: no cover
        raise NotImplementedError


@dataclass(slots=True)
class SMA(Indicator):
    period: int
    _window: deque = field(init=False)
    _sum: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("period must be >= 1")
        self._window = deque(maxlen=self.period)

    def update(self, x: float) -> float | None:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(x)
        self._sum += x
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._window) == self.period

    @property
    def value(self) -> float | None:
        return self._sum / self.period if self.ready else None


@dataclass(slots=True)
class EMA(Indicator):
    period: int
    _value: float | None = field(init=False, default=None)
    _seen: int = field(init=False, default=0)
    _seed: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("period must be >= 1")

    @property
    def alpha(self) -> float:
        return 2.0 / (self.period + 1.0)

    def update(self, x: float) -> float | None:
        self._seen += 1
        if self._seen < self.period:
            # Seed with an SMA so the first reading is not dominated by bar 1.
            self._seed += x
            return None
        if self._seen == self.period:
            self._seed += x
            self._value = self._seed / self.period
            return self._value
        self._value = self._value + self.alpha * (x - self._value)
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value


@dataclass(slots=True)
class RSI(Indicator):
    """Wilder's RSI."""

    period: int = 14
    _prev: float | None = field(init=False, default=None)
    _avg_gain: float | None = field(init=False, default=None)
    _avg_loss: float | None = field(init=False, default=None)
    _gains: list[float] = field(init=False, default_factory=list)
    _losses: list[float] = field(init=False, default_factory=list)
    _value: float | None = field(init=False, default=None)

    def update(self, x: float) -> float | None:
        if self._prev is None:
            self._prev = x
            return None
        change = x - self._prev
        self._prev = x
        gain, loss = max(change, 0.0), max(-change, 0.0)

        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self._value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self._value = 100.0 - 100.0 / (1.0 + rs)
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value


@dataclass(slots=True)
class ATR(Indicator):
    """Wilder's Average True Range -- the volatility unit every stop uses."""

    period: int = 14
    _prev_close: float | None = field(init=False, default=None)
    _value: float | None = field(init=False, default=None)
    _seed: list[float] = field(init=False, default_factory=list)

    def true_range(self, c: Candle) -> float:
        if self._prev_close is None:
            return c.high - c.low
        return max(
            c.high - c.low,
            abs(c.high - self._prev_close),
            abs(c.low - self._prev_close),
        )

    def update(self, c: Candle) -> float | None:
        tr = self.true_range(c)
        self._prev_close = c.close
        if self._value is None:
            self._seed.append(tr)
            if len(self._seed) < self.period:
                return None
            self._value = sum(self._seed) / self.period
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value


@dataclass(slots=True)
class Bollinger(Indicator):
    period: int = 20
    num_std: float = 2.0
    _window: deque = field(init=False)

    def __post_init__(self) -> None:
        self._window = deque(maxlen=self.period)

    def update(self, x: float) -> tuple[float, float, float] | None:
        self._window.append(x)
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._window) == self.period

    @property
    def std(self) -> float | None:
        if not self.ready:
            return None
        mean = sum(self._window) / self.period
        var = sum((v - mean) ** 2 for v in self._window) / self.period
        return var ** 0.5

    @property
    def value(self) -> tuple[float, float, float] | None:
        """(lower, middle, upper) or None."""
        if not self.ready:
            return None
        mean = sum(self._window) / self.period
        sd = self.std or 0.0
        return (mean - self.num_std * sd, mean, mean + self.num_std * sd)

    def zscore(self, x: float) -> float | None:
        """How many standard deviations `x` sits from the middle band."""
        if not self.ready:
            return None
        mean = sum(self._window) / self.period
        sd = self.std or 0.0
        if sd == 0:
            return 0.0
        return (x - mean) / sd


@dataclass(slots=True)
class Donchian(Indicator):
    """Rolling high/low channel -- the breakout agent's raw material."""

    period: int = 20
    _highs: deque = field(init=False)
    _lows: deque = field(init=False)

    def __post_init__(self) -> None:
        self._highs = deque(maxlen=self.period)
        self._lows = deque(maxlen=self.period)

    def update(self, c: Candle) -> tuple[float, float] | None:
        self._highs.append(c.high)
        self._lows.append(c.low)
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._highs) == self.period

    @property
    def value(self) -> tuple[float, float] | None:
        """(lowest low, highest high) over the window, or None."""
        if not self.ready:
            return None
        return (min(self._lows), max(self._highs))


@dataclass(slots=True)
class ADX(Indicator):
    """Wilder's ADX -- used as a trend-strength filter, never as a direction."""

    period: int = 14
    _prev: Candle | None = field(init=False, default=None)
    _atr: ATR = field(init=False)
    _plus_dm: float | None = field(init=False, default=None)
    _minus_dm: float | None = field(init=False, default=None)
    _dm_seed: list[tuple[float, float]] = field(init=False, default_factory=list)
    _dx_seed: list[float] = field(init=False, default_factory=list)
    _value: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._atr = ATR(self.period)

    def update(self, c: Candle) -> float | None:
        atr = self._atr.update(c)
        prev, self._prev = self._prev, c
        if prev is None:
            return None

        up_move = c.high - prev.high
        down_move = prev.low - c.low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        if self._plus_dm is None:
            self._dm_seed.append((plus_dm, minus_dm))
            if len(self._dm_seed) < self.period:
                return None
            self._plus_dm = sum(p for p, _ in self._dm_seed) / self.period
            self._minus_dm = sum(m for _, m in self._dm_seed) / self.period
        else:
            self._plus_dm = (self._plus_dm * (self.period - 1) + plus_dm) / self.period
            self._minus_dm = (self._minus_dm * (self.period - 1) + minus_dm) / self.period

        if not atr:
            return self._value

        plus_di = 100.0 * self._plus_dm / atr
        minus_di = 100.0 * self._minus_dm / atr
        denom = plus_di + minus_di
        dx = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom

        if self._value is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) < self.period:
                return None
            self._value = sum(self._dx_seed) / self.period
        else:
            self._value = (self._value * (self.period - 1) + dx) / self.period
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value
