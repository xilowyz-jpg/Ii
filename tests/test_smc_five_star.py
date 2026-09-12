"""The five-star setup, built as a figure and then broken one star at a time.

`scenario()` draws a complete, valid setup on XAU_USD. Each flag breaks exactly
one condition, leaving the rest intact, so a test that expects a refusal can
name which star did the refusing. That is the only way to know the agent is
enforcing five separate rules rather than one rule five times.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fxagents.agents.base import AgentContext
from fxagents.agents.smc_five_star import SMCFiveStarAgent, StarReport
from fxagents.instruments import get_instrument
from fxagents.journal import Journal
from fxagents.smc import Direction
from fxagents.types import AccountState, Candle

PARIS = ZoneInfo("Europe/Paris")

# 15:10 Paris. In July that is 13:10 UTC, in January 14:10 -- the figure is
# always anchored to the LOCAL time, which is what the rule is written in.
SUMMER_TAP = datetime(2024, 7, 16, 13, 10, tzinfo=timezone.utc)
WINTER_TAP = datetime(2024, 1, 16, 14, 10, tzinfo=timezone.utc)


class Figure:
    """Draws M15 intent, renders it as the M5 bars the agent consumes.

    Timestamps are assigned last, working backwards from the moment the setup
    completes. Variants can therefore add or remove bars without sliding the
    entry out of the session window and making a test pass for the wrong
    reason.
    """

    def __init__(self) -> None:
        self.m15_bars: list[tuple[float, float, float, float]] = []
        self.m5_tail: list[tuple[float, float, float, float]] = []

    def m15(self, o: float, h: float, l: float, c: float) -> None:
        self.m15_bars.append((o, h, l, c))

    def m5(self, o: float, h: float, l: float, c: float) -> None:
        """A bar in the final M15 slot, where the exact shape matters."""
        self.m5_tail.append((o, h, l, c))

    def render(self, tap_at: datetime) -> list[Candle]:
        assert len(self.m5_tail) == 3, "the tail must be exactly one M15 slot"
        # Last bar sits at offset 15*n + 10 minutes, so `start` lands on a
        # quarter-hour boundary and the M15 aggregation reproduces the figure.
        start = tap_at - timedelta(minutes=15 * len(self.m15_bars) + 10)
        assert start.minute % 15 == 0, "tap_at must fall on :10, :25, :40 or :55"

        out: list[Candle] = []
        for slot, (o, h, l, c) in enumerate(self.m15_bars):
            base = start + timedelta(minutes=15 * slot)
            mid = (o + c) / 2
            hi, lo = max(h, mid, o, c), min(l, mid, o, c)
            out += [
                Candle(ts=base, open=o, high=max(o, mid), low=min(o, mid), close=mid),
                Candle(ts=base + timedelta(minutes=5), open=mid, high=hi, low=lo, close=mid),
                Candle(ts=base + timedelta(minutes=10), open=mid, high=max(mid, c), low=min(mid, c), close=c),
            ]
        tail_base = start + timedelta(minutes=15 * len(self.m15_bars))
        for i, (o, h, l, c) in enumerate(self.m5_tail):
            out.append(Candle(ts=tail_base + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c))
        assert out[-1].ts == tap_at
        return out


def scenario(
    *,
    tap_at: datetime = SUMMER_TAP,
    touch_the_block: bool = False,
    fill_the_imbalance: bool = False,
    liquidity_behind: bool = False,
    newer_opposite_block: bool = False,
    no_engulfing: bool = False,
) -> list[Candle]:
    f = Figure()

    # A steady uptrend. Monotonic on purpose: it creates no swings, no blocks
    # and no liquidity of its own, and leaves the moving average below.
    for i in range(40):
        lvl = 1940 + 1.45 * i
        if liquidity_behind and i in (34, 37):
            # Two equal lows at 1988: below the block's 1998 floor, and close
            # enough to it to be worth sweeping.
            f.m15(lvl, lvl + 1.4, 1988.0, lvl + 1.0)
        else:
            f.m15(lvl, lvl + 1.4, lvl - 0.2, lvl + 1.0)

    f.m15(1998, 2020, 1998, 2005)      # swing HIGH at 2020
    f.m15(2005, 2006, 2003, 2004)
    f.m15(2004, 2005, 2001, 2002)
    f.m15(2002, 2003, 2000, 2001)
    f.m15(2005, 2006, 1998, 1999)      # <-- THE ORDER BLOCK (last down candle)
    f.m15(1999, 2014, 1999, 2013)      # impulse

    if fill_the_imbalance:
        # Both legs trade back over the previous bar's extreme, so the impulse
        # leaves no untraded space anywhere -- not just at the first gap.
        f.m15(2013, 2030, 2006, 2028)
        f.m15(2028, 2032, 2013, 2030)
    else:
        f.m15(2013, 2030, 2012, 2028)  # BOS above 2020, and an FVG 2006 -> 2012
        f.m15(2028, 2032, 2026, 2030)

    if newer_opposite_block:
        # Build a bearish block ABOVE the zone, so it becomes the most recent
        # block on the chart without ever touching 1998-2006.
        f.m15(2030, 2033, 2029, 2032)
        f.m15(2032, 2033, 2022, 2023)  # swing LOW at 2022
        f.m15(2023, 2030, 2023, 2029)
        f.m15(2029, 2031, 2028, 2030)  # confirms the swing
        f.m15(2030, 2032, 2029, 2031)  # <-- the bearish block (last up candle)
        f.m15(2031, 2031, 2018, 2019)  # impulse down through 2022
    elif touch_the_block:
        f.m15(2030, 2031, 2003, 2025)  # dips into the zone and leaves again
        f.m15(2025, 2026, 2018, 2019)
    else:
        f.m15(2030, 2031, 2024, 2025)
        f.m15(2025, 2026, 2018, 2019)

    f.m15(2019, 2020, 2012, 2013)
    f.m15(2013, 2014, 2008, 2009)

    # The entry, drawn at M5 so the confirmation candle is explicit.
    f.m5(2009, 2009.5, 2006.5, 2007.0)
    f.m5(2007, 2007.2, 2004.0, 2004.5)         # down into the zone
    if no_engulfing:
        f.m5(2004.5, 2006.0, 2004.0, 2005.5)   # a weak up bar, engulfs nothing
    else:
        f.m5(2004.0, 2012.0, 2003.5, 2011.0)   # engulfing
    return f.render(tap_at)


def run(candles: list[Candle], **agent_kwargs):
    """Feed the figure to the agent; return (signals, agent)."""
    params = dict(
        bias_timeframes=("M15",), ma_period=20, ob_timeframe="M15",
        max_liquidity_distance=10.0,
    )
    params.update(agent_kwargs)
    agent = SMCFiveStarAgent(**params)
    instrument = get_instrument("XAU_USD")
    agent.on_start([instrument])
    journal = Journal()
    signals = []
    for candle in candles:
        ctx = AgentContext(
            ts=candle.ts, instrument=instrument, candle=candle, history=[candle],
            account=AccountState("USD", 10_000.0, 10_000.0),
            position=None, journal=journal,
        )
        sig = agent.on_bar(ctx)
        if sig is not None and sig.direction != 0:
            signals.append(sig)
    return signals, agent


def last_candidate(agent: SMCFiveStarAgent) -> StarReport:
    """The final report that actually had a block to judge."""
    with_stars = [r for r in agent.reports if r.stars]
    assert with_stars, "the agent never found a candidate block at all"
    return with_stars[-1]


# ---- the complete setup ------------------------------------------------

def test_a_complete_five_star_setup_fires_exactly_once():
    signals, agent = run(scenario())
    assert len(signals) == 1
    assert signals[0].direction == pytest.approx(1.0)
    assert agent.complete_setups()


def test_the_signal_names_the_block_it_came_from():
    signals, _ = run(scenario())
    assert "1998.00-2006.00" in signals[0].reason


def test_the_stop_sits_beyond_the_far_edge_of_the_block():
    """Price closing through the block means the idea was wrong."""
    signals, _ = run(scenario())
    signal = signals[0]
    entry = 2011.0                       # the engulfing candle's close
    stop = entry - signal.stop_distance
    assert stop < 1998.0, "the stop must sit below the block, not inside it"
    assert stop == pytest.approx(1998.0 - 0.3)     # far edge minus the buffer


def test_the_stars_light_up_in_order_as_the_setup_completes():
    _, agent = run(scenario())
    counts = [r.count for r in agent.reports if r.stars]
    assert max(counts) == 6
    assert 5 in counts, "there should be a bar where only the confirmation is missing"


# ---- one star at a time ------------------------------------------------

def test_star_1_a_block_price_already_returned_to_is_refused():
    signals, agent = run(scenario(touch_the_block=True))
    assert signals == []
    assert last_candidate(agent).stars["1_untouched"] is False


def test_star_2_a_move_that_left_no_imbalance_is_refused():
    signals, agent = run(scenario(fill_the_imbalance=True))
    assert signals == []
    assert last_candidate(agent).stars["2_imbalance"] is False


def test_star_3_liquidity_resting_behind_the_block_is_refused():
    signals, agent = run(scenario(liquidity_behind=True))
    assert signals == []
    report = last_candidate(agent)
    assert report.stars["3_no_liquidity_behind"] is False
    assert "pool" in report.notes["3_no_liquidity_behind"]


def test_star_4_a_more_recent_opposing_block_is_refused():
    signals, agent = run(scenario(newer_opposite_block=True))
    assert signals == []


def test_star_5_the_same_setup_outside_the_window_is_refused():
    """Identical price action, completing three hours earlier in the day."""
    early = scenario(tap_at=SUMMER_TAP - timedelta(hours=3))
    signals, agent = run(early)
    assert signals == []
    report = last_candidate(agent)
    assert report.stars["5_session_window"] is False


def test_the_confirmation_candle_is_required():
    signals, agent = run(scenario(no_engulfing=True))
    assert signals == []
    assert last_candidate(agent).stars["6_engulfing"] is False


def test_relaxing_a_star_lets_the_same_figure_through():
    """Proof the refusals above come from the stars and not from something else."""
    assert run(scenario(no_engulfing=True))[0] == []
    assert run(scenario(no_engulfing=True), require_engulfing=False)[0] != []


# ---- bias --------------------------------------------------------------

def test_no_trade_while_the_bias_timeframes_disagree():
    signals, _ = run(scenario(), bias_timeframes=("M15", "H4"), ma_period=20)
    assert signals == []


def test_a_bearish_bias_refuses_a_bullish_block():
    """The moving average is the gate: below it, bullish blocks are not traded."""
    candles = scenario()
    _, agent = run(candles, ma_period=3)     # a short MA the pullback drops under
    assert not agent.complete_setups()


# ---- daylight saving ---------------------------------------------------

def test_the_window_follows_paris_across_the_dst_boundary():
    """15:00-17:00 Paris is 13:00-15:00 UTC in July and 14:00-16:00 in January.

    A fixed UTC offset would trade the wrong hour for half the year.
    """
    signals, _ = run(scenario(tap_at=WINTER_TAP))       # 14:10 UTC = 15:10 Paris
    assert len(signals) == 1

    # The same UTC hour that works in July is an hour too early in January.
    too_early = WINTER_TAP - timedelta(hours=1)          # 13:10 UTC = 14:10 Paris
    assert run(scenario(tap_at=too_early))[0] == []


def test_the_summer_and_winter_taps_land_on_different_utc_hours():
    summer = scenario()[-1].ts
    winter = scenario(tap_at=WINTER_TAP)[-1].ts
    assert summer.hour != winter.hour
    assert summer.astimezone(PARIS).hour == winter.astimezone(PARIS).hour


# ---- reporting ---------------------------------------------------------

def test_the_failure_histogram_explains_a_quiet_run():
    _, agent = run(scenario(no_engulfing=True))
    counts = agent.failure_counts()
    assert counts, "a run with no trades must still say why"
    assert "6_engulfing" in counts


def test_a_report_renders_the_missing_star():
    _, agent = run(scenario(no_engulfing=True))
    text = last_candidate(agent).render()
    assert "6_engulfing" in text and "5/6" in text


# ---- the full pipeline -------------------------------------------------

def test_a_five_star_setup_becomes_a_real_trade_through_the_whole_stack():
    """Signal -> consensus -> risk sizing -> broker fill -> exit at 2R.

    The unit tests above prove the agent recognises the figure. This proves the
    recognition survives the rest of the system, which is a different question:
    a signal the risk manager refuses to size is not a trade.
    """
    from fxagents.backtest.engine import Backtester
    from fxagents.presets import build_smc_registry

    candles = scenario()
    # Let the trade run: the order fills on the next bar's open, then price
    # travels the 2R distance to the target.
    last = candles[-1].ts
    entry = 2011.0
    stop_distance = entry - (1998.0 - 0.3)
    target = entry + 2 * stop_distance
    tail = [
        Candle(ts=last + timedelta(minutes=5 * i),
               open=entry + i, high=entry + i + 2, low=entry + i - 1, close=entry + i + 1)
        for i in range(1, 20)
    ]
    tail.append(Candle(ts=last + timedelta(minutes=100),
                       open=target - 2, high=target + 5, low=target - 3, close=target + 3))

    agent = SMCFiveStarAgent(bias_timeframes=("M15",), ma_period=20,
                             ob_timeframe="M15", max_liquidity_distance=10.0)
    result = Backtester(
        registry=build_smc_registry(agent=agent),
        instruments=[get_instrument("XAU_USD")],
        starting_balance=10_000.0,
    ).run((c.ts, {"XAU_USD": c}) for c in candles + tail)

    assert result.metrics.trades == 1, "the five-star setup did not become a trade"
    trade = result.broker.closed_trades[0]
    assert trade.side.value == "BUY"
    assert trade.reason == "take profit"
    assert trade.pnl > 0


def test_the_risk_manager_sizes_the_trade_from_the_block_and_rounds_down():
    """The stop is the block; the size follows from it, not the other way round.

    Gold trades in whole ounces, so on a small account the granularity is
    coarse: a 13.30 stop on a 10,000 account wants 7.5 ounces and can only
    have 7. The rounding must always go DOWN -- taking 8 would risk 6% more
    than the setting allows, every trade, forever.
    """
    from fxagents.backtest.engine import Backtester
    from fxagents.presets import build_smc_registry

    candles = scenario()
    last = candles[-1].ts
    tail = [Candle(ts=last + timedelta(minutes=5), open=2011, high=2013, low=2010, close=2012)]

    agent = SMCFiveStarAgent(bias_timeframes=("M15",), ma_period=20,
                             ob_timeframe="M15", max_liquidity_distance=10.0)
    result = Backtester(
        registry=build_smc_registry(agent=agent),
        instruments=[get_instrument("XAU_USD")],
        starting_balance=10_000.0,
    ).run((c.ts, {"XAU_USD": c}) for c in candles + tail)

    position_fills = [f for f in result.broker.fills if f.tag]
    assert position_fills, "no entry fill was recorded"
    fill = position_fills[0]

    budget = 10_000.0 * 0.01
    stop_distance = 13.3
    risked = fill.units * stop_distance
    assert fill.units == float(int(fill.units)), "gold trades in whole ounces"
    assert risked <= budget, "rounding up would over-risk every single trade"
    assert risked > budget - stop_distance, "rounded down by more than one unit"


def test_the_target_sits_at_twice_the_stop_distance():
    _, agent = run(scenario())
    from fxagents.presets import build_smc_registry
    limits = build_smc_registry().risk.limits
    assert limits.take_profit_r == 2.0
