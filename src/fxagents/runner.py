"""The orchestration loop shared by backtesting and live paper trading.

One bar, one pass:

    1. broker fills whatever was queued on the previous bar, at this open
    2. broker walks the bar for stops and targets, marks to the close
    3. every signal agent sees the closed bar (always -- indicators must stay
       warm even while we hold a position, or the exit logic goes blind)
    4. holding a position   -> trail the stop, exit on a genuine reversal
       flat                 -> filters, then consensus, then risk, then execute

Because step 4 only ever *queues* an order, and step 1 fills it at the next
open, no decision can act on the price that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from fxagents.agents.base import AgentContext, AgentRegistry
from fxagents.instruments import Instrument
from fxagents.journal import Journal
from fxagents.types import Candle, Position, Signal

Frame = tuple[datetime, dict[str, Candle]]


@dataclass(slots=True)
class RunnerConfig:
    history_size: int = 300
    exit_on_reversal: bool = True
    reversal_threshold: float = 0.25   # opposing net score needed to close early
    warmup_bars: int = 0               # bars to observe before trading at all


@dataclass(slots=True)
class Runner:
    registry: AgentRegistry
    broker: object                      # SimulatedBroker or a live Broker
    instruments: Sequence[Instrument]
    journal: Journal = field(default_factory=Journal)
    config: RunnerConfig = field(default_factory=RunnerConfig)
    _history: dict[str, list[Candle]] = field(default_factory=dict, init=False)
    _bars_seen: int = field(default=0, init=False)

    def start(self) -> None:
        self.registry.start(self.instruments)
        self._history = {i.symbol: [] for i in self.instruments}

    def finish(self) -> None:
        self.registry.finish()

    # ---- one bar -------------------------------------------------------

    def on_frame(self, ts: datetime, bars: dict[str, Candle]) -> None:
        broker = self.broker
        for inst in self.instruments:
            candle = bars.get(inst.symbol)
            if candle is None:
                continue
            if hasattr(broker, "on_bar_open"):
                broker.on_bar_open(inst.symbol, candle)
            if hasattr(broker, "on_bar"):
                broker.on_bar(inst.symbol, candle)

        account = broker.account()
        if self.registry.risk is not None:
            self.registry.risk.on_equity_update(ts, account.equity)

        self._bars_seen += 1
        for inst in self.instruments:
            candle = bars.get(inst.symbol)
            if candle is None:
                continue
            self._process(ts, inst, candle, account)

    def _process(self, ts: datetime, inst: Instrument, candle: Candle, account) -> None:
        history = self._history.setdefault(inst.symbol, [])
        history.append(candle)
        if len(history) > self.config.history_size:
            del history[0]

        position: Position | None = self.broker.position(inst.symbol)
        ctx = AgentContext(
            ts=ts,
            instrument=inst,
            candle=candle,
            history=history,
            account=account,
            position=position,
            journal=self.journal,
        )

        # Signal agents always run: an agent whose indicators go stale while a
        # position is open cannot be trusted to call the exit.
        signals: list[Signal] = []
        for agent in self.registry.signals:
            sig = agent.on_bar(ctx)
            if sig is not None:
                signals.append(sig)

        # Filters also always run, so their own rolling state stays warm.
        blocks: list[str] = []
        for f in self.registry.filters:
            allowed, reason = f.allows(ctx)
            if not allowed:
                blocks.append(f"{f.name}: {reason}")

        portfolio = self.registry.portfolio
        proposal = portfolio.combine(ctx, signals) if portfolio and signals else None

        if position is not None:
            self._manage_open(ctx, position, proposal)
            return

        if self._bars_seen <= self.config.warmup_bars:
            return
        if proposal is None:
            return
        if blocks:
            ctx.log("veto", "filters", "; ".join(blocks))
            return
        risk = self.registry.risk
        execution = self.registry.execution
        if risk is None or execution is None:
            return
        intent = risk.size(ctx, proposal)
        if intent is not None:
            execution.execute(ctx, intent, self.broker)

    def _manage_open(self, ctx: AgentContext, position: Position, proposal) -> None:
        risk = self.registry.risk
        if risk is not None and hasattr(risk, "trail_stop"):
            new_stop = risk.trail_stop(ctx, position)
            if new_stop is not None:
                self.broker.amend_stop(ctx.symbol, new_stop)
                ctx.log("info", "risk_manager", f"trailing stop -> {new_stop:.5f}")

        if not self.config.exit_on_reversal or proposal is None:
            return
        opposes = (proposal.direction > 0) != position.is_long
        if opposes and abs(proposal.direction) >= self.config.reversal_threshold:
            self.broker.close_position(ctx.ts, ctx.symbol, "reversal signal")
            ctx.log("order", "runner", f"closing on reversal (net {proposal.direction:+.2f})")

    # ---- whole run -----------------------------------------------------

    def run(self, frames: Iterable[Frame]) -> None:
        self.start()
        last_ts: datetime | None = None
        for ts, bars in frames:
            self.on_frame(ts, bars)
            last_ts = ts
        if last_ts is not None and hasattr(self.broker, "force_close_all"):
            self.broker.force_close_all(last_ts)
        self.finish()
