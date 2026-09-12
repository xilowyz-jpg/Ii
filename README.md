# fxagents

A multi-agent forex trading system: a team of narrow agents that argue about
direction, a risk manager that decides whether anyone gets to act, and a
simulator built to be pessimistic in the places backtests usually cheat.

**Backtesting and paper trading only.** Nothing in this package can place a
real order. Wiring a live broker is a separate, deliberate step — see
[`docs/going-live.md`](docs/going-live.md).

```bash
pip install -e .
python -m fxagents.cli backtest --instruments EUR_USD,GBP_USD,USD_JPY --bars 5000
```

## The team

One bar in, one decision out. Each agent has a narrow job, and the boundaries
are the point: a strategy that could size its own position is a strategy that
can blow up the account on its own.

```
   bar closes
       │
       ├─► trend_following ──┐
       ├─► mean_reversion  ──┤ signals (direction, confidence, suggested stop)
       └─► breakout        ──┘
                             │
              consensus_portfolio    weighted vote; refuses when agents split
                             │
              session_filter ├─ vetoes  outside London/NY, weekend
           volatility_filter ┘          dead markets and volatility spikes
                             │
                  risk_manager         the only agent that sizes, and the only
                             │         one that can say no
              market_execution         queues the order
                             │
                        broker         fills at the NEXT bar's open
```

| Agent | Role |
|---|---|
| `trend_following` | EMA crossover, gated by ADX so it stays out of chop |
| `mean_reversion` | Fades Bollinger extremes, only while ADX says ranging |
| `breakout` | Donchian channel break, measured in ATRs |
| `session_filter` | London/New York only; never over the weekend gap |
| `volatility_filter` | Refuses dead markets and volatility spikes alike |
| `consensus_portfolio` | Confidence-weighted vote; needs agreement, not just a net score |
| `risk_manager` | Sizing, stops, exposure caps, daily loss limit, drawdown kill switch |
| `market_execution` | Submits the order; holds no opinion of its own |
| `smc_five_star` | A Smart Money Concepts setup for gold — five conditions or no trade. Runs on its own; see [`docs/smc-five-star.md`](docs/smc-five-star.md) |

Trend and mean-reversion are near-opposites by construction. That is deliberate:
when they cancel out, the market is directionless and the right trade is none.

## What the simulator refuses to pretend

Most of the value here is in what the backtest *won't* let you get away with:

- **Orders fill on the next bar's open**, never on the close that produced them.
  Enforced structurally and tested in `tests/test_no_lookahead.py`.
- **Every fill crosses the spread** and takes slippage on top.
- **When a bar touches both the stop and the target, the stop wins.** The
  intrabar sequence is unknowable; the pessimistic reading is the only honest one.
- **A gap through a stop fills at the gap**, not at the stop level.
- **P&L is converted into the account currency.** A USD_JPY profit arrives in
  JPY; treating it as USD makes a position ~150x wrong. When a pair needs a
  cross rate the instrument cannot supply (EUR_GBP on a USD account), it says
  so rather than silently assuming 1.0.

## Risk controls

All of it lives in `RiskLimits` (`src/fxagents/agents/risk.py`), so the entire
downside of the system can be audited by reading one class.

| Limit | Default | What it stops |
|---|---|---|
| `risk_per_trade_pct` | 1% of equity | Any single trade mattering too much |
| `max_open_positions` | 3 | Death by a thousand small bets |
| `max_currency_leverage` | 5× equity | Three "different" trades that are one leveraged USD bet |
| `daily_loss_limit_pct` | 3% | Revenge-trading a bad day; resets at the day boundary |
| `max_drawdown_pct` | 20% | Everything. **Latches** — recovering equity does not re-enable trading |
| `min/max_stop_pips` | 5 / 200 | Stops too tight to survive noise, or too wide to size sanely |
| `take_profit_r` | 2.0 | — (target placed at 2× the stop distance) |

Position size comes from the stop, not the other way round:

```
units = (equity × risk_per_trade_pct) / (stop_distance × quote→account rate)
```

so a wider stop buys a smaller position and the money at risk stays constant.

## Usage

```bash
# describe the wired-up team and its limits
python -m fxagents.cli agents

# a realistic backtest: costs on, three pairs, decision journal
python -m fxagents.cli backtest \
    --instruments EUR_USD,GBP_USD,USD_JPY --bars 6000 \
    --commission 25 --slippage 0.3 --trades 20 --journal 40

# one strategy at a time
python -m fxagents.cli backtest --strategy breakout --instruments EUR_USD

# the five-star SMC setup on gold, and what its detector actually finds
python -m fxagents.cli backtest --strategy smc --instruments XAU_USD --granularity M5 --bars 130000
python -m fxagents.cli smc-scan --instruments XAU_USD --granularity M5 --bars 130000 --near-misses 4

# paper trade: replay history fast, or poll a live feed
python -m fxagents.cli paper --instruments EUR_USD --replay
python -m fxagents.cli paper --instruments EUR_USD --source oanda --poll 60

# export the equity curve
python -m fxagents.cli backtest --export runs/equity.csv
```

### Data

| Source | Flag | Notes |
|---|---|---|
| Synthetic | `--source synthetic` | Default. Offline, deterministic, no credentials |
| Dukascopy | `fxagents fetch` | Free tick history back to the early 2000s, no account. The practical source for gold at M5 |
| CSV | `--source csv --data-dir data` | Reads `EUR_USD_H1.csv`; matches headers by name, so column order and naming vary freely |
| OANDA | `--source oanda` | Practice account. Needs `OANDA_API_TOKEN` — see `.env.example` |

```bash
# three years of gold at M5, cached so a re-run costs nothing
fxagents fetch --instruments XAU_USD --granularity M5 --from 2022-01-01 --to 2024-12-31
```

See [`docs/getting-data.md`](docs/getting-data.md) for the alternatives and for
what to check before trusting a file.

> **The synthetic source is a plumbing test, not a market.** It is a
> regime-switching random walk with realistic session volatility and a closed
> weekend. It exists so the system runs end to end with no network. Performance
> measured on it says nothing about whether a strategy has an edge, and the CLI
> prints that warning on every run.

Concretely — the same strategy, the same settings, 3,000 H1 bars on three pairs,
across 15 seeds:

```
worst -21.7%   median -18.9%   best +5.9%      profitable on 1/15 seeds
```

That is the correct result. A random walk minus the spread minus commission is
a losing game, and a system that reported otherwise here would be telling you
about a bug, not an edge. (An earlier version of the generator *did* report
otherwise: its trending regimes drifted 0.55σ per bar, which is a ramp rather
than a market, and the trend follower "made" 100× on it. Real FX drifts around
0.02σ per bar.)

Note also that the drawdowns cluster at 20–22%: that is the kill switch
latching at its 20% limit and stopping the system, which is exactly what it is
there for.

## Running it somewhere always-on

The engine belongs on a small machine that never sleeps, not on a phone or
tablet — Android suspends background processes, and a strategy defined by a
two-hour daily window cannot afford to miss it. A 1 vCPU / 2 GB VPS is ample,
and it is also the easiest place to run the multi-gigabyte data download.

```bash
curl -fsSL https://raw.githubusercontent.com/xilowyz-jpg/Ii/claude/salut-6jamph/scripts/setup-vps.sh | bash
```

See [`docs/vps-setup.md`](docs/vps-setup.md) for what to buy, connecting from a
tablet, and why every long-running command belongs in `tmux`.

## Claude Code agents

`.claude/agents/` holds five subagents for working on this codebase:

| Agent | Use it for |
|---|---|
| `strategy-researcher` | Turning a trading idea into a tested `SignalAgent` |
| `backtest-analyst` | Running backtests and telling you when a result is noise |
| `risk-auditor` | Adversarial audit of everything that can end an account |
| `market-data-engineer` | Feeds, CSV imports, and validating a series before you trust it |
| `trading-code-reviewer` | Reviewing changes for lookahead bias and vanishing costs |

## Layout

```
src/fxagents/
  types.py          Candle, Signal, Proposal, OrderIntent, Position, ClosedTrade
  instruments.py    pip sizes, unit rounding, quote→account conversion
  indicators.py     streaming SMA/EMA/RSI/ATR/Bollinger/Donchian/ADX
  smc.py            swings, order blocks, fair value gaps, liquidity pools
  timeframes.py     higher-timeframe bars built from one base feed
  journal.py        every decision and every refusal, with its reason
  runner.py         the one bar → one decision loop, shared by backtest and live
  presets.py        ready-made agent teams
  cli.py            backtest / paper / agents
  agents/           base contracts, signals, filters, portfolio, risk, execution
  brokers/          broker interface + the paper simulator
  data/             synthetic, CSV, OANDA, Dukascopy ticks
  backtest/         engine + performance metrics
  live/             paper-trading session loop
```

## Tests

```bash
pip install -e '.[dev]'
PYTHONPATH=src python -m pytest tests/ -q
```

213 tests. The ones that matter most:

- `tests/test_no_lookahead.py` — the system provably cannot act on the bar that
  produced its signal, even when the next bar gaps 100 pips away.
- `tests/test_risk.py` — sizing risks exactly the configured fraction, the
  currency conversion is right for every instrument shape, and every limit
  actually binds.
- `tests/test_paper_broker.py` — stops beat targets, gaps fill at the gap, and
  costs never improve a result.
- `test_paper_trading_reproduces_the_backtest_on_the_same_data` — the live loop
  and the simulator agree exactly on identical input. Without that, a paper
  result means nothing.
- `tests/test_smc.py` and `tests/test_smc_five_star.py` — every SMC primitive
  checked against a hand-drawn figure, then the full setup broken one star at a
  time so each refusal can be traced to the rule that caused it.
- `tests/test_timeframes.py` — a higher-timeframe bar is invisible until it has
  closed, so a multi-timeframe strategy cannot read a bar that has not finished.

## Status and honest limits

Working: the agent pipeline, the simulator, backtesting, paper trading, three
strategies, the risk controls, the CLI, the test suite.

Not done, and you should know before trusting it with anything:

- **No strategy here has a demonstrated edge.** They are reference
  implementations of well-known mechanisms, not researched signals.
- **No swap/rollover costs.** Holding overnight is free in this simulator and
  is not free in reality.
- **No slippage model beyond a fixed pip cost.** Real slippage widens with
  volatility and around news.
- **Single position per instrument.** No scaling in or out, no partial closes.
- **Cross rates that neither leg can resolve** (EUR_GBP on a USD account) fall
  back to 1.0 and log a warning. Set `require_exact_conversion=True` to refuse
  those trades instead.
- **Backtested on H1 bars.** Intrabar behaviour is approximated; the stop-wins
  rule is a floor on that error, not a fix for it.

## License

MIT.
