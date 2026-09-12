from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.types import Candle


@pytest.fixture
def t0() -> datetime:
    return datetime(2024, 3, 5, 9, 0, tzinfo=timezone.utc)   # a Tuesday, London session


def make_candles(start: datetime, closes, spread_frac: float = 0.0002, step_minutes: int = 60):
    """Build a well-formed OHLC series from a list of closes."""
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        high = max(open_, close) * (1 + spread_frac)
        low = min(open_, close) * (1 - spread_frac)
        out.append(
            Candle(
                ts=start + timedelta(minutes=step_minutes * i),
                open=open_, high=high, low=low, close=close, volume=100.0,
            )
        )
        prev = close
    return out
