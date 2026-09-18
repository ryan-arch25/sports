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
from cfb_edge.oddsmath import format_american
from cfb_edge.report import ET, kickoff_et

BETTER = "better"
WORSE = "worse"
NEUTRAL = "neutral"

# Edges inside this band are noise rather than an opinion, so the cell stays
# grey and keeps its number to itself.
NEUTRAL_BAND_PCT = 0.5

# A moneyline past this, either way, is left in plain text. A better price on a
# 14-to-1 shot is real but not actionable, and colouring it green reads as a
# recommendation the number cannot support.
LONGSHOT_PRICE = 400.0

# Which market each of a game's two display rows shows, in the order a
# sportsbook lists them: away team on top with the Over, home team beneath with
# the Under.
TOTAL_SIDES = ("Over", "Under")


def compare_side(row: EdgeRow | None) -> str | None:
    """Is DraftKings' offer better than the sharp book's on this side?

    The verdict is the edge, which already carries both halves of the question:
    the number gap, converted through the half-point table, and the juice. A
    better number bought with much worse juice is not a better bet, and colouring
    it green would say it was.

    None means there is nothing to compare against -- no sharp line, so no edge.
    """
    if row is None or row.edge_pct is None:
        return None
    if row.market == "h2h" and abs(float(row.dk_price)) > LONGSHOT_PRICE:
        return None
    if row.edge_pct >= NEUTRAL_BAND_PCT:
        return BETTER
    if row.edge_pct <= -NEUTRAL_BAND_PCT:
        return WORSE
    return NEUTRAL


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
    verdict = compare_side(row)
    return {
        "number": _number(market, row, prefix),
        "price": format_american(row.dk_price),
        "sharp_number": _sharp_number(market, row, prefix),
        # Empty rather than a dash: the page decides how to say "nothing here".
        "sharp_price": "" if row.sharp_price is None else format_american(row.sharp_price),
        "sharp_source": row.sharp_source or "",
        "line_diff": row.line_diff,
        "edge_pct": None if row.edge_pct is None else round(row.edge_pct, 2),
        "verdict": verdict,
        "translated": row.status == "translated",
        "estimated": row.is_estimated,
        # The page prints the number only when it is worth reading, which is
        # never for a cell it has decided not to colour.
        "show_edge": (
            verdict is not None
            and row.edge_pct is not None
            and row.edge_pct >= NEUTRAL_BAND_PCT
        ),
        "has_sharp": row.sharp_price is not None,
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
        side = {
            "label": team,
            "total_label": total_side,
            "spread": serialize_cell("spreads", index.get(game.event_id, "spreads", team)),
            "total": serialize_cell(
                "totals", index.get(game.event_id, "totals", total_side), f"{total_side[0]} "
            ),
            "h2h": serialize_cell("h2h", index.get(game.event_id, "h2h", team)),
        }
        # One `est.` per row rather than one per cell: it is a property of how
        # the row was priced, not of any single market.
        side["estimated"] = any(
            cell is not None and cell["estimated"]
            for cell in (side["spread"], side["total"], side["h2h"])
        )
        sides.append(side)
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
