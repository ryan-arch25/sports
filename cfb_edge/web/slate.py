"""The Slate view: every game on the board, DraftKings beside the sharp book.

The Edges view answers "what should I bet". This answers "what does the board
look like", which is a different question: it keeps the games with no edge, and
both sides of every market, so you can see where DraftKings sits against the
sharp number rather than only where it is beatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from cfb_edge.edges import EdgeRow
from cfb_edge.models import Game
from cfb_edge.oddsmath import OddsError, american_to_prob, format_american
from cfb_edge.report import ET, kickoff_et

BETTER = "better"
WORSE = "worse"
SAME = "same"

# Which market each of a game's two display rows shows, in the order a
# sportsbook lists them: away team on top with the Over, home team beneath with
# the Under.
TOTAL_SIDES = ("Over", "Under")


def compare_side(row: EdgeRow | None) -> str | None:
    """Is DraftKings' offer better than the sharp book's on this side?

    The number decides it when the books are on different ones, because half a
    point is worth more than a cent or two of price. When they agree on the
    number the price decides: a lower implied probability means DraftKings is
    paying more for the same outcome.
    """
    if row is None or row.sharp_price is None:
        return None
    diff = row.line_diff
    if diff:
        return BETTER if diff > 0 else WORSE
    # A consensus price is an average, so it can miss an identical DraftKings
    # price by a rounding hair. Never colour a cell when the two prices on
    # screen read the same.
    if round(float(row.dk_price)) == round(float(row.sharp_price)):
        return SAME
    try:
        dk = american_to_prob(row.dk_price)
        sharp = american_to_prob(row.sharp_price)
    except OddsError:
        return None
    if abs(dk - sharp) < 1e-9:
        return SAME
    return BETTER if dk < sharp else WORSE


def _number(market: str, row: EdgeRow | None, prefix: str = "") -> str:
    if row is None or row.dk_point is None:
        return ""
    if market == "spreads":
        return f"{row.dk_point:+g}"
    return f"{prefix}{row.dk_point:g}".strip()


def _sharp_number(market: str, row: EdgeRow | None, prefix: str = "") -> str:
    if row is None or row.sharp_point is None:
        return ""
    if market == "spreads":
        return f"{row.sharp_point:+g}"
    return f"{prefix}{row.sharp_point:g}".strip()


def serialize_cell(market: str, row: EdgeRow | None, prefix: str = "") -> dict[str, Any] | None:
    """One market for one side: DK's offer, the sharp book's, and the verdict."""
    if row is None:
        return None
    return {
        "number": _number(market, row, prefix),
        "price": format_american(row.dk_price),
        "sharp_number": _sharp_number(market, row, prefix),
        "sharp_price": format_american(row.sharp_price),
        "sharp_source": row.sharp_source or "",
        "line_diff": row.line_diff,
        "edge_pct": None if row.edge_pct is None else round(row.edge_pct, 2),
        "verdict": compare_side(row),
        "translated": row.status == "translated",
    }


@dataclass
class RowIndex:
    """Scan rows looked up by the side they belong to."""

    by_key: dict[tuple[str, str, str], EdgeRow]

    @classmethod
    def build(cls, rows: Iterable[EdgeRow]) -> "RowIndex":
        return cls({(r.event_id, r.market, r.side): r for r in rows})

    def get(self, event_id: str, market: str, side: str) -> EdgeRow | None:
        return self.by_key.get((event_id, market, side))


def day_label(moment: datetime) -> str:
    local = moment.astimezone(ET)
    return f"{local:%A, %B} {local.day}"


def day_key(moment: datetime) -> str:
    return moment.astimezone(ET).strftime("%Y-%m-%d")


def serialize_game(game: Game, index: RowIndex) -> dict[str, Any]:
    """A game as two display rows: away with the Over, home with the Under."""
    sides = []
    for team, total_side in ((game.away_team, "Over"), (game.home_team, "Under")):
        sides.append({
            "label": team,
            "total_label": total_side,
            "spread": serialize_cell("spreads", index.get(game.event_id, "spreads", team)),
            "total": serialize_cell(
                "totals", index.get(game.event_id, "totals", total_side), f"{total_side[0]} "
            ),
            "h2h": serialize_cell("h2h", index.get(game.event_id, "h2h", team)),
        })
    local = game.commence_time.astimezone(ET)
    return {
        "event_id": game.event_id,
        "matchup": game.matchup,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "kickoff_utc": game.commence_time.isoformat().replace("+00:00", "Z"),
        "kickoff_et": kickoff_et(game.commence_time),
        "kickoff_time_et": f"{local.hour % 12 or 12}:{local:%M %p}",
        # Precomputed so the search box does not have to rebuild it per keystroke.
        "search": f"{game.away_team} {game.home_team}".lower(),
        "sides": sides,
        "has_prices": any(
            side[market] is not None for side in sides for market in ("spread", "total", "h2h")
        ),
    }


def build_slate(games: Sequence[Game], rows: Sequence[EdgeRow]) -> list[dict[str, Any]]:
    """Every game grouped by kickoff day in Eastern time, earliest first."""
    index = RowIndex.build(rows)
    days: dict[str, dict[str, Any]] = {}
    for game in sorted(games, key=lambda g: (g.commence_time, g.matchup)):
        key = day_key(game.commence_time)
        day = days.setdefault(
            key, {"key": key, "label": day_label(game.commence_time), "games": []}
        )
        day["games"].append(serialize_game(game, index))
    return [days[key] for key in sorted(days)]
