"""Command line interface.

    fxagents backtest --instruments EUR_USD,GBP_USD --bars 4000
    fxagents paper    --instruments EUR_USD --replay --speed 0
    fxagents agents
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from fxagents import __version__
from fxagents.backtest.engine import Backtester
from fxagents.data.base import align
from fxagents.data.csv_source import CsvSource
from fxagents.data.synthetic import SyntheticSource
from fxagents.instruments import get_instrument
from fxagents.presets import build_registry, build_smc_registry
from fxagents.runner import RunnerConfig

SYNTHETIC_WARNING = (
    "NOTE: synthetic data is a plumbing test, not a market. Performance measured\n"
    "      on it says nothing about whether a strategy has an edge.\n"
)


def _parse_date(text: str) -> "datetime":
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad date {text!r}; use YYYY-MM-DD or YYYY-MM-DDTHH:MM"
        ) from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cmd_fetch(args) -> int:
    """Download real history and write it where `--source csv` will find it."""
    from datetime import timezone

    from fxagents.data.dukascopy import DukascopyError, DukascopyFetcher

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = args.start, args.end

    for symbol in _symbols(args.instruments):
        print(f"{symbol} {args.granularity}: {start:%Y-%m-%d} to {end:%Y-%m-%d}")

        state = {"last": -1, "start": time.monotonic()}

        def progress(done: int, total: int, hour, state=state) -> None:
            pct = int(done * 100 / max(total, 1))
            if pct != state["last"]:
                state["last"] = pct
                elapsed = time.monotonic() - state["start"]
                rate = done / elapsed if elapsed else 0
                left = (total - done) / rate if rate else 0
                print(
                    f"  {pct:>3}%  {hour:%Y-%m-%d %H}h  "
                    f"{done:,}/{total:,} hours  "
                    f"~{left / 60:.0f} min left  "
                    f"pause {fetcher.current_pause:.2f}s",
                    flush=True,
                )

        def retried(hour, attempt, exc) -> None:
            # Keep the tail: the status code or errno is at the END of the
            # message, and truncating the front threw away the diagnosis.
            detail = str(exc)
            if len(detail) > 90:
                detail = "..." + detail[-87:]
            print(f"\r  retry {attempt} for {hour:%Y-%m-%d %H}h: {detail}")

        fetcher = DukascopyFetcher(
            cache_dir=args.cache, on_progress=progress, on_retry=retried,
            retries=args.retries, pause=args.pause,
        )
        try:
            candles = fetcher.candles(symbol, start, end, args.granularity)
        except DukascopyError as exc:
            print(f"\n  failed: {exc}", file=sys.stderr)
            return 1
        print("\r" + " " * 60, end="\r")

        if fetcher.failures:
            print(
                f"  {len(fetcher.failures)} hour(s) could not be downloaded and are "
                f"missing from the file.\n"
                f"  Re-run the same command to retry just those -- everything else is "
                f"cached.\n"
                f"  First: {fetcher.failures[0][0]:%Y-%m-%d %H}h",
                file=sys.stderr,
            )

        if not candles:
            print("  no data returned -- check the dates and the instrument name",
                  file=sys.stderr)
            return 1

        path = out_dir / f"{symbol}_{args.granularity.upper()}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "open", "high", "low", "close", "volume"])
            for c in candles:
                writer.writerow([
                    c.ts.astimezone(timezone.utc).isoformat(),
                    f"{c.open:.5f}", f"{c.high:.5f}", f"{c.low:.5f}", f"{c.close:.5f}",
                    f"{c.volume:.2f}",
                ])

        span_days = (candles[-1].ts - candles[0].ts).days or 1
        print(
            f"  {len(candles):,} bars  {candles[0].ts:%Y-%m-%d} to {candles[-1].ts:%Y-%m-%d}"
            f"  ({len(candles) / span_days:,.0f}/day)  -> {path}"
        )
    return 0


def _build_source(args):
    if args.source == "synthetic":
        return SyntheticSource(seed=args.seed)
    if args.source == "csv":
        return CsvSource(directory=args.data_dir)
    if args.source == "oanda":
        from fxagents.data.oanda import OandaSource

        return OandaSource()
    if args.source == "dukascopy":
        from fxagents.data.dukascopy import DukascopySource

        return DukascopySource()
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

    if args.strategy == "smc":
        registry = build_smc_registry(risk_pct=args.risk, take_profit_r=args.take_profit_r)
    else:
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

    registry = (
        build_smc_registry(risk_pct=args.risk)
        if args.strategy == "smc"
        else build_registry(strategy=args.strategy, risk_pct=args.risk)
    )
    session = PaperSession(
        registry=registry,
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


def cmd_smc_scan(args) -> int:
    """Print what the five-star detector actually found, setup by setup.

    The point is verification against your own chart. A backtest number for a
    rule this specific is worthless until you have confirmed the detector marks
    the same blocks you would mark by eye -- so this lists the completed setups
    with their timestamps and zones, and the near-misses with the star that
    stopped them.
    """
    from zoneinfo import ZoneInfo

    from fxagents.agents.smc_five_star import SMCFiveStarAgent
    from fxagents.agents.base import AgentContext
    from fxagents.journal import Journal
    from fxagents.types import AccountState

    symbol = _symbols(args.instruments)[0]
    instrument = get_instrument(symbol)
    source = _build_source(args)
    candles = source.candles(symbol, args.granularity, args.bars)
    if not candles:
        print(f"no data for {symbol}", file=sys.stderr)
        return 1

    agent = SMCFiveStarAgent(
        ob_timeframe=args.ob_timeframe,
        ma_period=args.ma_period,
        max_liquidity_distance=args.liquidity_distance,
        liquidity_tolerance=args.liquidity_tolerance,
    )

    # A 200 MA on the daily needs 200 daily closes before it says anything, and
    # until then the bias gate is shut and nothing can trade. On M5 data that
    # is ~57,000 bars of pure warmup -- easy to mistake for "the rule found
    # nothing" when it is really "the rule never got to run".
    from fxagents.timeframes import period_minutes

    base_minutes = period_minutes(args.granularity)
    slowest = max(period_minutes(tf) for tf in agent.bias_timeframes)
    needed = agent.ma_period * slowest // base_minutes
    if len(candles) < needed * 1.5:
        print(
            f"WARNING: {len(candles):,} bars is thin for a {agent.ma_period} MA on "
            f"{'/'.join(agent.bias_timeframes)}.\n"
            f"         The bias gate stays shut for the first ~{needed:,} bars, so most of\n"
            f"         this run cannot trade at all. Use more history, a shorter\n"
            f"         --ma-period, or faster bias timeframes.\n",
            file=sys.stderr,
        )
    agent.on_start([instrument])
    journal = Journal()
    account = AccountState(args.currency, args.balance, args.balance)
    for candle in candles:
        agent.on_bar(AgentContext(
            ts=candle.ts, instrument=instrument, candle=candle, history=[candle],
            account=account, position=None, journal=journal,
        ))

    zone = ZoneInfo(agent.session_tz)
    print(f"{symbol} {args.granularity}: {len(candles):,} bars, "
          f"{candles[0].ts:%Y-%m-%d} to {candles[-1].ts:%Y-%m-%d}")
    print(f"order blocks read on {args.ob_timeframe}; bias from "
          f"{'/'.join(agent.bias_timeframes)} ma{args.ma_period}")
    print(f"window {agent.session_start:%H:%M}-{agent.session_end:%H:%M} {agent.session_tz}\n")

    if args.source == "synthetic":
        print("NOTE: synthetic data has no real market structure. An order block\n"
              "      detector will happily find 'setups' in noise. Use real data\n"
              "      before reading anything into these.\n")

    print("Where setups died")
    print("-----------------")
    total = sum(agent.failure_counts().values()) or 1
    for reason, count in agent.failure_counts().items():
        print(f"  {count:>7,}  {count / total:>5.1%}  {reason}")

    complete = agent.complete_setups()
    print(f"\nComplete 5-star setups: {len(complete)}")
    if complete:
        print("-" * 78)
        for r in complete:
            block = r.block
            local = r.ts.astimezone(zone)
            print(f"  {r.ts:%Y-%m-%d %H:%M} UTC  ({local:%H:%M} {local.tzname()})  "
                  f"{r.bias.name.lower():7} block {block.bottom:.2f}-{block.top:.2f}  "
                  f"stop {block.far_edge():.2f}")

    if args.near_misses:
        near = agent.near_misses(args.near_misses)
        print(f"\nNear misses ({args.near_misses}+ stars): {len(near)}")
        print("-" * 78)
        for r in near[-args.show:]:
            local = r.ts.astimezone(zone)
            print(f"  {r.ts:%Y-%m-%d %H:%M} ({local:%H:%M} local)  {r.render()}")
    return 0


def cmd_agents(args) -> int:
    if args.strategy == "smc":
        registry = build_smc_registry()
    else:
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
        sp.add_argument("--strategy", default="all",
                        choices=["all", "trend", "reversion", "breakout", "smc"])
        sp.add_argument("--risk", type=float, default=0.01, help="fraction of equity per trade")
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--currency", default="USD")
        sp.add_argument("--source", default="synthetic",
                        choices=["synthetic", "csv", "oanda", "dukascopy"])
        sp.add_argument("--data-dir", default="data")
        sp.add_argument("--seed", type=int, default=7)

    bt = sub.add_parser("backtest", help="run a historical simulation")
    common(bt)
    bt.add_argument("--sessions", default="london,newyork", help="comma list, or empty for 24h")
    bt.add_argument("--slippage", type=float, default=0.2, help="pips per fill")
    bt.add_argument("--commission", type=float, default=0.0, help="account ccy per 1M units")
    bt.add_argument("--max-positions", type=int, default=3)
    bt.add_argument("--warmup", type=int, default=0, help="bars to observe before trading")
    bt.add_argument("--take-profit-r", type=float, default=2.0,
                    help="target as a multiple of the stop distance (smc strategy)")
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

    sc = sub.add_parser("smc-scan", help="list the five-star setups found in the data")
    common(sc)
    sc.add_argument("--ob-timeframe", default="M15", help="timeframe the order blocks are read on")
    sc.add_argument("--ma-period", type=int, default=200)
    sc.add_argument("--liquidity-distance", type=float, default=10.0,
                    help="how far behind a block liquidity still counts (price units)")
    sc.add_argument("--liquidity-tolerance", type=float, default=0.5,
                    help="how close two swings must be to count as equal")
    sc.add_argument("--near-misses", type=int, default=0,
                    help="also list setups reaching at least N stars")
    sc.add_argument("--show", type=int, default=25, help="how many near misses to print")
    sc.set_defaults(func=cmd_smc_scan)

    fe = sub.add_parser("fetch", help="download real history into a CSV")
    fe.add_argument("--instruments", default="XAU_USD")
    fe.add_argument("--granularity", default="M5", help="M1 M5 M15 M30 H1 H4 D")
    fe.add_argument("--from", dest="start", type=_parse_date, required=True,
                    metavar="YYYY-MM-DD")
    fe.add_argument("--to", dest="end", type=_parse_date, required=True,
                    metavar="YYYY-MM-DD")
    fe.add_argument("--out", default="data", help="directory for the CSV files")
    fe.add_argument("--cache", default=".cache/dukascopy",
                    help="where raw hourly files are kept, so a re-run downloads nothing")
    fe.add_argument("--retries", type=int, default=5,
                    help="attempts per hour before recording it as a gap")
    fe.add_argument("--pause", type=float, default=0.15,
                    help="seconds between requests; raise it if the host throttles you")
    fe.set_defaults(func=cmd_fetch)

    ag = sub.add_parser("agents", help="describe the wired-up agent team")
    ag.add_argument("--strategy", default="all",
                    choices=["all", "trend", "reversion", "breakout", "smc"])
    ag.set_defaults(func=cmd_agents)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
