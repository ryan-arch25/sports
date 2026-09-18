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


MULTIPLICATIVE = "multiplicative"
POWER = "power"
DEVIG_METHODS = (POWER, MULTIPLICATIVE)
DEFAULT_DEVIG_METHOD = POWER


def _validated(probs: Sequence[float]) -> list[float]:
    values = [float(p) for p in probs]
    if len(values) < 2:
        raise OddsError("need at least two sides to de-vig")
    if any(p <= 0.0 for p in values):
        raise OddsError(f"implied probabilities must be positive, got {probs!r}")
    return values


def devig_multiplicative(probs: Sequence[float]) -> list[float]:
    """Remove the vig by proportional normalization.

    Each side's implied probability is divided by the sum of all sides, so the
    result sums to exactly 1 while preserving the ratios between sides. Simple,
    but it takes the same proportion out of every side, which leaves longshots
    overstated: books do not price a 5% shot with the same margin as an even
    one.
    """
    values = _validated(probs)
    total = sum(values)
    return [p / total for p in values]


def devig_power(
    probs: Sequence[float], tolerance: float = 1e-12, max_iterations: int = 200
) -> list[float]:
    """Remove the vig by solving for the k where the powered sum is 1.

    Find k such that sum(p_i ** k) = 1, then take p_i ** k as fair. Because
    raising a small probability to a power above 1 cuts it proportionally
    harder than a large one, the margin comes disproportionately off the
    longshot, which is where books actually put it.

    sum(p_i ** k) is strictly decreasing in k for probabilities in (0, 1), so a
    bisection is both safe and fast.
    """
    values = _validated(probs)
    if any(p >= 1.0 for p in values):
        raise OddsError(
            f"the power method needs every probability below 1, got {probs!r}"
        )
    total = sum(values)
    if abs(total - 1.0) <= tolerance:
        return list(values)  # already fair; k would be 1

    def powered(k: float) -> float:
        return sum(p ** k for p in values)

    # Bracket the root. Overround (total > 1) needs k > 1; the underround case
    # needs k < 1, and powered(k) -> len(values) > 1 as k -> 0, so both ends
    # are always reachable.
    if total > 1.0:
        low, high = 1.0, 2.0
        while powered(high) > 1.0:
            high *= 2.0
            if high > 1e6:
                raise OddsError(f"could not de-vig {probs!r}: no solution found")
    else:
        low, high = 0.5, 1.0
        while powered(low) < 1.0:
            low /= 2.0
            if low < 1e-6:
                raise OddsError(f"could not de-vig {probs!r}: no solution found")

    for _ in range(max_iterations):
        middle = (low + high) / 2.0
        value = powered(middle)
        if abs(value - 1.0) <= tolerance:
            break
        if value > 1.0:
            low = middle
        else:
            high = middle
    else:
        middle = (low + high) / 2.0

    fair = [p ** middle for p in values]
    # The bisection stops within tolerance rather than exactly on 1; normalize
    # so callers can rely on the result summing to one.
    total_fair = sum(fair)
    return [p / total_fair for p in fair]


def devig(probs: Sequence[float], method: str = DEFAULT_DEVIG_METHOD) -> list[float]:
    """Remove the vig from a set of implied probabilities."""
    if method == POWER:
        return devig_power(probs)
    if method == MULTIPLICATIVE:
        return devig_multiplicative(probs)
    raise OddsError(f"unknown de-vig method {method!r}; choose from {', '.join(DEVIG_METHODS)}")


def devig_american(
    odds: Iterable[float], method: str = DEFAULT_DEVIG_METHOD
) -> list[float]:
    """De-vig straight from American prices: [-110, -110] -> [0.5, 0.5]."""
    return devig([american_to_prob(o) for o in odds], method=method)


def devig_k(probs: Sequence[float]) -> float:
    """The exponent the power method solved for. 1.0 means a vig-free market."""
    values = _validated(probs)
    fair = devig_power(values)
    import math

    # Recover k from any side; the largest is the numerically steadiest.
    index = max(range(len(values)), key=lambda i: values[i])
    return math.log(fair[index]) / math.log(values[index])


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
