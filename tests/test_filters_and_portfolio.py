from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fxagents.agents.base import AgentContext
from fxagents.agents.filters import SessionFilter, VolatilityFilter
from fxagents.agents.portfolio import ConsensusPortfolio
from fxagents.instruments import get_instrument
from fxagents.journal import Journal
from fxagents.types import AccountState, Candle, Signal


def ctx_at(ts: datetime, price: float = 1.1000, rng: float = 0.0020):
    inst = get_instrument("EUR_USD")
    candle = Candle(ts=ts, open=price, high=price + rng / 2, low=price - rng / 2, close=price)
    return AgentContext(
        ts=ts, instrument=inst, candle=candle, history=[candle],
        account=AccountState("USD", 10_000.0, 10_000.0),
        position=None, journal=Journal(),
    )


# ---- sessions ---------------------------------------------------------

@pytest.mark.parametrize("hour,expected", [(3, False), (8, True), (13, True), (18, True), (23, False)])
def test_the_session_filter_tracks_london_and_new_york(hour, expected):
    f = SessionFilter(allowed=("london", "newyork"))
    allowed, _ = f.allows(ctx_at(datetime(2024, 3, 5, hour, tzinfo=timezone.utc)))
    assert allowed is expected


def test_saturday_is_always_closed():
    f = SessionFilter()
    allowed, reason = f.allows(ctx_at(datetime(2024, 3, 9, 13, tzinfo=timezone.utc)))
    assert not allowed and "weekend" in reason


def test_friday_evening_and_sunday_daytime_are_closed():
    f = SessionFilter()
    friday_late = ctx_at(datetime(2024, 3, 8, 22, tzinfo=timezone.utc))
    sunday_noon = ctx_at(datetime(2024, 3, 10, 12, tzinfo=timezone.utc))
    assert not f.allows(friday_late)[0]
    assert not f.allows(sunday_noon)[0]


def test_session_names_are_case_insensitive():
    assert SessionFilter(allowed=("LONDON", " NewYork ")).allowed == ("london", "newyork")


def test_an_unknown_session_name_fails_loudly():
    with pytest.raises(ValueError, match="unknown session"):
        SessionFilter(allowed=("frankfurt",))


def test_an_empty_session_list_means_trade_around_the_clock():
    f = SessionFilter(allowed=())
    assert f.allows(ctx_at(datetime(2024, 3, 5, 3, tzinfo=timezone.utc)))[0]


# ---- volatility -------------------------------------------------------

def test_the_volatility_filter_rejects_a_dead_market():
    from datetime import timedelta

    f = VolatilityFilter(atr_period=14, lookback=100, min_percentile=0.10)
    f.on_start([get_instrument("EUR_USD")])
    base = datetime(2024, 3, 5, 8, tzinfo=timezone.utc)

    # 60 lively bars, then the range collapses.
    for i in range(60):
        f.allows(ctx_at(base + timedelta(hours=i), rng=0.0030))
    allowed, reason = f.allows(ctx_at(base + timedelta(hours=60), rng=0.00001))
    assert not allowed and "too quiet" in reason


# ---- consensus --------------------------------------------------------

def sig(agent, direction, confidence):
    return Signal(agent, "EUR_USD", direction, confidence)


def test_unanimous_agents_produce_a_full_conviction_proposal():
    p = ConsensusPortfolio(min_agreement=0.6)
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    proposal = p.combine(ctx, [sig("a", 1.0, 1.0), sig("b", 1.0, 1.0)])
    assert proposal is not None and proposal.direction == pytest.approx(1.0)


def test_agents_pulling_in_opposite_directions_produce_no_trade():
    p = ConsensusPortfolio(min_agreement=0.6)
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    assert p.combine(ctx, [sig("a", 1.0, 1.0), sig("b", -1.0, 1.0)]) is None


def test_a_bare_majority_is_rejected_when_agreement_is_required():
    """2 against 1 is 67% -- enough at 0.6, not enough at 0.8."""
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    signals = [sig("a", 1.0, 1.0), sig("b", 1.0, 1.0), sig("c", -1.0, 1.0)]
    assert ConsensusPortfolio(min_agreement=0.6).combine(ctx, signals) is not None
    assert ConsensusPortfolio(min_agreement=0.8).combine(ctx, signals) is None


def test_abstaining_agents_do_not_dilute_the_vote():
    """A flat signal is an abstention, not a vote against."""
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    p = ConsensusPortfolio(min_agreement=0.6)
    with_abstainers = p.combine(ctx, [sig("a", 1.0, 1.0), sig("b", 0.0, 0.0), sig("c", 0.0, 0.0)])
    assert with_abstainers is not None and with_abstainers.direction == pytest.approx(1.0)


def test_a_weak_net_view_is_not_a_trade():
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    p = ConsensusPortfolio(min_net_score=0.5, min_agreement=0.0)
    assert p.combine(ctx, [sig("a", 1.0, 0.2)]) is None


def test_weights_shift_the_outcome():
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    signals = [sig("loud", 1.0, 1.0), sig("quiet", -1.0, 1.0)]
    p = ConsensusPortfolio(weights={"loud": 3.0, "quiet": 1.0}, min_agreement=0.6)
    proposal = p.combine(ctx, signals)
    assert proposal is not None and proposal.direction > 0


def test_the_tightest_stop_among_agreeing_agents_wins():
    ctx = ctx_at(datetime(2024, 3, 5, 13, tzinfo=timezone.utc))
    p = ConsensusPortfolio(min_agreement=0.0)
    signals = [
        Signal("a", "EUR_USD", 1.0, 1.0, stop_distance=0.0080),
        Signal("b", "EUR_USD", 1.0, 1.0, stop_distance=0.0030),
    ]
    assert p.combine(ctx, signals).stop_distance == pytest.approx(0.0030)
