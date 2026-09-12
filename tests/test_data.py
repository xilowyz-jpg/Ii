from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.data.base import align, granularity_minutes
from fxagents.data.csv_source import CsvSource
from fxagents.data.synthetic import SyntheticSource
from fxagents.types import Candle


def test_synthetic_data_is_deterministic_for_a_given_seed():
    a = SyntheticSource(seed=99).candles("EUR_USD", "H1", 200)
    b = SyntheticSource(seed=99).candles("EUR_USD", "H1", 200)
    assert a == b


def test_a_different_seed_gives_a_different_series():
    a = SyntheticSource(seed=1).candles("EUR_USD", "H1", 200)
    b = SyntheticSource(seed=2).candles("EUR_USD", "H1", 200)
    assert a != b


def test_synthetic_data_skips_the_closed_weekend():
    candles = SyntheticSource().candles("EUR_USD", "H1", 2000)
    assert not any(c.ts.weekday() == 5 for c in candles)
    assert not any(c.ts.weekday() == 4 and c.ts.hour >= 21 for c in candles)


def test_synthetic_candles_are_internally_consistent():
    for c in SyntheticSource().candles("USD_JPY", "H1", 500):
        assert c.low <= c.open <= c.high
        assert c.low <= c.close <= c.high


def test_timestamps_are_strictly_increasing_and_utc():
    candles = SyntheticSource().candles("EUR_USD", "H1", 500)
    assert all(candles[i].ts > candles[i - 1].ts for i in range(1, len(candles)))
    assert all(c.ts.tzinfo is timezone.utc for c in candles)


def test_align_pairs_up_matching_timestamps():
    base = datetime(2024, 3, 5, 8, tzinfo=timezone.utc)
    mk = lambda ts, p: Candle(ts=ts, open=p, high=p, low=p, close=p)
    a = [mk(base, 1.1), mk(base + timedelta(hours=1), 1.2)]
    b = [mk(base, 1.3), mk(base + timedelta(hours=1), 1.4)]
    frames = list(align({"EUR_USD": a, "GBP_USD": b}))
    assert len(frames) == 2
    assert sorted(frames[0][1]) == ["EUR_USD", "GBP_USD"]


def test_align_omits_an_instrument_that_has_no_bar_rather_than_carrying_a_stale_one():
    base = datetime(2024, 3, 5, 8, tzinfo=timezone.utc)
    mk = lambda ts, p: Candle(ts=ts, open=p, high=p, low=p, close=p)
    a = [mk(base, 1.1), mk(base + timedelta(hours=1), 1.2)]
    b = [mk(base, 1.3)]                              # missing the second bar
    frames = list(align({"EUR_USD": a, "GBP_USD": b}))
    assert sorted(frames[1][1]) == ["EUR_USD"]


def test_unknown_granularity_is_rejected():
    with pytest.raises(ValueError, match="unknown granularity"):
        granularity_minutes("H3")


# ---- CSV ---------------------------------------------------------------

def test_csv_source_reads_a_standard_export(tmp_path):
    path = tmp_path / "EUR_USD_H1.csv"
    path.write_text(
        "time,open,high,low,close,volume\n"
        "2024-03-05T08:00:00Z,1.1000,1.1020,1.0990,1.1010,120\n"
        "2024-03-05T09:00:00Z,1.1010,1.1030,1.1000,1.1025,140\n"
    )
    candles = CsvSource(directory=tmp_path).candles("EUR_USD", "H1")
    assert len(candles) == 2
    assert candles[0].close == pytest.approx(1.1010)
    assert candles[1].ts.hour == 9


def test_csv_source_handles_alternative_headers_and_orders(tmp_path):
    """Dukascopy-style: different names, different column order, dotted dates."""
    path = tmp_path / "feed.csv"
    path.write_text(
        "Gmt time,Close,Open,Low,High,Volume\n"
        "05.03.2024 08:00:00,1.1010,1.1000,1.0990,1.1020,120\n"
    )
    candles = CsvSource(path=path).candles("EUR_USD", "H1")
    assert candles[0].open == pytest.approx(1.1000)
    assert candles[0].high == pytest.approx(1.1020)


def test_csv_rows_are_sorted_even_when_the_file_is_not(tmp_path):
    path = tmp_path / "EUR_USD_H1.csv"
    path.write_text(
        "time,open,high,low,close\n"
        "2024-03-05T09:00:00Z,1.1010,1.1030,1.1000,1.1025\n"
        "2024-03-05T08:00:00Z,1.1000,1.1020,1.0990,1.1010\n"
    )
    candles = CsvSource(directory=tmp_path).candles("EUR_USD", "H1")
    assert candles[0].ts < candles[1].ts


def test_a_csv_missing_a_required_column_fails_with_a_useful_message(tmp_path):
    path = tmp_path / "EUR_USD_H1.csv"
    path.write_text("time,open,high\n2024-03-05T08:00:00Z,1.10,1.11\n")
    with pytest.raises(ValueError, match="missing required column"):
        CsvSource(directory=tmp_path).candles("EUR_USD", "H1")


def test_a_missing_file_points_at_the_synthetic_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="synthetic"):
        CsvSource(directory=tmp_path).candles("EUR_USD", "H1")
