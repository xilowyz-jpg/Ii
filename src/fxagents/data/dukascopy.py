"""Dukascopy tick history.

Dukascopy publish raw tick data back to the early 2000s at a public URL, one
file per instrument per hour. It is the most practical free source of real
XAU_USD history at M5 and finer.

    https://datafeed.dukascopy.com/datafeed/XAUUSD/2024/04/16/13h_ticks.bi5
                                             symbol  YYYY  MM DD  HH

The month is **zero-indexed** -- 00 is January -- which is the single most
common mistake when reading this feed, and it fails silently by returning a
different month's data rather than an error.

Each file is an LZMA-alone stream of 20-byte big-endian records:

    uint32  milliseconds since the top of the hour
    uint32  ask, as an integer to be divided by the instrument's point value
    uint32  bid, same
    float32 ask volume
    float32 bid volume

An empty (0-byte) file means no ticks that hour -- a weekend, a holiday, or a
market break. That is normal and must not be treated as an error.

The data belongs to Dukascopy and is published for personal research. Fetch at
a civilised rate and cache what you download; `DukascopyFetcher` does both.
"""

from __future__ import annotations

import lzma
import math
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from fxagents.data.base import DataSource, granularity_minutes
from fxagents.types import Candle

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
TICK_STRUCT = struct.Struct(">3I2f")
TICK_SIZE = TICK_STRUCT.size          # 20

# How many integer units make one unit of price. Dukascopy quote most FX pairs
# to five decimals, JPY crosses and metals to three.
POINT_VALUES: dict[str, int] = {
    "XAU_USD": 1000, "XAG_USD": 1000,
    "USD_JPY": 1000, "EUR_JPY": 1000, "GBP_JPY": 1000, "AUD_JPY": 1000,
    "CHF_JPY": 1000, "CAD_JPY": 1000, "NZD_JPY": 1000,
}
DEFAULT_POINT_VALUE = 100_000

# Rough price magnitudes, used only to catch a wrong point value before it
# silently produces a chart that is off by a factor of a hundred.
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "XAU_USD": (200.0, 20_000.0),
    "XAG_USD": (2.0, 200.0),
}
DEFAULT_PLAUSIBLE = (0.01, 1000.0)


class DukascopyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def dukascopy_symbol(instrument: str) -> str:
    """'XAU_USD' -> 'XAUUSD'."""
    return instrument.upper().replace("_", "").replace("/", "")


def hour_url(instrument: str, hour: datetime) -> str:
    """Build the feed URL. Remember: the month is zero-indexed."""
    hour = hour.astimezone(timezone.utc)
    return (
        f"{BASE_URL}/{dukascopy_symbol(instrument)}/{hour.year:04d}/"
        f"{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def point_value_for(instrument: str) -> int:
    return POINT_VALUES.get(instrument.upper().replace("/", "_"), DEFAULT_POINT_VALUE)


def decode_ticks(payload: bytes, hour: datetime, point_value: int) -> list[Tick]:
    """Decompress and unpack one hour's file.

    An empty payload yields no ticks -- that is a closed hour, not a failure.
    """
    if not payload:
        return []
    try:
        raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(payload)
    except lzma.LZMAError as exc:
        raise DukascopyError(
            f"{hour:%Y-%m-%d %H}h: payload is not an LZMA-alone stream ({exc}). "
            "A short HTML body here usually means the URL was wrong -- check the "
            "zero-indexed month."
        ) from exc

    if len(raw) % TICK_SIZE:
        raise DukascopyError(
            f"{hour:%Y-%m-%d %H}h: {len(raw)} bytes is not a whole number of "
            f"{TICK_SIZE}-byte ticks"
        )

    hour = hour.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out: list[Tick] = []
    for offset in range(0, len(raw), TICK_SIZE):
        ms, ask, bid, ask_vol, bid_vol = TICK_STRUCT.unpack_from(raw, offset)
        out.append(
            Tick(
                ts=hour + timedelta(milliseconds=ms),
                bid=bid / point_value,
                ask=ask / point_value,
                bid_volume=bid_vol,
                ask_volume=ask_vol,
            )
        )
    return out


def check_plausible(instrument: str, ticks: list[Tick], point_value: int) -> None:
    """Fail loudly on a wrong point value rather than returning a wrong chart."""
    if not ticks:
        return
    low, high = PLAUSIBLE.get(instrument.upper().replace("/", "_"), DEFAULT_PLAUSIBLE)
    sample = ticks[len(ticks) // 2].mid
    if low <= sample <= high:
        return
    suggestion = point_value * (10 ** round(math.log10(sample / low if sample > high else low / sample)))
    raise DukascopyError(
        f"{instrument}: decoded a price of {sample:,.4f}, which is outside the "
        f"plausible range {low}-{high}. The point value {point_value:,} is probably "
        f"wrong for this instrument -- try around {suggestion:,.0f} "
        f"(pass point_value=... to override)."
    )


def ticks_to_candles(ticks: Iterable[Tick], granularity: str) -> list[Candle]:
    """Aggregate ticks into OHLC bars on mid prices.

    Mid rather than bid: the simulator applies its own spread on top, and
    charging the feed's spread as well would double-count the cost.
    """
    minutes = granularity_minutes(granularity)
    period = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    buckets: dict[datetime, list[float]] = {}
    volumes: dict[datetime, float] = {}
    for tick in ticks:
        elapsed = int((tick.ts - epoch).total_seconds() // 60)
        start = epoch + timedelta(minutes=(elapsed // minutes) * minutes)
        buckets.setdefault(start, []).append(tick.mid)
        volumes[start] = volumes.get(start, 0.0) + tick.bid_volume + tick.ask_volume

    out: list[Candle] = []
    for start in sorted(buckets):
        prices = buckets[start]
        out.append(
            Candle(
                ts=start,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=round(volumes[start], 4),
            )
        )
    return out


def _http_get(url: str, timeout: float) -> bytes:
    """Fetch one file. 404 means the hour does not exist, which is not an error."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DukascopyError(
            "fetching needs `requests`: pip install 'fxagents[live]'"
        ) from exc

    try:
        response = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "fxagents/0.1 (personal research)"},
        )
    except requests.exceptions.ProxyError as exc:
        raise DukascopyError(
            f"a proxy refused the connection to datafeed.dukascopy.com.\n"
            f"This machine is behind an egress policy that does not allow the host. "
            f"Run the fetch from a machine with direct internet access, or see "
            f"docs/getting-data.md for the alternatives.\n  ({exc})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise DukascopyError(f"could not reach {url}: {exc}") from exc

    if response.status_code == 404:
        return b""
    if response.status_code != 200:
        raise DukascopyError(f"{url} returned {response.status_code}")
    return response.content


@dataclass(slots=True)
class DukascopyFetcher:
    """Downloads hourly tick files, with an on-disk cache.

    The cache is the point: a year of M5 gold is ~6,000 hourly files, and you
    will want to re-run the aggregation without re-downloading any of it.
    """

    cache_dir: Path | str = ".cache/dukascopy"
    timeout: float = 30.0
    pause: float = 0.15                 # seconds between requests
    get: Callable[[str, float], bytes] = _http_get
    sleep: Callable[[float], None] = time.sleep
    on_progress: Callable[[int, int, datetime], None] | None = None

    def _cache_path(self, instrument: str, hour: datetime) -> Path:
        return (
            Path(self.cache_dir) / dukascopy_symbol(instrument)
            / f"{hour:%Y/%m/%d}" / f"{hour:%H}h_ticks.bi5"
        )

    def fetch_hour(self, instrument: str, hour: datetime) -> bytes:
        path = self._cache_path(instrument, hour)
        if path.exists():
            return path.read_bytes()
        payload = self.get(hour_url(instrument, hour), self.timeout)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if self.pause:
            self.sleep(self.pause)
        return payload

    def ticks(self, instrument: str, start: datetime, end: datetime,
              point_value: int | None = None) -> list[Tick]:
        point_value = point_value or point_value_for(instrument)
        hours = int((end - start).total_seconds() // 3600) + 1
        out: list[Tick] = []
        checked = False

        for i in range(hours):
            hour = (start + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
            # Saturday and the closed part of Sunday never have files; skipping
            # them locally avoids thousands of pointless requests per year.
            if hour.weekday() == 5 or (hour.weekday() == 6 and hour.hour < 21):
                continue
            payload = self.fetch_hour(instrument, hour)
            ticks = decode_ticks(payload, hour, point_value)
            if ticks and not checked:
                check_plausible(instrument, ticks, point_value)
                checked = True
            out.extend(ticks)
            if self.on_progress:
                self.on_progress(i + 1, hours, hour)
        return out

    def candles(self, instrument: str, start: datetime, end: datetime,
                granularity: str = "M5", point_value: int | None = None) -> list[Candle]:
        return ticks_to_candles(self.ticks(instrument, start, end, point_value), granularity)


@dataclass(slots=True)
class DukascopySource(DataSource):
    """DataSource wrapper, for `--source dukascopy`.

    Counts backwards from `end` in whole days until it has enough bars, so it
    fits the `candles(instrument, granularity, count)` contract the rest of the
    system uses.
    """

    end: datetime | None = None
    fetcher: DukascopyFetcher = field(default_factory=DukascopyFetcher)
    point_value: int | None = None

    def candles(self, instrument: str, granularity: str = "M5", count: int = 5000) -> list[Candle]:
        minutes = granularity_minutes(granularity)
        end = (self.end or datetime.now(timezone.utc)).replace(
            minute=0, second=0, microsecond=0
        )
        # ~5 trading days a week, ~24h a day; ask for 1.6x to absorb the weekends.
        span_hours = math.ceil(count * minutes / 60 * 1.6) + 24
        start = end - timedelta(hours=span_hours)
        bars = self.fetcher.candles(instrument, start, end, granularity, self.point_value)
        return bars[-count:] if count else bars
