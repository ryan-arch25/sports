"""Odds conversion, de-vigging, EV and Kelly math.

Everything in here is pure: no I/O, no globals. Probabilities are fractions in
(0, 1), American odds are ints/floats with |odds| >= 100.
"""

from __future__ import annotations

from typing import Iterable, Sequence


class OddsError(ValueError):
    """Raised for odds values that cannot represent a real price."""


def validate_american(odds: float) -> float:
    """Return `odds` as a float, rejecting prices no book can post."""
    try:
        value = float(odds)
    except (TypeError, ValueError) as exc:
        raise OddsError(f"not a number: {odds!r}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise OddsError(f"not a finite number: {odds!r}")
    if -100 < value < 100:
        raise OddsError(f"American odds must be <= -100 or >= 100, got {odds!r}")
    return value


def american_to_decimal(odds: float) -> float:
    """+150 -> 2.5, -120 -> 1.8333..."""
    value = validate_american(odds)
    if value > 0:
        return 1.0 + value / 100.0
    return 1.0 + 100.0 / abs(value)


def decimal_to_american(decimal_odds: float) -> float:
    """2.5 -> +150, 1.8333... -> -120. Inverse of `american_to_decimal`."""
    dec = float(decimal_odds)
    if dec <= 1.0:
        raise OddsError(f"decimal odds must be > 1.0, got {decimal_odds!r}")
    if dec >= 2.0:
        return (dec - 1.0) * 100.0
    return -100.0 / (dec - 1.0)


def american_to_prob(odds: float) -> float:
    """Implied probability *including* the book's vig.

    -110 -> 0.5238, +150 -> 0.40.
    """
    value = validate_american(odds)
    if value > 0:
        return 100.0 / (value + 100.0)
    return abs(value) / (abs(value) + 100.0)


def prob_to_american(prob: float) -> float:
    """Fair American price for a probability. 0.5 -> +100, 0.6 -> -150."""
    p = float(prob)
    if not 0.0 < p < 1.0:
        raise OddsError(f"probability must be in (0, 1), got {prob!r}")
    if p > 0.5:
        return -100.0 * p / (1.0 - p)
    if p < 0.5:
        return 100.0 * (1.0 - p) / p
    return 100.0


def prob_to_decimal(prob: float) -> float:
    p = float(prob)
    if not 0.0 < p < 1.0:
        raise OddsError(f"probability must be in (0, 1), got {prob!r}")
    return 1.0 / p


def hold(probs: Sequence[float]) -> float:
    """Book hold (overround) for a set of implied probabilities: sum - 1."""
    return sum(probs) - 1.0


def devig(probs: Sequence[float]) -> list[float]:
    """Remove the vig by proportional (multiplicative) normalization.

    Each side's implied probability is divided by the sum of all sides, so the
    result sums to exactly 1 while preserving the ratios between sides.
    """
    values = [float(p) for p in probs]
    if len(values) < 2:
        raise OddsError("need at least two sides to de-vig")
    if any(p <= 0.0 for p in values):
        raise OddsError(f"implied probabilities must be positive, got {probs!r}")
    total = sum(values)
    return [p / total for p in values]


def devig_american(odds: Iterable[float]) -> list[float]:
    """De-vig straight from American prices: [-110, -110] -> [0.5, 0.5]."""
    return devig([american_to_prob(o) for o in odds])


def ev_per_100(prob: float, odds: float) -> float:
    """Expected profit, in dollars, on a $100 stake at `odds` given `prob`."""
    p = float(prob)
    if not 0.0 <= p <= 1.0:
        raise OddsError(f"probability must be in [0, 1], got {prob!r}")
    profit = (american_to_decimal(odds) - 1.0) * 100.0
    return p * profit - (1.0 - p) * 100.0


def edge(fair_prob: float, offered_odds: float) -> float:
    """Fair probability minus the offered price's implied (vigged) probability."""
    return float(fair_prob) - american_to_prob(offered_odds)


def kelly_fraction(prob: float, odds: float) -> float:
    """Full-Kelly fraction of bankroll. Negative expectations return 0.0."""
    p = float(prob)
    if not 0.0 <= p <= 1.0:
        raise OddsError(f"probability must be in [0, 1], got {prob!r}")
    b = american_to_decimal(odds) - 1.0
    f = (p * (b + 1.0) - 1.0) / b
    return max(f, 0.0)


def kelly_stake(
    prob: float,
    odds: float,
    bankroll: float,
    fraction: float = 0.25,
    max_bet_pct: float | None = None,
) -> float:
    """Fractional-Kelly stake in dollars (quarter-Kelly by default).

    `max_bet_pct` caps the stake at that percentage of bankroll.
    """
    if bankroll < 0:
        raise OddsError(f"bankroll must be >= 0, got {bankroll!r}")
    stake = bankroll * float(fraction) * kelly_fraction(prob, odds)
    if max_bet_pct is not None:
        stake = min(stake, bankroll * float(max_bet_pct) / 100.0)
    return max(stake, 0.0)


def format_american(odds: float | None) -> str:
    """Display helper: 150.0 -> '+150', -110.4 -> '-110'."""
    if odds is None:
        return "-"
    value = round(float(odds))
    return f"+{value}" if value > 0 else str(value)
