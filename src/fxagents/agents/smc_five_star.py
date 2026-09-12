"""The five-star SMC setup.

Five conditions, all of which must hold, plus an engulfing confirmation. Miss
one and there is no trade -- that is the whole idea, so nothing here is
"nearly" satisfied.

    1. the order block has not been touched since it formed
    2. the impulse out of it left an imbalance (a fair value gap)
    3. there is no liquidity resting behind it
    4. it is the LAST order block in the direction of the bias
    5. the entry happens inside the Paris 15:00-17:00 window
    +  an engulfing candle confirms the tap before entering

Bias comes from price against a 200 MA on the higher timeframes -- daily, H4
and M30 must agree. The block is detected on M15; the engulfing confirmation is
read on whatever timeframe the feed runs at (M5 by default).

Every evaluation produces a `StarReport` naming exactly which star failed. On a
ruleset this strict almost every bar is a refusal, and "no trades" is useless
feedback without knowing which condition did the refusing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fxagents.agents.base import AgentContext, SignalAgent
from fxagents.indicators import SMA
from fxagents.smc import (
    Direction,
    OrderBlock,
    find_liquidity,
    find_order_blocks,
    find_swings,
    is_engulfing,
    is_untouched,
    liquidity_behind,
)
from fxagents.timeframes import MultiTimeframe
from fxagents.types import Candle, Signal

STAR_NAMES = (
    "1_untouched",
    "2_imbalance",
    "3_no_liquidity_behind",
    "4_last_block",
    "5_session_window",
    "6_engulfing",
)


@dataclass(slots=True)
class StarReport:
    """Which stars lined up, and why the others did not."""

    ts: datetime
    instrument: str
    bias: Direction | None = None
    block: OrderBlock | None = None
    stars: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return sum(1 for v in self.stars.values() if v)

    @property
    def complete(self) -> bool:
        return bool(self.stars) and all(self.stars.values())

    def first_failure(self) -> str | None:
        for name in STAR_NAMES:
            if name in self.stars and not self.stars[name]:
                return name
        return None

    def render(self) -> str:
        marks = " ".join(
            f"{'*' if self.stars.get(n) else '.'}{n.split('_')[0]}"
            for n in STAR_NAMES if n in self.stars
        )
        failure = self.first_failure()
        tail = "" if failure is None else f"  <- {failure}: {self.notes.get(failure, '')}"
        return f"{self.count}/{len(self.stars)} [{marks}]{tail}"


@dataclass(slots=True)
class SMCFiveStarAgent(SignalAgent):
    name: str = "smc_five_star"

    # --- timeframes -----------------------------------------------------
    bias_timeframes: tuple[str, ...] = ("D", "H4", "M30")
    ob_timeframe: str = "M15"
    ma_period: int = 200
    daily_offset_minutes: int = 0        # 0 = UTC midnight; 1260 = 21:00 UTC FX close

    # --- the session window ---------------------------------------------
    session_tz: str = "Europe/Paris"
    session_start: time = time(15, 0)
    session_end: time = time(17, 0)

    # --- detection tuning ------------------------------------------------
    swing_strength: int = 2
    impulse_window: int = 5
    use_wicks: bool = True
    ob_history: int = 400
    liquidity_tolerance: float = 0.5      # price units; ~50 cents on gold
    max_liquidity_distance: float | None = 30.0
    stop_buffer: float = 0.3              # beyond the block's far edge

    # --- which stars to enforce (all on by default) ----------------------
    require_untouched: bool = True
    require_imbalance: bool = True
    require_no_liquidity_behind: bool = True
    require_last_block: bool = True
    require_session: bool = True
    require_engulfing: bool = True
    require_bias_agreement: bool = True

    # --- state ------------------------------------------------------------
    # Three separate records, because they answer different questions:
    #   `setups`  -- every complete setup, never dropped; these are the trades.
    #   `reports` -- a rolling window of recent evaluations, for inspection.
    #   `_tally`  -- a running count over EVERY bar, so the histogram describes
    #                the whole run rather than whichever slice happened to fit.
    setups: list[StarReport] = field(default_factory=list)
    reports: deque = field(default_factory=lambda: deque(maxlen=5000))
    keep_reports: int = 5000
    _tally: dict[str, int] = field(default_factory=dict)
    _state: dict = field(default_factory=dict)

    # ---- lifecycle ------------------------------------------------------

    def _fresh(self) -> dict:
        timeframes = list(dict.fromkeys([*self.bias_timeframes, self.ob_timeframe]))
        return {
            "mtf": MultiTimeframe(
                timeframes,
                daily_offset_minutes=self.daily_offset_minutes,
                history=max(self.ob_history, self.ma_period + 5),
            ),
            "ma": {tf: SMA(self.ma_period) for tf in self.bias_timeframes},
            "blocks": [],
            "prev": None,              # previous base candle, for the engulfing check
        }

    def on_start(self, instruments) -> None:
        self._state = {i.symbol: self._fresh() for i in instruments}
        self.reports = deque(maxlen=self.keep_reports)
        self.setups = []
        self._tally = {}

    # ---- helpers --------------------------------------------------------

    @property
    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.session_tz)

    def in_session(self, ts: datetime) -> bool:
        """Is `ts` inside the trading window, in the configured local timezone?

        Going through the timezone rather than a fixed UTC offset is the point:
        15:00 Paris is 13:00 UTC in summer and 14:00 UTC in winter, and a
        hardcoded offset silently trades the wrong hour for half the year.
        """
        local = ts.astimezone(self._zone).time()
        if self.session_start <= self.session_end:
            return self.session_start <= local < self.session_end
        return local >= self.session_start or local < self.session_end

    def bias(self, state: dict, price: float) -> tuple[Direction | None, str]:
        """Price against the 200 MA on every bias timeframe.

        The MA is built from CLOSED higher-timeframe bars only and compared to
        the current price -- exactly what a live chart shows, with no part of
        an unfinished bar in it.
        """
        readings: dict[str, float] = {}
        for tf in self.bias_timeframes:
            ma = state["ma"][tf]
            if not ma.ready:
                return None, f"{tf} ma{self.ma_period} still warming up"
            readings[tf] = ma.value

        above = [tf for tf, v in readings.items() if price > v]
        below = [tf for tf, v in readings.items() if price <= v]

        if not below:
            return Direction.BULLISH, "price above ma200 on " + ", ".join(above)
        if not above:
            return Direction.BEARISH, "price below ma200 on " + ", ".join(below)
        if self.require_bias_agreement:
            return None, f"timeframes disagree (above: {','.join(above) or '-'}; below: {','.join(below) or '-'})"
        return (Direction.BULLISH if len(above) > len(below) else Direction.BEARISH), "majority bias"

    # ---- the five stars -------------------------------------------------

    def evaluate(self, ctx: AgentContext, state: dict) -> StarReport:
        report = StarReport(ts=ctx.ts, instrument=ctx.symbol)
        bars: list[Candle] = state["mtf"][self.ob_timeframe].closed_bars

        direction, why = self.bias(state, ctx.price)
        report.bias = direction
        if direction is None:
            report.notes["bias"] = why
            return report
        report.notes["bias"] = why

        blocks: list[OrderBlock] = [b for b in state["blocks"] if b.direction is direction]
        if not blocks:
            report.notes["block"] = f"no {direction.name.lower()} order block detected yet"
            return report

        # Star 4 first: the candidate is the LAST block, and only that one.
        # Falling back to an older block when the latest fails would quietly
        # turn "five stars" into "five stars somewhere on the chart".
        block = blocks[-1]
        report.block = block
        newer_opposite = [b for b in state["blocks"] if b.confirmed_index > block.confirmed_index]
        report.stars["4_last_block"] = not newer_opposite
        if newer_opposite:
            report.notes["4_last_block"] = (
                f"a {newer_opposite[-1].direction.name.lower()} block formed more recently"
            )

        # Star 1 -- untouched.
        untouched = is_untouched(block, bars)
        report.stars["1_untouched"] = untouched
        if not untouched:
            report.notes["1_untouched"] = "price has already traded back into the zone"

        # Star 2 -- the impulse left an imbalance.
        report.stars["2_imbalance"] = block.has_fvg
        if not block.has_fvg:
            report.notes["2_imbalance"] = "the move out of the block left no fair value gap"

        # Star 3 -- nothing resting behind it.
        swings = find_swings(bars, self.swing_strength)
        pools = find_liquidity(bars, swings, tolerance=self.liquidity_tolerance)
        behind = liquidity_behind(block, pools, self.max_liquidity_distance)
        report.stars["3_no_liquidity_behind"] = not behind
        if behind:
            nearest = min(behind, key=lambda p: abs(p.price - block.far_edge()))
            report.notes["3_no_liquidity_behind"] = (
                f"{len(behind)} pool(s) behind; nearest {nearest.kind} at {nearest.price:.2f}"
            )

        # Star 5 -- the window.
        in_window = self.in_session(ctx.ts)
        report.stars["5_session_window"] = in_window
        if not in_window:
            local = ctx.ts.astimezone(self._zone)
            report.notes["5_session_window"] = f"{local:%H:%M} local, outside the window"

        # The tap: price has to actually be in the zone right now.
        tapped = ctx.candle.low <= block.top and ctx.candle.high >= block.bottom
        if not tapped:
            report.notes["tap"] = "price is not in the zone on this bar"
            report.stars["6_engulfing"] = False
            return report

        # Confirmation -- an engulfing candle in the trade direction.
        prev = state["prev"]
        engulfed = prev is not None and is_engulfing(prev, ctx.candle, direction)
        report.stars["6_engulfing"] = engulfed
        if not engulfed:
            report.notes["6_engulfing"] = "tapped the zone, waiting for an engulfing candle"
        return report

    # ---- the agent contract ---------------------------------------------

    def on_bar(self, ctx: AgentContext) -> Signal | None:
        state = self._state.setdefault(ctx.symbol, self._fresh())
        mtf: MultiTimeframe = state["mtf"]

        closed = mtf.update(ctx.candle)
        for tf, bar in closed.items():
            if tf in state["ma"]:
                state["ma"][tf].update(bar.close)

        # Re-detect blocks only when the detection timeframe prints a new bar.
        if self.ob_timeframe in closed:
            bars = mtf[self.ob_timeframe].closed_bars
            if len(bars) > self.swing_strength * 2 + self.impulse_window:
                state["blocks"] = find_order_blocks(
                    bars,
                    swing_strength=self.swing_strength,
                    impulse_window=self.impulse_window,
                    use_wicks=self.use_wicks,
                )

        report = self.evaluate(ctx, state)
        state["prev"] = ctx.candle

        key = report.first_failure() or ("complete" if report.complete else "no_candidate")
        self._tally[key] = self._tally.get(key, 0) + 1
        self.reports.append(report)
        if report.complete:
            self.setups.append(report)

        required = {
            "1_untouched": self.require_untouched,
            "2_imbalance": self.require_imbalance,
            "3_no_liquidity_behind": self.require_no_liquidity_behind,
            "4_last_block": self.require_last_block,
            "5_session_window": self.require_session,
            "6_engulfing": self.require_engulfing,
        }
        enforced = {k: v for k, v in report.stars.items() if required.get(k, True)}
        if not enforced or not all(enforced.values()):
            missing = report.first_failure()
            return self.flat_signal(ctx, report.render() if missing else "no setup")

        block = report.block
        assert block is not None and report.bias is not None

        # The stop sits beyond the block: if price closes through it, the idea
        # was wrong, and there is nothing left to be right about.
        far = block.far_edge()
        stop_distance = abs(ctx.price - far) + self.stop_buffer
        if stop_distance <= 0:
            return self.flat_signal(ctx, "degenerate stop distance")

        ctx.log(
            "signal", self.name,
            f"5/5 {report.bias.name.lower()} block {block.bottom:.2f}-{block.top:.2f} "
            f"stop {far:.2f} ({stop_distance:.2f})",
        )
        return Signal(
            agent=self.name,
            instrument=ctx.symbol,
            direction=float(int(report.bias)),
            confidence=1.0,
            reason=f"5-star {report.bias.name.lower()} OB {block.bottom:.2f}-{block.top:.2f}",
            stop_distance=stop_distance,
        )

    # ---- reporting -------------------------------------------------------

    def failure_counts(self) -> dict[str, int]:
        """How often each star was the first to fail, over every bar of the run."""
        return dict(sorted(self._tally.items(), key=lambda kv: -kv[1]))

    def complete_setups(self) -> list[StarReport]:
        return list(self.setups)

    def near_misses(self, min_stars: int = 4) -> list[StarReport]:
        """Recent evaluations that got close. Where the tuning arguments happen."""
        return [r for r in self.reports if r.stars and not r.complete and r.count >= min_stars]
