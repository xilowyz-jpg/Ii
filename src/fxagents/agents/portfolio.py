"""Portfolio agent -- turns N disagreeing signals into one number."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from fxagents.agents.base import AgentContext, PortfolioAgent
from fxagents.types import Proposal, Signal


@dataclass(slots=True)
class ConsensusPortfolio(PortfolioAgent):
    """Confidence-weighted vote across signal agents.

    Two guards matter more than the weighting itself:

    * `min_net_score` -- a weak net view is not a trade, it is noise.
    * `min_agreement` -- if the agents that have an opinion are split, the
      net score can look decent while the team is actually in disagreement.
      Requiring a majority on the same side keeps the system out of chop.
    """

    name: str = "consensus_portfolio"
    weights: dict[str, float] = field(default_factory=dict)
    min_net_score: float = 0.15
    min_agreement: float = 0.6      # share of opinionated weight on the winning side
    opinion_threshold: float = 0.05  # below this |score| an agent counts as abstaining

    def weight_of(self, agent: str) -> float:
        return self.weights.get(agent, 1.0)

    def combine(self, ctx: AgentContext, signals: Sequence[Signal]) -> Proposal | None:
        opinionated = [s for s in signals if abs(s.score) >= self.opinion_threshold]
        if not opinionated:
            return None

        total_weight = sum(self.weight_of(s.agent) for s in opinionated)
        if total_weight <= 0:
            return None

        net = sum(self.weight_of(s.agent) * s.score for s in opinionated) / total_weight
        if abs(net) < self.min_net_score:
            ctx.log("proposal", self.name, f"net {net:+.2f} below {self.min_net_score} -- no trade")
            return None

        winning_side = 1.0 if net > 0 else -1.0
        agreeing = sum(
            self.weight_of(s.agent) for s in opinionated
            if (s.direction > 0) == (winning_side > 0)
        )
        agreement = agreeing / total_weight
        if agreement < self.min_agreement:
            ctx.log(
                "proposal", self.name,
                f"agents split ({agreement:.0%} agree, need {self.min_agreement:.0%})",
            )
            return None

        # The stop is the tightest one any agreeing agent asked for: the most
        # cautious view wins, since a stop that is too wide silently inflates risk.
        stops = [
            s.stop_distance for s in opinionated
            if s.stop_distance and (s.direction > 0) == (winning_side > 0)
        ]
        stop = min(stops) if stops else None

        contributors = tuple(opinionated)
        ctx.log(
            "proposal", self.name,
            f"net={net:+.2f} agreement={agreement:.0%} from "
            + ", ".join(f"{s.agent}:{s.score:+.2f}" for s in contributors),
        )
        return Proposal(
            instrument=ctx.symbol,
            direction=max(-1.0, min(1.0, net)),
            confidence=min(1.0, abs(net)),
            contributors=contributors,
            stop_distance=stop,
        )
