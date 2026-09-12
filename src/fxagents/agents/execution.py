"""Execution agent -- the only component that talks to the broker."""

from __future__ import annotations

from dataclasses import dataclass

from fxagents.agents.base import AgentContext, ExecutionAgent
from fxagents.types import OrderIntent


@dataclass(slots=True)
class MarketExecution(ExecutionAgent):
    """Submit as a market order; the broker decides the fill.

    Kept deliberately thin. Anything cleverer (iceberg, limit-then-chase,
    time-sliced entries) belongs in a sibling class, not in here, so the
    simple path stays easy to reason about.
    """

    name: str = "market_execution"
    dry_run: bool = False

    def execute(self, ctx: AgentContext, intent: OrderIntent, broker) -> None:
        if self.dry_run:
            ctx.log("info", self.name, f"DRY RUN -- would send {intent.side.value} {intent.units:,.0f}")
            return
        broker.submit(ctx.ts, intent)
        ctx.log(
            "fill", self.name,
            f"queued {intent.side.value} {intent.units:,.0f} "
            f"sl={intent.stop_loss:.5f} tp={intent.take_profit:.5f}"
            if intent.stop_loss and intent.take_profit
            else f"queued {intent.side.value} {intent.units:,.0f}",
        )
