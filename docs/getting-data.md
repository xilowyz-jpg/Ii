# Getting real data

The synthetic source exists to exercise the plumbing. Nothing measured on it
means anything, and for an SMC strategy it is worse than useless: an order
block detector will happily mark "structure" in pure noise.

So before any number from this system is worth reading, you need real history.
Here is how to get it.

## Dukascopy (recommended, free, no account)

Dukascopy publish raw tick data back to the early 2000s. It is the most
practical free source of XAU_USD at M5 and finer, and there is a reader built
in:

```bash
pip install -e '.[live]'

fxagents fetch --instruments XAU_USD --granularity M5 \
    --from 2022-01-01 --to 2024-12-31 --out data
```

That writes `data/XAU_USD_M5.csv`, which is exactly where `--source csv` looks:

```bash
fxagents smc-scan --instruments XAU_USD --granularity M5 \
    --source csv --bars 200000 --near-misses 4
```

**What to expect.** Three years of gold is roughly 18,000 hourly files. At the
built-in 0.15s pause that is about 45 minutes, most of it waiting. Every file is
cached under `.cache/dukascopy`, so a second run costs nothing and an
interrupted run resumes where it stopped. Budget around 1-2 GB of cache for
three years of gold ticks.

**Memory.** The aggregation streams: ticks are folded into bars one hour at a
time and then discarded, so peak memory is set by the number of bars rather
than the number of ticks. Three years of M5 gold stays around 10 MB however
many tens of millions of ticks went into it, which is what makes the fetch
possible on a 1-2 GB machine. `DukascopyFetcher.ticks()` returns the whole list
instead and is fine for a few days -- but a few years of it would need several
gigabytes, so use `candles()` (what `fxagents fetch` calls) for long ranges.

**If it fails with a proxy error**, the machine is behind an egress policy that
blocks the host — some CI runners and managed environments do. Run the fetch
from your own machine; the resulting CSV is all the system needs.

**Start with one month.** Check it looks right before committing to a long
download:

```bash
fxagents fetch --instruments XAU_USD --granularity M5 --from 2024-05-01 --to 2024-05-31
head -3 data/XAU_USD_M5.csv
```

Gold should print around 2300-2400 for May 2024. If it comes out near 2.35 or
235,000, the point value is wrong — the fetcher checks this and refuses rather
than writing a chart that is off by a factor of a hundred, but it is worth
seeing the numbers yourself.

## OANDA practice account (free, needs a signup)

A demo account costs nothing and takes a few minutes. It gives clean, broker-
native candles, which is useful precisely because it is the same data a live
system would see.

1. Open a practice account and create a personal access token.
2. Put it in your environment (see `.env.example`):
   ```
   OANDA_API_TOKEN=...
   OANDA_ACCOUNT_ID=...
   OANDA_ENV=practice
   ```
3. Use it directly: `--source oanda`.

History is shallower than Dukascopy's and the candle count per request is
capped at 5,000 (the reader pages backwards automatically), so it is better for
recent data and live paper trading than for a multi-year backtest.

## Anything else, via CSV

`--source csv` matches column names case-insensitively and accepts most common
date formats, so HistData, MT5 exports, TradingView exports and broker
downloads load without editing. Drop the file at
`data/<INSTRUMENT>_<GRANULARITY>.csv` — for example `data/XAU_USD_M5.csv`.

The required columns are time, open, high, low, close; volume is optional.
Aliases like `Gmt time`, `BidOpen` or `Tick volume` are understood.

## Check the data before you trust it

Bad data produces confident, wrong backtests. Before drawing conclusions:

- **Timezone.** Everything in this system is UTC. If a vendor exports in local
  time with DST, every session boundary moves twice a year — and the five-star
  setup is defined by a two-hour window, so that shifts half your trades out of
  it. Sanity check: the daily volume peak should land in the London/New York
  overlap, roughly 12:00-16:00 UTC.
- **Weekend bars.** Their presence means the vendor filled them in. There
  should be no bars from Friday ~21:00 UTC to Sunday ~21:00 UTC.
- **Gaps.** Missing bars inside a session are normal in thin hours and a
  problem in busy ones.
- **Which side of the book.** The simulator applies its own spread on top of
  the feed. Loading bid-only data and letting it charge a spread as well
  double-counts the cost. The Dukascopy reader aggregates to **mid** for
  exactly this reason.
- **Spikes.** A bar whose range is more than ~10x the trailing ATR is usually a
  bad tick rather than a market event.

The `market-data-engineer` agent in `.claude/agents/` carries this checklist
and can run it over a file for you.

## How much history the five-star setup needs

More than you would think, and for a reason that has nothing to do with the
rule: the bias gate uses a 200 MA on the **daily**, which needs 200 daily
closes before it reports anything. On M5 that is about **57,600 bars of pure
warmup**.

Fetch one year and you get zero trades — not because no setup appeared, but
because the strategy never got to start. Two to three years is the working
minimum. `smc-scan` warns when the history is thin for the configured MA.
