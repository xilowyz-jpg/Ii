from __future__ import annotations

import pytest

from fxagents.instruments import Instrument, get_instrument


@pytest.mark.parametrize("raw", ["EUR_USD", "EURUSD", "eur/usd", "eur-usd"])
def test_symbol_parsing_accepts_the_common_spellings(raw):
    inst = Instrument.parse(raw)
    assert (inst.symbol, inst.base, inst.quote) == ("EUR_USD", "EUR", "USD")


def test_jpy_pairs_use_a_two_decimal_pip():
    assert Instrument.parse("USD_JPY").pip_size == 0.01
    assert Instrument.parse("EUR_USD").pip_size == 0.0001


def test_unparseable_symbols_are_rejected():
    with pytest.raises(ValueError):
        Instrument.parse("NOTAPAIR")


def test_quote_equals_account_currency_needs_no_conversion():
    assert get_instrument("EUR_USD").quote_to_account_rate(1.08, "USD") == 1.0


def test_base_equals_account_currency_inverts_the_price():
    """USD_JPY on a USD account: profit arrives in JPY, worth 1/price USD."""
    rate = get_instrument("USD_JPY").quote_to_account_rate(150.0, "USD")
    assert rate == pytest.approx(1 / 150.0)


def test_a_cross_the_instrument_cannot_resolve_returns_none():
    """EUR_GBP on a USD account needs GBP/USD, which this pair does not carry."""
    assert get_instrument("EUR_GBP").quote_to_account_rate(0.85, "USD") is None


def test_pip_conversions_round_trip():
    inst = get_instrument("EUR_USD")
    assert inst.pips(inst.price_from_pips(25.0)) == pytest.approx(25.0)


def test_round_units_snaps_down_and_keeps_the_sign():
    inst = Instrument.parse("EUR_USD", min_units=1000)
    assert inst.round_units(15_900) == 15_000
    assert inst.round_units(-15_900) == -15_000
    assert inst.round_units(500) == 0
