"""Deterministic synthetic FX data.

This exists so the whole system runs, and the tests pass, with no network and
no broker account. It is not a market simulator and no result measured on it
means anything about a real edge -- it reproduces the *shape* of FX data
(trending and ranging regimes, intraday volatility seasonality, a closed
weekend, mean-reverting drift) so the plumbing can be exercised end to end.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fxagents.data.base import DataSource, granularity_minutes
from fxagents.instruments import get_instrument
from fxagents.types import Candle


# Rough spot levels, so generated series start somewhere plausible.
ANCHORS: dict[str, float] = {
    "EUR_USD": 1.0850, "GBP_USD": 1.2700, "USD_JPY": 149.50, "USD_CHF": 0.8800,
    "AUD_USD": 0.6600, "USD_CAD": 1.3600, "NZD_USD": 0.6100, "EUR_GBP": 0.8540,
    "EUR_JPY": 162.20, "GBP_JPY": 189.90,
}


def _is_market_open(ts: datetime) -> bool:
    """FX week: Sunday 21:00 UTC to Friday 21:00 UTC."""
    wd, hour = ts.weekday(), ts.hour
    if wd == 5:
        return False
    if wd == 4 and hour >= 21:
        return False
    if wd == 6 and hour < 21:
        return False
    return True


def _session_volatility(ts: datetime) -> float:
    """Intraday volatility seasonality: quiet Asia, busy London/NY overlap."""
    hour = ts.hour + ts.minute / 60.0
    london = math.exp(-((hour - 10.0) ** 2) / 18.0)
    newyork = math.exp(-((hour - 15.0) ** 2) / 12.0)
    tokyo = 0.45 * math.exp(-((hour - 2.0) ** 2) / 14.0)
    return 0.35 + london + newyork + tokyo


@dataclass(slots=True)
class SyntheticSource(DataSource):
    """Regime-switching random walk with realistic bar microstructure."""

    seed: int = 7
    annual_vol: float = 0.08           # ~8% annualised, typical for a major
    regime_switch_prob: float = 0.004  # per bar -> ~250-bar regimes

    # Drift during a trending regime, in units of ONE BAR's sigma. This number
    # decides whether the generator is a market or a gift. A currency that
    # trends 10% over a year of H1 bars drifts ~0.02 sigma per bar; at 0.5 the
    # series becomes a near-deterministic ramp that any trend follower prints
    # money on, which makes every result measured on it meaningless.
    trend_strength: float = 0.02

    # Pull back toward the regime's anchor while ranging, as a fraction of the
    # current deviation per bar. 0.02 gives a ~35-bar half-life.
    reversion_strength: float = 0.02
    end: datetime | None = None      # last bar lands near here; series runs backwards from it

    def candles(self, instrument: str, granularity: str = "H1", count: int = 5000) -> list[Candle]:
        inst = get_instrument(instrument)
        minutes = granularity_minutes(granularity)
        rng = random.Random(f"{self.seed}:{inst.symbol}:{granularity}")

        price = ANCHORS.get(inst.symbol, 1.0)
        bars_per_year = 365 * 24 * 60 / minutes
        sigma = price * self.annual_vol / math.sqrt(bars_per_year)

        end = self.end or datetime(2024, 1, 1, tzinfo=timezone.utc)
        ts = end - timedelta(minutes=minutes * int(count * 1.45))  # slack for closed bars

        regime = rng.choice([-1, 0, 1])       # -1 down-trend, 0 range, +1 up-trend
        anchor = price
        out: list[Candle] = []

        while len(out) < count:
            ts += timedelta(minutes=minutes)
            if not _is_market_open(ts):
                continue

            if rng.random() < self.regime_switch_prob:
                regime = rng.choice([-1, 0, 1])
                anchor = price

            vol = sigma * _session_volatility(ts)
            if regime == 0:
                # Ranging: pull back toward the anchor set when the regime began.
                drift = -self.reversion_strength * (price - anchor)
            else:
                drift = regime * self.trend_strength * vol

            step = drift + rng.gauss(0.0, vol)
            open_ = price
            close = max(price + step, inst.pip_size)

            # Wicks: a bar's extremes extend beyond its body by a fraction of
            # the bar's own volatility, which is what makes ATR behave sanely.
            body_high, body_low = max(open_, close), min(open_, close)
            high = body_high + abs(rng.gauss(0.0, 0.55 * vol))
            low = max(body_low - abs(rng.gauss(0.0, 0.55 * vol)), inst.pip_size / 2)

            price = close
            out.append(
                Candle(
                    ts=ts,
                    open=round(open_, 6),
                    high=round(max(high, body_high), 6),
                    low=round(min(low, body_low), 6),
                    close=round(close, 6),
                    volume=round(400 * _session_volatility(ts) * rng.uniform(0.6, 1.4)),
                )
            )
        return out
