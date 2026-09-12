"""SMC primitives, each checked against a hand-built figure.

Every fixture here is drawn deliberately so the expected answer is obvious by
inspection. A detector that passes a synthetic-data smoke test but fails these
is finding something other than what it claims.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.smc import (
    Direction,
    OrderBlock,
    find_fvgs,
    find_liquidity,
    find_order_blocks,
    find_swings,
    is_engulfing,
    is_untouched,
    last_swing_before,
    liquidity_behind,
)
from fxagents.types import Candle

BASE = datetime(2024, 3, 5, 13, 0, tzinfo=timezone.utc)


def c(i: int, o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(ts=BASE + timedelta(minutes=5 * i), open=o, high=h, low=l, close=cl)


def flat(i: int, price: float, spread: float = 1.0) -> Candle:
    """A neutral candle, for padding a fixture without adding structure."""
    return c(i, price, price + spread, price - spread, price)


# ---- swings ------------------------------------------------------------

def test_a_peak_is_a_swing_high():
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    swings = find_swings(candles, strength=2)
    highs = [s for s in swings if s.direction is Direction.BULLISH]
    assert len(highs) == 1
    assert highs[0].index == 2
    assert highs[0].price == 2020


def test_a_trough_is_a_swing_low():
    candles = [flat(0, 2010), flat(1, 2008), c(2, 2006, 2007, 1990, 2005), flat(3, 2008), flat(4, 2010)]
    lows = [s for s in find_swings(candles, 2) if s.direction is Direction.BEARISH]
    assert len(lows) == 1 and lows[0].price == 1990


def test_a_swing_is_only_confirmed_strength_bars_after_it_forms():
    """Live you cannot see a peak until bars print to its right."""
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    swing = [s for s in find_swings(candles, 2) if s.direction is Direction.BULLISH][0]
    assert swing.index == 2
    assert swing.confirmed_index == 4, "a swing known before its right-hand bars is lookahead"


def test_a_monotonic_ramp_has_no_interior_swings():
    candles = [flat(i, 2000 + i) for i in range(20)]
    assert find_swings(candles, 2) == []


def test_last_swing_before_ignores_swings_not_yet_confirmed():
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    swings = find_swings(candles, 2)
    assert last_swing_before(swings, 3, Direction.BULLISH) is None    # confirmed at 4
    assert last_swing_before(swings, 4, Direction.BULLISH) is not None


def test_strength_must_be_positive():
    with pytest.raises(ValueError):
        find_swings([flat(0, 2000)], strength=0)


# ---- fair value gaps ---------------------------------------------------

def test_a_bullish_fvg_is_a_gap_between_candle_one_and_three():
    candles = [
        c(0, 2000, 2002, 1999, 2001),     # high 2002
        c(1, 2001, 2015, 2001, 2014),     # the impulse
        c(2, 2014, 2018, 2005, 2016),     # low 2005 > 2002 -> gap
    ]
    gaps = find_fvgs(candles)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.direction is Direction.BULLISH
    assert (g.bottom, g.top) == (2002, 2005)
    assert g.index == 1                   # the middle, impulsive candle
    assert g.contains(2003.5)


def test_a_bearish_fvg_is_the_mirror():
    candles = [
        c(0, 2010, 2011, 2008, 2009),     # low 2008
        c(1, 2009, 2009, 1995, 1996),
        c(2, 1996, 2004, 1992, 1998),     # high 2004 < 2008 -> gap
    ]
    g = find_fvgs(candles)[0]
    assert g.direction is Direction.BEARISH
    assert (g.bottom, g.top) == (2004, 2008)


def test_overlapping_candles_leave_no_gap():
    candles = [c(0, 2000, 2005, 1999, 2004), c(1, 2004, 2010, 2003, 2009), c(2, 2009, 2012, 2004, 2011)]
    assert find_fvgs(candles) == []


def test_min_size_filters_out_trivial_gaps():
    candles = [c(0, 2000, 2002, 1999, 2001), c(1, 2001, 2015, 2001, 2014), c(2, 2014, 2018, 2002.1, 2016)]
    assert find_fvgs(candles) != []
    assert find_fvgs(candles, min_size=1.0) == []


# ---- order blocks ------------------------------------------------------

def bullish_ob_fixture() -> list[Candle]:
    """A swing high at index 2, a down candle at 6, then a break above it."""
    return [
        flat(0, 2000),
        flat(1, 2002),
        c(2, 2004, 2020, 2003, 2005),     # swing high at 2020, confirmed at index 4
        flat(3, 2004),
        flat(4, 2002),
        flat(5, 2001),
        c(6, 2005, 2006, 1998, 1999),     # THE order block: last down candle
        c(7, 1999, 2014, 1999, 2013),     # impulse up
        c(8, 2013, 2030, 2012, 2028),     # breaks 2020 -> BOS
    ]


def test_a_bullish_order_block_is_the_last_down_candle_before_the_break():
    blocks = [b for b in find_order_blocks(bullish_ob_fixture(), impulse_window=5)
              if b.direction is Direction.BULLISH]
    assert len(blocks) == 1
    ob = blocks[0]
    assert ob.index == 6
    assert (ob.bottom, ob.top) == (1998, 2006)
    assert ob.broke_level == 2020
    assert ob.confirmed_index == 8


def test_a_down_candle_with_no_break_of_structure_is_not_an_order_block():
    """Without the BOS requirement, half the candles on a chart qualify."""
    candles = bullish_ob_fixture()
    candles[8] = c(8, 2013, 2015, 2012, 2014)      # rally stalls below 2020
    blocks = [b for b in find_order_blocks(candles, impulse_window=5)
              if b.direction is Direction.BULLISH]
    assert blocks == []


def test_the_break_must_happen_inside_the_impulse_window():
    candles = bullish_ob_fixture()
    assert find_order_blocks(candles, impulse_window=5)
    assert [b for b in find_order_blocks(candles, impulse_window=1)
            if b.direction is Direction.BULLISH] == []


def test_a_down_candle_followed_by_another_down_candle_is_not_the_block():
    """The block is the LAST opposing candle before the move, not an earlier one."""
    candles = bullish_ob_fixture()
    candles[5] = c(5, 2003, 2004, 2000, 2001)      # also a down candle, one bar earlier
    blocks = [b.index for b in find_order_blocks(candles, impulse_window=5)
              if b.direction is Direction.BULLISH]
    assert 5 not in blocks and 6 in blocks


def test_body_mode_gives_a_tighter_zone_than_wick_mode():
    candles = bullish_ob_fixture()
    wick = [b for b in find_order_blocks(candles) if b.direction is Direction.BULLISH][0]
    body = [b for b in find_order_blocks(candles, use_wicks=False) if b.direction is Direction.BULLISH][0]
    assert (body.bottom, body.top) == (1999, 2005)
    assert body.height < wick.height


def test_the_block_records_whether_its_impulse_left_an_imbalance():
    candles = bullish_ob_fixture()
    ob = [b for b in find_order_blocks(candles) if b.direction is Direction.BULLISH][0]
    assert ob.has_fvg is True                       # 2006 -> 2012 is a gap

    no_gap = list(candles)
    no_gap[8] = c(8, 2013, 2030, 2000, 2028)        # candle 8 fills back over candle 6's high
    ob2 = [b for b in find_order_blocks(no_gap) if b.direction is Direction.BULLISH][0]
    assert ob2.has_fvg is False


def test_a_bearish_order_block_is_the_mirror():
    candles = [
        flat(0, 2020), flat(1, 2018),
        c(2, 2016, 2017, 2000, 2015),                # swing low at 2000
        flat(3, 2016), flat(4, 2018), flat(5, 2019),
        c(6, 2015, 2022, 2014, 2021),                # the block: last UP candle
        c(7, 2021, 2021, 2006, 2007),                # impulse down
        c(8, 2007, 2008, 1990, 1992),                # breaks 2000
    ]
    blocks = [b for b in find_order_blocks(candles, impulse_window=5)
              if b.direction is Direction.BEARISH]
    assert len(blocks) == 1 and blocks[0].index == 6
    assert blocks[0].far_edge() == 2022             # stop goes above the block
    assert blocks[0].entry_edge() == 2014


# ---- mitigation --------------------------------------------------------

def test_a_block_price_never_returned_to_is_untouched():
    candles = bullish_ob_fixture() + [flat(9, 2035), flat(10, 2040)]
    ob = [b for b in find_order_blocks(candles) if b.direction is Direction.BULLISH][0]
    assert is_untouched(ob, candles)


def test_a_block_price_traded_back_into_is_touched():
    candles = bullish_ob_fixture() + [c(9, 2028, 2029, 2004, 2010)]    # dips into 1998-2006
    ob = [b for b in find_order_blocks(candles) if b.direction is Direction.BULLISH][0]
    assert not is_untouched(ob, candles)


def test_touching_can_be_evaluated_as_of_an_earlier_bar():
    candles = bullish_ob_fixture() + [flat(9, 2035), c(10, 2035, 2036, 2005, 2010)]
    ob = [b for b in find_order_blocks(candles) if b.direction is Direction.BULLISH][0]
    assert is_untouched(ob, candles, until_index=9)      # not yet
    assert not is_untouched(ob, candles, until_index=10)


# ---- liquidity ---------------------------------------------------------

def equal_lows_fixture() -> list[Candle]:
    """Two swing lows at the same level -- a textbook sell-side liquidity pool."""
    return [
        flat(0, 2010), flat(1, 2008),
        c(2, 2006, 2007, 1990, 2005),      # swing low 1990
        flat(3, 2008), flat(4, 2010), flat(5, 2009), flat(6, 2008),
        c(7, 2006, 2007, 1990.2, 2005),    # swing low 1990.2 -- "equal"
        flat(8, 2008), flat(9, 2010),
    ]


def test_two_lows_at_the_same_level_form_an_equal_lows_pool():
    pools = find_liquidity(equal_lows_fixture(), tolerance=0.5)
    equal = [p for p in pools if p.kind == "equal" and p.direction is Direction.BEARISH]
    assert len(equal) == 1
    assert equal[0].touches == 2
    assert equal[0].price == pytest.approx(1990.1)


def test_tolerance_decides_what_counts_as_equal():
    assert [p for p in find_liquidity(equal_lows_fixture(), tolerance=0.01) if p.kind == "equal"] == []


def test_an_unswept_swing_is_liquidity_on_its_own():
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    pools = find_liquidity(candles, tolerance=0.5)
    assert any(p.kind == "swing" and p.price == 2020 for p in pools)


def test_a_swing_price_has_traded_through_is_no_longer_liquidity():
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005),
               flat(3, 2004), flat(4, 2002), c(5, 2005, 2030, 2004, 2028)]   # sweeps 2020
    pools = find_liquidity(candles, tolerance=0.5)
    assert not any(p.kind == "swing" and p.price == 2020 for p in pools)


def test_unswept_swings_can_be_excluded():
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    assert find_liquidity(candles, include_unswept_swings=False) == []


# ---- liquidity behind a block -----------------------------------------

def make_block(direction: Direction, bottom: float, top: float) -> OrderBlock:
    return OrderBlock(index=0, confirmed_index=0, ts=BASE, top=top, bottom=bottom,
                      direction=direction, impulse_end_index=0, broke_level=0.0, has_fvg=True)


def test_liquidity_below_a_bullish_block_is_behind_it():
    """Price will sweep down through the block to reach those stops."""
    block = make_block(Direction.BULLISH, 1998, 2006)
    pools = find_liquidity(equal_lows_fixture(), tolerance=0.5)
    behind = liquidity_behind(block, pools)
    assert behind and all(p.price < 1998 for p in behind)


def test_liquidity_above_a_bullish_block_is_not_behind_it():
    block = make_block(Direction.BULLISH, 1998, 2006)
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    pools = find_liquidity(candles, tolerance=0.5)          # a pool at 2020, above
    assert liquidity_behind(block, pools) == []


def test_for_a_bearish_block_behind_means_above():
    block = make_block(Direction.BEARISH, 2010, 2014)
    candles = [flat(0, 2000), flat(1, 2002), c(2, 2004, 2020, 2003, 2005), flat(3, 2004), flat(4, 2002)]
    pools = find_liquidity(candles, tolerance=0.5)
    behind = liquidity_behind(block, pools)
    assert behind and all(p.price > 2014 for p in behind)


def test_max_distance_ignores_pools_too_far_to_matter():
    block = make_block(Direction.BULLISH, 1998, 2006)
    pools = find_liquidity(equal_lows_fixture(), tolerance=0.5)
    assert liquidity_behind(block, pools, max_distance=1.0) == []
    assert liquidity_behind(block, pools, max_distance=100.0)


# ---- engulfing ---------------------------------------------------------

def test_a_bullish_engulfing_swallows_the_previous_down_body():
    prev = c(0, 2010, 2011, 2004, 2005)              # down body 2005..2010
    curr = c(1, 2004, 2016, 2003, 2012)              # up body 2004..2012
    assert is_engulfing(prev, curr, Direction.BULLISH)


def test_an_up_candle_that_does_not_cover_the_body_is_not_engulfing():
    prev = c(0, 2010, 2011, 2004, 2005)
    curr = c(1, 2006, 2010, 2005, 2009)
    assert not is_engulfing(prev, curr, Direction.BULLISH)


def test_a_bullish_engulfing_must_consume_a_down_candle_by_default():
    prev = c(0, 2005, 2011, 2004, 2010)              # an UP candle
    curr = c(1, 2004, 2016, 2003, 2012)
    assert not is_engulfing(prev, curr, Direction.BULLISH)
    assert is_engulfing(prev, curr, Direction.BULLISH, require_opposite_prior=False)


def test_a_bearish_engulfing_is_the_mirror():
    prev = c(0, 2005, 2012, 2004, 2011)              # up body 2005..2011
    curr = c(1, 2012, 2013, 2000, 2003)              # down body 2003..2012
    assert is_engulfing(prev, curr, Direction.BEARISH)
    assert not is_engulfing(prev, curr, Direction.BULLISH)
