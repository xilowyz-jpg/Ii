"""The Dukascopy reader, tested entirely offline.

The HTTP layer is injected, so every test here builds real LZMA payloads and
feeds them through the same decode and aggregation path the live fetcher uses.
No network, and no pretending the parsing works because it compiled.
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timedelta, timezone

import pytest

from fxagents.data.dukascopy import (
    DukascopyError,
    DukascopyFetcher,
    TransientError,
    DukascopySource,
    Tick,
    check_plausible,
    decode_ticks,
    dukascopy_symbol,
    hour_url,
    point_value_for,
    ticks_to_candles,
)

HOUR = datetime(2024, 5, 16, 13, 0, tzinfo=timezone.utc)


def payload(records: list[tuple[int, int, int, float, float]]) -> bytes:
    """Build a real .bi5 body: LZMA-alone over 20-byte big-endian records."""
    raw = b"".join(struct.pack(">3I2f", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def gold(ms: int, ask: float, bid: float, vol: float = 1.0):
    return (ms, int(round(ask * 1000)), int(round(bid * 1000)), vol, vol)


# ---- urls --------------------------------------------------------------

def test_the_month_in_the_url_is_zero_indexed():
    """The single most common way to read the wrong month from this feed."""
    assert "/2024/04/16/13h_ticks.bi5" in hour_url("XAU_USD", HOUR)     # May -> 04


@pytest.mark.parametrize("month,expected", [(1, "00"), (6, "05"), (12, "11")])
def test_every_month_maps_one_lower(month, expected):
    ts = datetime(2024, month, 5, 9, tzinfo=timezone.utc)
    assert f"/2024/{expected}/05/09h_ticks.bi5" in hour_url("EUR_USD", ts)


def test_the_symbol_loses_its_separator():
    assert dukascopy_symbol("XAU_USD") == "XAUUSD"
    assert dukascopy_symbol("eur/usd") == "EURUSD"


def test_metals_and_jpy_pairs_use_a_three_decimal_point_value():
    assert point_value_for("XAU_USD") == 1000
    assert point_value_for("USD_JPY") == 1000
    assert point_value_for("EUR_USD") == 100_000


# ---- decoding ----------------------------------------------------------

def test_ticks_decode_with_their_prices_and_timestamps():
    body = payload([gold(0, 2350.123, 2349.987), gold(1_500, 2350.200, 2350.050)])
    ticks = decode_ticks(body, HOUR, 1000)

    assert len(ticks) == 2
    assert ticks[0].ts == HOUR
    assert ticks[0].ask == pytest.approx(2350.123)
    assert ticks[0].bid == pytest.approx(2349.987)
    assert ticks[0].mid == pytest.approx((2350.123 + 2349.987) / 2)
    assert ticks[1].ts == HOUR + timedelta(milliseconds=1_500)


def test_an_empty_file_is_a_closed_hour_not_an_error():
    """Weekends and holidays return zero bytes. That is normal."""
    assert decode_ticks(b"", HOUR, 1000) == []


def test_a_non_lzma_body_names_the_likely_cause():
    with pytest.raises(DukascopyError, match="zero-indexed month"):
        decode_ticks(b"<html>404</html>", HOUR, 1000)


def test_a_truncated_stream_is_rejected():
    raw = struct.pack(">3I2f", 0, 2350123, 2349987, 1.0, 1.0)[:13]
    with pytest.raises(DukascopyError, match="whole number"):
        decode_ticks(lzma.compress(raw, format=lzma.FORMAT_ALONE), HOUR, 1000)


def test_a_wrong_point_value_is_caught_instead_of_silently_shrinking_gold():
    """Decoding gold with the FX divisor gives 23.50 instead of 2350."""
    ticks = decode_ticks(payload([gold(0, 2350.0, 2349.9)]), HOUR, 100_000)
    assert ticks[0].mid < 100                       # the damage
    with pytest.raises(DukascopyError, match="plausible range"):
        check_plausible("XAU_USD", ticks, 100_000)


def test_a_correct_point_value_passes_the_check():
    ticks = decode_ticks(payload([gold(0, 2350.0, 2349.9)]), HOUR, 1000)
    check_plausible("XAU_USD", ticks, 1000)          # must not raise


def test_the_plausibility_check_ignores_an_empty_hour():
    check_plausible("XAU_USD", [], 1000)


# ---- aggregation -------------------------------------------------------

def ticks_at(offsets_and_mids: list[tuple[int, float]]) -> list[Tick]:
    return [
        Tick(ts=HOUR + timedelta(seconds=s), bid=m - 0.05, ask=m + 0.05,
             bid_volume=1.0, ask_volume=1.0)
        for s, m in offsets_and_mids
    ]


def test_ticks_become_ohlc_bars_on_the_granularity_grid():
    ticks = ticks_at([(0, 2350.0), (60, 2355.0), (120, 2345.0), (280, 2352.0)])
    bars = ticks_to_candles(ticks, "M5")

    assert len(bars) == 1
    bar = bars[0]
    assert bar.ts == HOUR
    assert bar.open == pytest.approx(2350.0)
    assert bar.high == pytest.approx(2355.0)
    assert bar.low == pytest.approx(2345.0)
    assert bar.close == pytest.approx(2352.0)


def test_ticks_split_across_the_period_boundary():
    ticks = ticks_at([(0, 2350.0), (299, 2351.0), (300, 2360.0), (599, 2361.0)])
    bars = ticks_to_candles(ticks, "M5")

    assert [b.ts for b in bars] == [HOUR, HOUR + timedelta(minutes=5)]
    assert bars[0].close == pytest.approx(2351.0)
    assert bars[1].open == pytest.approx(2360.0)


def test_bars_land_on_the_granularity_grid_not_on_the_first_tick():
    ticks = ticks_at([(137, 2350.0)])           # first tick 2m17s into the hour
    assert ticks_to_candles(ticks, "M5")[0].ts == HOUR


def test_volume_is_summed_across_both_sides():
    bars = ticks_to_candles(ticks_at([(0, 2350.0), (60, 2351.0)]), "M5")
    assert bars[0].volume == pytest.approx(4.0)   # 2 ticks x (1.0 bid + 1.0 ask)


def test_aggregating_no_ticks_gives_no_bars():
    assert ticks_to_candles([], "M5") == []


def test_the_granularity_is_validated():
    with pytest.raises(ValueError, match="unknown granularity"):
        ticks_to_candles(ticks_at([(0, 2350.0)]), "M7")


# ---- the fetcher -------------------------------------------------------

class FakeFeed:
    """Stands in for the HTTP layer and counts what was actually requested."""

    def __init__(self, bodies: dict[str, bytes] | None = None):
        self.bodies = bodies or {}
        self.requested: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.requested.append(url)
        return self.bodies.get(url, b"")


def fetcher_for(tmp_path, feed: FakeFeed, **kwargs) -> DukascopyFetcher:
    return DukascopyFetcher(
        cache_dir=tmp_path / "cache", get=feed, pause=0.0,
        sleep=lambda _: None, **kwargs,
    )


def test_downloaded_hours_are_cached_and_not_fetched_twice(tmp_path):
    url = hour_url("XAU_USD", HOUR)
    feed = FakeFeed({url: payload([gold(0, 2350.0, 2349.9)])})
    fetcher = fetcher_for(tmp_path, feed)

    first = fetcher.fetch_hour("XAU_USD", HOUR)
    second = fetcher.fetch_hour("XAU_USD", HOUR)

    assert first == second
    assert len(feed.requested) == 1, "the cached hour was downloaded again"


def test_a_closed_hour_is_cached_too_so_it_is_not_retried(tmp_path):
    feed = FakeFeed()                     # everything returns empty
    fetcher = fetcher_for(tmp_path, feed)
    fetcher.fetch_hour("XAU_USD", HOUR)
    fetcher.fetch_hour("XAU_USD", HOUR)
    assert len(feed.requested) == 1


def test_the_closed_weekend_is_never_requested(tmp_path):
    """Saturday has no files; asking for them is thousands of wasted requests."""
    feed = FakeFeed()
    fetcher = fetcher_for(tmp_path, feed)
    saturday = datetime(2024, 5, 18, 0, tzinfo=timezone.utc)
    fetcher.ticks("XAU_USD", saturday, saturday + timedelta(hours=23))
    assert feed.requested == []


def test_sunday_evening_is_requested_because_the_week_opens_then(tmp_path):
    feed = FakeFeed()
    fetcher = fetcher_for(tmp_path, feed)
    sunday = datetime(2024, 5, 19, 20, tzinfo=timezone.utc)
    fetcher.ticks("XAU_USD", sunday, sunday + timedelta(hours=3))
    hours = [u.rsplit("/", 1)[-1] for u in feed.requested]
    assert "20h_ticks.bi5" not in hours        # still closed
    assert "21h_ticks.bi5" in hours            # open


def test_the_fetcher_turns_hours_into_candles(tmp_path):
    urls = {
        hour_url("XAU_USD", HOUR): payload([gold(0, 2350.0, 2349.9), gold(299_000, 2355.0, 2354.9)]),
        hour_url("XAU_USD", HOUR + timedelta(hours=1)): payload([gold(0, 2360.0, 2359.9)]),
    }
    feed = FakeFeed(urls)
    bars = fetcher_for(tmp_path, feed).candles(
        "XAU_USD", HOUR, HOUR + timedelta(hours=1), "M5"
    )
    assert len(bars) == 2
    assert bars[0].open == pytest.approx(2349.95)
    assert bars[-1].ts == HOUR + timedelta(hours=1)


def test_progress_is_reported_per_hour(tmp_path):
    seen: list[tuple[int, int]] = []
    fetcher = fetcher_for(
        tmp_path, FakeFeed(),
        on_progress=lambda done, total, hour: seen.append((done, total)),
    )
    fetcher.ticks("XAU_USD", HOUR, HOUR + timedelta(hours=3))
    assert seen and seen[-1][0] <= seen[-1][1]


def test_a_wrong_point_value_fails_on_the_first_real_hour(tmp_path):
    feed = FakeFeed({hour_url("XAU_USD", HOUR): payload([gold(0, 2350.0, 2349.9)])})
    fetcher = fetcher_for(tmp_path, feed)
    with pytest.raises(DukascopyError, match="plausible range"):
        fetcher.ticks("XAU_USD", HOUR, HOUR, point_value=100_000)


# ---- the DataSource wrapper -------------------------------------------

def test_the_source_returns_at_most_the_requested_bar_count(tmp_path):
    bodies = {}
    for i in range(6):
        hour = HOUR + timedelta(hours=i)
        bodies[hour_url("XAU_USD", hour)] = payload(
            [gold(m * 60_000, 2350.0 + i, 2349.9 + i) for m in range(60)]
        )
    source = DukascopySource(
        end=HOUR + timedelta(hours=5),
        fetcher=fetcher_for(tmp_path, FakeFeed(bodies)),
    )
    bars = source.candles("XAU_USD", "M5", count=10)
    assert len(bars) <= 10
    assert all(bars[i].ts > bars[i - 1].ts for i in range(1, len(bars)))


# ---- memory --------------------------------------------------------------

def _hourly_bodies(hours: int) -> dict[str, bytes]:
    bodies = {}
    for i in range(hours):
        hour = HOUR + timedelta(hours=i)
        if hour.weekday() == 5 or (hour.weekday() == 6 and hour.hour < 21):
            continue
        bodies[hour_url("XAU_USD", hour)] = payload(
            [gold(s * 1000, 2350.0 + s % 7, 2349.9 + s % 7) for s in range(3_600)]
        )
    return bodies


def _peaks(tmp_path, hours: int) -> tuple[int, int, int]:
    """Peak bytes for streaming vs collecting over `hours`, and the tick count."""
    import tracemalloc

    fetcher = fetcher_for(tmp_path / str(hours), FakeFeed(_hourly_bodies(hours)))
    end = HOUR + timedelta(hours=hours - 1)

    tracemalloc.start()
    fetcher.candles("XAU_USD", HOUR, end, "M5")
    _, streamed = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    ticks = fetcher.ticks("XAU_USD", HOUR, end)
    _, collected = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return streamed, collected, len(ticks)


def test_aggregation_memory_does_not_grow_with_the_range(tmp_path):
    """The property that makes a multi-year fetch possible on a small machine.

    Three years of gold is tens of millions of ticks; holding them to
    aggregate at the end costs gigabytes, and a 2 GB VPS would be killed
    partway through a 45-minute download. So peak memory must be set by the
    number of BARS, not the number of ticks -- flat as the range grows.
    """
    short_stream, short_collect, short_ticks = _peaks(tmp_path, 6)
    long_stream, long_collect, long_ticks = _peaks(tmp_path, 96)

    assert long_ticks > short_ticks * 5, "the fixture does not scale enough to prove anything"

    assert long_stream < short_stream * 1.5, (
        f"streaming grew from {short_stream:,} to {long_stream:,} bytes over "
        f"{short_ticks:,} -> {long_ticks:,} ticks; the ticks are still being held"
    )
    assert long_collect > short_collect * 2, (
        "the collecting path was expected to grow with the range; if it no "
        "longer does, this test is measuring nothing"
    )


def test_streaming_and_collecting_produce_identical_bars(tmp_path):
    """The optimisation must not change a single price."""
    bodies = {}
    for i in range(4):
        hour = HOUR + timedelta(hours=i)
        bodies[hour_url("XAU_USD", hour)] = payload(
            [gold(s * 5_000, 2350.0 + (s % 11) * 0.1, 2349.9 + (s % 11) * 0.1)
             for s in range(720)]
        )
    fetcher = fetcher_for(tmp_path, FakeFeed(bodies))
    end = HOUR + timedelta(hours=3)

    streamed = fetcher.candles("XAU_USD", HOUR, end, "M5")
    collected = ticks_to_candles(fetcher.ticks("XAU_USD", HOUR, end), "M5")
    assert streamed == collected


def test_the_accumulator_keeps_the_first_and_last_price_by_time(tmp_path):
    """Ticks may arrive out of order within a bar; open and close follow the clock."""
    from fxagents.data.dukascopy import CandleAccumulator

    acc = CandleAccumulator("M5")
    for seconds, mid in [(120, 2355.0), (0, 2350.0), (240, 2352.0), (60, 2360.0)]:
        acc.add(Tick(ts=HOUR + timedelta(seconds=seconds), bid=mid - 0.05, ask=mid + 0.05,
                     bid_volume=1.0, ask_volume=1.0))

    bar = acc.candles()[0]
    assert bar.open == pytest.approx(2350.0)     # earliest, not first added
    assert bar.close == pytest.approx(2352.0)    # latest, not last added
    assert bar.high == pytest.approx(2360.0)
    assert bar.low == pytest.approx(2350.0)


def test_the_accumulator_reports_how_many_bars_it_holds():
    from fxagents.data.dukascopy import CandleAccumulator

    acc = CandleAccumulator("M5")
    acc.add_many(ticks_at([(0, 2350.0), (299, 2351.0), (300, 2360.0)]))
    assert len(acc) == 2


# ---- resilience ----------------------------------------------------------

class FlakyFeed:
    """Fails the first `fail_times` calls for each URL, then succeeds."""

    def __init__(self, bodies: dict[str, bytes], fail_times: int,
                 error: type[Exception] = TransientError):
        self.bodies = bodies
        self.fail_times = fail_times
        self.error = error
        self.attempts: dict[str, int] = {}

    def __call__(self, url: str, timeout: float) -> bytes:
        self.attempts[url] = self.attempts.get(url, 0) + 1
        if self.attempts[url] <= self.fail_times:
            raise self.error(f"{url}: simulated failure {self.attempts[url]}")
        return self.bodies.get(url, b"")


def test_a_transient_failure_is_retried_and_then_succeeds(tmp_path):
    """A timeout at hour 9,000 of 18,000 must not end a 45-minute download."""
    url = hour_url("XAU_USD", HOUR)
    feed = FlakyFeed({url: payload([gold(0, 2350.0, 2349.9)])}, fail_times=2)
    fetcher = fetcher_for(tmp_path, feed, retries=5, backoff=0.0)

    ticks = decode_ticks(fetcher.fetch_hour("XAU_USD", HOUR), HOUR, 1000)
    assert len(ticks) == 1
    assert feed.attempts[url] == 3, "expected two failures then a success"


def test_the_wait_between_retries_doubles(tmp_path):
    waits: list[float] = []
    url = hour_url("XAU_USD", HOUR)
    feed = FlakyFeed({url: payload([gold(0, 2350.0, 2349.9)])}, fail_times=3)
    fetcher = DukascopyFetcher(
        cache_dir=tmp_path, get=feed, pause=0.0, retries=5, backoff=1.0,
        sleep=waits.append,
    )
    fetcher.fetch_hour("XAU_USD", HOUR)
    assert waits[:3] == [1.0, 2.0, 4.0]


def test_giving_up_on_one_hour_does_not_lose_the_rest(tmp_path):
    """The hour is recorded as a gap; the download carries on."""
    bodies = {}
    for i in range(4):
        hour = HOUR + timedelta(hours=i)
        if i != 1:                                   # hour 1 is never served
            bodies[hour_url("XAU_USD", hour)] = payload([gold(0, 2350.0 + i, 2349.9 + i)])

    class Broken:
        def __call__(self, url, timeout):
            if url not in bodies:
                raise TransientError(f"{url}: down")
            return bodies[url]

    fetcher = fetcher_for(tmp_path, Broken(), retries=2, backoff=0.0)
    bars = fetcher.candles("XAU_USD", HOUR, HOUR + timedelta(hours=3), "M5")

    assert bars, "the good hours were thrown away with the bad one"
    assert len(fetcher.failures) == 1
    failed_hour, message = fetcher.failures[0]
    assert failed_hour == HOUR + timedelta(hours=1)
    assert "down" in message


def test_a_host_that_refuses_everything_stops_early_with_a_diagnosis(tmp_path):
    """Better than spending 45 minutes producing an empty file."""
    class Refusing:
        def __call__(self, url, timeout):
            raise TransientError("connection reset by peer")

    fetcher = fetcher_for(tmp_path, Refusing(), retries=1, backoff=0.0,
                          max_consecutive_failures=5)
    with pytest.raises(DukascopyError, match="refusing this machine"):
        fetcher.candles("XAU_USD", HOUR, HOUR + timedelta(hours=20), "M5")


def test_a_successful_hour_resets_the_consecutive_failure_count(tmp_path):
    """Scattered failures are normal; only an unbroken run means we are blocked."""
    good = {}
    for i in range(0, 12, 2):                        # every other hour works
        good[hour_url("XAU_USD", HOUR + timedelta(hours=i))] = payload(
            [gold(0, 2350.0, 2349.9)]
        )

    class Alternating:
        def __call__(self, url, timeout):
            if url not in good:
                raise TransientError("flaky")
            return good[url]

    fetcher = fetcher_for(tmp_path, Alternating(), retries=1, backoff=0.0,
                          max_consecutive_failures=3)
    bars = fetcher.candles("XAU_USD", HOUR, HOUR + timedelta(hours=11), "M5")
    assert bars
    assert 0 < len(fetcher.failures) < 12


def test_a_genuine_bug_is_not_retried(tmp_path):
    """A corrupt stream is a defect, not weather -- retrying it wastes time."""
    class Corrupt:
        def __init__(self):
            self.calls = 0

        def __call__(self, url, timeout):
            self.calls += 1
            return b"<html>not lzma</html>"

    feed = Corrupt()
    fetcher = fetcher_for(tmp_path, feed, retries=5, backoff=0.0)
    with pytest.raises(DukascopyError, match="zero-indexed month"):
        fetcher.candles("XAU_USD", HOUR, HOUR, "M5")
    assert feed.calls == 1, "a decode bug was retried as though it were a timeout"


def test_retries_are_reported_so_a_slow_run_is_explainable(tmp_path):
    seen: list[tuple[int, str]] = []
    url = hour_url("XAU_USD", HOUR)
    feed = FlakyFeed({url: payload([gold(0, 2350.0, 2349.9)])}, fail_times=2)
    fetcher = fetcher_for(tmp_path, feed, retries=5, backoff=0.0,
                          on_retry=lambda hour, attempt, exc: seen.append((attempt, str(exc))))
    fetcher.fetch_hour("XAU_USD", HOUR)
    assert [a for a, _ in seen] == [1, 2]
