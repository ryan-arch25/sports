"""Normalized view of an Odds API payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Markets available on the main /odds endpoint.
MARKETS = ("h2h", "spreads", "totals")

# Markets that only come back from the per-event endpoint, one request each.
ALT_MARKETS = ("alternate_spreads", "alternate_totals", "team_totals")

PROP_MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_completions",
    "player_pass_attempts",
    "player_pass_interceptions",
    "player_rush_yds",
    "player_rush_attempts",
    "player_receptions",
    "player_reception_yds",
    "player_anytime_td",
    "player_1st_td",
    "player_kicking_points",
    "player_field_goals",
    "player_tackles_assists",
)

EVENT_MARKETS = ALT_MARKETS + PROP_MARKETS

# Markets whose number is a football margin or point total, so the half-point
# table can translate between two numbers.
SPREAD_LIKE = ("spreads", "alternate_spreads")
TOTAL_LIKE = ("totals", "alternate_totals")
# A team total is a team's own points, not the game's, so the game-total
# distribution does not describe it and it is never translated.
TEAM_TOTAL_LIKE = ("team_totals",)

MARKET_LABELS = {
    "h2h": "Moneyline",
    "spreads": "Spread",
    "totals": "Total",
    "alternate_spreads": "Alt Spread",
    "alternate_totals": "Alt Total",
    "team_totals": "Team Total",
    "player_pass_yds": "Pass Yds",
    "player_pass_tds": "Pass TDs",
    "player_pass_completions": "Completions",
    "player_pass_attempts": "Pass Att",
    "player_pass_interceptions": "INTs",
    "player_rush_yds": "Rush Yds",
    "player_rush_attempts": "Rush Att",
    "player_receptions": "Receptions",
    "player_reception_yds": "Rec Yds",
    "player_anytime_td": "Anytime TD",
    "player_1st_td": "First TD",
    "player_kicking_points": "Kicking Pts",
    "player_field_goals": "Field Goals",
    "player_tackles_assists": "Tackles+Ast",
}


def market_label(market: str) -> str:
    return MARKET_LABELS.get(market, market.replace("_", " ").title())


def is_prop(market: str) -> bool:
    return market.startswith("player_")


def parse_commence_time(value: str) -> datetime:
    """The API returns ISO-8601 UTC like '2026-09-20T23:30:00Z'."""
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    name: str
    price: float
    point: float | None = None
    # Player props carry the subject in `description`: {"description": "QB Name",
    # "name": "Over", "point": 249.5}.
    description: str | None = None

    @property
    def group(self) -> str | None:
        """What this outcome is a side *of*: a player for props, else the game."""
        return self.description

    @property
    def key(self) -> str:
        """Side identity within its group, unique across a market."""
        if self.description:
            return f"{self.description} {self.name}"
        return self.name


@dataclass(frozen=True)
class BookMarket:
    """One bookmaker's prices for one market of one game."""

    book: str
    market: str
    outcomes: tuple[Outcome, ...]
    last_update: str | None = None

    def outcome(self, key: str) -> Outcome | None:
        for o in self.outcomes:
            if o.key == key:
                return o
        return None

    def groups(self) -> dict[str | None, tuple[Outcome, ...]]:
        """Split a market into its two-sided sub-markets.

        Game markets have a single group (keyed None). A player prop market
        holds one group per player, each with its own Over/Under pair.
        """
        grouped: dict[str | None, list[Outcome]] = {}
        for outcome in self.outcomes:
            grouped.setdefault(outcome.group, []).append(outcome)
        return {key: tuple(values) for key, values in grouped.items()}

    @property
    def is_two_sided(self) -> bool:
        return len(self.outcomes) == 2 and len({o.key for o in self.outcomes}) == 2

    @property
    def signature(self) -> tuple[tuple[str, float | None], ...]:
        """Identifies 'the same line' across books: sides plus their numbers."""
        return tuple(sorted((o.key, o.point) for o in self.outcomes))


def outcomes_signature(outcomes: Iterable[Outcome]) -> tuple[tuple[str, float | None], ...]:
    return tuple(sorted((o.key, o.point) for o in outcomes))


@dataclass
class Game:
    event_id: str
    commence_time: datetime
    home_team: str
    away_team: str
    # book key -> market key -> BookMarket
    books: dict[str, dict[str, BookMarket]] = field(default_factory=dict)

    @property
    def matchup(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    def market(self, book: str, market: str) -> BookMarket | None:
        return self.books.get(book, {}).get(market)

    def books_with(self, market: str, candidates: Iterable[str]) -> list[str]:
        return [b for b in candidates if self.market(b, market) is not None]

    def hours_to_kickoff(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.commence_time - now).total_seconds() / 3600.0


def _parse_outcomes(raw_outcomes: Iterable[dict[str, Any]]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for raw in raw_outcomes or []:
        try:
            outcomes.append(
                Outcome(
                    name=raw["name"],
                    price=float(raw["price"]),
                    point=None if raw.get("point") is None else float(raw["point"]),
                    description=raw.get("description") or None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return outcomes


def _parse_bookmakers(game: Game, bookmakers: Iterable[dict[str, Any]]) -> None:
    for bookmaker in bookmakers or []:
        book_key = bookmaker.get("key")
        if not book_key:
            continue
        markets = game.books.setdefault(book_key, {})
        for market in bookmaker.get("markets") or []:
            market_key = market.get("key")
            if not market_key:
                continue
            outcomes = _parse_outcomes(market.get("outcomes"))
            if outcomes:
                markets[market_key] = BookMarket(
                    book=book_key,
                    market=market_key,
                    outcomes=tuple(outcomes),
                    last_update=market.get("last_update") or bookmaker.get("last_update"),
                )
        if not markets:
            game.books.pop(book_key, None)


def parse_games(payload: Iterable[dict[str, Any]]) -> list[Game]:
    """Turn raw Odds API JSON into `Game` objects, skipping malformed entries."""
    games: list[Game] = []
    for event in payload:
        try:
            game = Game(
                event_id=event["id"],
                commence_time=parse_commence_time(event["commence_time"]),
                home_team=event["home_team"],
                away_team=event["away_team"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        _parse_bookmakers(game, event.get("bookmakers") or [])
        games.append(game)
    games.sort(key=lambda g: (g.commence_time, g.matchup))
    return games


def merge_event_payload(game: Game, payload: dict[str, Any]) -> None:
    """Fold a per-event odds response into a game already on the board."""
    if not isinstance(payload, dict):
        return
    if payload.get("id") and payload["id"] != game.event_id:
        return
    _parse_bookmakers(game, payload.get("bookmakers") or [])
