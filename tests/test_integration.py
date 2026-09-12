"""End-to-end behaviour: the whole team, the CLI, and the invariants that
must hold on any data at all."""

from __future__ import annotations

import pytest

from fxagents.backtest.engine import Backtester
from fxagents.cli import main
from fxagents.data.base import align
from fxagents.data.synthetic import SyntheticSource
from fxagents.instruments import get_instrument
from fxagents.presets import build_registry


def run_backtest(strategy="all", symbols=("EUR_USD", "USD_JPY"), bars=2500, seed=7,
                 risk_pct=0.01, risk_overrides=None, **kwargs):
    source = SyntheticSource(seed=seed)
    series = {s: source.candles(s, "H1", bars) for s in symbols}
    bt = Backtester(
        registry=build_registry(strategy=strategy, risk_pct=risk_pct,
                                **(risk_overrides or {})),
        instruments=[get_instrument(s) for s in symbols],
        **kwargs,
    )
    return bt.run(align(series))


# The drawdown kill switch latches at 20% and stops all trading. On synthetic
# data most runs reach it, which flattens any comparison made downstream of it
# -- so the monotonicity properties below are measured with it effectively off.
NO_KILL_SWITCH = {"max_drawdown_pct": 10.0}


def test_a_full_run_completes_and_produces_metrics():
    result = run_backtest()
    assert result.bars > 0
    assert result.metrics.starting_equity == pytest.approx(10_000.0)
    assert "Total return" in result.summary()


def test_the_same_seed_reproduces_the_same_result():
    a, b = run_backtest(seed=5), run_backtest(seed=5)
    assert a.metrics.ending_equity == pytest.approx(b.metrics.ending_equity)
    assert len(a.broker.closed_trades) == len(b.broker.closed_trades)


@pytest.mark.parametrize("strategy", ["trend", "reversion", "breakout", "all"])
def test_every_preset_runs_and_trades(strategy):
    result = run_backtest(strategy=strategy)
    assert result.metrics.trades > 0, f"{strategy} never opened a position"


def test_the_run_ends_flat_so_the_final_equity_is_all_realised():
    result = run_backtest()
    assert result.broker.positions == {}
    assert result.broker.equity() == pytest.approx(result.broker.balance)


def test_no_trade_ever_loses_more_than_the_risk_budget_plus_slippage():
    """A stop can gap, but not by an order of magnitude on continuous data."""
    result = run_backtest()
    budget = 10_000.0 * 0.01
    for t in result.broker.closed_trades:
        assert t.pnl > -budget * 3, f"{t.instrument} lost {t.pnl:.2f} against a {budget:.2f} budget"


def test_equity_never_goes_negative():
    result = run_backtest()
    assert all(equity > 0 for _, equity in result.equity_curve)


def test_costs_make_the_result_strictly_worse():
    """If adding commission improved the P&L, the accounting would be wrong."""
    free = run_backtest(commission_per_million=0.0, risk_overrides=NO_KILL_SWITCH)
    costly = run_backtest(commission_per_million=100.0, risk_overrides=NO_KILL_SWITCH)
    assert costly.metrics.total_costs > 0
    assert costly.metrics.ending_equity < free.metrics.ending_equity


def test_wider_slippage_makes_the_result_worse_too():
    tight = run_backtest(slippage_pips=0.0, risk_overrides=NO_KILL_SWITCH)
    wide = run_backtest(slippage_pips=2.0, risk_overrides=NO_KILL_SWITCH)
    assert wide.metrics.ending_equity < tight.metrics.ending_equity


def test_smaller_risk_produces_a_smaller_drawdown():
    """Position size scales linearly with the risk setting, so the pain does too."""
    def dd(risk):
        return run_backtest(risk_pct=risk, risk_overrides=NO_KILL_SWITCH).metrics.max_drawdown

    assert dd(0.005) < dd(0.02)


def test_the_drawdown_kill_switch_caps_the_loss_it_is_set_to_allow():
    """With the default 20% limit the system must stop rather than keep sinking.

    The cap is not exact -- the switch is checked before opening a trade, so
    positions already open can carry the equity a little further down -- but it
    has to bind, and clearly.
    """
    worst = min(
        run_backtest(seed=seed, bars=3000).metrics.max_drawdown
        for seed in (1, 3, 7, 13)
    )
    deepest = max(
        run_backtest(seed=seed, bars=3000).metrics.max_drawdown
        for seed in (1, 3, 7, 13)
    )
    assert worst > 0, "these seeds should all draw down"
    assert deepest < 0.30, f"kill switch set at 20% let the account reach {deepest:.1%}"


def test_the_journal_explains_why_trades_were_refused():
    result = run_backtest()
    assert result.journal.vetoes(), "a full run with filters should veto something"
    assert all(e.message for e in result.journal.vetoes())


# ---- CLI ---------------------------------------------------------------

def test_cli_backtest_runs_and_warns_about_synthetic_data(capsys):
    assert main(["backtest", "--instruments", "EUR_USD", "--bars", "800"]) == 0
    out = capsys.readouterr().out
    assert "says nothing about whether a strategy has an edge" in out
    assert "Max drawdown" in out


def test_cli_agents_lists_the_team_and_its_limits(capsys):
    assert main(["agents"]) == 0
    out = capsys.readouterr().out
    for expected in ("trend_following", "risk_manager", "drawdown kill"):
        assert expected in out


def test_cli_paper_replay_never_touches_a_real_broker(capsys):
    assert main(["paper", "--instruments", "EUR_USD", "--bars", "600", "--replay"]) == 0
    assert "final equity" in capsys.readouterr().out


def test_cli_exports_the_equity_curve(tmp_path, capsys):
    out_file = tmp_path / "equity.csv"
    assert main(["backtest", "--instruments", "EUR_USD", "--bars", "600",
                 "--export", str(out_file)]) == 0
    lines = out_file.read_text().splitlines()
    assert lines[0] == "timestamp,equity" and len(lines) > 100


def test_cli_rejects_an_unknown_strategy(capsys):
    with pytest.raises(SystemExit):
        main(["backtest", "--strategy", "astrology"])


# ---- backtest / paper parity -------------------------------------------

def test_paper_trading_reproduces_the_backtest_on_the_same_data():
    """The two modes share the runner and the broker; they must not diverge.

    This is the invariant that makes a paper-trading result worth anything: if
    the live loop and the simulator disagree on identical input, one of them is
    lying and there is no way to tell which.
    """
    from fxagents.live.runner import PaperSession

    symbols = ("EUR_USD", "USD_JPY")
    source = SyntheticSource(seed=7)
    series = {s: source.candles(s, "H1", 1200) for s in symbols}
    instruments = [get_instrument(s) for s in symbols]
    frames = list(align(series))

    backtest = Backtester(
        registry=build_registry("all"), instruments=instruments
    ).run(iter(frames))

    session = PaperSession(
        registry=build_registry("all"), instruments=instruments, source=source
    )
    session.run_replay(iter(frames))
    session.broker.force_close_all(frames[-1][0])     # match the backtest's liquidation

    assert len(session.broker.closed_trades) == backtest.metrics.trades
    assert session.broker.equity() == pytest.approx(backtest.metrics.ending_equity)
