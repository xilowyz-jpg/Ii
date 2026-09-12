# Going live

Nothing in this package can place a real order. That is a design choice, not an
oversight: the gap between a working paper system and a working live system is
where most retail trading capital goes.

This document is the checklist to close that gap. It is deliberately hard.

## Before you consider it

Work through these in order. Each one has ended plenty of systems that looked
fine on the previous step.

1. **Replace the synthetic data.** Nothing measured on
   `SyntheticSource` means anything. Get real H1 (or finer) history — several
   years, including 2015's CHF break, 2016's Brexit gap and 2020's March — and
   run `--source csv` against it.
2. **Validate the data before trusting it.** Timezone, DST shifts, weekend
   bars, bad ticks, and which side of the book the file contains. The
   `market-data-engineer` agent has the full checklist.
3. **Split your history.** Develop on the first 70%. Do not look at the last
   30% until you have stopped changing things. Every time you peek and then
   adjust, that segment stops being out-of-sample.
4. **Add the costs this simulator omits.** Swap/rollover on overnight
   positions, and slippage that widens with volatility rather than a flat pip
   figure. Both are currently absent and both are real.
5. **Paper trade on live prices for at least a full quarter.**
   `--source oanda --poll 60`. Not a backtest — real spreads, real data gaps,
   real weekend handling, and your own reaction to watching it lose.
6. **Compare paper to backtest on the same period.** If they diverge materially,
   your simulator is wrong and you have just learned something valuable for
   free. Fix it before risking anything.
7. **Decide, in writing and in advance, what makes you stop.** A drawdown
   figure, a number of consecutive losses, a date. Written down before you are
   losing money, because that decision cannot be made honestly while losing.

## What implementing a live broker actually requires

`brokers/base.py` defines the interface. A live implementation needs everything
`PaperBroker` does, plus everything that can go wrong when the other side is a
real exchange:

- **Idempotency.** Every order carries a client-generated ID, so a retry after
  a timeout cannot open the position twice. This is the single most common way
  live systems lose money that backtests never see.
- **Reconciliation on startup.** Fetch actual open positions from the broker
  and reconcile against local state before acting. Never assume you are flat.
- **Partial fills and rejections.** `PaperBroker` fills everything completely.
  Real brokers do not.
- **Stops held broker-side.** If your process dies, the stop must still exist.
  A stop that only lives in your Python process is not a stop.
- **Reconnection and backfill.** Data feeds drop. On reconnect, backfill the
  missed bars before resuming decisions, or the indicators are silently wrong.
- **A kill switch that does not need your code to work.** You must be able to
  flatten everything without the trading process cooperating.

## Deliberate friction

`data/oanda.py` refuses to connect to the live host unless
`FXAGENTS_ALLOW_LIVE=1` is set. Keep that pattern for any live broker you add.
It should take a conscious act to point this at real money, and the failure
mode of forgetting should be "nothing happens", never "it traded".

## The part that is not code

Position sizing at 1% per trade with a 20% drawdown kill switch is survivable.
It is also slow, and slow is what makes people abandon a working system for a
worse one. The risk limits in this repository are the parameters most likely to
be quietly widened after a losing month, and widening them is the most common
way an account ends.

If you change `RiskLimits`, write down why, with the date, before the change.
