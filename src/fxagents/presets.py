"""Ready-made agent teams, so a run is one line rather than ten."""

from __future__ import annotations

from fxagents.agents.base import AgentRegistry
from fxagents.agents.execution import MarketExecution
from fxagents.agents.filters import SessionFilter, VolatilityFilter
from fxagents.agents.portfolio import ConsensusPortfolio
from fxagents.agents.risk import RiskLimits, RiskManager
from fxagents.agents.signals import BreakoutAgent, MeanReversionAgent, TrendFollowingAgent


def build_registry(
    strategy: str = "all",
    risk_pct: float = 0.01,
    sessions: tuple[str, ...] = ("london", "newyork"),
    **risk_overrides,
) -> AgentRegistry:
    """Wire a team.

    strategy: 'trend' | 'reversion' | 'breakout' | 'all'
    """
    catalogue = {
        "trend": TrendFollowingAgent(),
        "reversion": MeanReversionAgent(),
        "breakout": BreakoutAgent(),
    }
    if strategy == "all":
        signals = list(catalogue.values())
    elif strategy in catalogue:
        signals = [catalogue[strategy]]
    else:
        raise ValueError(
            f"unknown strategy {strategy!r}; expected one of "
            f"{', '.join([*catalogue, 'all'])}"
        )

    limits = RiskLimits(risk_per_trade_pct=risk_pct, **risk_overrides)

    # A lone agent has nobody to agree with, so the consensus guard has to
    # relax or it would veto every single trade.
    solo = len(signals) == 1
    portfolio = ConsensusPortfolio(
        weights={"trend_following": 1.0, "mean_reversion": 0.8, "breakout": 1.0},
        min_net_score=0.15,
        min_agreement=0.0 if solo else 0.6,
    )

    return AgentRegistry(
        signals=signals,
        filters=[SessionFilter(allowed=sessions), VolatilityFilter()],
        portfolio=portfolio,
        risk=RiskManager(limits=limits),
        execution=MarketExecution(),
    )
