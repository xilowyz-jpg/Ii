"""Broker interface shared by the simulator and the live connector."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from fxagents.types import AccountState, Candle, ClosedTrade, OrderIntent, Position


class Broker(ABC):
    """Minimal surface the agents need. Anything richer is broker-specific."""

    account_currency: str = "USD"

    @abstractmethod
    def account(self) -> AccountState:
        ...

    @abstractmethod
    def position(self, instrument: str) -> Position | None:
        ...

    @abstractmethod
    def submit(self, ts: datetime, intent: OrderIntent) -> None:
        """Queue an order. Simulated brokers fill it on the next bar's open."""

    @abstractmethod
    def close_position(self, ts: datetime, instrument: str, reason: str = "") -> None:
        ...

    @abstractmethod
    def amend_stop(self, instrument: str, stop_loss: float) -> None:
        ...

    @property
    @abstractmethod
    def closed_trades(self) -> list[ClosedTrade]:
        ...


class SimulatedBroker(Broker):
    """A broker that is driven bar by bar by the backtest engine."""

    @abstractmethod
    def on_bar_open(self, instrument: str, candle: Candle) -> None:
        """Fill anything queued during the previous bar, at this bar's open."""

    @abstractmethod
    def on_bar(self, instrument: str, candle: Candle) -> None:
        """Walk the bar: check stops and targets, then mark to market."""
