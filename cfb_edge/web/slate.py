"""The Slate view: every game on the board, DraftKings beside the sharp book.

The Edges view answers "what should I bet". This answers "what does the board
look like", which is a different question: it keeps the games with no edge, and
both sides of every market, so you can see where DraftKings sits against the
sharp number rather than only where it is beatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from cfb_edge.edges import EdgeRow
from cfb_edge.models import Game
from cfb_edge.oddsmath import format_american
from cfb_edge.report import ET, kickoff_et

BETTER = "better"
WORSE = "worse"
NEUTRAL = "neutral"

# The bands are deliberately uneven, and a long way apart. DraftKings' standard
# juice runs about 2% worse than Pinnacle's fair price, so the ordinary line on
# the board is a couple of points negative and colouring that red would flag the
# whole slate. Red is reserved for prices worse than DK's own usual -- a bad half
# point, say -- and green still means DK is the better price outright.
BETTER_BAND_PCT = 0.5
WORSE_BAND_PCT = -3.0

# A moneyline past this, either way, is left in plain text. A better price on a
# 14-to-1 shot is real but not actionable, and colouring it green reads as a
# recommendation the number cannot support.
LONGSHOT_PRICE = 400.0

# Which market each of a game's two display rows shows, in the order a
# sportsbook lists them: away team on top with the Over, home team beneath with
# the Under.
TOTAL_SIDES = ("Over", "Under")

# The three markets a display row carries, in the order the table prints them.
MARKET_KEYS = ("spread", "total", "h2h")


def verdict_for(market: str, price: float | None, edge_pct: float | None) -> str | None:
    """Which band an offer falls in. One rule, so every coloured cell agrees.

    The verdict is the edge, which already carries both halves of the question:
    the number gap, converted through the half-point table, and the juice. A
    better number bought with much worse juice is not a better bet, and colouring
    it green would say it was.

    None means there is nothing to compare against -- no sharp line, so no edge.
    """
    if edge_pct is None or price is None:
        return None
    if market == "h2h" and abs(float(price)) > LONGSHOT_PRICE:
        return None
    if edge_pct >= BETTER_BAND_PCT:
        return BETTER
    if edge_pct <= WORSE_BAND_PCT:
        return WORSE
    return NEUTRAL


def compare_side(row: EdgeRow | None) -> str | None:
    """Is DraftKings' offer better than the sharp book's on this side?"""
    if row is None:
        return None
    return verdict_for(row.market, row.dk_price, row.edge_pct)


def number_text(market: str, point: float | None, prefix: str = "") -> str:
    """A line's number as the board prints it: signed for spreads, O/U for totals."""
    if point is None:
        return ""
    if market == "spreads":
        return f"{point:+g}"
    return f"{prefix}{point:g}".strip()


def serialize_cell(
    market: str,
    row: EdgeRow | None,
    prefix: str = "",
    best: Any = None,
    moved: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One market for one side: DK's offer, the sharp book's, and the verdict."""
    if row is None:
        return None
    from cfb_edge.shop import serialize_offer

    verdict = compare_side(row)
    moved = moved or {}
    return {
        "number": number_text(market, row.dk_point, prefix),
        "price": format_american(row.dk_price),
        "sharp_number": number_text(market, row.sharp_point, prefix),
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
            and row.edge_pct >= BETTER_BAND_PCT
        ),
        "has_sharp": row.sharp_price is not None,
        # The best of the US books we pull, which is often DraftKings itself.
        "best": serialize_offer(best, market, prefix),
        # Present only when the number or the price moved inside the arrow
        # window, so the page can render an arrow without deciding anything.
        "dk_moved": moved.get("dk"),
        "sharp_moved": moved.get("sharp"),
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


def day_phrase(moment: datetime, now: datetime | None = None) -> str:
    """How the summary line refers to this day: "today", or "on Saturday".

    A slate usually spans several days, so the same sentence sits above more
    than one table. Calling Saturday "today" on a Thursday would be wrong in the
    one place the group is meant to glance and trust.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(ET).date()
    day: date = moment.astimezone(ET).date()
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"on {day:%A}"


def tally_day(games: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """How many of the day's comparable lines DraftKings wins.

    Only cells with a verdict count, so a side with no sharp price and a
    suppressed long shot are both out of the denominator: neither is a line the
    board has an opinion about.
    """
    better = comparable = 0
    for game in games:
        for side in game["sides"]:
            for market in MARKET_KEYS:
                cell = side[market]
                if cell is None or cell["verdict"] is None:
                    continue
                comparable += 1
                if cell["verdict"] == BETTER:
                    better += 1
    return better, comparable


def day_summary(better: int, comparable: int, phrase: str) -> str:
    """The one line above a day's table, so the shape of the day reads first."""
    if not comparable:
        return f"No comparable lines {phrase}."
    lines = "line" if comparable == 1 else "lines"
    return f"DK is the better price on {better} of {comparable} {lines} {phrase}."


def serialize_game(
    game: Game,
    index: RowIndex,
    best: dict[tuple[str, str, str], Any] | None = None,
    moved: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A game as two display rows: away with the Over, home with the Under."""
    best = best or {}
    moved = moved or {}

    def cell(market: str, side_name: str, prefix: str = "") -> dict[str, Any] | None:
        return serialize_cell(
            market,
            index.get(game.event_id, market, side_name),
            prefix,
            best=best.get((game.event_id, market, side_name)),
            moved=moved.get((game.event_id, market, side_name)),
        )

    sides = []
    for team, total_side in ((game.away_team, "Over"), (game.home_team, "Under")):
        side = {
            "label": team,
            "total_label": total_side,
            "spread": cell("spreads", team),
            "total": cell("totals", total_side, f"{total_side[0]} "),
            "h2h": cell("h2h", team),
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


def build_slate(
    games: Sequence[Game],
    rows: Sequence[EdgeRow],
    now: datetime | None = None,
    best: dict[tuple[str, str, str], Any] | None = None,
    moved: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every game grouped by kickoff day in Eastern time, earliest first."""
    index = RowIndex.build(rows)
    days: dict[str, dict[str, Any]] = {}
    for game in sorted(games, key=lambda g: (g.commence_time, g.matchup)):
        key = day_key(game.commence_time)
        day = days.setdefault(
            key,
            {
                "key": key,
                "label": day_label(game.commence_time),
                "phrase": day_phrase(game.commence_time, now),
                "games": [],
            },
        )
        day["games"].append(serialize_game(game, index, best, moved))
    ordered = [days[key] for key in sorted(days)]
    for day in ordered:
        better, comparable = tally_day(day["games"])
        day["better"] = better
        day["comparable"] = comparable
        # The page recounts this for a filtered view, from the same verdicts;
        # this is the sentence for the whole day, and for anyone reading the API.
        day["summary"] = day_summary(better, comparable, day["phrase"])
    return ordered
