"""Ready-made agent teams, so a run is one line rather than ten."""

from __future__ import annotations

from fxagents.agents.base import AgentRegistry
from fxagents.agents.execution import MarketExecution
from fxagents.agents.filters import SessionFilter, VolatilityFilter
from fxagents.agents.portfolio import ConsensusPortfolio
from fxagents.agents.risk import RiskLimits, RiskManager
from fxagents.agents.signals import BreakoutAgent, MeanReversionAgent, TrendFollowingAgent
from fxagents.agents.smc_five_star import SMCFiveStarAgent


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


def build_smc_registry(
    risk_pct: float = 0.01,
    take_profit_r: float = 2.0,
    agent: SMCFiveStarAgent | None = None,
    **risk_overrides,
) -> AgentRegistry:
    """The five-star SMC setup, wired to run on its own.

    Two deliberate differences from `build_registry`:

    * **No filter agents.** The rule is "five stars or no trade", and the star
      list already contains a session filter with its own definition. Stacking
      the generic session and volatility filters on top would silently veto
      setups the strategy considers valid -- that is someone else's strategy.
    * **Wider stop bounds.** Gold stops are measured in dollars, and a block on
      M15 routinely sits 10-30 USD away (100-300 pips at gold's 0.1 pip).

    `take_profit_r` defaults to 2.0: risk one, target two.
    """
    limits = RiskLimits(
        risk_per_trade_pct=risk_pct,
        take_profit_r=take_profit_r,
        min_stop_pips=risk_overrides.pop("min_stop_pips", 20.0),     # 2 USD on gold
        max_stop_pips=risk_overrides.pop("max_stop_pips", 600.0),    # 60 USD on gold
        max_open_positions=risk_overrides.pop("max_open_positions", 1),
        **risk_overrides,
    )
    return AgentRegistry(
        signals=[agent or SMCFiveStarAgent()],
        filters=[],
        portfolio=ConsensusPortfolio(min_net_score=0.5, min_agreement=0.0),
        risk=RiskManager(limits=limits, trailing_atr_mult=None),   # the stop is the block
        execution=MarketExecution(),
    )
