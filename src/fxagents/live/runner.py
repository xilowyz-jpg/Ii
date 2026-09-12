"""Live paper-trading loop.

Runs the *same* agents and the *same* simulated broker as the backtest, but
driven by a clock instead of a file. Two feed modes:

    poll    -- ask the data source for the newest closed bar each cycle
    replay  -- stream a historical series at an accelerated rate, which is how
               you smoke-test the loop without waiting an hour for an H1 bar

Nothing here can place a real order: the broker is always PaperBroker. Wiring
a live broker is a deliberate, separate step -- see docs/going-live.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from fxagents.agents.base import AgentRegistry
from fxagents.brokers.paper import PaperBroker
from fxagents.data.base import DataSource, Frame, align, granularity_minutes
from fxagents.instruments import Instrument
from fxagents.journal import Journal
from fxagents.runner import Runner, RunnerConfig
from fxagents.types import Candle


@dataclass(slots=True)
class PaperSession:
    registry: AgentRegistry
    instruments: Sequence[Instrument]
    source: DataSource
    granularity: str = "H1"
    starting_balance: float = 10_000.0
    account_currency: str = "USD"
    poll_seconds: float = 30.0
    history_bars: int = 400
    on_update: Callable[["PaperSession", datetime], None] | None = None
    journal: Journal = field(default_factory=Journal)
    broker: PaperBroker = field(init=False)
    runner: Runner = field(init=False)
    _seen: dict[str, datetime] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.broker = PaperBroker(
            starting_balance=self.starting_balance,
            account_currency=self.account_currency,
        )
        self.runner = Runner(
            registry=self.registry,
            broker=self.broker,
            instruments=self.instruments,
            journal=self.journal,
            config=RunnerConfig(),
        )

    # ---- warmup --------------------------------------------------------

    def warmup(self) -> int:
        """Replay recent history so indicators are usable before the first live bar."""
        self.runner.start()
        series = {
            inst.symbol: self.source.candles(inst.symbol, self.granularity, self.history_bars)
            for inst in self.instruments
        }
        frames = list(align(series))
        for ts, bars in frames:
            self.runner.on_frame(ts, bars)
            for sym, candle in bars.items():
                self._seen[sym] = candle.ts
        return len(frames)

    # ---- live loop -----------------------------------------------------

    def poll_once(self) -> Frame | None:
        """Fetch the newest closed bar per instrument; returns a frame if any is new."""
        fresh: dict[str, Candle] = {}
        for inst in self.instruments:
            try:
                candles = self.source.candles(inst.symbol, self.granularity, 2)
            except Exception as exc:                      # network hiccup, not fatal
                self.journal.record(
                    datetime.now(timezone.utc), "info", "paper_session", inst.symbol,
                    f"data fetch failed: {exc}",
                )
                continue
            if not candles:
                continue
            latest = candles[-1]
            if self._seen.get(inst.symbol) and latest.ts <= self._seen[inst.symbol]:
                continue
            self._seen[inst.symbol] = latest.ts
            fresh[inst.symbol] = latest

        if not fresh:
            return None
        ts = max(c.ts for c in fresh.values())
        return ts, fresh

    def run_poll(self, max_cycles: int | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            frame = self.poll_once()
            if frame is not None:
                ts, bars = frame
                self.runner.on_frame(ts, bars)
                if self.on_update:
                    self.on_update(self, ts)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleep(self.poll_seconds)

    def run_replay(self, frames: Iterable[Frame], speed: float = 0.0,
                   sleep: Callable[[float], None] = time.sleep) -> None:
        """Stream pre-fetched frames. `speed` is seconds of wall clock per bar."""
        self.runner.start()
        for ts, bars in frames:
            self.runner.on_frame(ts, bars)
            if self.on_update:
                self.on_update(self, ts)
            if speed > 0:
                sleep(speed)

    # ---- reporting -----------------------------------------------------

    def status_line(self, ts: datetime) -> str:
        acct = self.broker.account()
        pnl = acct.equity - self.starting_balance
        positions = ", ".join(
            f"{sym} {'+' if p.units > 0 else ''}{p.units:,.0f}@{p.avg_price:.5f}"
            for sym, p in acct.positions.items()
        ) or "flat"
        return (
            f"{ts:%Y-%m-%d %H:%M}  equity {acct.equity:>11,.2f} "
            f"({pnl:+,.2f})  trades {len(self.broker.closed_trades):>3}  {positions}"
        )
