"""Risk agent -- the only agent allowed to say no, and the only one that sizes.

Everything that can end an account lives in this file: position sizing, the
per-currency exposure cap, the daily loss limit and the drawdown kill switch.
Keeping them here (rather than sprinkled through the strategies) means you can
audit the whole downside of the system by reading one class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from fxagents.agents.base import AgentContext, RiskAgent
from fxagents.indicators import ATR
from fxagents.instruments import get_instrument
from fxagents.types import OrderIntent, Position, Proposal, Side


@dataclass(slots=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.01      # fraction of equity risked to the stop
    max_open_positions: int = 3
    # Notional per currency as a MULTIPLE of equity, not a percentage. Normal
    # FX sizing is already leveraged -- 1% risk with a 50-pip stop is ~2.2x
    # notional -- so this has to sit well above 1.0 to catch stacking rather
    # than block the first trade. At 5.0 two correlated positions pass and a
    # third is refused.
    max_currency_leverage: float = 5.0
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.20        # hard stop: stops opening anything new
    min_stop_pips: float = 5.0
    max_stop_pips: float = 200.0
    default_stop_atr_mult: float = 2.0
    take_profit_r: float = 2.0            # TP placed at N x the stop distance
    max_units_per_trade: float = 1_000_000.0
    require_exact_conversion: bool = False  # refuse trades needing an unknown cross rate


@dataclass(slots=True)
class RiskManager(RiskAgent):
    name: str = "risk_manager"
    limits: RiskLimits = field(default_factory=RiskLimits)
    atr_period: int = 14
    trailing_atr_mult: float | None = 2.5   # None disables trailing stops
    _atr: dict = field(default_factory=dict)
    _equity_peak: float = 0.0
    _day: date | None = None
    _day_start_equity: float = 0.0
    _halted_reason: str = ""

    # ---- lifecycle -----------------------------------------------------

    def on_start(self, instruments) -> None:
        self._atr = {i.symbol: ATR(self.atr_period) for i in instruments}

    def on_equity_update(self, ts: datetime, equity: float) -> None:
        if self._day != ts.date():
            self._day = ts.date()
            self._day_start_equity = equity
        self._equity_peak = max(self._equity_peak, equity)

    # ---- state queries -------------------------------------------------

    @property
    def halted(self) -> bool:
        return bool(self._halted_reason)

    @property
    def halt_reason(self) -> str:
        return self._halted_reason

    def drawdown(self, equity: float) -> float:
        if self._equity_peak <= 0:
            return 0.0
        return (self._equity_peak - equity) / self._equity_peak

    def day_loss(self, equity: float) -> float:
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity)

    # ---- the gate ------------------------------------------------------

    def _account_blocks(self, ctx: AgentContext) -> str | None:
        equity = ctx.account.equity
        if equity <= 0:
            return "account wiped out"

        dd = self.drawdown(equity)
        if dd >= self.limits.max_drawdown_pct:
            self._halted_reason = f"max drawdown hit ({dd:.1%})"
            return self._halted_reason

        day = self.day_loss(equity)
        if day >= self.limits.daily_loss_limit_pct:
            return f"daily loss limit hit ({day:.1%})"

        if len(ctx.account.positions) >= self.limits.max_open_positions:
            return f"{len(ctx.account.positions)} positions already open"

        if ctx.symbol in ctx.account.positions:
            return "already positioned in this instrument"
        return None

    def _notional(self, inst, units: float, price: float, account_ccy: str) -> float:
        """The trade's size in ACCOUNT currency.

        Both legs of an FX position are worth the same: buying 20,000 EUR_USD
        at 1.10 is simultaneously +22,000 USD of EUR and -22,000 USD of USD.
        Everything has to be converted before it can be compared to one cap --
        4.5M JPY of notional is ~30k USD, not 4.5M of anything.
        """
        rate = inst.quote_to_account_rate(price, account_ccy)
        return abs(units) * price * (rate if rate is not None else 1.0)

    def _currency_exposure(self, ctx: AgentContext) -> dict[str, float]:
        """Net exposure per currency, in account-currency terms.

        Long EUR_USD and short USD_JPY are both short USD; without netting them
        here the system can stack three 'different' trades into one leveraged bet.
        """
        exposure: dict[str, float] = {}
        for sym, pos in ctx.account.positions.items():
            inst = get_instrument(sym)
            sign = 1.0 if pos.units > 0 else -1.0
            notional = self._notional(inst, pos.units, pos.avg_price, ctx.account.currency)
            exposure[inst.base] = exposure.get(inst.base, 0.0) + sign * notional
            exposure[inst.quote] = exposure.get(inst.quote, 0.0) - sign * notional
        return exposure

    def size(self, ctx: AgentContext, proposal: Proposal) -> OrderIntent | None:
        blocked = self._account_blocks(ctx)
        if blocked:
            ctx.log("veto", self.name, blocked)
            return None

        inst = ctx.instrument
        atr_ind = self._atr.setdefault(ctx.symbol, ATR(self.atr_period))
        atr = atr_ind.update(ctx.candle)

        stop_distance = proposal.stop_distance
        if stop_distance is None:
            if atr is None:
                ctx.log("veto", self.name, "no stop proposed and atr not ready")
                return None
            stop_distance = self.limits.default_stop_atr_mult * atr

        stop_pips = inst.pips(stop_distance)
        if stop_pips < self.limits.min_stop_pips:
            ctx.log("veto", self.name, f"stop {stop_pips:.1f}p below floor")
            return None
        if stop_pips > self.limits.max_stop_pips:
            ctx.log("veto", self.name, f"stop {stop_pips:.1f}p above ceiling")
            return None

        entry = ctx.price
        rate = inst.quote_to_account_rate(entry, ctx.account.currency)
        if rate is None:
            if self.limits.require_exact_conversion:
                ctx.log(
                    "veto", self.name,
                    f"needs {inst.quote}/{ctx.account.currency} cross rate, none available",
                )
                return None
            ctx.log(
                "info", self.name,
                f"assuming {inst.quote}/{ctx.account.currency} = 1.0 "
                "(size and P&L are approximate for this pair)",
            )
            rate = 1.0

        # Risk the configured fraction of equity between entry and stop.
        risk_amount = ctx.account.equity * self.limits.risk_per_trade_pct
        loss_per_unit = stop_distance * rate
        if loss_per_unit <= 0:
            ctx.log("veto", self.name, "degenerate stop distance")
            return None

        units = risk_amount / loss_per_unit
        # Scale by conviction so a marginal proposal does not get a full-size bet.
        units *= max(0.25, min(1.0, proposal.confidence / 0.5))
        units = min(units, self.limits.max_units_per_trade)
        units = inst.round_units(units)
        if units < inst.min_units:
            ctx.log("veto", self.name, "sized below the minimum tradable unit")
            return None

        side = proposal.side
        sign = side.sign

        # Per-currency exposure cap, measured after this hypothetical fill.
        cap = self.limits.max_currency_leverage * ctx.account.equity
        exposure = self._currency_exposure(ctx)
        new_notional = self._notional(inst, units, entry, ctx.account.currency)
        exposure[inst.base] = exposure.get(inst.base, 0.0) + sign * new_notional
        exposure[inst.quote] = exposure.get(inst.quote, 0.0) - sign * new_notional
        for ccy, amount in exposure.items():
            if abs(amount) > cap:
                ctx.log(
                    "veto", self.name,
                    f"{ccy} notional {abs(amount):,.0f} exceeds "
                    f"{self.limits.max_currency_leverage:.1f}x equity cap ({cap:,.0f})",
                )
                return None

        stop_loss = entry - sign * stop_distance
        take_profit = (
            entry + sign * stop_distance * self.limits.take_profit_r
            if self.limits.take_profit_r
            else None
        )

        ctx.log(
            "order", self.name,
            f"{side.value} {units:,.0f} @~{entry:.5f} stop {stop_pips:.1f}p "
            f"risk {risk_amount:,.2f} {ctx.account.currency}",
        )
        return OrderIntent(
            instrument=ctx.symbol,
            side=side,
            units=units,
            stop_loss=stop_loss,
            take_profit=take_profit,
            tag=f"conf={proposal.confidence:.2f}",
        )

    # ---- open-position management --------------------------------------

    def trail_stop(self, ctx: AgentContext, position: Position) -> float | None:
        """Ratchet the stop in the trade's favour; never loosen it."""
        if self.trailing_atr_mult is None:
            return None
        atr = self._atr.get(ctx.symbol)
        if atr is None or not atr.ready:
            return None
        distance = self.trailing_atr_mult * (atr.value or 0.0)
        if distance <= 0:
            return None

        if position.is_long:
            candidate = ctx.price - distance
            if position.stop_loss is None or candidate > position.stop_loss:
                return candidate
        else:
            candidate = ctx.price + distance
            if position.stop_loss is None or candidate < position.stop_loss:
                return candidate
        return None
