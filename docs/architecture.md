# Architecture

## One bar, one pass

The whole system is a loop in `runner.py`. Everything else is a participant.

```
Runner.on_frame(ts, bars)
  │
  ├─ 1. broker.on_bar_open(candle)    fill what was queued LAST bar, at THIS open
  ├─ 2. broker.on_bar(candle)         walk the bar for stops/targets, mark to close
  ├─ 3. risk.on_equity_update(...)    drawdown peak, daily boundary
  │
  └─ for each instrument:
       ├─ 4. every SignalAgent.on_bar(ctx)     → signals
       ├─ 5. every FilterAgent.allows(ctx)     → vetoes
       ├─ 6. portfolio.combine(signals)        → one proposal, or None
       │
       ├─ holding a position?  trail the stop; close on a genuine reversal
       └─ flat?                risk.size(proposal) → execution.execute() → QUEUED
```

Step 4 and step 5 run **every bar**, even while a position is open and even
when a filter has already vetoed. An agent whose indicators go stale while it
holds a position cannot be trusted to call the exit.

Step 6 only ever *queues*. Step 1 of the next bar fills it. That ordering is
what makes lookahead structurally impossible rather than merely discouraged,
and `tests/test_no_lookahead.py` asserts it directly: the fill timestamp is
strictly later than the signal timestamp, and the fill price is the next bar's
open even when that open gaps 100 pips away.

## Why the roles are split this way

| Role | May do | May not do |
|---|---|---|
| `SignalAgent` | Read bars, emit direction + confidence + a suggested stop | Size, order, see the account |
| `FilterAgent` | Veto new entries | Open anything, close anything |
| `PortfolioAgent` | Combine signals into one view | Size, order |
| `RiskAgent` | Size, refuse, set stops and targets, halt the system | Form an opinion on direction |
| `ExecutionAgent` | Send the order | Change the size, the stop or the direction |

The constraint that earns its keep is the third column of the first row. A
strategy that can size its own position can blow up an account by itself, and
no amount of review catches every instance once the capability exists. Here it
does not exist: `SignalAgent` cannot reach the account at all.

The second payoff is auditability. Every limit that can end the account is in
one class (`RiskLimits`), so the system's entire downside can be read in one
sitting — which is what makes the `risk-auditor` agent a tractable job rather
than a whole-codebase sweep.

## Streaming indicators, and why not vectorised

`indicators.py` consumes one value at a time and returns `None` while warming
up. Pandas rolling windows would be shorter and faster.

They would also be a different code path from the live loop, and the whole
value of this system rests on the backtest and the live session behaving
identically. A vectorised backtest is a second implementation of your strategy,
and the two drift. Streaming means there is one implementation, exercised the
same way in both modes.

The Donchian channel shows why the detail matters: `BreakoutAgent` reads the
channel **before** folding the current bar in, because a bar cannot break a
channel it is itself part of. In a vectorised implementation that is a `shift(1)`
you either remember or silently get wrong.

## Currency conversion

The subtlety that makes forex different: profit lands in the **quote** currency.

- `EUR_USD` on a USD account → quote is USD → rate 1.0.
- `USD_JPY` on a USD account → profit in JPY, worth `1/price` USD. Miss this and
  positions are ~150× too large.
- `EUR_GBP` on a USD account → needs GBP/USD, which this instrument does not
  carry. `quote_to_account_rate` returns `None` and the caller decides: refuse
  (`require_exact_conversion=True`) or proceed with a logged warning.

The same conversion governs the per-currency exposure cap. Both legs of an FX
position are worth the same amount — buying 20,000 EUR_USD at 1.10 is
simultaneously +22,000 USD of EUR and −22,000 USD of USD — and every leg must
be converted before it can be compared against one cap. Comparing raw JPY
notional against a USD cap is a bug this codebase has already had once.

## Extending it

- **A new strategy** → a `SignalAgent` in `agents/signals.py`, registered in
  `presets.py`. Nothing else changes.
- **A new indicator** → `indicators.py`, same `update()` / `ready` / `value`
  shape.
- **A new data feed** → a `DataSource` in `data/`. Keep the dependency optional
  so the core still runs with no network.
- **A new execution style** → a sibling of `MarketExecution`, not a branch
  inside it.
- **A new risk rule** → a field on `RiskLimits` and a check in
  `RiskManager.size`. Resist putting it anywhere else, however convenient.
