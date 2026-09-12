"""fxagents -- a multi-agent forex trading system (backtest + paper trading)."""

__version__ = "0.1.0"

from fxagents.types import Candle, OrderIntent, Position, Side, Signal
from fxagents.instruments import Instrument

__all__ = ["Candle", "OrderIntent", "Position", "Side", "Signal", "Instrument", "__version__"]
