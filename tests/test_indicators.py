"""Indicator values checked against hand-computed references."""

from __future__ import annotations

import pytest

from fxagents.indicators import ADX, ATR, EMA, RSI, Bollinger, Donchian, SMA
from tests.conftest import make_candles


def test_sma_warms_up_then_averages():
    sma = SMA(3)
    assert sma.update(1.0) is None
    assert sma.update(2.0) is None
    assert sma.update(3.0) == pytest.approx(2.0)
    assert sma.update(6.0) == pytest.approx(11 / 3)     # rolls off the 1.0


def test_ema_seeds_with_an_sma():
    ema = EMA(3)
    for v in (1.0, 2.0):
        assert ema.update(v) is None
    assert ema.update(3.0) == pytest.approx(2.0)        # seed = mean(1,2,3)
    # alpha = 2/(3+1) = 0.5 -> 2.0 + 0.5*(4-2) = 3.0
    assert ema.update(4.0) == pytest.approx(3.0)


def test_rsi_is_100_on_a_pure_uptrend_and_0_on_a_pure_downtrend():
    up, down = RSI(14), RSI(14)
    for i in range(30):
        up.update(float(i))
        down.update(float(-i))
    assert up.value == pytest.approx(100.0)
    assert down.value == pytest.approx(0.0)


def test_rsi_sits_near_50_on_an_alternating_series():
    rsi = RSI(14)
    for i in range(80):
        rsi.update(100.0 + (1.0 if i % 2 else -1.0))
    assert 45 <= rsi.value <= 55


def test_atr_on_a_constant_range_equals_that_range():
    candles = make_candles_flat_range(14 + 5, high_low=0.0010)
    atr = ATR(14)
    value = None
    for c in candles:
        value = atr.update(c)
    assert value == pytest.approx(0.0010, rel=1e-6)


def make_candles_flat_range(n: int, high_low: float):
    from datetime import datetime, timedelta, timezone

    from fxagents.types import Candle

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Constant close means true range is always the bar's own high-low.
    return [
        Candle(
            ts=base + timedelta(hours=i),
            open=1.1, high=1.1 + high_low / 2, low=1.1 - high_low / 2, close=1.1,
        )
        for i in range(n)
    ]


def test_bollinger_zscore_is_zero_on_a_flat_series():
    bb = Bollinger(20, 2.0)
    for _ in range(20):
        bb.update(1.1)
    assert bb.zscore(1.1) == pytest.approx(0.0)
    assert bb.std == pytest.approx(0.0)


def test_donchian_reports_window_extremes(t0):
    dc = Donchian(5)
    candles = make_candles(t0, [1.10, 1.12, 1.09, 1.11, 1.13])
    for c in candles:
        dc.update(c)
    low, high = dc.value
    assert high == pytest.approx(max(c.high for c in candles))
    assert low == pytest.approx(min(c.low for c in candles))


def test_donchian_window_rolls(t0):
    """A spike must leave the channel once it falls out of the window."""
    from datetime import timedelta

    from fxagents.types import Candle

    # Built directly: make_candles chains open=prev close, which would smear
    # the spike into the following bar and defeat the point of the test.
    highs = [1.11, 1.20, 1.11, 1.11, 1.11, 1.11]
    candles = [
        Candle(ts=t0 + timedelta(hours=i), open=1.10, high=h, low=1.09, close=1.10)
        for i, h in enumerate(highs)
    ]
    dc = Donchian(3)
    values = [dc.update(c) for c in candles]
    assert values[0] is None and values[1] is None   # 3-bar window still filling
    assert values[2][1] == pytest.approx(1.20)       # spike inside the window
    assert dc.value[1] == pytest.approx(1.11)        # and gone once it rolls off


def test_adx_is_high_on_a_clean_trend_and_low_on_chop(t0):
    """ADX separates a directional market from a directionless one."""
    import random

    trending = make_candles(t0, [1.10 + 0.001 * i for i in range(120)])

    # Chop needs genuinely varying highs and lows. A series whose extremes
    # repeat exactly bar after bar is degenerate -- Wilder's DM is then zero
    # on one side forever, and ADX correctly but uselessly pins at 100.
    rng = random.Random(11)
    choppy = make_candles(
        t0,
        [1.10 + rng.uniform(-0.0015, 0.0015) for _ in range(120)],
        spread_frac=0.0004,
    )

    def final_adx(candles):
        adx = ADX(14)
        value = None
        for c in candles:
            value = adx.update(c)
        return value

    assert final_adx(trending) > 40
    assert final_adx(choppy) < 30


@pytest.mark.parametrize("cls,period", [(SMA, 0), (EMA, 0), (SMA, -1)])
def test_invalid_periods_are_rejected(cls, period):
    with pytest.raises(ValueError):
        cls(period)
