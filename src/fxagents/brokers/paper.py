"""Simulated broker.

The point of this class is to be pessimistic in the places backtests usually
cheat:

* orders fill on the NEXT bar's open, never on the close that produced them;
* every fill crosses the spread, plus a configurable slippage;
* when a bar touches both the stop and the target, the stop is assumed first;
* a gap through a stop fills at the gapped open, not at the stop price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fxagents.brokers.base import SimulatedBroker
from fxagents.instruments import Instrument, get_instrument
from fxagents.types import (
    AccountState,
    Candle,
    ClosedTrade,
    Fill,
    OrderIntent,
    Position,
    Side,
)


@dataclass(slots=True)
class _Pending:
    intent: OrderIntent | None          # None means "close whatever is open"
    ts: datetime
    reason: str = ""


@dataclass(slots=True)
class PaperBroker(SimulatedBroker):
    """Bar-driven simulator with spread, slippage and commission."""

    starting_balance: float = 10_000.0
    account_currency: str = "USD"
    slippage_pips: float = 0.2
    commission_per_million: float = 0.0   # account currency, per 1M units traded
    spread_override_pips: dict[str, float] = field(default_factory=dict)

    balance: float = field(init=False, default=0.0)
    positions: dict[str, Position] = field(init=False, default_factory=dict)
    _trades: list[ClosedTrade] = field(init=False, default_factory=list)
    fills: list[Fill] = field(init=False, default_factory=list)
    _pending: dict[str, list[_Pending]] = field(init=False, default_factory=dict)
    _marks: dict[str, float] = field(init=False, default_factory=dict)
    _rejections: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.balance = self.starting_balance

    # ---- pricing -------------------------------------------------------

    def _instrument(self, symbol: str) -> Instrument:
        return get_instrument(symbol)

    def half_spread(self, symbol: str) -> float:
        inst = self._instrument(symbol)
        pips = self.spread_override_pips.get(symbol, inst.typical_spread_pips)
        return inst.price_from_pips(pips) / 2.0

    def _fill_price(self, symbol: str, side: Side, mid: float) -> float:
        """Buys lift the ask, sells hit the bid; slippage always hurts."""
        inst = self._instrument(symbol)
        slip = inst.price_from_pips(self.slippage_pips)
        return mid + side.sign * (self.half_spread(symbol) + slip)

    def _commission(self, units: float) -> float:
        return abs(units) / 1_000_000.0 * self.commission_per_million

    def _to_account(self, symbol: str, amount_quote: float, price: float) -> float:
        inst = self._instrument(symbol)
        rate = inst.quote_to_account_rate(price, self.account_currency)
        return amount_quote * (rate if rate is not None else 1.0)

    # ---- Broker interface ---------------------------------------------

    def account(self) -> AccountState:
        return AccountState(
            currency=self.account_currency,
            balance=self.balance,
            equity=self.equity(),
            used_margin=self.used_margin(),
            positions=dict(self.positions),
        )

    def position(self, instrument: str) -> Position | None:
        return self.positions.get(instrument)

    def submit(self, ts: datetime, intent: OrderIntent) -> None:
        self._pending.setdefault(intent.instrument, []).append(_Pending(intent, ts))

    def close_position(self, ts: datetime, instrument: str, reason: str = "") -> None:
        if instrument in self.positions:
            self._pending.setdefault(instrument, []).append(_Pending(None, ts, reason))

    def amend_stop(self, instrument: str, stop_loss: float) -> None:
        pos = self.positions.get(instrument)
        if pos is not None:
            pos.stop_loss = stop_loss

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        return self._trades

    @property
    def rejections(self) -> list[str]:
        return self._rejections

    # ---- valuation -----------------------------------------------------

    def mark(self, symbol: str) -> float | None:
        return self._marks.get(symbol)

    def unrealised(self) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            price = self._marks.get(sym)
            if price is None:
                continue
            total += self._to_account(sym, pos.unrealised_quote(price), price)
        return total

    def equity(self) -> float:
        return self.balance + self.unrealised()

    def used_margin(self) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            price = self._marks.get(sym, pos.avg_price)
            inst = self._instrument(sym)
            notional_quote = abs(pos.units) * price
            total += self._to_account(sym, notional_quote, price) * inst.margin_rate
        return total

    # ---- bar mechanics -------------------------------------------------

    def on_bar_open(self, instrument: str, candle: Candle) -> None:
        """Execute everything queued during the previous bar at this open."""
        queue = self._pending.pop(instrument, [])
        for item in queue:
            if item.intent is None:
                self._execute_close(instrument, candle.ts, candle.open, item.reason or "signal exit")
            else:
                self._execute_open(item.intent, candle.ts, candle.open)
        self._marks[instrument] = candle.open

    def on_bar(self, instrument: str, candle: Candle) -> None:
        """Walk the bar for stop/target hits, then mark to the close."""
        pos = self.positions.get(instrument)
        if pos is not None:
            self._walk_bar(instrument, pos, candle)
        self._marks[instrument] = candle.close

    def _walk_bar(self, instrument: str, pos: Position, candle: Candle) -> None:
        stop, target = pos.stop_loss, pos.take_profit
        if pos.is_long:
            stop_hit = stop is not None and candle.low <= stop
            target_hit = target is not None and candle.high >= target
        else:
            stop_hit = stop is not None and candle.high >= stop
            target_hit = target is not None and candle.low <= target

        if not stop_hit and not target_hit:
            return

        # Within one bar we cannot know the order of events. Assume the stop
        # came first -- the pessimistic reading, and the only safe default.
        if stop_hit:
            level = stop                    # type: ignore[assignment]
            gapped = candle.open if (pos.is_long and candle.open < level) or (
                not pos.is_long and candle.open > level
            ) else level
            self._execute_close(instrument, candle.ts, gapped, "stop loss")
        else:
            level = target                  # type: ignore[assignment]
            gapped = candle.open if (pos.is_long and candle.open > level) or (
                not pos.is_long and candle.open < level
            ) else level
            self._execute_close(instrument, candle.ts, gapped, "take profit")

    # ---- fills ---------------------------------------------------------

    def _execute_open(self, intent: OrderIntent, ts: datetime, mid: float) -> None:
        if intent.instrument in self.positions:
            self._rejections.append(f"{ts}: already positioned in {intent.instrument}")
            return
        if intent.units <= 0:
            return

        price = self._fill_price(intent.instrument, intent.side, mid)
        cost = self._commission(intent.units)
        self.balance -= cost
        self.fills.append(
            Fill(ts, intent.instrument, intent.side, intent.units, price, cost, intent.tag)
        )
        self.positions[intent.instrument] = Position(
            instrument=intent.instrument,
            units=intent.units * intent.side.sign,
            avg_price=price,
            opened_at=ts,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            tag=intent.tag,
        )
        self._marks[intent.instrument] = mid

    def _execute_close(self, instrument: str, ts: datetime, mid: float, reason: str) -> None:
        pos = self.positions.pop(instrument, None)
        if pos is None:
            return

        exit_side = Side.SELL if pos.is_long else Side.BUY
        price = self._fill_price(instrument, exit_side, mid)
        pnl_quote = (price - pos.avg_price) * pos.units
        pnl = self._to_account(instrument, pnl_quote, price)
        cost = self._commission(pos.units)

        self.balance += pnl - cost
        self.fills.append(Fill(ts, instrument, exit_side, abs(pos.units), price, cost, reason))
        self._trades.append(
            ClosedTrade(
                instrument=instrument,
                side=pos.side,
                units=abs(pos.units),
                entry_price=pos.avg_price,
                exit_price=price,
                opened_at=pos.opened_at,
                closed_at=ts,
                pnl=pnl - cost,
                costs=cost,
                reason=reason,
                tag=pos.tag,
            )
        )
        self._marks[instrument] = mid

    def force_close_all(self, ts: datetime) -> None:
        """End-of-run liquidation so the final equity reflects real, closed P&L."""
        for instrument in list(self.positions):
            mid = self._marks.get(instrument, self.positions[instrument].avg_price)
            self._execute_close(instrument, ts, mid, "end of run")
