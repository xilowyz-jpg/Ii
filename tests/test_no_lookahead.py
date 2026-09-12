"""Structural proof that a decision cannot act on the bar that produced it.

This is the property that separates a backtest from a fantasy. It is tested
structurally -- by watching what the runner actually does with a scripted
agent -- rather than by eyeballing a P&L curve.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.agents.base import AgentContext, AgentRegistry, SignalAgent
from fxagents.agents.execution import MarketExecution
from fxagents.agents.portfolio import ConsensusPortfolio
from fxagents.agents.risk import RiskLimits, RiskManager
from fxagents.brokers.paper import PaperBroker
from fxagents.instruments import get_instrument
from fxagents.runner import Runner, RunnerConfig
from fxagents.types import Candle, Signal

START = datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc)   # Tuesday, London open


class AlwaysLong(SignalAgent):
    """Fires once, on a nominated bar, and records every bar it was shown."""

    def __init__(self, fire_on: int):
        self.name = "always_long"
        self.fire_on = fire_on
        self.seen: list[datetime] = []

    def on_bar(self, ctx: AgentContext) -> Signal | None:
        self.seen.append(ctx.ts)
        if len(self.seen) == self.fire_on:
            return Signal(self.name, ctx.symbol, 1.0, 1.0, "scripted",
                          stop_distance=0.0050)
        return None


def build(fire_on: int):
    agent = AlwaysLong(fire_on)
    registry = AgentRegistry(
        signals=[agent],
        filters=[],                                    # filters would muddy the proof
        portfolio=ConsensusPortfolio(min_agreement=0.0),
        risk=RiskManager(limits=RiskLimits(min_stop_pips=1.0), trailing_atr_mult=None),
        execution=MarketExecution(),
    )
    broker = PaperBroker(starting_balance=10_000.0, slippage_pips=0.0,
                         spread_override_pips={"EUR_USD": 0.0})
    runner = Runner(
        registry=registry,
        broker=broker,
        instruments=[get_instrument("EUR_USD")],
        config=RunnerConfig(exit_on_reversal=False),
    )
    return agent, broker, runner


def series(closes, opens=None):
    """One bar per hour; `opens` lets a bar gap away from the previous close."""
    out = []
    for i, close in enumerate(closes):
        open_ = opens[i] if opens else (closes[i - 1] if i else close)
        out.append(
            Candle(
                ts=START + timedelta(hours=i),
                open=open_,
                high=max(open_, close) + 0.0005,
                low=min(open_, close) - 0.0005,
                close=close,
            )
        )
    return out


def test_the_fill_happens_on_the_bar_after_the_signal():
    agent, broker, runner = build(fire_on=3)
    candles = series([1.1000, 1.1010, 1.1020, 1.1030, 1.1040])
    runner.start()
    for c in candles:
        runner.on_frame(c.ts, {"EUR_USD": c})

    fills = [f for f in broker.fills]
    assert len(fills) == 1
    signal_bar = candles[2]      # the 3rd bar is where the agent fired
    fill = fills[0]
    assert fill.ts > signal_bar.ts, "filled on the same bar that produced the signal"
    assert fill.ts == candles[3].ts


def test_the_fill_price_is_the_next_bars_open_not_the_signal_bars_close():
    agent, broker, runner = build(fire_on=3)
    # The bar after the signal gaps 100 pips away from the signal bar's close.
    candles = series(
        [1.1000, 1.1010, 1.1020, 1.1120, 1.1130],
        opens=[1.1000, 1.1000, 1.1010, 1.1120, 1.1120],
    )
    runner.start()
    for c in candles:
        runner.on_frame(c.ts, {"EUR_USD": c})

    fill = broker.fills[0]
    assert fill.price == pytest.approx(1.1120)          # the gapped open
    assert fill.price != pytest.approx(candles[2].close)  # not the signal close


def test_an_agent_is_never_shown_a_bar_from_the_future():
    agent, broker, runner = build(fire_on=10_000)       # never fires
    candles = series([1.1000 + 0.0001 * i for i in range(40)])
    runner.start()
    for c in candles:
        runner.on_frame(c.ts, {"EUR_USD": c})
        # After processing bar i, the agent must have seen exactly bars 0..i.
        assert agent.seen[-1] == c.ts
    assert agent.seen == [c.ts for c in candles]


def test_the_agents_history_window_never_contains_a_later_timestamp():
    class Inspector(SignalAgent):
        name = "inspector"

        def __init__(self):
            self.violations = []

        def on_bar(self, ctx):
            if any(h.ts > ctx.ts for h in ctx.history):
                self.violations.append(ctx.ts)
            return None

    inspector = Inspector()
    registry = AgentRegistry(signals=[inspector], portfolio=ConsensusPortfolio(),
                             risk=RiskManager(), execution=MarketExecution())
    runner = Runner(registry=registry, broker=PaperBroker(),
                    instruments=[get_instrument("EUR_USD")])
    runner.start()
    for c in series([1.1000 + 0.0001 * i for i in range(50)]):
        runner.on_frame(c.ts, {"EUR_USD": c})

    assert inspector.violations == []
