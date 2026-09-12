"""Higher-timeframe aggregation, and the rule that keeps it honest."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.timeframes import Aggregator, MultiTimeframe, bucket_start, period_minutes
from fxagents.types import Candle

BASE = datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc)


def m5(i: int, o: float, h: float, l: float, c: float, vol: float = 10.0) -> Candle:
    return Candle(ts=BASE + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=vol)


def ramp(n: int, start: float = 2000.0, step: float = 1.0) -> list[Candle]:
    return [m5(i, start + step * i, start + step * i + 0.5,
               start + step * i - 0.5, start + step * i + 0.25) for i in range(n)]


# ---- bucketing ---------------------------------------------------------

@pytest.mark.parametrize("minute,expected", [(0, 0), (7, 0), (14, 0), (15, 15), (44, 30), (59, 45)])
def test_m15_buckets_land_on_quarter_hours(minute, expected):
    ts = datetime(2024, 3, 5, 8, minute, tzinfo=timezone.utc)
    assert bucket_start(ts, 15).minute == expected


def test_h4_buckets_land_on_the_standard_grid():
    for hour, expected in [(0, 0), (3, 0), (4, 4), (11, 8), (12, 12), (23, 20)]:
        ts = datetime(2024, 3, 5, hour, 30, tzinfo=timezone.utc)
        assert bucket_start(ts, 240).hour == expected


def test_the_daily_offset_moves_the_day_boundary():
    """The FX day rolls at 21:00 UTC, not midnight -- a 22:00 bar is tomorrow's."""
    ts = datetime(2024, 3, 5, 22, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, 1440, offset_minutes=0).day == 5
    assert bucket_start(ts, 1440, offset_minutes=21 * 60).day == 5      # bucket opens 5th 21:00
    assert bucket_start(ts, 1440, offset_minutes=21 * 60).hour == 21


def test_unknown_timeframe_is_rejected():
    with pytest.raises(ValueError, match="unknown timeframe"):
        period_minutes("M7")


# ---- aggregation -------------------------------------------------------

def test_three_m5_bars_make_one_m15_bar():
    agg = Aggregator("M15")
    bars = [
        m5(0, 2000.0, 2005.0, 1999.0, 2003.0, vol=10),
        m5(1, 2003.0, 2008.0, 2002.0, 2004.0, vol=20),
        m5(2, 2004.0, 2006.0, 1997.0, 2001.0, vol=30),
    ]
    for b in bars:
        assert agg.update(b) is None          # nothing has closed yet
    closed = agg.update(m5(3, 2001.0, 2002.0, 2000.0, 2001.5))

    assert closed is not None
    assert closed.ts == BASE
    assert closed.open == 2000.0              # first bar's open
    assert closed.close == 2001.0             # last bar's close
    assert closed.high == 2008.0              # highest high
    assert closed.low == 1997.0               # lowest low
    assert closed.volume == 60.0              # summed


def test_a_bar_is_invisible_until_it_closes():
    """The property the whole design rests on."""
    agg = Aggregator("M15")
    for b in ramp(3):                          # exactly one M15 period, still forming
        agg.update(b)

    assert agg.last_closed is None
    assert agg.closed() == []
    assert agg.forming is not None             # available, but only for reporting

    agg.update(m5(3, 2100.0, 2100.0, 2100.0, 2100.0))
    assert agg.last_closed is not None


def test_the_closed_bar_never_contains_data_from_after_its_period():
    agg = Aggregator("M15")
    bars = ramp(3) + [m5(3, 9999.0, 9999.0, 9999.0, 9999.0)]   # a spike in the NEXT period
    closed = None
    for b in bars:
        result = agg.update(b)
        if result is not None:
            closed = result
    assert closed is not None
    assert closed.high < 9000.0, "the next period's spike leaked into the closed bar"


def test_history_is_bounded():
    agg = Aggregator("M15", history=4)
    for b in ramp(60):
        agg.update(b)
    assert len(agg.closed_bars) == 4


def test_closed_bars_are_ordered_and_contiguous():
    agg = Aggregator("M15")
    for b in ramp(40):
        agg.update(b)
    bars = agg.closed()
    assert all(bars[i].ts > bars[i - 1].ts for i in range(1, len(bars)))
    assert all(bars[i].ts - bars[i - 1].ts == timedelta(minutes=15) for i in range(1, len(bars)))


def test_a_gap_in_the_base_feed_does_not_invent_bars():
    """A weekend gap means no bar, not an empty one."""
    agg = Aggregator("M15")
    for b in ramp(3):
        agg.update(b)
    far = Candle(ts=BASE + timedelta(days=2), open=2000.0, high=2001.0, low=1999.0, close=2000.0)
    closed = agg.update(far)
    assert closed is not None                       # the pre-gap bar closes
    assert len(agg.closed_bars) == 1                # and nothing was fabricated across the gap


# ---- the bundle --------------------------------------------------------

def test_multitimeframe_drives_every_aggregator_from_one_feed():
    mtf = MultiTimeframe(["M15", "M30", "H4"])
    closes: dict[str, int] = {}
    for b in ramp(12 * 4 * 2):                    # 8 hours of M5
        for tf in mtf.update(b):
            closes[tf] = closes.get(tf, 0) + 1

    assert closes["M15"] > closes["M30"] > closes["H4"]
    assert closes["M15"] == pytest.approx(closes["M30"] * 2, abs=1)


def test_every_timeframe_agrees_on_the_extremes_of_the_same_span():
    mtf = MultiTimeframe(["M15", "H1"])
    bars = ramp(12)                                # exactly one hour of M5
    for b in bars:
        mtf.update(b)
    mtf.update(m5(12, 2100.0, 2100.0, 2100.0, 2100.0))   # closes both

    h1 = mtf["H1"].last_closed
    m15s = mtf["M15"].closed(4)
    assert h1.high == pytest.approx(max(b.high for b in m15s))
    assert h1.low == pytest.approx(min(b.low for b in m15s))
    assert h1.open == pytest.approx(m15s[0].open)
    assert h1.close == pytest.approx(m15s[-1].close)
