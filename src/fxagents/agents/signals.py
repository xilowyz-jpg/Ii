"""Signal agents -- three uncorrelated ways of reading the same chart.

They are meant to disagree. Trend and mean-reversion are near-opposites by
construction, and the portfolio agent's job is to notice when they cancel out
(a chop regime, where doing nothing is the right trade).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fxagents.agents.base import AgentContext, SignalAgent
from fxagents.indicators import ADX, ATR, RSI, Bollinger, Donchian, EMA
from fxagents.types import Signal


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass(slots=True)
class TrendFollowingAgent(SignalAgent):
    """EMA crossover, gated by ADX so it stays out of directionless markets."""

    name: str = "trend_following"
    fast: int = 20
    slow: int = 50
    adx_period: int = 14
    adx_threshold: float = 20.0
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    _state: dict = field(default_factory=dict)

    def on_start(self, instruments) -> None:
        self._state = {
            i.symbol: {
                "fast": EMA(self.fast),
                "slow": EMA(self.slow),
                "adx": ADX(self.adx_period),
                "atr": ATR(self.atr_period),
            }
            for i in instruments
        }

    def on_bar(self, ctx: AgentContext) -> Signal | None:
        st = self._state.setdefault(
            ctx.symbol,
            {
                "fast": EMA(self.fast),
                "slow": EMA(self.slow),
                "adx": ADX(self.adx_period),
                "atr": ATR(self.atr_period),
            },
        )
        fast = st["fast"].update(ctx.price)
        slow = st["slow"].update(ctx.price)
        adx = st["adx"].update(ctx.candle)
        atr = st["atr"].update(ctx.candle)

        if fast is None or slow is None or adx is None or atr is None or atr <= 0:
            return None

        if adx < self.adx_threshold:
            return self.flat_signal(ctx, f"adx {adx:.1f} < {self.adx_threshold} (no trend)")

        separation = (fast - slow) / atr          # crossover distance in ATRs
        direction = 1.0 if separation > 0 else -1.0
        # Confidence grows with both the separation and the trend strength.
        conf = _clamp(abs(separation) / 1.5) * _clamp((adx - self.adx_threshold) / 25.0)
        if conf < 0.05:
            return self.flat_signal(ctx, "crossover too shallow")

        return Signal(
            agent=self.name,
            instrument=ctx.symbol,
            direction=direction,
            confidence=conf,
            reason=f"ema{self.fast}/{self.slow} sep={separation:+.2f}atr adx={adx:.1f}",
            stop_distance=self.atr_stop_mult * atr,
        )


@dataclass(slots=True)
class MeanReversionAgent(SignalAgent):
    """Fade Bollinger extremes, but only while ADX says the market is ranging."""

    name: str = "mean_reversion"
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    adx_period: int = 14
    adx_max: float = 25.0
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    _state: dict = field(default_factory=dict)

    def _fresh(self) -> dict:
        return {
            "bb": Bollinger(self.bb_period, self.bb_std),
            "rsi": RSI(self.rsi_period),
            "adx": ADX(self.adx_period),
            "atr": ATR(self.atr_period),
        }

    def on_start(self, instruments) -> None:
        self._state = {i.symbol: self._fresh() for i in instruments}

    def on_bar(self, ctx: AgentContext) -> Signal | None:
        st = self._state.setdefault(ctx.symbol, self._fresh())
        bb = st["bb"]
        # zscore must be read against the window *including* this bar, which is
        # how a live feed would see it, so update first and then measure.
        bb.update(ctx.price)
        z = bb.zscore(ctx.price)
        rsi = st["rsi"].update(ctx.price)
        adx = st["adx"].update(ctx.candle)
        atr = st["atr"].update(ctx.candle)

        if z is None or rsi is None or adx is None or atr is None or atr <= 0:
            return None

        if adx > self.adx_max:
            return self.flat_signal(ctx, f"adx {adx:.1f} > {self.adx_max} (trending, stand aside)")

        stretched_up = z >= self.bb_std and rsi >= self.rsi_high
        stretched_down = z <= -self.bb_std and rsi <= self.rsi_low
        if not (stretched_up or stretched_down):
            return self.flat_signal(ctx, f"z={z:+.2f} rsi={rsi:.0f} (inside bands)")

        direction = -1.0 if stretched_up else 1.0
        # Further from the mean and more extreme RSI -> more confident.
        z_conf = _clamp((abs(z) - self.bb_std) / 1.5 + 0.3)
        rsi_extreme = (rsi - self.rsi_high) / (100 - self.rsi_high) if stretched_up \
            else (self.rsi_low - rsi) / self.rsi_low
        conf = _clamp(0.5 * z_conf + 0.5 * _clamp(rsi_extreme + 0.3))

        return Signal(
            agent=self.name,
            instrument=ctx.symbol,
            direction=direction,
            confidence=conf,
            reason=f"z={z:+.2f} rsi={rsi:.0f} adx={adx:.1f}",
            stop_distance=self.atr_stop_mult * atr,
        )


@dataclass(slots=True)
class BreakoutAgent(SignalAgent):
    """Donchian channel break, sized against the volatility that produced it."""

    name: str = "breakout"
    channel: int = 20
    atr_period: int = 14
    min_break_atr: float = 0.1     # ignore breaks that barely clear the channel
    atr_stop_mult: float = 2.5
    _state: dict = field(default_factory=dict)

    def _fresh(self) -> dict:
        return {"dc": Donchian(self.channel), "atr": ATR(self.atr_period)}

    def on_start(self, instruments) -> None:
        self._state = {i.symbol: self._fresh() for i in instruments}

    def on_bar(self, ctx: AgentContext) -> Signal | None:
        st = self._state.setdefault(ctx.symbol, self._fresh())
        dc, atr_ind = st["dc"], st["atr"]

        # Read the channel BEFORE folding this bar in: a bar can't break a
        # channel it is itself part of.
        channel = dc.value
        atr = atr_ind.update(ctx.candle)
        dc.update(ctx.candle)

        if channel is None or atr is None or atr <= 0:
            return None

        low, high = channel
        if ctx.price > high:
            excess = (ctx.price - high) / atr
            direction = 1.0
        elif ctx.price < low:
            excess = (low - ctx.price) / atr
            direction = -1.0
        else:
            return self.flat_signal(ctx, f"inside {self.channel}-bar channel")

        if excess < self.min_break_atr:
            return self.flat_signal(ctx, f"break of only {excess:.2f} atr")

        conf = _clamp(0.35 + excess / 1.2)
        return Signal(
            agent=self.name,
            instrument=ctx.symbol,
            direction=direction,
            confidence=conf,
            reason=f"{self.channel}-bar break by {excess:.2f} atr",
            stop_distance=self.atr_stop_mult * atr,
        )
