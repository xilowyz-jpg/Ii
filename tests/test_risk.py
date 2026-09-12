"""Risk sizing and the hard limits -- the part of the system that ends accounts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.agents.base import AgentContext
from fxagents.agents.risk import RiskLimits, RiskManager
from fxagents.instruments import get_instrument
from fxagents.journal import Journal
from fxagents.types import AccountState, Candle, Position, Proposal, Side


def ctx_for(symbol="EUR_USD", price=1.1000, equity=10_000.0, positions=None, ts=None):
    inst = get_instrument(symbol)
    ts = ts or datetime(2024, 3, 5, 12, 0, tzinfo=timezone.utc)
    candle = Candle(ts=ts, open=price, high=price * 1.001, low=price * 0.999, close=price)
    account = AccountState(
        currency="USD", balance=equity, equity=equity, positions=positions or {}
    )
    return AgentContext(
        ts=ts, instrument=inst, candle=candle, history=[candle],
        account=account, position=None, journal=Journal(),
    )


def manager(**limit_kwargs) -> RiskManager:
    rm = RiskManager(limits=RiskLimits(**limit_kwargs), trailing_atr_mult=None)
    rm.on_start([get_instrument("EUR_USD"), get_instrument("USD_JPY")])
    return rm


def test_position_size_risks_exactly_the_configured_fraction():
    """10,000 equity, 1% risk, a 50-pip stop -> 20,000 units puts $100 at risk."""
    rm = manager(risk_per_trade_pct=0.01)
    ctx = ctx_for(equity=10_000.0)
    rm.on_equity_update(ctx.ts, 10_000.0)

    proposal = Proposal("EUR_USD", direction=1.0, confidence=1.0, stop_distance=0.0050)
    intent = rm.size(ctx, proposal)

    assert intent is not None
    assert intent.units == pytest.approx(20_000)
    risked = abs(ctx.price - intent.stop_loss) * intent.units
    assert risked == pytest.approx(100.0)


def test_size_scales_down_with_a_weaker_conviction():
    rm = manager(risk_per_trade_pct=0.01)
    ctx = ctx_for()
    rm.on_equity_update(ctx.ts, 10_000.0)

    strong = rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050))
    weak = rm.size(ctx, Proposal("EUR_USD", 0.3, 0.3, stop_distance=0.0050))
    assert weak.units < strong.units


def test_a_jpy_pair_is_sized_through_the_currency_conversion():
    """Risk must land in USD, not in JPY -- otherwise the size is 150x wrong."""
    rm = manager(risk_per_trade_pct=0.01, min_stop_pips=1.0)
    ctx = ctx_for("USD_JPY", price=150.0)
    rm.on_equity_update(ctx.ts, 10_000.0)

    intent = rm.size(ctx, Proposal("USD_JPY", 1.0, 1.0, stop_distance=0.50))  # 50 pips
    risked_jpy = abs(ctx.price - intent.stop_loss) * intent.units
    assert risked_jpy / 150.0 == pytest.approx(100.0, rel=1e-3)


def test_take_profit_sits_at_the_configured_r_multiple():
    rm = manager(take_profit_r=2.0)
    ctx = ctx_for()
    rm.on_equity_update(ctx.ts, 10_000.0)
    intent = rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050))

    assert intent.stop_loss == pytest.approx(1.1000 - 0.0050)
    assert intent.take_profit == pytest.approx(1.1000 + 0.0100)


def test_a_short_puts_the_stop_above_and_the_target_below():
    rm = manager()
    ctx = ctx_for()
    rm.on_equity_update(ctx.ts, 10_000.0)
    intent = rm.size(ctx, Proposal("EUR_USD", -1.0, 1.0, stop_distance=0.0050))

    assert intent.side is Side.SELL
    assert intent.stop_loss > ctx.price
    assert intent.take_profit < ctx.price


@pytest.mark.parametrize("stop_distance,why", [(0.0001, "too tight"), (0.0500, "too wide")])
def test_stops_outside_the_allowed_band_are_refused(stop_distance, why):
    rm = manager(min_stop_pips=5.0, max_stop_pips=200.0)
    ctx = ctx_for()
    rm.on_equity_update(ctx.ts, 10_000.0)
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=stop_distance)) is None
    assert ctx.journal.vetoes()


def test_the_position_count_cap_blocks_a_new_trade():
    rm = manager(max_open_positions=2)
    open_positions = {
        sym: Position(sym, 1000, 1.1, datetime.now(timezone.utc))
        for sym in ("GBP_USD", "AUD_USD")
    }
    ctx = ctx_for(positions=open_positions)
    rm.on_equity_update(ctx.ts, 10_000.0)
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is None


def test_it_refuses_to_double_up_on_an_instrument_already_held():
    rm = manager()
    held = {"EUR_USD": Position("EUR_USD", 1000, 1.1, datetime.now(timezone.utc))}
    ctx = ctx_for(positions=held)
    rm.on_equity_update(ctx.ts, 10_000.0)
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is None


def test_the_daily_loss_limit_stops_new_trades_for_the_day():
    rm = manager(daily_loss_limit_pct=0.03)
    start = datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc)
    rm.on_equity_update(start, 10_000.0)                       # day opens at 10,000
    rm.on_equity_update(start + timedelta(hours=4), 9_600.0)   # down 4%

    ctx = ctx_for(equity=9_600.0, ts=start + timedelta(hours=4))
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is None
    assert any("daily loss" in e.message for e in ctx.journal.vetoes())


def test_the_daily_limit_resets_on_the_next_day():
    rm = manager(daily_loss_limit_pct=0.03)
    day1 = datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc)
    rm.on_equity_update(day1, 10_000.0)
    rm.on_equity_update(day1 + timedelta(hours=4), 9_600.0)

    day2 = day1 + timedelta(days=1)
    rm.on_equity_update(day2, 9_600.0)
    ctx = ctx_for(equity=9_600.0, ts=day2)
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is not None


def test_the_drawdown_kill_switch_latches_on():
    rm = manager(max_drawdown_pct=0.20)
    ts = datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc)
    rm.on_equity_update(ts, 12_000.0)                 # peak
    rm.on_equity_update(ts + timedelta(days=10), 9_000.0)   # -25% from peak

    ctx = ctx_for(equity=9_000.0, ts=ts + timedelta(days=10))
    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is None
    assert rm.halted

    # Recovering some equity does not un-latch it: a killed system stays killed
    # until a human looks at why.
    rm.on_equity_update(ts + timedelta(days=20), 11_000.0)
    assert rm.halted


def test_the_currency_exposure_cap_catches_a_stacked_usd_bet():
    """Long EUR_USD and long GBP_USD are the same short-USD trade twice over."""
    rm = manager(max_currency_leverage=1.0, max_open_positions=10)
    ts = datetime(2024, 3, 5, 12, 0, tzinfo=timezone.utc)
    held = {"GBP_USD": Position("GBP_USD", 9_000, 1.2700, ts)}   # ~11.4k USD short
    ctx = ctx_for(positions=held, equity=10_000.0)
    rm.on_equity_update(ts, 10_000.0)

    assert rm.size(ctx, Proposal("EUR_USD", 1.0, 1.0, stop_distance=0.0050)) is None
    assert any("notional" in e.message for e in ctx.journal.vetoes())


def test_an_unresolvable_cross_rate_is_refused_in_strict_mode():
    rm = manager(require_exact_conversion=True)
    ctx = ctx_for("EUR_GBP", price=0.8500)
    rm.on_equity_update(ctx.ts, 10_000.0)
    assert rm.size(ctx, Proposal("EUR_GBP", 1.0, 1.0, stop_distance=0.0050)) is None


def test_an_unresolvable_cross_rate_is_flagged_but_allowed_by_default():
    rm = manager(require_exact_conversion=False)
    ctx = ctx_for("EUR_GBP", price=0.8500)
    rm.on_equity_update(ctx.ts, 10_000.0)
    intent = rm.size(ctx, Proposal("EUR_GBP", 1.0, 1.0, stop_distance=0.0050))
    assert intent is not None
    assert any("approximate" in e.message for e in ctx.journal.entries)


def test_the_trailing_stop_only_ever_ratchets_in_the_trades_favour():
    rm = RiskManager(limits=RiskLimits(), trailing_atr_mult=2.0)
    rm.on_start([get_instrument("EUR_USD")])
    ts = datetime(2024, 3, 5, 12, 0, tzinfo=timezone.utc)

    # Warm the ATR up on bars with a 20-pip range.
    for i in range(20):
        c = Candle(ts=ts + timedelta(hours=i), open=1.1000, high=1.1010, low=1.0990, close=1.1000)
        rm._atr["EUR_USD"].update(c)

    pos = Position("EUR_USD", 10_000, 1.1000, ts, stop_loss=1.0950)
    up = ctx_for(price=1.1100)
    raised = rm.trail_stop(up, pos)
    assert raised is not None and raised > 1.0950

    pos.stop_loss = raised
    down = ctx_for(price=1.1000)
    assert rm.trail_stop(down, pos) is None      # never loosened
