"""Smart Money Concepts primitives: swings, order blocks, FVGs, liquidity.

SMC is a vocabulary, not a specification. Every term below is defined
differently by different traders, so each function here states the exact rule
it implements. When a detection disagrees with your chart, the fix is to change
the rule and say so -- not to widen it until something matches.

Everything is a pure function over a list of CLOSED candles, oldest first. That
makes each rule testable against a hand-built fixture, which is the only way to
know a detector finds what you think it finds. Nothing here reads beyond the
list it is given, so a caller that passes only history cannot leak the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from fxagents.types import Candle


class Direction(IntEnum):
    BULLISH = 1
    BEARISH = -1

    @property
    def opposite(self) -> "Direction":
        return Direction(-int(self))


# ---------------------------------------------------------------------------
# Swing points
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Swing:
    """A fractal high or low.

    `index` is where the extreme sits; `confirmed_index` is where it could
    first be *known*, which is `strength` bars later. Live, you cannot see a
    swing high until enough bars have printed to its right -- so anything that
    reacts to a swing must use `confirmed_index`, or the backtest is reading
    the future by `strength` bars.
    """

    index: int
    confirmed_index: int
    ts: datetime
    price: float
    direction: Direction      # BULLISH = swing high, BEARISH = swing low


def find_swings(candles: list[Candle], strength: int = 2) -> list[Swing]:
    """Fractal swings: an extreme with `strength` lower highs / higher lows each side."""
    if strength < 1:
        raise ValueError("strength must be >= 1")
    out: list[Swing] = []
    for i in range(strength, len(candles) - strength):
        window = candles[i - strength: i + strength + 1]
        c = candles[i]
        if all(c.high >= w.high for w in window) and any(c.high > w.high for w in window):
            out.append(Swing(i, i + strength, c.ts, c.high, Direction.BULLISH))
        if all(c.low <= w.low for w in window) and any(c.low < w.low for w in window):
            out.append(Swing(i, i + strength, c.ts, c.low, Direction.BEARISH))
    return out


def last_swing_before(swings: list[Swing], index: int, direction: Direction) -> Swing | None:
    """The most recent swing of `direction` that was already CONFIRMED at `index`."""
    candidates = [s for s in swings if s.direction is direction and s.confirmed_index <= index]
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Fair value gaps (imbalance)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FairValueGap:
    """A three-candle inefficiency: price moved so fast it left untraded space.

    Bullish: candle 3's low prints above candle 1's high.
    Bearish: candle 3's high prints below candle 1's low.

    `index` is the middle candle -- the impulsive one that created the gap.
    """

    index: int
    ts: datetime
    top: float
    bottom: float
    direction: Direction

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def find_fvgs(candles: list[Candle], min_size: float = 0.0) -> list[FairValueGap]:
    """Every FVG in the series. `min_size` filters out gaps too small to matter."""
    out: list[FairValueGap] = []
    for i in range(1, len(candles) - 1):
        first, middle, third = candles[i - 1], candles[i], candles[i + 1]
        if third.low > first.high and (third.low - first.high) >= min_size:
            out.append(FairValueGap(i, middle.ts, third.low, first.high, Direction.BULLISH))
        elif third.high < first.low and (first.low - third.high) >= min_size:
            out.append(FairValueGap(i, middle.ts, first.low, third.high, Direction.BEARISH))
    return out


# ---------------------------------------------------------------------------
# Order blocks
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderBlock:
    """The last opposing candle before an impulsive move that broke structure.

    Bullish (a zone to buy from): the last DOWN candle before price rallied and
    took out the prior swing high. Bearish is the mirror.

    `top`/`bottom` bound the zone. `impulse_end_index` is where the break
    completed -- the block is only *known* from that bar onward, which is what
    `confirmed_index` records.
    """

    index: int
    confirmed_index: int
    ts: datetime
    top: float
    bottom: float
    direction: Direction
    impulse_end_index: int
    broke_level: float
    has_fvg: bool

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def entry_edge(self) -> float:
        """The edge price reaches first when returning to the zone."""
        return self.top if self.direction is Direction.BULLISH else self.bottom

    def far_edge(self) -> float:
        """The edge beyond which the block has failed -- where a stop belongs."""
        return self.bottom if self.direction is Direction.BULLISH else self.top


def find_order_blocks(
    candles: list[Candle],
    swings: list[Swing] | None = None,
    swing_strength: int = 2,
    impulse_window: int = 5,
    use_wicks: bool = True,
) -> list[OrderBlock]:
    """Detect order blocks confirmed by a break of structure.

    The rule, for a bullish block at candle `i`:

    1. candle `i` closes down (it is the supply being absorbed);
    2. candle `i+1` closes up -- so `i` is the LAST down candle before the move;
    3. within `impulse_window` bars the high exceeds the most recent swing high
       that was already confirmed before `i` (the break of structure).

    Without (3) any down candle in an uptrend qualifies, which is how an order
    block detector ends up marking half the chart.

    `use_wicks` bounds the zone by the candle's high/low; otherwise by its body.
    """
    if swings is None:
        swings = find_swings(candles, swing_strength)
    fvgs = find_fvgs(candles)
    out: list[OrderBlock] = []

    for i in range(len(candles) - 1):
        c, nxt = candles[i], candles[i + 1]

        for direction in (Direction.BULLISH, Direction.BEARISH):
            bullish = direction is Direction.BULLISH
            # (1) and (2): the last opposing candle before the move.
            if bullish:
                if not (c.close < c.open and nxt.close > nxt.open):
                    continue
            else:
                if not (c.close > c.open and nxt.close < nxt.open):
                    continue

            reference = last_swing_before(
                swings, i, Direction.BULLISH if bullish else Direction.BEARISH
            )
            if reference is None:
                continue

            # (3) the break of structure, within the impulse window.
            end = min(i + impulse_window, len(candles) - 1)
            broke_at: int | None = None
            for j in range(i + 1, end + 1):
                if bullish and candles[j].high > reference.price:
                    broke_at = j
                    break
                if not bullish and candles[j].low < reference.price:
                    broke_at = j
                    break
            if broke_at is None:
                continue

            top = c.high if use_wicks else max(c.open, c.close)
            bottom = c.low if use_wicks else min(c.open, c.close)
            has_fvg = any(
                f.direction is direction and i < f.index <= broke_at for f in fvgs
            )
            out.append(
                OrderBlock(
                    index=i,
                    confirmed_index=broke_at,
                    ts=c.ts,
                    top=top,
                    bottom=bottom,
                    direction=direction,
                    impulse_end_index=broke_at,
                    broke_level=reference.price,
                    has_fvg=has_fvg,
                )
            )
    return out


def is_untouched(block: OrderBlock, candles: list[Candle], until_index: int | None = None) -> bool:
    """True while price has not traded back into the zone since the block formed.

    Measured from the bar after the impulse ended: the impulse itself leaves the
    zone, and the candles that created the block obviously sit inside it.
    """
    end = len(candles) if until_index is None else until_index + 1
    for c in candles[block.impulse_end_index + 1: end]:
        if c.low <= block.top and c.high >= block.bottom:
            return False
    return True


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LiquidityPool:
    """Resting stop orders: a level price is drawn to before it can do anything else."""

    price: float
    ts: datetime
    index: int
    direction: Direction      # BULLISH = buy-side liquidity (above), BEARISH = sell-side (below)
    kind: str                 # "equal" | "swing"
    touches: int = 1


def find_liquidity(
    candles: list[Candle],
    swings: list[Swing] | None = None,
    swing_strength: int = 2,
    tolerance: float = 0.0,
    include_unswept_swings: bool = True,
) -> list[LiquidityPool]:
    """Locate resting liquidity.

    Two kinds, both of which count:

    * **equal highs / lows** -- two or more swings within `tolerance` of each
      other. The flat level is visible to everyone, so stops pile up behind it.
    * **unswept swings** -- a single swing whose level price has not traded
      through since. The stops placed beyond it are still sitting there.

    `tolerance` is in price units. For XAU_USD, something like 0.5-1.0 (50-100
    cents) is a reasonable "equal" -- exact equality never happens.
    """
    if swings is None:
        swings = find_swings(candles, swing_strength)
    pools: list[LiquidityPool] = []

    for direction in (Direction.BULLISH, Direction.BEARISH):
        group = [s for s in swings if s.direction is direction]
        used: set[int] = set()

        # Equal levels first -- they are the stronger signal of the two.
        for a_pos, a in enumerate(group):
            if a_pos in used:
                continue
            cluster = [a]
            for b_pos in range(a_pos + 1, len(group)):
                if b_pos in used:
                    continue
                if abs(group[b_pos].price - a.price) <= tolerance:
                    cluster.append(group[b_pos])
                    used.add(b_pos)
            if len(cluster) > 1:
                used.add(a_pos)
                last = cluster[-1]
                pools.append(
                    LiquidityPool(
                        price=sum(s.price for s in cluster) / len(cluster),
                        ts=last.ts, index=last.index, direction=direction,
                        kind="equal", touches=len(cluster),
                    )
                )

        if not include_unswept_swings:
            continue

        for pos, s in enumerate(group):
            if pos in used:
                continue
            later = candles[s.index + 1:]
            swept = (
                any(c.high > s.price for c in later) if direction is Direction.BULLISH
                else any(c.low < s.price for c in later)
            )
            if not swept:
                pools.append(
                    LiquidityPool(s.price, s.ts, s.index, direction, "swing", 1)
                )

    return sorted(pools, key=lambda p: p.index)


def liquidity_behind(
    block: OrderBlock,
    pools: list[LiquidityPool],
    max_distance: float | None = None,
) -> list[LiquidityPool]:
    """Pools sitting BEYOND the block -- the ones that make it a trap.

    For a bullish block (you buy from it), "beyond" is below: price sweeps
    down through the block to take those stops, and your entry is the liquidity
    it used to get there. For a bearish block, beyond is above.

    `max_distance` ignores pools so far away that the block would be a trade
    long before price reached them.
    """
    if block.direction is Direction.BULLISH:
        edge = block.bottom
        found = [p for p in pools if p.price < edge]
    else:
        edge = block.top
        found = [p for p in pools if p.price > edge]

    if max_distance is not None:
        found = [p for p in found if abs(p.price - edge) <= max_distance]
    return found


# ---------------------------------------------------------------------------
# Candle patterns
# ---------------------------------------------------------------------------

def is_engulfing(previous: Candle, current: Candle, direction: Direction,
                 require_opposite_prior: bool = True) -> bool:
    """Classic engulfing: the body swallows the previous body, in `direction`.

    `require_opposite_prior` enforces the textbook form -- a bullish engulfing
    must consume a DOWN candle. Without it, any large up candle after a small
    up candle qualifies, which fires far too often to be a confirmation.
    """
    prev_top, prev_bottom = max(previous.open, previous.close), min(previous.open, previous.close)

    if direction is Direction.BULLISH:
        if current.close <= current.open:
            return False
        if require_opposite_prior and previous.close >= previous.open:
            return False
        return current.close >= prev_top and current.open <= prev_bottom

    if current.close >= current.open:
        return False
    if require_opposite_prior and previous.close <= previous.open:
        return False
    return current.close <= prev_bottom and current.open >= prev_top
