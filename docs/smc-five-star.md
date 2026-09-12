# The five-star SMC setup

Five conditions, all of which must hold, plus an engulfing candle to confirm.
Miss one and there is no trade.

| ⭐ | Rule | How it is decided in code |
|---|---|---|
| 1 | The order block has not been touched | No candle has traded into the zone since the impulse that created it ended |
| 2 | The move out of it left an imbalance | A fair value gap exists inside the impulse leg: candle 3's low prints above candle 1's high |
| 3 | No liquidity resting behind it | No equal highs/lows and no unswept swing beyond the block's far edge, within `max_liquidity_distance` |
| 4 | It is the *last* order block | No block of either direction has been confirmed more recently |
| 5 | Entry falls in the 15:00–17:00 window | Evaluated in `Europe/Paris`, so it follows DST rather than a fixed UTC hour |
| ✓ | An engulfing candle confirms | After price taps the zone, the current candle's body must swallow the previous opposite body |

Direction comes from a 200 MA on the higher timeframes: price above it on
**daily, H4 and M30** means only bullish blocks are traded, and below means only
bearish. If the three disagree, nothing trades.

```bash
# what the detector finds, and where setups die
fxagents smc-scan --instruments XAU_USD --granularity M5 --bars 130000 --near-misses 4

# backtest it
fxagents backtest --strategy smc --instruments XAU_USD --granularity M5 \
    --bars 130000 --source csv --data-dir data
```

## Verify the detector before you trust a single number

A backtest of a rule this specific is worthless until the detector marks the
same blocks you would mark by eye. SMC is a vocabulary, not a specification —
two traders implement "order block" differently, and the version in `smc.py` is
one reading of it, written down explicitly so it can be argued with.

So start with `smc-scan`. It prints every completed setup with its timestamp
(UTC and local), the block's zone and where the stop goes. Open those dates on
your own chart. When a setup is marked that you would not have taken — or one
you *would* have taken is missing — that is a rule to change, and
`tests/test_smc_five_star.py` is where the change gets pinned down.

`--near-misses 4` lists the setups that reached four or five stars and names
the one that stopped them. That is usually where the disagreement is.

## What the funnel looks like

From 130,000 synthetic M5 bars — ~21 months — with the default settings:

```
  95,190  73.2%  no_candidate            bias not ready, or no block in its direction
  26,400  20.3%  1_untouched             the block had already been mitigated
   4,609   3.5%  3_no_liquidity_behind
   3,726   2.9%  2_imbalance
      72   0.1%  5_session_window
       3   0.0%  6_engulfing
       0          complete
```

Read the shape, not the numbers: synthetic data has no real market structure,
so an order-block detector finds patterns in noise and the *rate* means nothing.
What the shape does say is that **star 1 and the two-hour window are the binding
constraints**. Only 75 bars in 130,000 got past star 4, and 72 of those were
outside 15:00–17:00.

Expect a handful of setups per year, not per week. That is the intended
behaviour of a five-condition filter, but it has a consequence worth being
honest about: a strategy producing five trades a year cannot be evaluated
statistically. Ten years of data gives fifty trades, which is still not enough
to distinguish skill from luck. Whatever the backtest says, the sample will be
too small to settle the question — so the reason to trade it has to be that you
believe the mechanism, not that a number came back green.

## Warmup: the trap

A 200 MA on the **daily** needs 200 daily closes before it reports anything, and
until then the bias gate is shut and nothing can trade. On M5 data that is about
**57,600 bars of pure warmup** — roughly 200 trading days.

Run a year of M5 data and you get zero trades, not because the rule found
nothing but because it never got to run. `smc-scan` warns when the history is
thin for the configured MA. To actually measure anything, you want two years or
more, or a shorter `--ma-period`.

## Tuning knobs

The two that change results most:

- **`--liquidity-distance`** (default 10.0, in dollars for gold) — how far
  behind a block a pool still counts. Too wide and star 3 vetoes everything,
  since there is always *some* old low down there; too narrow and it stops
  catching the sweeps it exists to avoid.
- **`--liquidity-tolerance`** (default 0.5) — how close two swings must sit to
  count as equal. Exact equality never happens on real data.

Also on the agent, not yet on the CLI: `swing_strength` (fractal width, default
2), `impulse_window` (how many bars the break of structure may take, default 5),
`use_wicks` (zone bounded by the candle's wicks or its body), and `stop_buffer`
(how far beyond the block the stop sits, default 0.30).

Every star can be switched off individually — `require_untouched`,
`require_imbalance`, `require_no_liquidity_behind`, `require_last_block`,
`require_session`, `require_engulfing` — which is how you find out which
condition is actually carrying the result. Turning one off and seeing no change
means that star was never binding.

## Data

Gold needs its own instrument definition, already in the catalogue: `XAU_USD`,
one unit = one troy ounce, pip = 0.10, typical retail spread 2.8 pips (28
cents), margin 5%.

On a small account the unit granularity bites: a 13.30 stop on a 10,000 account
wants 7.5 ounces and can only have 7, so the realised risk lands ~7% below the
setting. The rounding always goes **down** — rounding up would over-risk every
trade, forever.

Real M5 history goes in `data/XAU_USD_M5.csv` and is read with
`--source csv`. Column names are matched case-insensitively, so Dukascopy,
HistData and MT5 exports all load without editing.

## Known limits

- **The engulfing confirmation is read on the feed's own timeframe.** Running M5
  data means an M5 engulfing. Checking it on M1 needs M1 data as the base feed.
- **One position at a time**, no scaling in or out.
- **No news filter.** Gold moves hard on CPI and FOMC, both of which land inside
  the 15:00–17:00 Paris window.
- **`is_untouched` is measured on the order-block timeframe** (M15 by default).
  A wick that pierced the zone on M1 but not on M15 does not count as a touch.
