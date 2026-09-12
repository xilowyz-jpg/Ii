"""Backtest engine -- feeds frames to the runner and records the equity curve."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from fxagents.agents.base import AgentRegistry
from fxagents.backtest import metrics as metrics_mod
from fxagents.brokers.paper import PaperBroker
from fxagents.data.base import Frame
from fxagents.instruments import Instrument
from fxagents.journal import Journal
from fxagents.runner import Runner, RunnerConfig


@dataclass(slots=True)
class BacktestResult:
    metrics: metrics_mod.Metrics
    equity_curve: list[tuple[datetime, float]]
    broker: PaperBroker
    journal: Journal
    instruments: tuple[str, ...]
    bars: int

    def summary(self) -> str:
        head = f"Backtest -- {', '.join(self.instruments)} over {self.bars:,} bars"
        return f"{head}\n{'=' * len(head)}\n{self.metrics.render()}"


@dataclass(slots=True)
class Backtester:
    registry: AgentRegistry
    instruments: Sequence[Instrument]
    starting_balance: float = 10_000.0
    account_currency: str = "USD"
    slippage_pips: float = 0.2
    commission_per_million: float = 0.0
    runner_config: RunnerConfig = field(default_factory=RunnerConfig)
    journal: Journal = field(default_factory=Journal)

    def run(self, frames: Iterable[Frame]) -> BacktestResult:
        broker = PaperBroker(
            starting_balance=self.starting_balance,
            account_currency=self.account_currency,
            slippage_pips=self.slippage_pips,
            commission_per_million=self.commission_per_million,
        )
        runner = Runner(
            registry=self.registry,
            broker=broker,
            instruments=self.instruments,
            journal=self.journal,
            config=self.runner_config,
        )
        runner.start()

        equity_curve: list[tuple[datetime, float]] = []
        bars = 0
        bars_in_market = 0
        last_ts: datetime | None = None

        for ts, frame_bars in frames:
            runner.on_frame(ts, frame_bars)
            bars += 1
            last_ts = ts
            if broker.positions:
                bars_in_market += 1
            equity_curve.append((ts, broker.equity()))

        if last_ts is not None:
            broker.force_close_all(last_ts)
            equity_curve.append((last_ts, broker.equity()))
        runner.finish()

        if len(equity_curve) < 2:
            raise ValueError("the data feed produced fewer than two bars")

        stats = metrics_mod.compute(
            equity_curve, broker.closed_trades, bars_in_market, bars
        )
        return BacktestResult(
            metrics=stats,
            equity_curve=equity_curve,
            broker=broker,
            journal=self.journal,
            instruments=tuple(i.symbol for i in self.instruments),
            bars=bars,
        )
