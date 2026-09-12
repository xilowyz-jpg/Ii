from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fxagents.backtest import metrics
from fxagents.types import ClosedTrade, Side

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def curve(values, step=timedelta(days=1)):
    return [(START + step * i, v) for i, v in enumerate(values)]


def trade(pnl, days=1, reason="take profit"):
    return ClosedTrade(
        instrument="EUR_USD", side=Side.BUY, units=10_000,
        entry_price=1.10, exit_price=1.11,
        opened_at=START, closed_at=START + timedelta(days=days),
        pnl=pnl, costs=1.0, reason=reason,
    )


def test_total_return_is_measured_end_to_end():
    m = metrics.compute(curve([10_000, 10_500, 11_000]), [])
    assert m.total_return == pytest.approx(0.10)


def test_max_drawdown_is_measured_from_the_running_peak():
    m = metrics.compute(curve([10_000, 12_000, 9_000, 11_000]), [])
    assert m.max_drawdown == pytest.approx(0.25)      # 12,000 -> 9,000


def test_drawdown_duration_spans_peak_to_the_last_underwater_bar():
    m = metrics.compute(curve([10_000, 12_000, 11_000, 10_500, 13_000]), [])
    assert m.max_drawdown_duration == timedelta(days=2)


def test_a_monotonically_rising_curve_has_no_drawdown():
    m = metrics.compute(curve([10_000, 10_100, 10_200, 10_300]), [])
    assert m.max_drawdown == 0.0
    assert m.calmar == 0.0        # undefined rather than infinite


def test_profit_factor_is_gross_win_over_gross_loss():
    m = metrics.compute(curve([10_000, 10_050]), [trade(300), trade(-100), trade(-50)])
    assert m.profit_factor == pytest.approx(300 / 150)
    assert m.win_rate == pytest.approx(1 / 3)


def test_expectancy_is_the_mean_trade():
    m = metrics.compute(curve([10_000, 10_150]), [trade(300), trade(-100), trade(-50)])
    assert m.expectancy == pytest.approx(150 / 3)


def test_a_strategy_with_no_losers_reports_infinite_profit_factor():
    m = metrics.compute(curve([10_000, 10_300]), [trade(300)])
    assert m.profit_factor == float("inf")


def test_exits_are_counted_by_reason():
    trades = [trade(1, reason="stop loss"), trade(1, reason="stop loss"), trade(1, reason="take profit")]
    m = metrics.compute(curve([10_000, 10_003]), trades)
    assert m.exits == {"stop loss": 2, "take profit": 1}


def test_sharpe_is_annualised_from_the_observed_bar_spacing():
    """The same returns sampled hourly must annualise higher than daily."""
    returns = [1.0, 1.01, 1.005, 1.02, 1.015, 1.03]
    daily = metrics.compute(curve([10_000 * r for r in returns], timedelta(days=1)), [])
    hourly = metrics.compute(curve([10_000 * r for r in returns], timedelta(hours=1)), [])
    assert hourly.sharpe > daily.sharpe


def test_exposure_is_the_share_of_bars_holding_a_position():
    m = metrics.compute(curve([10_000, 10_100]), [], bars_in_market=30, total_bars=120)
    assert m.exposure == pytest.approx(0.25)


def test_a_single_observation_is_not_enough_to_measure_anything():
    with pytest.raises(ValueError):
        metrics.compute(curve([10_000]), [])


def test_render_produces_a_readable_block():
    m = metrics.compute(curve([10_000, 10_500]), [trade(500)])
    text = m.render()
    assert "Total return" in text and "Profit factor" in text
