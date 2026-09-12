"""Core value objects passed between agents.

Everything here is immutable or plainly mutable state -- no behaviour beyond
arithmetic that belongs to the value itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @staticmethod
    def from_sign(sign: float) -> "Side":
        return Side.BUY if sign >= 0 else Side.SELL


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed OHLC bar. `ts` is the bar's OPEN time, always UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"inconsistent OHLC at {self.ts}: {self}")

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True, slots=True)
class Signal:
    """A directional opinion from one signal agent.

    direction:  -1.0 (full short) .. +1.0 (full long); 0.0 means "no opinion"
    confidence:  0.0 .. 1.0 -- how strongly the agent stands behind it
    """

    agent: str
    instrument: str
    direction: float
    confidence: float
    reason: str = ""
    stop_distance: float | None = None  # price units, agent's own stop suggestion

    def __post_init__(self) -> None:
        if not -1.0 <= self.direction <= 1.0:
            raise ValueError(f"direction out of range: {self.direction}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")

    @property
    def score(self) -> float:
        return self.direction * self.confidence


@dataclass(frozen=True, slots=True)
class Proposal:
    """The portfolio agent's aggregated view for one instrument, pre-sizing."""

    instrument: str
    direction: float          # net -1..1
    confidence: float         # 0..1
    contributors: tuple[Signal, ...] = ()
    stop_distance: float | None = None

    @property
    def side(self) -> Side:
        return Side.from_sign(self.direction)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A sized, risk-approved order waiting to be executed."""

    instrument: str
    side: Side
    units: float                    # always positive; `side` carries direction
    stop_loss: float | None = None
    take_profit: float | None = None
    tag: str = ""
    reduce_only: bool = False

    @property
    def signed_units(self) -> float:
        return self.units * self.side.sign


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    instrument: str
    side: Side
    units: float
    price: float
    cost: float = 0.0       # commission, in account currency
    tag: str = ""


@dataclass(slots=True)
class Position:
    instrument: str
    units: float            # signed: >0 long, <0 short
    avg_price: float
    opened_at: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    tag: str = ""

    @property
    def side(self) -> Side:
        return Side.from_sign(self.units)

    @property
    def is_long(self) -> bool:
        return self.units > 0

    def unrealised_quote(self, price: float) -> float:
        """Unrealised P&L expressed in the instrument's QUOTE currency."""
        return (price - self.avg_price) * self.units


@dataclass(slots=True)
class ClosedTrade:
    instrument: str
    side: Side
    units: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    pnl: float              # account currency, net of costs
    costs: float = 0.0
    reason: str = ""
    tag: str = ""

    @property
    def duration(self) -> timedelta:
        return self.closed_at - self.opened_at

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(slots=True)
class AccountState:
    currency: str
    balance: float
    equity: float
    used_margin: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def free_margin(self) -> float:
        return self.equity - self.used_margin
