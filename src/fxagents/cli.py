"""Command line interface.

    fxagents backtest --instruments EUR_USD,GBP_USD --bars 4000
    fxagents paper    --instruments EUR_USD --replay --speed 0
    fxagents agents
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from fxagents import __version__
from fxagents.backtest.engine import Backtester
from fxagents.data.base import align
from fxagents.data.csv_source import CsvSource
from fxagents.data.synthetic import SyntheticSource
from fxagents.instruments import get_instrument
from fxagents.presets import build_registry
from fxagents.runner import RunnerConfig

SYNTHETIC_WARNING = (
    "NOTE: synthetic data is a plumbing test, not a market. Performance measured\n"
    "      on it says nothing about whether a strategy has an edge.\n"
)


def _build_source(args):
    if args.source == "synthetic":
        return SyntheticSource(seed=args.seed)
    if args.source == "csv":
        return CsvSource(directory=args.data_dir)
    if args.source == "oanda":
        from fxagents.data.oanda import OandaSource

        return OandaSource()
    raise ValueError(f"unknown source {args.source!r}")


def _symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _names(raw: str) -> list[str]:
    """Session names are lower case; keep them out of the symbol parser."""
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def cmd_backtest(args) -> int:
    symbols = _symbols(args.instruments)
    instruments = [get_instrument(s) for s in symbols]
    source = _build_source(args)

    series = {}
    for inst in instruments:
        series[inst.symbol] = source.candles(inst.symbol, args.granularity, args.bars)
        if not series[inst.symbol]:
            print(f"no data for {inst.symbol}", file=sys.stderr)
            return 1

    registry = build_registry(
        strategy=args.strategy,
        risk_pct=args.risk,
        sessions=tuple(_names(args.sessions)) if args.sessions else (),
        max_open_positions=args.max_positions,
    )
    bt = Backtester(
        registry=registry,
        instruments=instruments,
        starting_balance=args.balance,
        account_currency=args.currency,
        slippage_pips=args.slippage,
        commission_per_million=args.commission,
        runner_config=RunnerConfig(warmup_bars=args.warmup),
    )
    result = bt.run(align(series))

    if args.source == "synthetic":
        print(SYNTHETIC_WARNING)
    print(result.summary())

    if args.trades and result.broker.closed_trades:
        print("\nTrades")
        print("------")
        for t in result.broker.closed_trades[: args.trades]:
            print(
                f"{t.opened_at:%Y-%m-%d %H:%M} -> {t.closed_at:%Y-%m-%d %H:%M} "
                f"{t.instrument} {t.side.value:<4} {t.units:>9,.0f} "
                f"{t.entry_price:.5f} -> {t.exit_price:.5f}  "
                f"{t.pnl:>+10,.2f}  {t.reason}"
            )

    if args.journal:
        print("\nJournal (last %d)" % args.journal)
        print("-" * 20)
        print(result.journal.render(result.journal.tail(args.journal)))

    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "equity"])
            for ts, eq in result.equity_curve:
                w.writerow([ts.isoformat(), f"{eq:.2f}"])
        print(f"\nequity curve written to {out}")

    return 0


def cmd_paper(args) -> int:
    from fxagents.live.runner import PaperSession

    symbols = _symbols(args.instruments)
    instruments = [get_instrument(s) for s in symbols]
    source = _build_source(args)

    session = PaperSession(
        registry=build_registry(strategy=args.strategy, risk_pct=args.risk),
        instruments=instruments,
        source=source,
        granularity=args.granularity,
        starting_balance=args.balance,
        account_currency=args.currency,
        poll_seconds=args.poll,
        on_update=lambda s, ts: print(s.status_line(ts)),
    )

    if args.source == "synthetic":
        print(SYNTHETIC_WARNING)

    if args.replay:
        series = {i.symbol: source.candles(i.symbol, args.granularity, args.bars) for i in instruments}
        frames = list(align(series))
        print(f"replaying {len(frames):,} bars at {args.speed}s/bar (paper money only)\n")
        session.run_replay(frames, speed=args.speed)
    else:
        bars = session.warmup()
        print(f"warmed up on {bars:,} historical bars; polling every {args.poll:.0f}s\n")
        try:
            session.run_poll(max_cycles=args.cycles)
        except KeyboardInterrupt:
            print("\nstopped by user")

    acct = session.broker.account()
    print(
        f"\nfinal equity {acct.equity:,.2f} {acct.currency} "
        f"after {len(session.broker.closed_trades)} closed trades"
    )
    return 0


def cmd_agents(args) -> int:
    registry = build_registry(strategy=args.strategy)
    print("Signal agents")
    for a in registry.signals:
        print(f"  - {a.name}")
    print("Filter agents")
    for a in registry.filters:
        print(f"  - {a.name}")
    print(f"Portfolio agent\n  - {registry.portfolio.name}")
    print(f"Risk agent\n  - {registry.risk.name}")
    limits = registry.risk.limits
    print(f"      risk/trade         {limits.risk_per_trade_pct:.2%} of equity")
    print(f"      max open positions {limits.max_open_positions}")
    print(f"      daily loss limit   {limits.daily_loss_limit_pct:.1%}")
    print(f"      drawdown kill      {limits.max_drawdown_pct:.1%}")
    print(f"      take profit        {limits.take_profit_r:.1f}R")
    print(f"Execution agent\n  - {registry.execution.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fxagents", description=__doc__)
    p.add_argument("--version", action="version", version=f"fxagents {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--instruments", default="EUR_USD,GBP_USD,USD_JPY")
        sp.add_argument("--granularity", default="H1", help="M5 M15 M30 H1 H4 D")
        sp.add_argument("--bars", type=int, default=4000)
        sp.add_argument("--strategy", default="all", choices=["all", "trend", "reversion", "breakout"])
        sp.add_argument("--risk", type=float, default=0.01, help="fraction of equity per trade")
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--currency", default="USD")
        sp.add_argument("--source", default="synthetic", choices=["synthetic", "csv", "oanda"])
        sp.add_argument("--data-dir", default="data")
        sp.add_argument("--seed", type=int, default=7)

    bt = sub.add_parser("backtest", help="run a historical simulation")
    common(bt)
    bt.add_argument("--sessions", default="london,newyork", help="comma list, or empty for 24h")
    bt.add_argument("--slippage", type=float, default=0.2, help="pips per fill")
    bt.add_argument("--commission", type=float, default=0.0, help="account ccy per 1M units")
    bt.add_argument("--max-positions", type=int, default=3)
    bt.add_argument("--warmup", type=int, default=0, help="bars to observe before trading")
    bt.add_argument("--trades", type=int, default=0, help="print the first N trades")
    bt.add_argument("--journal", type=int, default=0, help="print the last N journal entries")
    bt.add_argument("--export", help="write the equity curve to this CSV path")
    bt.set_defaults(func=cmd_backtest)

    pa = sub.add_parser("paper", help="paper trade (no real orders, ever)")
    common(pa)
    pa.add_argument("--replay", action="store_true", help="stream history instead of polling live")
    pa.add_argument("--speed", type=float, default=0.0, help="seconds of wall clock per replayed bar")
    pa.add_argument("--poll", type=float, default=30.0, help="seconds between live polls")
    pa.add_argument("--cycles", type=int, default=None, help="stop after N polls (default: run forever)")
    pa.set_defaults(func=cmd_paper)

    ag = sub.add_parser("agents", help="describe the wired-up agent team")
    ag.add_argument("--strategy", default="all", choices=["all", "trend", "reversion", "breakout"])
    ag.set_defaults(func=cmd_agents)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
