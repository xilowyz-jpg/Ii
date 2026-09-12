"""The simulator must be pessimistic in exactly the places backtests cheat."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fxagents.brokers.paper import PaperBroker
from fxagents.types import Candle, OrderIntent, Side


@pytest.fixture
def broker():
    return PaperBroker(starting_balance=10_000.0, slippage_pips=0.0,
                       spread_override_pips={"EUR_USD": 1.0, "USD_JPY": 1.0})


def bar(ts, o, h, l, c):
    return Candle(ts=ts, open=o, high=h, low=l, close=c)


def test_an_order_fills_on_the_next_bars_open_not_the_current_close(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000))
    # Still flat: the order was only queued.
    assert broker.position("EUR_USD") is None

    next_bar = bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1005)
    broker.on_bar_open("EUR_USD", next_bar)
    pos = broker.position("EUR_USD")
    assert pos is not None
    # Bought at the ask: open + half the 1.0-pip spread.
    assert pos.avg_price == pytest.approx(1.1000 + 0.00005)


def test_buys_lift_the_ask_and_sells_hit_the_bid(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.SELL, 10_000))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1005))
    assert broker.position("EUR_USD").avg_price == pytest.approx(1.1000 - 0.00005)


def test_slippage_always_works_against_the_trade(t0):
    b = PaperBroker(slippage_pips=1.0, spread_override_pips={"EUR_USD": 0.0})
    b.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000))
    b.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    assert b.position("EUR_USD").avg_price == pytest.approx(1.1001)


def test_a_stop_is_honoured_when_the_bar_trades_through_it(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000, stop_loss=1.0950))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    broker.on_bar("EUR_USD", bar(t0 + timedelta(hours=2), 1.1000, 1.1005, 1.0940, 1.0960))

    assert broker.position("EUR_USD") is None
    trade = broker.closed_trades[0]
    assert trade.reason == "stop loss"
    assert trade.exit_price == pytest.approx(1.0950 - 0.00005)   # sold on the bid


def test_a_gap_through_the_stop_fills_at_the_gap_not_at_the_stop(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000, stop_loss=1.0950))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    # Monday open gaps 40 pips below the stop.
    broker.on_bar("EUR_USD", bar(t0 + timedelta(hours=2), 1.0910, 1.0920, 1.0900, 1.0915))

    trade = broker.closed_trades[0]
    assert trade.exit_price == pytest.approx(1.0910 - 0.00005)
    assert trade.exit_price < 1.0950     # worse than the stop, as it must be


def test_when_a_bar_hits_both_stop_and_target_the_stop_wins(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000,
                                  stop_loss=1.0950, take_profit=1.1050))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    # An outside bar touching both levels: intrabar order is unknowable, so the
    # simulator must assume the unfavourable one.
    broker.on_bar("EUR_USD", bar(t0 + timedelta(hours=2), 1.1000, 1.1060, 1.0940, 1.1000))

    assert broker.closed_trades[0].reason == "stop loss"


def test_pnl_on_a_jpy_pair_is_converted_into_the_usd_account(t0):
    b = PaperBroker(starting_balance=10_000.0, slippage_pips=0.0,
                    spread_override_pips={"USD_JPY": 0.0})
    b.submit(t0, OrderIntent("USD_JPY", Side.BUY, 10_000, take_profit=151.0))
    b.on_bar_open("USD_JPY", bar(t0 + timedelta(hours=1), 150.0, 150.2, 149.8, 150.1))
    b.on_bar("USD_JPY", bar(t0 + timedelta(hours=2), 150.1, 151.2, 150.0, 151.1))

    trade = b.closed_trades[0]
    # +1.00 JPY on 10,000 units = 10,000 JPY, converted at the 151.0 exit rate.
    assert trade.pnl == pytest.approx(10_000 * 1.0 / 151.0, rel=1e-6)


def test_equity_tracks_unrealised_pnl_and_balance_does_not(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 100_000))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    broker.on_bar("EUR_USD", bar(t0 + timedelta(hours=2), 1.1000, 1.1110, 1.0990, 1.1100))

    assert broker.balance == pytest.approx(10_000.0)         # nothing realised yet
    assert broker.equity() > 10_000.0
    assert broker.equity() == pytest.approx(10_000.0 + (1.1100 - 1.10005) * 100_000)


def test_commission_is_charged_on_entry_and_exit(t0):
    b = PaperBroker(slippage_pips=0.0, commission_per_million=50.0,
                    spread_override_pips={"EUR_USD": 0.0})
    b.submit(t0, OrderIntent("EUR_USD", Side.BUY, 1_000_000, stop_loss=1.0950))
    b.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    assert b.balance == pytest.approx(10_000.0 - 50.0)        # entry leg
    b.on_bar("EUR_USD", bar(t0 + timedelta(hours=2), 1.1000, 1.1005, 1.0940, 1.0960))
    assert b.closed_trades[0].costs == pytest.approx(50.0)    # exit leg


def test_a_second_order_in_the_same_instrument_is_rejected(broker, t0):
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000))
    broker.submit(t0, OrderIntent("EUR_USD", Side.BUY, 10_000))
    broker.on_bar_open("EUR_USD", bar(t0 + timedelta(hours=2), 1.1000, 1.1010, 1.0990, 1.1000))

    assert broker.position("EUR_USD").units == 10_000
    assert broker.rejections
