"""Performance statistics.

Deliberately unforgiving: the ratios are annualised from the actual bar
spacing, and drawdown is measured on the equity curve (including open
positions), not on closed trades -- the latter flatters every strategy that
holds losers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from fxagents.types import ClosedTrade

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(slots=True)
class Metrics:
    starting_equity: float
    ending_equity: float
    total_return: float
    cagr: float
    max_drawdown: float
    max_drawdown_duration: timedelta
    sharpe: float
    sortino: float
    calmar: float
    trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    avg_duration: timedelta
    exposure: float
    total_costs: float
    exits: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        def pct(x: float) -> str:
            return f"{x * 100:,.2f}%"

        rows = [
            ("Starting equity", f"{self.starting_equity:,.2f}"),
            ("Ending equity", f"{self.ending_equity:,.2f}"),
            ("Total return", pct(self.total_return)),
            ("CAGR", pct(self.cagr)),
            ("Max drawdown", pct(self.max_drawdown)),
            ("Longest drawdown", f"{self.max_drawdown_duration.days}d"),
            ("Sharpe (ann.)", f"{self.sharpe:,.2f}"),
            ("Sortino (ann.)", f"{self.sortino:,.2f}"),
            ("Calmar", f"{self.calmar:,.2f}"),
            ("", ""),
            ("Trades", f"{self.trades}"),
            ("Win rate", pct(self.win_rate)),
            ("Profit factor", f"{self.profit_factor:,.2f}"),
            ("Expectancy / trade", f"{self.expectancy:,.2f}"),
            ("Avg win / avg loss", f"{self.avg_win:,.2f} / {self.avg_loss:,.2f}"),
            ("Best / worst trade", f"{self.largest_win:,.2f} / {self.largest_loss:,.2f}"),
            ("Avg hold", f"{self.avg_duration}"),
            ("Time in market", pct(self.exposure)),
            ("Total costs", f"{self.total_costs:,.2f}"),
        ]
        width = max(len(k) for k, _ in rows)
        lines = [f"{k.ljust(width)}  {v}" if k else "" for k, v in rows]
        if self.exits:
            lines.append("")
            lines.append("Exits".ljust(width) + "  " + ", ".join(
                f"{k}: {v}" for k, v in sorted(self.exits.items(), key=lambda kv: -kv[1])
            ))
        return "\n".join(lines)


def _drawdown(equity: Sequence[tuple[datetime, float]]) -> tuple[float, timedelta]:
    peak = equity[0][1]
    peak_ts = equity[0][0]
    max_dd = 0.0
    longest = timedelta(0)
    for ts, value in equity:
        if value > peak:
            peak, peak_ts = value, ts
        elif peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
            longest = max(longest, ts - peak_ts)
    return max_dd, longest


def _annualisation_factor(equity: Sequence[tuple[datetime, float]]) -> float:
    """Periods per year, inferred from the median gap between observations."""
    if len(equity) < 3:
        return 1.0
    gaps = sorted(
        (equity[i][0] - equity[i - 1][0]).total_seconds()
        for i in range(1, len(equity))
    )
    median = gaps[len(gaps) // 2]
    if median <= 0:
        return 1.0
    return SECONDS_PER_YEAR / median


def compute(
    equity_curve: Sequence[tuple[datetime, float]],
    trades: Sequence[ClosedTrade],
    bars_in_market: int = 0,
    total_bars: int = 0,
) -> Metrics:
    if len(equity_curve) < 2:
        raise ValueError("need at least two equity observations")

    start_equity = equity_curve[0][1]
    end_equity = equity_curve[-1][1]
    total_return = (end_equity / start_equity - 1.0) if start_equity else 0.0

    span = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds()
    years = span / SECONDS_PER_YEAR
    if years > 0 and start_equity > 0 and end_equity > 0:
        cagr = (end_equity / start_equity) ** (1 / years) - 1.0
    else:
        cagr = 0.0

    returns = [
        (equity_curve[i][1] / equity_curve[i - 1][1] - 1.0)
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1][1] > 0
    ]
    periods = _annualisation_factor(equity_curve)
    if returns:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd) * math.sqrt(periods) if sd > 0 else 0.0
        downside = [r for r in returns if r < 0]
        if downside:
            dvar = sum(r * r for r in downside) / len(downside)
            dsd = math.sqrt(dvar)
            sortino = (mean / dsd) * math.sqrt(periods) if dsd > 0 else 0.0
        else:
            sortino = float("inf") if mean > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    max_dd, dd_duration = _drawdown(equity_curve)
    calmar = (cagr / max_dd) if max_dd > 0 else 0.0

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0
    )
    pnls = [t.pnl for t in trades]

    exits: dict[str, int] = {}
    for t in trades:
        exits[t.reason or "unspecified"] = exits.get(t.reason or "unspecified", 0) + 1

    avg_duration = (
        sum((t.duration for t in trades), timedelta(0)) / len(trades)
        if trades else timedelta(0)
    )

    return Metrics(
        starting_equity=start_equity,
        ending_equity=end_equity,
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_dd,
        max_drawdown_duration=dd_duration,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        trades=len(trades),
        win_rate=(len(wins) / len(trades)) if trades else 0.0,
        profit_factor=profit_factor,
        expectancy=(sum(pnls) / len(pnls)) if pnls else 0.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        largest_win=max(pnls) if pnls else 0.0,
        largest_loss=min(pnls) if pnls else 0.0,
        avg_duration=avg_duration,
        exposure=(bars_in_market / total_bars) if total_bars else 0.0,
        total_costs=sum(t.costs for t in trades),
        exits=exits,
    )
