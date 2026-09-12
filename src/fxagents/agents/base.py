"""The agent contract.

Five roles, each a narrow job, wired together by the runner:

    SignalAgent     -- "I think EUR_USD goes up, 0.7 confident"
    FilterAgent     -- "not during the Asian session" / "not before NFP"
    PortfolioAgent  -- turns N disagreeing signals into one net view
    RiskAgent       -- decides whether, and how big; owns the kill switches
    ExecutionAgent  -- turns an approved intent into broker calls

Splitting them this way means a strategy idea can never quietly size itself,
and the risk rules stay in exactly one place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from fxagents.instruments import Instrument
from fxagents.journal import Journal
from fxagents.types import AccountState, Candle, OrderIntent, Position, Proposal, Signal


@dataclass(slots=True)
class AgentContext:
    """Everything an agent is allowed to see on a given bar.

    Deliberately limited to closed bars: there is no way to reach forward,
    so a strategy cannot peek at data it would not have had live.
    """

    ts: datetime
    instrument: Instrument
    candle: Candle
    history: Sequence[Candle]          # closed bars, oldest first, includes `candle`
    account: AccountState
    position: Position | None
    journal: Journal
    equity_peak: float = 0.0
    realised_today: float = 0.0

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def price(self) -> float:
        return self.candle.close

    def log(self, level, agent: str, message: str) -> None:
        self.journal.record(self.ts, level, agent, self.symbol, message)


class Agent(ABC):
    """Common lifecycle for every agent role."""

    name: str = "agent"

    def on_start(self, instruments: Sequence[Instrument]) -> None:
        """Called once before the first bar. Allocate per-instrument state here."""

    def on_finish(self) -> None:
        """Called once after the last bar."""

    def describe(self) -> str:
        return self.name


class SignalAgent(Agent):
    """Produces a directional opinion. Never sizes, never places orders."""

    @abstractmethod
    def on_bar(self, ctx: AgentContext) -> Signal | None:
        ...

    def flat_signal(self, ctx: AgentContext, reason: str) -> Signal:
        return Signal(self.name, ctx.symbol, 0.0, 0.0, reason)


class FilterAgent(Agent):
    """Vetoes new entries. Returns (allowed, reason)."""

    @abstractmethod
    def allows(self, ctx: AgentContext) -> tuple[bool, str]:
        ...


class PortfolioAgent(Agent):
    """Aggregates the signal agents' views into one proposal per instrument."""

    @abstractmethod
    def combine(self, ctx: AgentContext, signals: Sequence[Signal]) -> Proposal | None:
        ...


class RiskAgent(Agent):
    """Owns position sizing and every hard limit. The only agent that can say no."""

    @abstractmethod
    def size(self, ctx: AgentContext, proposal: Proposal) -> OrderIntent | None:
        ...

    def on_equity_update(self, ts: datetime, equity: float) -> None:
        """Optional hook for drawdown / daily-loss tracking."""


class ExecutionAgent(Agent):
    """Turns approved intents into broker calls."""

    @abstractmethod
    def execute(self, ctx: AgentContext, intent: OrderIntent, broker) -> None:
        ...


@dataclass(slots=True)
class AgentRegistry:
    """The wired-up team for one run."""

    signals: list[SignalAgent] = field(default_factory=list)
    filters: list[FilterAgent] = field(default_factory=list)
    portfolio: PortfolioAgent | None = None
    risk: RiskAgent | None = None
    execution: ExecutionAgent | None = None

    def all_agents(self) -> list[Agent]:
        agents: list[Agent] = [*self.signals, *self.filters]
        for a in (self.portfolio, self.risk, self.execution):
            if a is not None:
                agents.append(a)
        return agents

    def start(self, instruments: Sequence[Instrument]) -> None:
        for a in self.all_agents():
            a.on_start(instruments)

    def finish(self) -> None:
        for a in self.all_agents():
            a.on_finish()
