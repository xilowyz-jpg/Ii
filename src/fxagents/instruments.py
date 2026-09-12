"""Instrument definitions and the currency arithmetic that forex needs.

The subtle part of forex P&L is that a trade's profit lands in the QUOTE
currency, not the account currency. `quote_to_account_rate` handles the two
cases we can resolve exactly from the instrument's own price, and reports the
third case honestly instead of silently pretending the rate is 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str             # "EUR_USD"
    base: str               # "EUR"
    quote: str              # "USD"
    pip_size: float         # 0.0001, or 0.01 for JPY quotes
    min_units: float = 1.0  # smallest tradable increment (units of BASE)
    typical_spread_pips: float = 1.0
    margin_rate: float = 0.033          # ~30:1 leverage, retail EU default

    @staticmethod
    def parse(symbol: str, **overrides) -> "Instrument":
        """Build an instrument from 'EUR_USD' / 'EURUSD' / 'eur/usd'."""
        s = symbol.upper().replace("/", "_").replace("-", "_")
        if "_" in s:
            base, quote = s.split("_", 1)
        elif len(s) == 6:
            base, quote = s[:3], s[3:]
        else:
            raise ValueError(f"cannot parse instrument symbol: {symbol!r}")
        pip = 0.01 if quote == "JPY" else 0.0001
        defaults = dict(
            symbol=f"{base}_{quote}",
            base=base,
            quote=quote,
            pip_size=pip,
            min_units=1.0,
            typical_spread_pips=1.0,
            margin_rate=0.033,
        )
        defaults.update(overrides)
        return Instrument(**defaults)

    def pips(self, price_distance: float) -> float:
        """Convert a distance in price units to pips."""
        return price_distance / self.pip_size

    def price_from_pips(self, pips: float) -> float:
        """Convert pips to a distance in price units."""
        return pips * self.pip_size

    def round_units(self, units: float) -> float:
        """Snap a unit count down to the instrument's tradable increment."""
        if self.min_units <= 0:
            return units
        return (abs(units) // self.min_units) * self.min_units * (1 if units >= 0 else -1)

    def quote_to_account_rate(self, price: float, account_ccy: str) -> float | None:
        """How many units of the ACCOUNT currency one unit of QUOTE is worth.

        Returns None when the conversion needs a cross rate this instrument
        cannot supply on its own (e.g. EUR_GBP on a USD account); callers
        decide whether to fall back or refuse.
        """
        if self.quote == account_ccy:
            return 1.0
        if self.base == account_ccy:
            # e.g. USD_JPY on a USD account: 1 JPY = 1/price USD
            return 1.0 / price if price else None
        return None


# A small catalogue of the majors, with realistic retail spreads.
CATALOGUE: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        Instrument.parse("EUR_USD", typical_spread_pips=0.8),
        Instrument.parse("GBP_USD", typical_spread_pips=1.2),
        Instrument.parse("USD_JPY", typical_spread_pips=0.9),
        Instrument.parse("USD_CHF", typical_spread_pips=1.3),
        Instrument.parse("AUD_USD", typical_spread_pips=1.0),
        Instrument.parse("USD_CAD", typical_spread_pips=1.4),
        Instrument.parse("NZD_USD", typical_spread_pips=1.5),
        Instrument.parse("EUR_GBP", typical_spread_pips=1.1),
        Instrument.parse("EUR_JPY", typical_spread_pips=1.4),
        Instrument.parse("GBP_JPY", typical_spread_pips=2.2),
        # Metals. Gold is quoted to 2 decimals and a pip is 0.10, so a typical
        # 25-cent retail spread is 2.5 pips. One unit is one troy ounce, and
        # margin is nearer 5% than the 3.3% of a currency pair.
        Instrument.parse(
            "XAU_USD", pip_size=0.1, min_units=1.0,
            typical_spread_pips=2.8, margin_rate=0.05,
        ),
        Instrument.parse(
            "XAG_USD", pip_size=0.001, min_units=1.0,
            typical_spread_pips=2.5, margin_rate=0.05,
        ),
    ]
}


def get_instrument(symbol: str) -> Instrument:
    """Look the symbol up in the catalogue, falling back to parsed defaults."""
    parsed = Instrument.parse(symbol)
    return CATALOGUE.get(parsed.symbol, parsed)
